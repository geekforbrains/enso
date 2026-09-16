"""Scheduler, gated job execution, bounded session follow-ups, and failure alerts."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Literal

from .. import db, execution, messages, routing, runs, scheduling, tasks, workflows
from .. import log as logctx
from ..config import Config, LiveConfig, Paths, check_config, project_directory
from ..execution import NOTIFY_LIMIT as NOTIFY_LIMIT
from ..execution import alert_text, enso_error
from ..locks import LockPathError, acquire_file_lock
from ..providers import make_provider
from ..transports import Transport
from . import Job, _job_path, command_stage, load_jobs, parse_job, taskflow

log = logging.getLogger(__name__)

FAILURE_RENOTIFY_SECONDS = 24 * 3600
POSTRUN_FEEDBACK_LIMIT = 64 * 1024
LOCK_FILENAME = ".run.lock"
GROUP_LOCK_DIRNAME = ".concurrency"
INTERRUPTED_ERROR = "interrupted (enso exited before the run finished)"

Decision = Literal["first", "wait", "fire", "misfire"]


@dataclass(frozen=True)
class RunResult:
    """How one trigger ended; a per-job lock collision never creates a run row."""

    status: Literal["ok", "error", "timeout", "no_work", "prerun_error", "skipped"]
    run_id: str | None = None
    output: str = ""
    error: str = ""
    exit_code: int | None = None
    postrun_error: str = ""
    session_id: str | None = None
    task: str | None = None  # the task a stage job claimed, when it did

    def as_dict(self) -> dict:
        return {"ok": self.status in ("ok", "no_work"), **self.__dict__}


@dataclass(frozen=True)
class Prerun:
    outcome: Literal["open", "no_work", "error"]
    output: str = ""
    diagnostic: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class Postrun:
    """One hook invocation, including complete bounded follow-up feedback."""

    exit_code: int | None = None
    output: str = ""
    error: str = ""


def decide(job: Job, last_run: datetime | None, now: datetime) -> Decision:
    """Whether a cron slot passed since ``last_run`` and whether it may still fire."""
    if last_run is None:
        return "first"
    due = job.next_run(last_run)
    if due > now:
        return "wait"
    if not job.catch_up and now - due > timedelta(seconds=job.misfire_grace_seconds):
        return "misfire"
    return "fire"


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse_stamp(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def acquire_lock(job_dir: Path) -> IO[str] | None:
    """Take the per-job ``flock``, or None when another process holds it."""
    return acquire_file_lock(job_dir / LOCK_FILENAME)


def acquire_group_lock(paths: Paths, group: str) -> IO[str] | None:
    """Take a shared execution lock, or None when another group member owns it.

    File locks are released by the operating system if Enso or its host process dies, unlike
    a database flag that would need expiry and recovery rules.  The filename is a digest so a
    user-authored group name can never escape Enso's private runtime directory.
    """
    directory = paths.runtime_dir / GROUP_LOCK_DIRNAME
    if paths.runtime_dir.is_symlink() or directory.is_symlink():
        raise LockPathError(f"group lock directory must not be a symbolic link: {directory}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(group.encode()).hexdigest()
    return acquire_file_lock(directory / f"{name}.lock")


def acquire_project_slot(paths: Paths, project: str, limit: int) -> IO[str] | None:
    """Reserve one project execution slot across schedulers and manual job processes."""
    for number in range(limit):
        lock = acquire_group_lock(paths, f"project:{project}:slot:{number}")
        if lock is not None:
            return lock
    return None


class JobRunner:
    """Fires due jobs, runs them one instance at a time, and alerts on failure."""

    def __init__(self, config: Config, transports: dict[str, Transport] | None = None):
        # The configuration this runner was built with: recovery and runs started outside
        # a tick use it. A tick reads the file fresh and every run keeps its own snapshot.
        self.config = config
        self._live = LiveConfig(config)
        self.paths = config.paths
        self.transports = transports or {}
        self._running: dict[str, asyncio.Task[RunResult]] = {}
        self._problems: dict[str, list[str]] = {}
        self._run_env: dict[str, dict[str, str]] = {}  # per-run additions, by run id
        self._sweep_failures: dict[str, str] = {}
        self._sweep_locks: dict[str, asyncio.Lock] = {}  # one sweep per project at a time

    def running(self) -> list[str]:
        return sorted(name for name, task in self._running.items() if not task.done())

    # -- Scheduler --

    async def scheduler(self) -> None:
        """Every minute, on the minute, fire the jobs whose slot has come."""
        await scheduling.minute_loop({"jobs": self.tick})

    async def tick(self, now: datetime) -> None:
        """One scheduler pass: reload JOB.md files and start whatever is due."""
        from ..maintenance import paused

        if paused(self.paths):
            return
        config = await asyncio.to_thread(self._live.current)
        await workflows.drain_events(self.paths, config)
        jobs, problems = await asyncio.to_thread(load_jobs, self.paths, config)
        if problems != self._problems:
            for name, found in problems.items():
                log.warning("%s: %s", name, "; ".join(found))
            self._problems = problems
        states = await asyncio.to_thread(db.job_states, self.paths)
        for job in jobs:
            if paused(self.paths):
                return
            if not job.enabled or job.ref in problems or job.ref in self._running:
                continue
            if job.schedule is None:
                # A stage job without a schedule fires when a task waits; an idle stage
                # leaves no row and no line, so polling costs nothing to read later.
                if await self._ready(job, config):
                    self.start(job, trigger="ready", config=config)
                continue
            state = states.get(job.ref)
            decision = decide(job, _parse_stamp(state.last_run if state else None), now)
            log.debug("%s: %s", job.ref, decision)
            if decision == "wait":
                continue
            await asyncio.to_thread(db.set_last_run, self.paths, job.ref, _stamp(now))
            if decision == "misfire":
                log.warning(
                    "%s missed its slot by more than %ss; skipping (catch_up is off)",
                    job.ref,
                    job.misfire_grace_seconds,
                )
            elif decision == "fire":
                if job.stage is not None and not await self._ready(job, config):
                    log.debug("%s: slot passed but no task is ready", job.ref)
                    continue
                self.start(job, trigger="schedule", config=config)
        for key, project in config.projects.items():
            if project.repo is not None:
                await self._sweep(key, config)

    async def _sweep(self, key: str, config: Config) -> list[str]:
        """Sweep one project's worktrees; a tick and a finishing run never sweep it at once."""
        lock = self._sweep_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await taskflow.sweep(self.paths, config, key, self._sweep_failures)

    async def _ready(self, job: Job, config: Config) -> bool:
        assert job.project is not None and job.stage is not None
        try:
            return await asyncio.to_thread(tasks.ready, self.paths, config, job.project, job.stage)
        except tasks.TaskError as exc:
            log.warning("%s: %s", job.ref, exc)
            return False

    def start(
        self, job: Job, *, trigger: str, config: Config | None = None
    ) -> asyncio.Task[RunResult]:
        """Run a job in the background, remembering it until it finishes."""
        task = asyncio.create_task(
            self.run(job, trigger=trigger, config=config), name=f"job:{job.ref}"
        )
        self._running[job.ref] = task

        def forget(done: asyncio.Task[RunResult]) -> None:
            if self._running.get(job.ref) is done:
                del self._running[job.ref]
            if not done.cancelled() and done.exception() is not None:
                log.error("job %s crashed", job.ref, exc_info=done.exception())

        task.add_done_callback(forget)
        return task

    async def stop(self) -> None:
        """Cancel every active run and wait until each has closed its row."""
        tasks = [task for task in self._running.values() if not task.done()]
        if not tasks:
            return
        log.info("stopping %d running jobs", len(tasks))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def recover(self) -> int:
        """Close ``running`` rows whose owner is gone; returns how many."""
        closed = 0
        for run in runs.unfinished(self.paths):
            job_dir = self.paths.job(run.job).parent
            if any(path.is_symlink() for path in (job_dir, *job_dir.parents)):
                log.warning("cannot recover %s: its job path contains a symbolic link", run.job)
                continue
            if job_dir.is_dir():
                lock = acquire_lock(job_dir)
                if lock is None:
                    # The row belongs to an ``enso job run`` still holding the per-job
                    # lock in another process (§3.9); it will close its own row.
                    continue
                lock.close()
            # A missing job directory means nobody can hold its lock: an orphan too. An
            # owner that closes its row between the listing and here keeps its outcome.
            if runs.abandon(self.paths, run.id, INTERRUPTED_ERROR):
                closed += 1
                # The run can no longer hand off, so the claims it took would stay forever
                # and the task would never be ready again; let them go like any run that
                # ended without a handoff.
                message = f"run {run.id} ended ({INTERRUPTED_ERROR}) without a handoff"
                for ref in taskflow.release_orphans(self.paths, self.config, run.id, message):
                    log.warning("run=%s released %s: %s", run.id, ref, INTERRUPTED_ERROR)
        return closed

    # -- One run --

    def _admission_problem(self, job: Job, config: Config) -> str:
        """Recheck queued work after locking; maintenance never interrupts an active run."""
        from ..maintenance import paused

        if paused(self.paths):
            return "Enso is paused for maintenance"
        if job.stage is None:
            return ""
        if config.source_hash is not None:
            fresh, _, _ = check_config(self.paths)
            if (
                fresh is None
                or fresh.source_hash != config.source_hash
                or fresh.projects.get(job.project or "") != config.projects.get(job.project or "")
            ):
                return (
                    "project configuration changed before the stage run started; trigger it again"
                )
        if job.path.exists():
            current, problems = parse_job(job.path, config)
            if problems or current != job:
                return "stage job definition changed before the run started; trigger it again"
        return ""

    async def run(self, job: Job, *, trigger: str, config: Config | None = None) -> RunResult:
        """Lock, prerun, provider, run row, postrun; scheduled runs also alert.

        ``config`` is the snapshot the whole run reads: the one its tick loaded, else the
        one this runner was built with, which is what validated ``job``.
        """
        with logctx.context(f"j:{job.ref}"):
            try:
                if _job_path(self.paths, job.ref) != job.path:
                    raise ValueError("job path does not match its owning workspace")
            except ValueError as exc:
                return RunResult("skipped", error=str(exc))
            lock = await asyncio.to_thread(acquire_lock, job.job_dir)
            if lock is None:
                log.info("already running (lock held); skipping this trigger")
                return RunResult("skipped", error="already running")
            try:
                selected = self.config if config is None else config
                problem = await asyncio.to_thread(self._admission_problem, job, selected)
                if problem:
                    log.info(problem)
                    return RunResult("skipped", error=problem)
                return await self._run_locked(job, trigger, selected)
            finally:
                # last_run was already stamped at dispatch in tick(); re-stamping here with
                # the completion time would shift the anchor a long run computes its next
                # slot from and hide a slot it outlived from the misfire check.
                lock.close()

    async def _run_locked(self, job: Job, trigger: str, config: Config) -> RunResult:
        notify = trigger in ("schedule", "ready")
        # Clamped once here so the run row and the provider command cannot disagree.
        effort = (
            job.effort
            if command_stage(job, config)
            else routing.clamp_effort(job.provider, job.model, job.effort)
        )
        run_id = await asyncio.to_thread(runs.start, self.paths, job, trigger, effort=effort)
        log.info(
            "start run=%s trigger=%s %s %s %s workspace=%s prerun=%s postrun=%s timeout=%ss",
            run_id,
            trigger,
            job.provider,
            job.model,
            effort,
            job.workspace,
            job.prerun or "-",
            job.postrun or "-",
            job.timeout,
        )
        started = time.monotonic()
        resource_locks: list[IO[str]] = []
        stage: taskflow.StageRun | None = None
        try:
            prerun = await self._prerun(job, run_id)
            if prerun.outcome == "error":
                result = RunResult(
                    "prerun_error", run_id, error=prerun.diagnostic, exit_code=prerun.exit_code
                )
            elif prerun.outcome == "no_work":
                result = RunResult("no_work", run_id, exit_code=1)
            else:
                resource_locks, collision = await self._acquire_resources(job, config)
                if collision:
                    log.info(collision)
                    result = RunResult("skipped", run_id, error=collision)
                else:
                    stage, early = await self._claim(job, run_id, prerun.output, config=config)
                    if early is not None:
                        result = early
                    else:
                        result = await self._turns(
                            job, run_id, prerun.output, effort, started, stage, config=config
                        )
            if job.postrun and result.status in ("no_work", "prerun_error", "skipped"):
                hook = await self._postrun(
                    job, result, int((time.monotonic() - started) * 1000), attempt=0
                )
                checked, diagnostic = self._checked_result(job, result, hook, attempt=0)
                await self._record_attempt(result, 0, hook=replace(hook, error=diagnostic))
                result = checked
            if stage is not None:
                result = replace(result, task=stage.task.ref)
                await self._settle(stage, run_id, result.status, config=config, error=result.error)
                await workflows.drain_events(self.paths, config)
                stage = taskflow.end_ownership(stage)
            await asyncio.to_thread(self._finish, result, config)
        except BaseException as exc:
            # A stop (cancellation) or a bug must not leave the row ``running`` forever:
            # runs.prune keeps such rows, so nothing else would ever close it.
            cancelled = isinstance(exc, asyncio.CancelledError)
            error = "cancelled (enso stopped)" if cancelled else f"{type(exc).__name__}: {exc}"
            task = stage.task.ref if stage is not None else None
            await asyncio.to_thread(
                self._finish, RunResult("error", run_id, error=error, task=task), config
            )
            log.warning("run=%s did not finish: %s", run_id, error)
            if stage is not None:
                await self._settle(
                    stage, run_id, "cancelled" if cancelled else "error", config=config, error=error
                )
            raise
        finally:
            self._run_env.pop(run_id, None)
            if stage is not None:
                taskflow.end_ownership(stage)
            for lock in resource_locks:
                lock.close()
        if stage is not None:
            await asyncio.to_thread(tasks.settle_dependencies, self.paths, config)
        if stage is not None and job.project is not None:
            settled = await asyncio.to_thread(tasks.get, self.paths, stage.task.ref)
            if settled.finished:
                await self._sweep(job.project, config)
        log.info(
            "finish status=%s exit=%s output_len=%d",
            result.status,
            result.exit_code,
            len(result.output),
        )
        if notify:
            await self._alert(job, prerun, result, config=config)
        return result

    async def _acquire_resources(self, job: Job, config: Config) -> tuple[list[IO[str]], str]:
        """Reserve explicit resources and project capacity without waiting or partial holds."""
        held: list[IO[str]] = []
        try:
            if job.group:
                lock = await asyncio.to_thread(acquire_group_lock, self.paths, job.group)
                if lock is None:
                    return [], f"concurrency group {job.group!r} is already running"
                held.append(lock)
            if job.project is not None:
                slot = await asyncio.to_thread(
                    acquire_project_slot,
                    self.paths,
                    job.project,
                    config.projects[job.project].max_concurrency,
                )
                if slot is None:
                    for lock in held:
                        lock.close()
                    return [], f"project {job.project} is at its run limit"
                held.append(slot)
        except BaseException:
            for lock in held:
                lock.close()
            raise
        return held, ""

    def _finish(self, result: RunResult, config: Config) -> int | None:
        """Close the row only after every turn and hook has finished."""
        assert result.run_id is not None
        duration_ms = runs.finish(
            self.paths,
            result.run_id,
            status=result.status,
            exit_code=result.exit_code,
            output=result.output,
            error=result.error,
            postrun_error=result.postrun_error,
            session_id=result.session_id,
        )
        pruned = runs.prune(self.paths, config.runs.keep, config.runs.max_age_days)
        if pruned:
            log.debug("pruned %d old runs", pruned)
        return duration_ms

    async def _claim(
        self, job: Job, run_id: str, prerun_output: str, *, config: Config
    ) -> tuple[taskflow.StageRun | None, RunResult | None]:
        """On a stage job, take and frame a task; a result here means no provider turn runs."""
        if job.stage is None:
            return None, None
        stage = await taskflow.begin(self.paths, config, job, run_id, prerun_output)
        if stage is None:
            log.info("no task is ready in %s/%s", job.project, job.stage)
            return None, RunResult("no_work", run_id)
        if stage.prompt is None:  # preparation settled or deferred; no provider may start
            return stage, RunResult(
                "no_work" if stage.deferred else "error", run_id, error=stage.error
            )
        self._run_env[run_id] = stage.env
        return stage, None

    async def _turns(
        self,
        job: Job,
        run_id: str,
        prerun_output: str,
        effort: str,
        started: float,
        stage: taskflow.StageRun | None,
        *,
        config: Config,
    ) -> RunResult:
        """The provider turns and hooks, framed by the stage's prompt when there is one."""
        if stage is not None and command_stage(job, config):
            return await self._stage_command(job, run_id, stage, started, config=config)
        if stage is not None:
            return await self._stage_turns(job, run_id, stage, effort, started, config=config)
        prompt = stage.prompt if stage is not None else None
        if job.postrun:
            return await self._execute_checked(
                job, run_id, prerun_output, effort, started, prompt=prompt, config=config
            )
        turn_started = time.monotonic()
        result = await self._execute(
            job, run_id, prerun_output, effort, prompt=prompt, config=config
        )
        await self._record_attempt(
            result, 1, duration_ms=int((time.monotonic() - turn_started) * 1000)
        )
        return result

    async def _stage_command(
        self, job: Job, run_id: str, stage: taskflow.StageRun, started: float, *, config: Config
    ) -> RunResult:
        """Run a configured command or integration transaction without invoking a model."""
        assert job.project is not None and job.stage is not None
        definition = config.projects[job.project].stage(job.stage)
        assert definition is not None
        output = ""
        exit_code: int | None = 0
        if definition.command is not None:
            output, stderr, exit_code, timed_out = await execution.run_process(
                ["bash", "-c", definition.command],
                cwd=project_directory(self.paths, job.workspace, job.project),
                env=self._env(job, run_id),
                timeout=job.timeout,
                merge_stderr=False,
                label=f"stage command {job.ref}",
                output_keep=POSTRUN_FEEDBACK_LIMIT,
            )
            if timed_out or exit_code != 0:
                result = RunResult(
                    "timeout" if timed_out else "error",
                    run_id,
                    output=output,
                    error=(
                        f"stage command timed out after {job.timeout}s"
                        if timed_out
                        else enso_error(stderr) or f"stage command exited with status {exit_code}"
                    ),
                    exit_code=exit_code,
                )
                await self._record_attempt(result, 1)
                return result
        tasks.move(
            self.paths,
            config,
            stage.task.ref,
            "advance",
            actor=tasks.ENSO_ACTOR,
            run_id=run_id,
            message="Integration requested" if definition.integrate else "Stage command completed",
        )
        result = RunResult("ok", run_id, output=output, exit_code=exit_code)
        await self._record_attempt(result, 1)
        if job.postrun:
            hook = await self._postrun(
                job, result, int((time.monotonic() - started) * 1000), attempt=1
            )
            result, diagnostic = self._checked_result(job, result, hook, attempt=1)
            await self._record_attempt(result, 1, hook=replace(hook, error=diagnostic))
            if result.status != "ok":
                return result
        evaluated = await workflows.evaluate(
            self.paths, config, stage.task.ref, run_id, self._env(job, run_id)
        )
        if evaluated.status != "accepted":
            return replace(result, status="error", error=evaluated.feedback)
        return result

    async def _stage_turns(
        self,
        job: Job,
        run_id: str,
        stage: taskflow.StageRun,
        effort: str,
        started: float,
        *,
        config: Config,
    ) -> RunResult:
        """Submit, stop writing, verify, and repair within one shared provider budget."""
        assert stage.prompt is not None
        provider = make_provider(job.provider, config.providers[job.provider].path)
        args = config.provider_args(job.workspace, job.provider)
        remaining = float(job.timeout)
        prompt = stage.prompt
        session_id = None
        attempt = 1
        postrun_followups = 0
        while True:
            turn_started = time.monotonic()
            turn = await execution.execute_turn(
                provider,
                prompt,
                job.model,
                effort,
                args,
                cwd=self.paths.workspace(job.workspace),
                env=self._env(job, run_id),
                timeout=remaining,
                session_id=session_id,
            )
            elapsed = time.monotonic() - turn_started
            remaining = max(0.0, remaining - elapsed)
            duration_ms = int(elapsed * 1000)
            result = RunResult(
                turn.status,
                run_id,
                output=turn.output,
                error=turn.error,
                exit_code=turn.exit_code,
                session_id=turn.session_id,
            )
            if result.status == "timeout":
                result = replace(
                    result, error=f"timed out after {job.timeout}s of provider runtime"
                )
            await self._record_attempt(result, attempt, duration_ms=duration_ms)
            if job.postrun:
                hook = await self._postrun(
                    job,
                    result,
                    int((time.monotonic() - started) * 1000),
                    attempt=postrun_followups + 1,
                )
                checked, diagnostic = self._checked_result(
                    job, result, hook, attempt=postrun_followups + 1
                )
                await self._record_attempt(
                    result, attempt, hook=replace(hook, error=diagnostic), duration_ms=duration_ms
                )
                if checked.status != "ok":
                    return checked
                if hook.exit_code == 10:
                    if remaining <= 0:
                        return replace(
                            result, status="timeout", error="provider time budget exhausted"
                        )
                    prompt = hook.output
                    session_id = result.session_id
                    postrun_followups += 1
                    attempt += 1
                    continue
            if result.status != "ok":
                return result
            evaluated = await workflows.evaluate(
                self.paths, config, stage.task.ref, run_id, self._env(job, run_id)
            )
            if evaluated.status == "accepted":
                return result
            if evaluated.status != "repair":
                return replace(result, status="error", error=evaluated.feedback)
            if remaining <= 0:
                return replace(result, status="timeout", error="provider time budget exhausted")
            prompt = (
                stage.prompt
                + "\n\n[Enso verification feedback]\n"
                + evaluated.feedback
                + "\nRepair the candidate and submit a new handoff, then stop."
            )
            session_id = result.session_id
            attempt += 1

    async def _settle(
        self,
        stage: taskflow.StageRun,
        run_id: str,
        status: str,
        *,
        config: Config,
        error: str = "",
    ) -> None:
        """Release a claim the agent never handed off; best effort, so a crash cannot mask one."""
        try:
            task = await taskflow.end(self.paths, config, stage, run_id, status, error)
        except tasks.TaskError, OSError:
            log.warning("run=%s could not release %s", run_id, stage.task.ref, exc_info=True)
            return
        if task.claim_run_id is None and task.stage != stage.task.stage:
            log.info("run=%s %s moved to %s", run_id, task.ref, task.stage)

    def _env(self, job: Job, run_id: str) -> dict[str, str]:
        return {
            **os.environ,
            "ENSO_JOB": job.ref,
            "ENSO_RUN_ID": run_id,
            "ENSO_WORKSPACE": job.workspace,
            "ENSO_HOME": str(self.paths.home),
            **self._run_env.get(run_id, {}),  # ENSO_TASK and ENSO_TASK_DIR on a stage run
        }

    async def _prerun(self, job: Job, run_id: str) -> Prerun:
        """exit 0 → run with stdout; 1 → no work; anything else, a timeout, or no script → error."""
        if job.prerun is None:
            return Prerun("open")
        script = job.job_dir / job.prerun
        if not script.is_file():
            return Prerun("error", diagnostic=f"prerun script not found: {job.prerun}")
        log.info("prerun %s timeout=%ss", job.prerun, job.prerun_timeout)
        try:
            stdout, stderr, rc, timed_out = await execution.run_process(
                ["bash", str(script)],
                cwd=job.job_dir,
                env=self._env(job, run_id),
                timeout=job.prerun_timeout,
                merge_stderr=False,
                label=f"prerun {job.ref}",
            )
        except OSError as exc:
            return Prerun("error", diagnostic=f"could not start prerun: {exc}")
        if timed_out:
            diagnostic = f"prerun timed out after {job.prerun_timeout}s"
            log.warning(diagnostic)
            return Prerun("error", diagnostic=diagnostic, exit_code=rc)
        if rc == 0:
            log.info("prerun open output_len=%d", len(stdout))
            return Prerun("open", output=stdout.strip(), exit_code=0)
        if rc == 1:
            log.debug("prerun closed: no work")
            return Prerun("no_work", exit_code=1)
        # Only an explicit ENSO_ERROR line reaches the alert: stdout and the rest of
        # stderr may hold whatever the script scraped.
        diagnostic = enso_error(stderr) or f"prerun exited with status {rc}"
        log.warning("prerun failed: %s", diagnostic)
        return Prerun("error", diagnostic=diagnostic, exit_code=rc)

    async def _execute(
        self,
        job: Job,
        run_id: str,
        prerun_output: str,
        effort: str,
        *,
        prompt: str | None = None,
        config: Config,
    ) -> RunResult:
        """Run the provider in batch mode with the prerun output substituted into the prompt.

        ``prompt`` replaces the job's own when a stage run has already framed it.
        """
        if prompt is None:
            prompt = job.prompt.replace("{{prerun_output}}", prerun_output)
        provider = make_provider(job.provider, config.providers[job.provider].path)
        args = config.provider_args(job.workspace, job.provider)
        cwd = self.paths.workspace(job.workspace)
        turn = await execution.execute_batch(
            provider,
            prompt,
            job.model,
            effort,
            args,
            cwd=cwd,
            env=self._env(job, run_id),
            timeout=job.timeout,
            label=f"job {job.ref}",
        )
        return RunResult(
            turn.status, run_id, output=turn.output, error=turn.error, exit_code=turn.exit_code
        )

    async def _record_attempt(
        self,
        result: RunResult,
        number: int,
        *,
        hook: Postrun | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Persist completed provider work before the hook, then attach the hook's result."""
        assert result.run_id is not None
        await asyncio.to_thread(
            runs.record_attempt,
            self.paths,
            result.run_id,
            number=number,
            status=result.status,
            exit_code=result.exit_code,
            output=result.output,
            error=result.error,
            session_id=result.session_id,
            postrun_exit_code=hook.exit_code if hook else None,
            postrun_output=hook.output if hook else "",
            postrun_error=hook.error if hook else "",
            duration_ms=duration_ms,
        )

    def _checked_result(
        self, job: Job, result: RunResult, hook: Postrun, *, attempt: int
    ) -> tuple[RunResult, str]:
        """Make hook failures authoritative without disguising an earlier provider failure."""
        diagnostic = hook.error
        if not diagnostic and hook.exit_code == 10:
            if attempt == 0:
                diagnostic = "postrun requested a follow-up, but no provider ran"
            elif result.status != "ok":
                diagnostic = f"postrun cannot request a follow-up after provider {result.status}"
            elif attempt > job.max_followups:
                diagnostic = (
                    f"postrun requested a follow-up beyond max_followups={job.max_followups}"
                )
            elif not result.session_id:
                diagnostic = (
                    "postrun requested a follow-up, but the provider returned no session id"
                )
        if not diagnostic:
            return result, ""
        log.warning("run=%s postrun failed: %s", result.run_id, diagnostic)
        if result.status in ("ok", "no_work", "skipped"):
            return replace(
                result, status="error", error=diagnostic, postrun_error=diagnostic
            ), diagnostic
        return replace(result, postrun_error=diagnostic), diagnostic

    async def _execute_checked(
        self,
        job: Job,
        run_id: str,
        prerun_output: str,
        effort: str,
        started: float,
        *,
        prompt: str | None = None,
        config: Config,
    ) -> RunResult:
        """Run one fresh session and bounded repairs, sharing only the provider time budget."""
        if prompt is None:
            prompt = job.prompt.replace("{{prerun_output}}", prerun_output)
        provider = make_provider(job.provider, config.providers[job.provider].path)
        args = config.provider_args(job.workspace, job.provider)
        remaining = float(job.timeout)
        session_id = None
        attempt = 1
        while True:
            turn_started = time.monotonic()
            turn = await execution.execute_turn(
                provider,
                prompt,
                job.model,
                effort,
                args,
                cwd=self.paths.workspace(job.workspace),
                env=self._env(job, run_id),
                timeout=remaining,
                session_id=session_id,
            )
            elapsed = time.monotonic() - turn_started
            remaining = max(0.0, remaining - elapsed)
            duration_ms = int(elapsed * 1000)
            result = RunResult(
                turn.status,
                run_id,
                output=turn.output,
                error=turn.error,
                exit_code=turn.exit_code,
                session_id=turn.session_id,
            )
            if result.status == "timeout":
                result = replace(
                    result, error=f"timed out after {job.timeout}s of provider runtime"
                )
            await self._record_attempt(result, attempt, duration_ms=duration_ms)
            hook = await self._postrun(
                job, result, int((time.monotonic() - started) * 1000), attempt=attempt
            )
            checked, diagnostic = self._checked_result(job, result, hook, attempt=attempt)
            if not diagnostic and hook.exit_code == 10 and remaining <= 0:
                diagnostic = (
                    "postrun requested a follow-up after the provider time budget was exhausted"
                )
                checked = replace(
                    result,
                    status="timeout",
                    error=f"timed out after {job.timeout}s of provider runtime",
                    postrun_error=diagnostic,
                )
            await self._record_attempt(
                result, attempt, hook=replace(hook, error=diagnostic), duration_ms=duration_ms
            )
            if hook.exit_code != 10 or diagnostic:
                return checked
            prompt = hook.output
            session_id = result.session_id
            attempt += 1

    async def _postrun(
        self, job: Job, result: RunResult, duration_ms: int, *, attempt: int
    ) -> Postrun:
        """Hand the latest outcome to a hook; exit 10 requests a complete feedback message."""
        assert job.postrun is not None
        assert result.run_id is not None
        script = job.job_dir / job.postrun
        if not script.is_file():
            return Postrun(error=f"postrun script not found: {job.postrun}")
        env = {
            **self._env(job, result.run_id),
            "ENSO_RUN_STATUS": result.status,
            "ENSO_RUN_EXIT_CODE": "" if result.exit_code is None else str(result.exit_code),
            "ENSO_RUN_DURATION_MS": str(duration_ms),
            "ENSO_RUN_ATTEMPT": str(attempt),
            "ENSO_RUN_FOLLOWUPS_REMAINING": str(max(0, job.max_followups - max(0, attempt - 1))),
        }
        log.info("postrun %s timeout=%ss", job.postrun, job.postrun_timeout)
        try:
            stdout, stderr, rc, timed_out = await execution.run_process(
                ["bash", str(script)],
                cwd=job.job_dir,
                env=env,
                timeout=job.postrun_timeout,
                merge_stderr=False,
                label=f"postrun {job.ref}",
                stdin=result.output.encode(),
                # Keep one sentinel byte so an oversized message can never be silently
                # shortened into different instructions for the next provider turn.
                output_keep=POSTRUN_FEEDBACK_LIMIT + 1,
            )
        except OSError as exc:
            return Postrun(error=f"could not start postrun: {exc}")
        if timed_out:
            return Postrun(rc, stdout, f"postrun timed out after {job.postrun_timeout}s")
        log.info("postrun exit=%s output_len=%d", rc, len(stdout))
        if rc == 0:
            return Postrun(rc, stdout)
        if rc == 10:
            if len(stdout.encode()) > POSTRUN_FEEDBACK_LIMIT:
                return Postrun(
                    rc, stdout, f"postrun follow-up exceeds {POSTRUN_FEEDBACK_LIMIT} bytes"
                )
            if not stdout.strip():
                return Postrun(
                    rc, stdout, "postrun exited with status 10 without a feedback message"
                )
            return Postrun(rc, stdout)
        return Postrun(rc, stdout, enso_error(stderr) or f"postrun exited with status {rc}")

    # -- Alerts --

    async def _alert(self, job: Job, prerun: Prerun, result: RunResult, *, config: Config) -> None:
        if result.status == "prerun_error":
            diagnostic = prerun.diagnostic
            if result.postrun_error:
                diagnostic += f"\nPostrun: {result.postrun_error}"
            await self._alert_prerun(job, replace(prerun, diagnostic=diagnostic), config=config)
            return
        await self._recovered(job, config=config)
        if result.status == "timeout":
            body = "\n".join(part for part in (result.postrun_error, result.output) if part)
            await self._send(job, f"⚠️ [{job.ref}] {result.error}", body, config=config)
        elif result.status == "error":
            if result.postrun_error and result.error == result.postrun_error:
                await self._send(
                    job, f"⚠️ [{job.ref}] postrun failed", result.postrun_error, config=config
                )
            else:
                label = f"exit {result.exit_code}" if result.exit_code not in (None, 0) else "error"
                body = "\n".join(
                    part for part in (result.postrun_error, result.output or result.error) if part
                )
                await self._send(job, f"⚠️ [{job.ref} ({label})]", body, config=config)

    async def _alert_prerun(self, job: Job, prerun: Prerun, *, config: Config) -> None:
        """Alert once per distinct prerun failure, again after a day of the same one."""
        fingerprint = hashlib.sha256(
            f"{prerun.exit_code}\0{prerun.diagnostic}".encode()
        ).hexdigest()
        state = await asyncio.to_thread(db.job_state, self.paths, job.ref)
        alerted = _parse_stamp(state.failure_alerted_at)
        if (
            state.failure_fingerprint == fingerprint
            and alerted is not None
            and datetime.now(alerted.tzinfo) - alerted < timedelta(seconds=FAILURE_RENOTIFY_SECONDS)
        ):
            log.info("same prerun failure already alerted; suppressed")
            return
        if await self._send(job, f"⚠️ [{job.ref}] prerun failed", prerun.diagnostic, config=config):
            await asyncio.to_thread(db.set_failure, self.paths, job.ref, fingerprint, db.now())

    async def _recovered(self, job: Job, *, config: Config) -> None:
        """One notice when a prerun works again after an alerted failure."""
        state = await asyncio.to_thread(db.job_state, self.paths, job.ref)
        if state.failure_fingerprint is None:
            return
        if await self._send(job, f"✅ [{job.ref}] prerun recovered", config=config):
            await asyncio.to_thread(db.set_failure, self.paths, job.ref, None, None)

    async def _send(self, job: Job, headline: str, body: str = "", *, config: Config) -> bool:
        """Deliver to the job's ``notify`` target, else the transport default; never raises."""
        try:
            target = config.resolve_target(job.notify) if job.notify else config.default_notify()
        except ValueError as exc:
            log.warning("alert not sent: %s", exc)
            return False
        if target is None:
            log.warning("alert not sent: no notify target is configured")
            return False
        transport = self.transports.get(target[0])
        if transport is None:
            log.warning("alert not sent: the %s transport is not running", target[0])
            return False
        text = alert_text(headline, body)
        try:
            await messages.deliver(
                self.paths,
                transport.send(target[1], text),
                workspace=job.workspace,
                transport=target[0],
                target=target[1],
                thread=None,
                text=text,
                source=f"job:{job.ref}",
            )
        except Exception:
            log.warning("alert not sent", exc_info=True)
            return False
        log.info("alert sent to %s:%s", *target)
        return True
