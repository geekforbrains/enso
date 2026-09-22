"""Scheduler, gated job execution, bounded session follow-ups, and failure alerts."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import IO, Literal

from .. import db, execution, messages, routing, runs, secrets, tasks, workflows
from .. import log as logctx
from ..config import (
    Config,
    LiveConfig,
    Paths,
    check_config,
    project_directory,
    split_job_ref,
)
from ..execution import alert_text, enso_error
from ..locks import acquire_file_lock
from ..providers import make_provider
from ..transports import Transport
from . import Job, _job_path, concurrency, execution_kind, load_jobs, parse_job, taskflow
from .concurrency import acquire_group_lock as acquire_group_lock

log = logging.getLogger(__name__)

FAILURE_RENOTIFY_SECONDS = 24 * 3600
POSTRUN_FEEDBACK_LIMIT = 64 * 1024
INTERRUPTED_ERROR = "interrupted (enso exited before the run finished)"

Decision = Literal["first", "wait", "fire", "misfire"]


@dataclass(frozen=True)
class RunResult:
    """How one trigger ended; a per-job lock collision never creates a run row."""

    status: Literal["ok", "error", "timeout", "no_work", "gate_error", "skipped"]
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
class Gate:
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


def acquire_lock(paths: Paths, ref: str) -> IO[str] | None:
    """Take the per-job ``flock``, or None when another process holds it."""
    return acquire_file_lock(paths.lock("jobs", *split_job_ref(ref)))


def acquire_project_slot(paths: Paths, project: str, limit: int) -> IO[str] | None:
    """Reserve one project execution slot across schedulers and manual job processes."""
    for number in range(limit):
        lock = acquire_file_lock(paths.lock("project-slots", project, str(number)))
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
        self._run_env: dict[str, dict[str, str]] = {}  # environment snapshots, never persisted
        self._sweep_failures: dict[str, str] = {}
        self._sweep_locks: dict[str, asyncio.Lock] = {}  # one sweep per project at a time

    def running(self) -> list[str]:
        return sorted(name for name, task in self._running.items() if not task.done())

    async def tick(self, now: datetime) -> None:
        """One scheduler pass: reload JOB.md files and start whatever is due."""
        from ..maintenance import paused

        if paused(self.paths):
            return
        config = await asyncio.to_thread(self._live.current)
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
            try:
                decision = decide(job, _parse_stamp(state.last_run if state else None), now)
            except (ValueError, OverflowError) as exc:
                log.warning("%s: could not determine next slot: %s", job.ref, exc)
                continue
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

    async def maintenance_tick(self, now: datetime) -> None:
        """Deliver lifecycle reactions and sweep independently of job dispatch."""
        from ..maintenance import paused

        if paused(self.paths):
            return
        config = await asyncio.to_thread(self._live.current)
        await workflows.drain_events(self.paths, config)
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
            lock = acquire_lock(self.paths, run.job)
            if lock is None:
                # The row belongs to an ``enso job run`` still holding the per-job
                # lock in another process (§3.9); it will close its own row.
                continue
            try:
                if runs.abandon(self.paths, run.id, INTERRUPTED_ERROR):
                    closed += 1
                    message = f"run {run.id} ended ({INTERRUPTED_ERROR}) without a handoff"
                    for ref in taskflow.release_orphans(self.paths, self.config, run.id, message):
                        log.warning("run=%s released %s: %s", run.id, ref, INTERRUPTED_ERROR)
            finally:
                lock.close()
        # An older process may have closed its row before releasing its task claim.
        # Reconcile these independently, including when retention removed the old row.
        for task in tasks.orphaned_job_claims(self.paths):
            assert task.claim_actor is not None and task.claim_run_id is not None
            lock = acquire_lock(self.paths, task.claim_actor.removeprefix("job:"))
            if lock is None:
                continue
            try:
                taskflow.release_orphans(
                    self.paths, self.config, task.claim_run_id, INTERRUPTED_ERROR
                )
            finally:
                lock.close()
        return closed

    # -- One run --

    def _admission_problem(self, job: Job, config: Config) -> str:
        """Recheck queued work after locking; maintenance never interrupts an active run."""
        from ..maintenance import paused

        if paused(self.paths):
            return "Enso is paused for maintenance"
        if job.stage is not None and config.source_hash is not None:
            fresh, _, _ = check_config(self.paths)
            if (
                fresh is None
                or fresh.source_hash != config.source_hash
                or fresh.projects.get(job.project or "") != config.projects.get(job.project or "")
            ):
                return (
                    "project configuration changed before the stage run started; trigger it again"
                )
        current, problems = parse_job(job.path, config)
        if problems or current != job:
            return "job definition changed before execution; trigger it again"
        return ""

    async def run(self, job: Job, *, trigger: str, config: Config | None = None) -> RunResult:
        """Lock, gate, provider, run row, postrun; scheduled runs also alert.

        ``config`` is the snapshot the whole run reads: the one its tick loaded, else the
        one this runner was built with, which is what validated ``job``.
        """
        with logctx.context(f"j:{job.ref}"):
            try:
                if _job_path(self.paths, job.ref) != job.path:
                    raise ValueError("job path does not match its owning workspace")
            except ValueError as exc:
                return RunResult("skipped", error=str(exc))
            lock = await self._lock(acquire_lock, self.paths, job.ref)
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

    async def _lock(self, function: Callable[..., IO[str] | None], *args: object) -> IO[str] | None:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            lock = await execution.finish_cleanup(worker)
            if lock is not None:
                lock.close()
            raise

    async def _open_run(
        self, job: Job, trigger: str, effort: str | None, kind: str, config: Config
    ) -> str:
        """Do not strand a late database insertion when admission is cancelled."""
        opening = asyncio.create_task(
            asyncio.to_thread(runs.start, self.paths, job, trigger, effort=effort, kind=kind)
        )
        try:
            run_id = await asyncio.shield(opening)
        except asyncio.CancelledError:
            run_id = await execution.finish_cleanup(opening)
            await execution.finish_cleanup(
                asyncio.to_thread(
                    self._finish,
                    RunResult("error", run_id, error="cancelled (enso stopped)"),
                    config,
                )
            )
            raise
        return run_id

    async def _run_locked(self, job: Job, trigger: str, config: Config) -> RunResult:
        agent = job.agent
        effort = routing.clamp_effort(agent.provider, agent.model, agent.effort) if agent else None
        kind = execution_kind(job, config)
        run_id = await self._open_run(job, trigger, effort, kind, config)
        log.info(
            "start run=%s trigger=%s kind=%s workspace=%s timeout=%ss",
            run_id,
            trigger,
            kind,
            job.workspace,
            job.timeout,
        )
        started = time.monotonic()
        resource_locks: list[IO[str]] = []
        stage: taskflow.StageRun | None = None
        gate: Gate | None = None
        try:
            self._run_env[run_id] = {
                **os.environ,
                **await asyncio.to_thread(
                    secrets.resolve, self.paths, job.secrets, key_file=config.secret_key
                ),
            }
            gate = await self._gate(job, run_id)
            if gate.outcome == "error":
                result = RunResult(
                    "gate_error", run_id, error=gate.diagnostic, exit_code=gate.exit_code
                )
            elif gate.outcome == "no_work":
                result = RunResult("no_work", run_id, exit_code=1)
            else:
                resource_locks, collision = await self._acquire_resources(job, run_id, config)
                if not collision:
                    collision = await asyncio.to_thread(self._admission_problem, job, config)
                if collision:
                    log.info(collision)
                    result = RunResult("skipped", run_id, error=collision)
                else:
                    stage, early = await self._claim(job, run_id, gate.output, config=config)
                    if early is not None:
                        result = early
                    else:
                        result = await self._turns(
                            job, run_id, gate.output, effort, started, stage, config=config
                        )
            if job.postrun and result.status in ("no_work", "gate_error", "skipped"):
                hook = await self._postrun(
                    job, result, int((time.monotonic() - started) * 1000), attempt=0
                )
                checked, diagnostic = self._checked_result(job, result, hook, attempt=0)
                await self._record_attempt(result, 0, hook=replace(hook, error=diagnostic))
                result = checked
            if stage is not None:
                result = replace(result, task=stage.task.ref)
                await self._settle(stage, run_id, result.status, config=config, error=result.error)
                await workflows.drain_events(self.paths, config, ref=stage.task.ref)
                stage = taskflow.end_ownership(stage)
            await execution.run_sync(self._finish, result, config)
        except secrets.SecretError as exc:
            result = RunResult("error", run_id, error=str(exc))
            await execution.run_sync(self._finish, result, config)
            log.warning("run=%s could not resolve secrets: %s", run_id, exc)
        except BaseException as exc:
            # A stop (cancellation) or a bug must not leave the row ``running`` forever:
            # runs.prune keeps such rows, so nothing else would ever close it.
            cancelled = isinstance(exc, asyncio.CancelledError)
            error = "cancelled (enso stopped)" if cancelled else f"{type(exc).__name__}: {exc}"
            task = stage.task.ref if stage is not None else None

            async def close_interrupted() -> None:
                if stage is not None:
                    await self._settle(
                        stage,
                        run_id,
                        "cancelled" if cancelled else "error",
                        config=config,
                        error=error,
                    )
                await asyncio.to_thread(
                    self._finish, RunResult("error", run_id, error=error, task=task), config
                )

            await execution.finish_cleanup(close_interrupted())
            log.warning("run=%s did not finish: %s", run_id, error)
            raise
        finally:
            self._run_env.pop(run_id, None)
            if stage is not None:
                taskflow.end_ownership(stage)
            for lock in resource_locks:
                lock.close()
        if stage is not None:
            await self._finish_stage(job, stage, config)
        log.info(
            "finish status=%s exit=%s output_len=%d",
            result.status,
            result.exit_code,
            len(result.output),
        )
        await self._alert(job, gate, result, trigger=trigger, config=config)
        return result

    async def _finish_stage(self, job: Job, stage: taskflow.StageRun, config: Config) -> None:
        await asyncio.to_thread(tasks.settle_dependencies, self.paths, config)
        if job.project is not None:
            settled = await asyncio.to_thread(tasks.get, self.paths, stage.task.ref)
            if settled.finished:
                await self._sweep(job.project, config)

    async def _acquire_resources(
        self, job: Job, run_id: str, config: Config
    ) -> tuple[list[IO[str]], str]:
        """Admit one group member, then reserve project capacity without partial holds."""
        held: list[IO[str]] = []
        try:
            admission = await concurrency.acquire(self.paths, job, run_id)
            if admission.reason != "acquired":
                why = {
                    "busy": "is already running",
                    "expired": "maximum wait expired",
                    "paused": "Enso is paused for maintenance",
                }[admission.reason]
                return [], f"concurrency group {job.group!r}: {why}"
            if admission.lock is not None:
                held.append(admission.lock)
            if job.project is not None:
                slot = await self._lock(
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
        self, job: Job, run_id: str, gate_output: str, *, config: Config
    ) -> tuple[taskflow.StageRun | None, RunResult | None]:
        """On a stage job, take and frame a task; a result here means no provider turn runs."""
        if job.stage is None:
            return None, None
        stage = await taskflow.begin(self.paths, config, job, run_id, gate_output)
        if stage is None:
            log.info("no task is ready in %s/%s", job.project, job.stage)
            return None, RunResult("no_work", run_id)
        if stage.prompt is None:  # preparation settled or deferred; no provider may start
            return stage, RunResult(
                "no_work" if stage.deferred else "error", run_id, error=stage.error
            )
        self._run_env[run_id].update(stage.env)
        return stage, None

    async def _turns(
        self,
        job: Job,
        run_id: str,
        gate_output: str,
        effort: str | None,
        started: float,
        stage: taskflow.StageRun | None,
        *,
        config: Config,
    ) -> RunResult:
        """One executor/check loop for ordinary jobs and workflow stages."""
        initial = (
            stage.prompt
            if stage is not None
            else job.prompt.replace("{{gate_output}}", gate_output)
        )
        assert initial is not None
        prompt = initial
        remaining = float(job.timeout)
        session_id = None
        attempt = 1
        followups = 0
        while True:
            turn_started = time.monotonic()
            result = await self._invoke(
                job, run_id, prompt, effort, remaining, session_id, stage, config
            )
            elapsed = time.monotonic() - turn_started
            remaining = max(0.0, remaining - elapsed)
            result, feedback = await self._check_attempt(
                job, result, attempt, int(elapsed * 1000), started, followups, remaining
            )
            if result.status != "ok":
                return result
            if feedback is not None:
                followups += 1
            elif stage is not None:
                evaluated = await workflows.evaluate(
                    self.paths, config, stage.task.ref, run_id, self._env(job, run_id)
                )
                if evaluated.status == "accepted":
                    return result
                if evaluated.status != "repair":
                    return replace(result, status="error", error=evaluated.feedback)
                feedback = (
                    initial
                    + "\n\n[Enso verification feedback]\n"
                    + evaluated.feedback
                    + "\nRepair the candidate and submit a new handoff, then stop."
                )
            if feedback is None:
                return result
            if job.agent is None or not result.session_id:
                return replace(
                    result,
                    status="error",
                    error="cannot repair: the provider returned no resumable session id",
                )
            if remaining <= 0:
                return replace(result, status="timeout", error="provider time budget exhausted")
            prompt, session_id = feedback, result.session_id
            attempt += 1

    async def _invoke(
        self,
        job: Job,
        run_id: str,
        prompt: str,
        effort: str | None,
        timeout: float,
        session_id: str | None,
        stage: taskflow.StageRun | None,
        config: Config,
    ) -> RunResult:
        """Execute work; the caller owns postrun and workflow acceptance for every outcome."""
        agent = job.agent
        if agent is None:
            return await self._command(job, run_id, stage, config)
        assert effort is not None
        provider = make_provider(agent.provider, config.providers[agent.provider].path)
        args = config.provider_args(job.workspace, agent.provider)
        cwd, env = self.paths.workspace(job.workspace), self._env(job, run_id)
        if job.postrun is not None or stage is not None:
            turn = await execution.execute_turn(
                provider,
                prompt,
                agent.model,
                effort,
                args,
                cwd=cwd,
                env=env,
                timeout=timeout,
                session_id=session_id,
            )
        else:
            turn = await execution.execute_batch(
                provider,
                prompt,
                agent.model,
                effort,
                args,
                cwd=cwd,
                env=env,
                timeout=timeout,
                label=f"job {job.ref}",
            )
        error = turn.error
        if turn.status == "timeout":
            error = f"timed out after {job.timeout}s of provider runtime"
        return RunResult(
            turn.status,
            run_id,
            output=turn.output,
            error=error,
            exit_code=turn.exit_code,
            session_id=turn.session_id,
        )

    async def _command(
        self, job: Job, run_id: str, stage: taskflow.StageRun | None, config: Config
    ) -> RunResult:
        """Commands share execution and checks; integration submits without a subprocess."""
        command, cwd = job.command, job.job_dir
        if stage is not None:
            assert job.project is not None and job.stage is not None
            definition = config.projects[job.project].stage(job.stage)
            assert definition is not None
            command = definition.command
            cwd = project_directory(self.paths, job.workspace, job.project)
        output = ""
        rc: int | None = 0
        if command is not None:
            try:
                output, stderr, rc, timed_out = await execution.run_process(
                    ["bash", "-c", command],
                    cwd=cwd,
                    env=self._env(job, run_id),
                    timeout=job.timeout,
                    merge_stderr=False,
                    label=f"command {job.ref}",
                )
            except OSError as exc:
                return RunResult("error", run_id, error=f"could not start command: {exc}")
            if timed_out or rc != 0:
                error = (
                    f"command timed out after {job.timeout}s"
                    if timed_out
                    else enso_error(stderr)
                    or stderr.strip()[-2000:]
                    or f"command exited with status {rc}"
                )
                return RunResult(
                    "timeout" if timed_out else "error",
                    run_id,
                    output=output,
                    error=error,
                    exit_code=rc,
                )
        if stage is not None:
            await execution.run_sync(
                tasks.move,
                self.paths,
                config,
                stage.task.ref,
                "advance",
                actor=tasks.ENSO_ACTOR,
                run_id=run_id,
                message="Stage command completed" if command else "Integration requested",
            )
        return RunResult("ok", run_id, output=output, exit_code=rc)

    async def _check_attempt(
        self,
        job: Job,
        result: RunResult,
        attempt: int,
        duration_ms: int,
        started: float,
        followups: int,
        remaining: float,
    ) -> tuple[RunResult, str | None]:
        """Persist work before checking, retaining the execution outcome when a hook fails."""
        await self._record_attempt(result, attempt, duration_ms=duration_ms)
        if job.postrun is None:
            return result, None
        hook = await self._postrun(
            job,
            result,
            int((time.monotonic() - started) * 1000),
            attempt=attempt,
            followups_used=followups,
        )
        checked, diagnostic = self._checked_result(
            job, result, hook, attempt=attempt, followups_used=followups
        )
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
        return checked, hook.output if hook.exit_code == 10 and not diagnostic else None

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
            **self._run_env[run_id],
            "ENSO_JOB": job.ref,
            "ENSO_RUN_ID": run_id,
            "ENSO_WORKSPACE": job.workspace,
            "ENSO_HOME": str(self.paths.home),
        }

    async def _gate(self, job: Job, run_id: str) -> Gate:
        """exit 0 → run with stdout; 1 → no work; anything else, a timeout, or no script → error."""
        if job.gate is None:
            return Gate("open")
        log.info("gate timeout=%ss", job.gate.timeout)
        try:
            stdout, stderr, rc, timed_out = await execution.run_process(
                ["bash", "-c", job.gate.command],
                cwd=job.job_dir,
                env=self._env(job, run_id),
                timeout=job.gate.timeout,
                merge_stderr=False,
                label=f"gate {job.ref}",
            )
        except OSError as exc:
            return Gate("error", diagnostic=f"could not start gate: {exc}")
        if timed_out:
            diagnostic = f"gate timed out after {job.gate.timeout}s"
            log.warning(diagnostic)
            return Gate("error", diagnostic=diagnostic, exit_code=rc)
        if rc == 0:
            log.info("gate open output_len=%d", len(stdout))
            return Gate("open", output=stdout.strip(), exit_code=0)
        if rc == 1:
            log.debug("gate closed: no work")
            return Gate("no_work", exit_code=1)
        # Only an explicit ENSO_ERROR line reaches the alert: stdout and the rest of
        # stderr may hold whatever the script scraped.
        diagnostic = enso_error(stderr) or f"gate exited with status {rc}"
        log.warning("gate failed: %s", diagnostic)
        return Gate("error", diagnostic=diagnostic, exit_code=rc)

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
        self,
        job: Job,
        result: RunResult,
        hook: Postrun,
        *,
        attempt: int,
        followups_used: int | None = None,
    ) -> tuple[RunResult, str]:
        """Make hook failures authoritative without disguising an earlier provider failure."""
        followups_used = max(0, attempt - 1) if followups_used is None else followups_used
        diagnostic = hook.error
        if not diagnostic and hook.exit_code == 10:
            if attempt == 0:
                diagnostic = "postrun requested a follow-up, but no provider ran"
            elif job.agent is None:
                diagnostic = "postrun cannot request an agent follow-up for a command job"
            elif result.status != "ok":
                diagnostic = f"postrun cannot request a follow-up after provider {result.status}"
            elif followups_used >= job.agent.max_followups:
                diagnostic = (
                    f"postrun requested a follow-up beyond max_followups={job.agent.max_followups}"
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

    async def _postrun(
        self,
        job: Job,
        result: RunResult,
        duration_ms: int,
        *,
        attempt: int,
        followups_used: int | None = None,
    ) -> Postrun:
        """Hand the latest outcome to a hook; exit 10 requests a complete feedback message."""
        assert job.postrun is not None
        assert result.run_id is not None
        followups_used = max(0, attempt - 1) if followups_used is None else followups_used
        env = {
            **self._env(job, result.run_id),
            "ENSO_RUN_STATUS": result.status,
            "ENSO_RUN_EXIT_CODE": "" if result.exit_code is None else str(result.exit_code),
            "ENSO_RUN_DURATION_MS": str(duration_ms),
            "ENSO_RUN_ATTEMPT": str(attempt),
            "ENSO_RUN_FOLLOWUPS_REMAINING": str(
                max(0, (job.agent.max_followups if job.agent else 0) - followups_used)
            ),
        }
        log.info("postrun timeout=%ss", job.postrun.timeout)
        try:
            stdout, stderr, rc, timed_out = await execution.run_process(
                ["bash", "-c", job.postrun.command],
                cwd=job.job_dir,
                env=env,
                timeout=job.postrun.timeout,
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
            return Postrun(rc, stdout, f"postrun timed out after {job.postrun.timeout}s")
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

    async def _alert(
        self, job: Job, gate: Gate | None, result: RunResult, *, trigger: str, config: Config
    ) -> None:
        if trigger not in ("schedule", "ready"):
            return
        if gate is None:
            # Secrets never resolved, so nothing started. A waiting stage task retries this
            # every tick; alert like a failing gate rather than once per attempt.
            await self._alert_once(job, "secrets", result.error, result.error, config=config)
            return
        if result.status == "gate_error":
            diagnostic = gate.diagnostic
            if result.postrun_error:
                diagnostic += f"\nPostrun: {result.postrun_error}"
            await self._alert_once(
                job, "gate", diagnostic, f"{gate.exit_code}\0{diagnostic}", config=config
            )
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

    async def _alert_once(
        self,
        job: Job,
        kind: Literal["gate", "secrets"],
        diagnostic: str,
        identity: str,
        *,
        config: Config,
    ) -> None:
        """Alert once per distinct failure before the agent, again after a day of the same one."""
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        if kind == "secrets":
            fingerprint = f"secrets:{fingerprint}"  # names the recovery; gate stays bare hex
        what = "gate failed" if kind == "gate" else "secrets unavailable"
        state = await asyncio.to_thread(db.job_state, self.paths, job.ref)
        alerted = _parse_stamp(state.failure_alerted_at)
        if (
            state.failure_fingerprint == fingerprint
            and alerted is not None
            and datetime.now(alerted.tzinfo) - alerted < timedelta(seconds=FAILURE_RENOTIFY_SECONDS)
        ):
            log.info("same %s failure already alerted; suppressed", kind)
            return
        if await self._send(job, f"⚠️ [{job.ref}] {what}", diagnostic, config=config):
            await asyncio.to_thread(db.set_failure, self.paths, job.ref, fingerprint, db.now())

    async def _recovered(self, job: Job, *, config: Config) -> None:
        """One notice when secrets resolve and the gate works again after an alerted failure."""
        state = await asyncio.to_thread(db.job_state, self.paths, job.ref)
        if state.failure_fingerprint is None:
            return
        kind = "secrets" if state.failure_fingerprint.startswith("secrets:") else "gate"
        if await self._send(job, f"✅ [{job.ref}] {kind} recovered", config=config):
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
