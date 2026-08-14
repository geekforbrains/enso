"""Job scheduling and execution — the cron scheduler and the run pipeline.

Split out of :class:`~enso.core.Runtime`: jobs share the runtime's process
management, workspace slots, and config, but nothing else about them belongs to
the interactive chat path.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
from asyncio.subprocess import Process
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from croniter import croniter

from . import runs
from .core import ExecutionContext, _redacted_command
from .jobs import Job, job_binding_error, job_config_error, load_jobs
from .teams import load_catalog

if TYPE_CHECKING:
    from .core import Runtime

log = logging.getLogger(__name__)


JOB_CONCURRENCY = int(os.environ.get("ENSO_JOB_CONCURRENCY", "2"))
JOB_FAILURE_RENOTIFY_SECS = int(os.environ.get("ENSO_JOB_FAILURE_RENOTIFY_SECS", str(24 * 60 * 60)))
PRERUN_DIAGNOSTIC_LIMIT = 500

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([A-Za-z0-9_-]*(?:api[_-]?key|token|secret|password|authorization)"
    r"[A-Za-z0-9_-]*)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)([^/@\s]+)@")
_SENSITIVE_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)")


def _job_chat_key(job: Job, workspace: str, policy: str) -> str:
    """Build a delimiter-safe session key that rotates with policy ownership."""
    payload = json.dumps(
        {"v": 1, "job": job.dir_name, "workspace": workspace, "policy": policy},
        sort_keys=True,
    )
    return f"job:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


@dataclass(frozen=True)
class PrerunResult:
    """Structured result from the deterministic gate before a job provider."""

    outcome: Literal["open", "no_work", "error", "timeout"]
    output: str = ""
    diagnostic: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class JobRunResult:
    """Terminal result shared by scheduled, CLI, and web job execution."""

    status: Literal["ok", "error", "timeout", "no_work", "prerun_error", "prerun_timeout"]
    run_id: str | None = None
    output: str = ""
    exit_code: int | None = None


class JobRunner:
    """Owns job scheduling, execution, and failure alerting for one Runtime.

    Holds a back-reference to the Runtime it serves: the job pipeline reuses
    that runtime's process spawning, workspace slots, provider construction,
    transport, and config. Job state lives here, but it is persisted through
    the runtime's single state file, so ``last_run`` and ``failure_alerts``
    are read and restored by ``Runtime.save_state``/``load_state``.
    """

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        # Persisted by Runtime.save_state — see the class docstring.
        self.last_run: dict[str, datetime] = {}
        self.failure_alerts: dict[str, dict[str, Any]] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(JOB_CONCURRENCY)

    def running_here(self) -> list[str]:
        """Names of jobs this process is currently running."""
        return [name for name, task in self._running_tasks.items() if not task.done()]

    async def run_scheduler(self) -> None:
        """Check for jobs to run every 60 seconds.

        Runs as a background task inside ``enso serve``. Each tick fires any
        due cron jobs.
        """
        log.info("Job scheduler started")
        while True:
            await asyncio.sleep(60)
            now = datetime.now()

            if self.runtime.update_in_progress:
                log.info("Job scheduler paused while Enso updates")
                continue

            for job in load_jobs():
                try:
                    if not job.enabled:
                        continue
                    if job.dir_name in self._running_tasks:
                        continue
                    if self._should_run_job(job, now):
                        log.info(
                            "[job:%s] scheduler dispatch (schedule=%r)",
                            job.dir_name,
                            job.schedule,
                        )
                        self.last_run[job.dir_name] = now
                        self.runtime.save_state()
                        task = asyncio.create_task(self._run_job_task(job))
                        self._running_tasks[job.dir_name] = task
                        # ``name`` is bound as a default so each callback forgets
                        # its own job rather than the loop's last one.
                        def _forget(_task: object, name: str = job.dir_name) -> None:
                            self._running_tasks.pop(name, None)

                        task.add_done_callback(_forget)
                except Exception:
                    # One broken job must not stop the others from being
                    # considered — or kill the scheduler task outright.
                    log.exception(
                        "[job:%s] scheduler dispatch failed",
                        job.dir_name,
                    )

    def _runs_cfg(self) -> dict:
        """Return the ``runs`` config block (defensive against bad shapes)."""
        cfg = self.runtime.config.get("runs")
        return cfg if isinstance(cfg, dict) else {}

    async def _run_job_task(self, job: Job) -> None:
        """Run one scheduled job without blocking the scheduler loop."""
        async with self._semaphore:
            try:
                await self._execute_job(job)
            except Exception:
                log.exception("Job '%s' failed", job.name)

    def _should_run_job(self, job: Job, now: datetime) -> bool:
        """Check if a job should run based on its cron schedule."""
        last_run = self.last_run.get(job.dir_name)
        if last_run is None:
            # First time seeing this job — record now, don't fire immediately
            self.last_run[job.dir_name] = now
            self.runtime.save_state()
            return False
        try:
            cron = croniter(job.schedule, last_run)
            next_run = cron.get_next(datetime)
        except (ValueError, KeyError):
            # JOB.md schedules are free-form user-edited text; one bad
            # expression must not take down the scheduler for every job.
            log.warning(
                "Job '%s' has an invalid cron schedule %r; skipping",
                job.name,
                job.schedule,
            )
            return False
        if next_run > now:
            return False
        if not job.catch_up and (now - next_run).total_seconds() > job.misfire_grace_seconds:
            log.warning(
                "Job '%s' missed scheduled run at %s by more than %ss; skipping catch-up",
                job.name,
                next_run.isoformat(),
                job.misfire_grace_seconds,
            )
            self.last_run[job.dir_name] = now
            self.runtime.save_state()
            return False
        return True

    def _sensitive_values(self) -> set[str]:
        """Return configured/environment secret values that diagnostics must redact."""
        values: set[str] = set()

        def visit(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for child_key, child_value in value.items():
                    visit(child_value, str(child_key))
            elif (
                _SENSITIVE_KEY_RE.search(key)
                and isinstance(value, (str, int))
                and len(str(value)) >= 4
            ):
                values.add(str(value))

        visit(self.runtime.config)
        for key, value in os.environ.items():
            if _SENSITIVE_KEY_RE.search(key) and len(value) >= 4:
                values.add(value)
        return values

    def _sanitize_job_diagnostic(self, text: str) -> str:
        """Redact and bound a one-line diagnostic intended for notifications."""
        cleaned = _ANSI_ESCAPE_RE.sub("", text)
        for secret in sorted(self._sensitive_values(), key=len, reverse=True):
            cleaned = cleaned.replace(secret, "<redacted>")
        cleaned = _BEARER_RE.sub("Bearer <redacted>", cleaned)
        cleaned = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", cleaned)
        cleaned = _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", cleaned)
        cleaned = "".join(ch if ch.isprintable() or ch.isspace() else " " for ch in cleaned)
        cleaned = " ".join(cleaned.split())
        if len(cleaned) > PRERUN_DIAGNOSTIC_LIMIT:
            cleaned = cleaned[: PRERUN_DIAGNOSTIC_LIMIT - 1].rstrip() + "…"
        return cleaned

    def _safe_prerun_diagnostic(self, stderr: bytes, fallback: str) -> str:
        """Extract only an explicitly safe ``ENSO_ERROR:`` stderr summary.

        Arbitrary stderr can contain source records or credentials, so it is never
        copied into a notification or run record. Preruns may opt into a useful
        one-line summary by prefixing it with ``ENSO_ERROR:``.
        """
        decoded = stderr.decode(errors="replace")
        for line in decoded.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("ENSO_ERROR:"):
                detail = stripped.removeprefix("ENSO_ERROR:").strip()
                return self._sanitize_job_diagnostic(detail) or fallback
        return fallback

    async def _run_job_prerun(self, job: Job, tag: str) -> PrerunResult:
        """Run a job's deterministic gate and classify its documented outcomes."""
        if not job.prerun:
            return PrerunResult("open")

        script = os.path.join(job.job_dir, job.prerun)
        if not os.path.isfile(script):
            diagnostic = f"Configured prerun script not found: {job.prerun}"
            log.warning("%s prerun error: %s", tag, diagnostic)
            return PrerunResult("error", diagnostic=diagnostic)

        log.info(
            "%s prerun start script=%s timeout=%ss",
            tag,
            job.prerun,
            job.prerun_timeout,
        )
        try:
            proc = await self.runtime._spawn_process(
                "bash",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=job.job_dir,
            )
            stdout, stderr, timed_out = await self.runtime._communicate_with_timeout(
                proc,
                f"Job '{job.name}' prerun",
                job.prerun_timeout,
            )
        except Exception as exc:
            detail = self._sanitize_job_diagnostic(f"{type(exc).__name__}: {exc}")
            diagnostic = f"Could not start prerun{f': {detail}' if detail else ''}"
            log.warning("%s prerun error: %s", tag, diagnostic, exc_info=True)
            return PrerunResult("error", diagnostic=diagnostic)

        if timed_out:
            diagnostic = f"Prerun timed out after {job.prerun_timeout}s"
            log.warning("%s %s", tag, diagnostic.lower())
            return PrerunResult("timeout", diagnostic=diagnostic)

        exit_code = proc.returncode if proc.returncode is not None else -1
        if exit_code == 0:
            output = stdout.decode(errors="replace").strip()
            log.info("%s prerun gate open (exit 0) output_len=%d", tag, len(output))
            return PrerunResult("open", output=output, exit_code=0)
        if exit_code == 1:
            log.info("%s prerun gate closed (exit 1) — no work, skipping", tag)
            return PrerunResult("no_work", exit_code=1)

        fallback = f"Prerun exited with status {exit_code}"
        diagnostic = self._safe_prerun_diagnostic(stderr or b"", fallback)
        log.warning("%s prerun error (exit %s): %s", tag, exit_code, diagnostic)
        return PrerunResult("error", diagnostic=diagnostic, exit_code=exit_code)

    async def _create_job_run(
        self,
        job: Job,
        trigger: str,
        tag: str,
        started_at: str,
    ) -> str | None:
        """Create a run-history row without allowing telemetry to abort the job."""
        try:
            return await asyncio.to_thread(
                runs.create,
                kind="job",
                name=job.dir_name,
                title=job.name,
                trigger=trigger,
                provider=job.provider,
                model=job.model,
                started_at=started_at,
            )
        except Exception:
            log.warning("%s could not create run record", tag, exc_info=True)
            return None

    async def _send_job_notification(self, job: Job, text: str, tag: str) -> bool:
        """Send a job notification without allowing transport faults to abort work."""
        if self.runtime.transport is None:
            return False
        try:
            await self.runtime.transport.notify(
                text[: self.runtime.transport.message_limit],
                destination=job.notify,
            )
        except Exception:
            log.warning("%s could not send job notification", tag, exc_info=True)
            return False
        return True

    def _failure_alert_fingerprint(
        self,
        job: Job,
        status: str,
        diagnostic: str,
        exit_code: int | None,
    ) -> str:
        transport = self.runtime.transport.name if self.runtime.transport is not None else ""
        material = "\0".join(
            (
                job.dir_name,
                transport,
                job.notify or "",
                status,
                str(exit_code),
                diagnostic,
            )
        )
        return hashlib.sha256(material.encode()).hexdigest()

    async def _notify_prerun_failure(
        self,
        job: Job,
        status: Literal["prerun_error", "prerun_timeout"],
        diagnostic: str,
        exit_code: int | None,
        tag: str,
    ) -> bool:
        """Notify once per failure fingerprint and re-notify after the cooldown."""
        if self.runtime.transport is None:
            return False

        now = datetime.now(timezone.utc)
        fingerprint = self._failure_alert_fingerprint(
            job,
            status,
            diagnostic,
            exit_code,
        )
        previous = self.failure_alerts.get(job.dir_name, {})
        last_notified: datetime | None = None
        try:
            if previous.get("last_notified_at"):
                last_notified = datetime.fromisoformat(previous["last_notified_at"])
                if last_notified.tzinfo is None:
                    last_notified = last_notified.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            last_notified = None

        same_failure = previous.get("fingerprint") == fingerprint
        within_cooldown = bool(
            last_notified and (now - last_notified).total_seconds() < JOB_FAILURE_RENOTIFY_SECS
        )
        if same_failure and within_cooldown:
            previous["last_seen_at"] = now.isoformat()
            try:
                suppressed = int(previous.get("suppressed", 0)) + 1
            except (TypeError, ValueError):
                suppressed = 1
            previous["suppressed"] = suppressed
            self.failure_alerts[job.dir_name] = previous
            self.runtime.save_state()
            log.info(
                "%s duplicate prerun alert suppressed count=%s",
                tag,
                previous["suppressed"],
            )
            return False

        headline = "prerun timed out" if status == "prerun_timeout" else "prerun failed"
        sent = await self._send_job_notification(
            job,
            f"⚠️ [{job.name}] {headline}\n{diagnostic}",
            tag,
        )
        if not sent:
            return False

        self.failure_alerts[job.dir_name] = {
            "fingerprint": fingerprint,
            "status": status,
            "destination": job.notify or "",
            "last_notified_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "suppressed": 0,
        }
        self.runtime.save_state()
        return True

    async def _notify_prerun_recovery(self, job: Job, tag: str) -> bool:
        """Send one recovery after a previously reported prerun failure."""
        if job.dir_name not in self.failure_alerts:
            return False
        sent = await self._send_job_notification(
            job,
            f"✅ [{job.name}] prerun recovered",
            tag,
        )
        self.failure_alerts.pop(job.dir_name, None)
        self.runtime.save_state()
        return sent

    _LOCK_FILENAME: ClassVar[str] = ".run.lock"

    def _acquire_job_lock(self, job: Job):
        """Take the cross-process per-job lock, or return ``None`` when held.

        The scheduler, the ``enso job run`` CLI, and the dashboard all run
        in separate processes with separate Runtimes; an in-memory guard
        cannot see the others, so overlap protection lives in an flock on
        a file in the job's directory. Falls back to running unlocked when
        the lock file cannot be created (the pipeline will then fail with
        its own clearer diagnostics).
        """
        path = os.path.join(job.job_dir, self._LOCK_FILENAME)
        try:
            # Deliberately outlives this scope; released in _execute_job's
            # finally via close().
            lock_file = open(path, "a")  # noqa: SIM115
        except OSError:
            log.warning(
                "[job:%s] could not create run lock; continuing unlocked",
                job.dir_name,
                exc_info=True,
            )
            return "unlocked"
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return None
        return lock_file

    def running_elsewhere(self) -> list[str]:
        """Names of jobs whose cross-process run lock is currently held.

        Includes this process's own running jobs; callers that care about
        the distinction (e.g. the update busy-check) already track those.
        """
        held: list[str] = []
        for job in load_jobs():
            path = os.path.join(job.job_dir, self._LOCK_FILENAME)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "a") as probe:
                    try:
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        held.append(job.dir_name)
            except OSError:
                continue
        return held

    def _job_execution_context(
        self,
        job: Job,
    ) -> tuple[ExecutionContext | None, str | None]:
        """Resolve a job's workspace-owned policy without any fallback."""
        if not job.workspace:
            return None, "workspace is required"

        catalog = load_catalog(self.runtime.config)
        error = job_binding_error(
            job.workspace,
            job.provider,
            catalog,
        )
        if error:
            return None, error

        workspace = catalog.workspaces[job.workspace]
        execution_policy = catalog.policy_for(workspace)
        if not os.path.isdir(workspace.path):
            return None, (
                "workspace path does not exist or is not a directory: "
                f"{workspace.path}"
            )
        return ExecutionContext(
            chat_key=_job_chat_key(job, workspace.name, execution_policy.name),
            path=workspace.path,
            workspace_id=workspace.name,
            concurrency=workspace.concurrency,
            workspace=workspace,
            policy=execution_policy,
        ), None

    async def _validated_job_execution(
        self,
        job: Job,
    ) -> tuple[ExecutionContext | None, str | None]:
        """Resolve a job's execution context, rejecting anything it cannot launch."""
        config_error = job_config_error(job.provider, job.model, self.runtime.models)
        if config_error:
            return None, config_error
        execution, config_error = self._job_execution_context(job)
        if config_error or execution is None:
            return None, config_error

        from .policy import prepare_launch

        assert execution.workspace is not None
        assert execution.policy is not None
        try:
            # Prove the complete native launch can be constructed before
            # running the trusted host-side prerun. The resulting launch is
            # deliberately discarded: policy is prepared again inside the
            # workspace slot at the actual provider spawn boundary.
            await asyncio.to_thread(
                prepare_launch,
                execution.workspace,
                execution.policy,
                job.provider,
            )
        except Exception as exc:
            detail = self._sanitize_job_diagnostic(str(exc))
            return None, (
                "native launch is unavailable"
                f"{f': {detail}' if detail else ''}"
            )
        return execution, None

    async def _failed_run_result(
        self,
        job: Job,
        *,
        run_id: str | None,
        output: str,
        exit_code: int | None,
        status: Literal["error", "timeout"],
        tag: str,
        notify_failures: bool,
    ) -> JobRunResult:
        """Record a failed run, alert when enabled, and return its terminal result."""
        await self._record_run_finish(run_id, output, exit_code, status, tag)
        if notify_failures:
            await self._send_job_notification(
                job,
                f"⚠️ [{job.name}] {output}",
                tag,
            )
        return JobRunResult(
            status,
            run_id=run_id,
            output=output,
            exit_code=exit_code,
        )

    async def _prerun_failure_result(
        self,
        job: Job,
        prerun: PrerunResult,
        *,
        trigger: Literal["schedule", "manual"],
        tag: str,
        started_at: str,
        notify_failures: bool,
    ) -> JobRunResult:
        """Record and announce a prerun that errored or timed out."""
        status: Literal["prerun_error", "prerun_timeout"] = (
            "prerun_timeout" if prerun.outcome == "timeout" else "prerun_error"
        )
        run_id = await self._create_job_run(job, trigger, tag, started_at)
        output = f"{status.replace('_', ' ').title()}: {prerun.diagnostic}"
        await self._record_run_finish(
            run_id,
            output,
            prerun.exit_code,
            status,
            tag,
        )
        if notify_failures:
            await self._notify_prerun_failure(
                job,
                status,
                prerun.diagnostic,
                prerun.exit_code,
                tag,
            )
        return JobRunResult(
            status,
            run_id=run_id,
            output=output,
            exit_code=prerun.exit_code,
        )

    async def _execute_job(
        self,
        job: Job,
        *,
        trigger: Literal["schedule", "manual"] = "schedule",
        notify_failures: bool = True,
    ) -> JobRunResult:
        """Run the shared prerun/provider pipeline and return a terminal result."""
        tag = f"[job:{job.dir_name}]"
        started = datetime.now()
        started_at = datetime.now(timezone.utc).isoformat()
        log.info(
            "%s start name=%r provider=%s model=%s timeout=%ss prerun=%s",
            tag,
            job.name,
            job.provider,
            job.model,
            job.timeout,
            job.prerun or "-",
        )
        lock_file = self._acquire_job_lock(job)
        if lock_file is None:
            output = (
                f"Job '{job.name}' is already running (possibly in another "
                "process); skipped this trigger."
            )
            log.warning("%s %s", tag, output)
            return JobRunResult("error", output=output, exit_code=-1)
        try:
            execution, config_error = await self._validated_job_execution(job)
            if config_error:
                run_id = await self._create_job_run(job, trigger, tag, started_at)
                output = f"Invalid job config: {config_error}"
                log.warning("%s %s", tag, output)
                return await self._failed_run_result(
                    job,
                    run_id=run_id,
                    output=output,
                    exit_code=-1,
                    status="error",
                    tag=tag,
                    notify_failures=notify_failures,
                )

            prerun = await self._run_job_prerun(job, tag)
            if prerun.outcome == "no_work":
                if notify_failures:
                    await self._notify_prerun_recovery(job, tag)
                return JobRunResult("no_work", exit_code=1)

            if prerun.outcome in {"error", "timeout"}:
                return await self._prerun_failure_result(
                    job,
                    prerun,
                    trigger=trigger,
                    tag=tag,
                    started_at=started_at,
                    notify_failures=notify_failures,
                )

            if notify_failures:
                await self._notify_prerun_recovery(job, tag)

            prompt = job.prompt.replace("{{prerun_output}}", prerun.output)

            run_id = await self._create_job_run(job, trigger, tag, started_at)
            proc: Process | None = None
            try:
                assert execution is not None
                async with self.runtime._workspace_slot(execution):
                    execution = await self.runtime._prepare_execution_context(
                        job.provider,
                        execution,
                    )
                    provider = self.runtime.make_provider(
                        job.provider,
                        timeout=job.timeout,
                        context=execution,
                    )
                    cmd = provider.build_batch_command(
                        prompt,
                        job.model,
                        launch=execution.launch,
                    )
                    log.info(
                        "%s spawning provider_class=%s cwd=%s prompt_len=%d",
                        tag,
                        provider.__class__.__name__,
                        execution.path,
                        len(prompt),
                    )
                    log.debug("%s command=%s", tag, _redacted_command(cmd))
                    spawn_kwargs: dict[str, Any] = {
                        "stdin": asyncio.subprocess.DEVNULL,
                        "stdout": asyncio.subprocess.PIPE,
                        "stderr": asyncio.subprocess.STDOUT,
                        "cwd": execution.path,
                    }
                    launch = execution.launch
                    if launch is not None and launch.env is not None:
                        spawn_kwargs["env"] = dict(launch.env)
                    proc = await self.runtime._spawn_process(*cmd, **spawn_kwargs)
                    log.info("%s pid=%s", tag, proc.pid)
                    stdout, _, timed_out = await self.runtime._communicate_with_timeout(
                        proc,
                        f"Job '{job.name}'",
                        job.timeout,
                    )
                    elapsed = (datetime.now() - started).total_seconds()
                    if timed_out:
                        output = f"Job timed out after {job.timeout}s; process tree was terminated"
                        log.warning(
                            "%s timeout after %ss (elapsed=%.1fs); process tree terminated",
                            tag,
                            job.timeout,
                            elapsed,
                        )
                        return await self._failed_run_result(
                            job,
                            run_id=run_id,
                            output=output,
                            exit_code=proc.returncode,
                            status="timeout",
                            tag=tag,
                            notify_failures=notify_failures,
                        )
                    output = provider.parse_batch_output(stdout.decode(errors="replace"))
            except Exception as exc:
                detail = self._sanitize_job_diagnostic(f"{type(exc).__name__}: {exc}")
                output = f"Job could not start or complete{f': {detail}' if detail else ''}"
                exit_code = proc.returncode if proc and proc.returncode is not None else -1
                log.warning("%s %s", tag, output, exc_info=True)
                return await self._failed_run_result(
                    job,
                    run_id=run_id,
                    output=output,
                    exit_code=exit_code,
                    status="error",
                    tag=tag,
                    notify_failures=notify_failures,
                )

            exit_code = proc.returncode if proc.returncode is not None else -1
            status: Literal["ok", "error"] = "ok" if exit_code == 0 else "error"
            await self._record_run_finish(run_id, output, exit_code, status, tag)
            notified = False
            if exit_code != 0 and notify_failures:
                log.warning(
                    "%s nonzero exit=%s output_len=%d notify=%s",
                    tag,
                    exit_code,
                    len(output),
                    job.notify or "default",
                )
                label = f"{job.name} (exit {exit_code})"
                notified = await self._send_job_notification(
                    job,
                    f"⚠️ [{label}]\n{output}",
                    tag,
                )

            elapsed = (datetime.now() - started).total_seconds()
            log.info(
                "%s complete exit=%s duration=%.1fs output_len=%d notified=%s",
                tag,
                exit_code,
                elapsed,
                len(output),
                notified,
            )
            return JobRunResult(
                status,
                run_id=run_id,
                output=output,
                exit_code=exit_code,
            )
        finally:
            if lock_file != "unlocked":
                with contextlib.suppress(OSError):
                    lock_file.close()
            if trigger == "schedule":
                self.last_run[job.dir_name] = datetime.now()
                self.runtime.save_state()

    # -- Run history instrumentation --

    async def _record_run_finish(
        self,
        run_id: str | None,
        output: str,
        exit_code: int | None,
        status: str,
        tag: str,
    ) -> None:
        """Record a terminal run in a worker so SQLite cannot block the loop."""
        await asyncio.to_thread(
            self._record_run_finish_sync,
            run_id,
            output,
            exit_code,
            status,
            tag,
        )

    def _record_run_finish_sync(
        self,
        run_id: str | None,
        output: str,
        exit_code: int | None,
        status: str,
        tag: str,
    ) -> None:
        """Write captured output and mark a run terminal (best-effort).

        Never raises: run instrumentation must not break the job it is
        observing. ``exit_code`` of ``None`` (e.g. a killed process) is stored
        as ``-1`` so the column is always populated.
        """
        if run_id is None:
            return
        if output:
            try:
                runs.append_output(run_id, output)
            except Exception:
                log.warning(
                    "%s could not append output for run id=%s",
                    tag,
                    run_id,
                    exc_info=True,
                )
        try:
            runs.finish(run_id, exit_code if exit_code is not None else -1, status)
        except Exception:
            log.warning("%s could not finish run record id=%s", tag, run_id, exc_info=True)
            return

        runs_cfg = self._runs_cfg()
        try:
            keep = max(0, int(runs_cfg.get("keep", 500)))
            max_age_days = max(0, int(runs_cfg.get("max_age_days", 30)))
        except (TypeError, ValueError):
            log.warning("%s invalid run-retention config; using defaults", tag)
            keep, max_age_days = 500, 30
        try:
            runs.prune(keep=keep, max_age_days=max_age_days)
        except Exception:
            log.warning("%s could not prune run history", tag, exc_info=True)

    # -- Run-now (manual triggers from CLI / web) --

    async def run_now(self, name: str) -> JobRunResult:
        """Run a job's full pipeline manually without sending notifications."""
        job = next((j for j in load_jobs() if j.dir_name == name), None)
        if job is None:
            raise ValueError(f"No such job: {name}")
        return await self._execute_job(
            job,
            trigger="manual",
            notify_failures=False,
        )
