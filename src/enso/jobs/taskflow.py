"""Claim stage tasks, retain execution ownership, frame work, and settle interruptions.

The workflow engine accepts submissions after execution and checks stop. This module keeps
the task and worktree reserved through preparation and hands ownership back to the runner
for the complete transaction. See ``docs/tasks.md`` and ``docs/concepts.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path

from .. import tasks, workflows, worktrees
from ..config import Config, Paths
from . import Job

log = logging.getLogger(__name__)

INSTRUCTIONS_LIMIT = 64 * 1024
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


@dataclass(frozen=True)
class StageRun:
    """One claimed task and everything the provider turn needs to work on it."""

    task: tasks.Task
    prompt: str | None = None  # None when preparation failed; ``error`` says why
    env: dict[str, str] = field(default_factory=dict)
    error: str = ""
    deferred: bool = False
    ownership: AbstractContextManager[None] | None = field(default=None, compare=False, repr=False)


def actor(job: Job) -> str:
    return f"job:{job.dir_name}"


async def _join_worker[T](worker: asyncio.Task[T]) -> T:
    """During cancellation cleanup, repeated stops must still wait for the real writer."""
    while True:
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            if worker.cancelled():
                raise


async def _reserve(paths: Paths, config: Config, job: Job, run_id: str) -> tasks.Task | None:
    assert job.project is not None and job.stage is not None
    worker = asyncio.create_task(
        asyncio.to_thread(
            tasks.take, paths, config, job.project, job.stage, run_id=run_id, actor=actor(job)
        )
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        task = await _join_worker(worker)
        if task is not None:
            await _join_worker(
                asyncio.create_task(
                    asyncio.to_thread(
                        workflows.interrupt,
                        paths,
                        config,
                        task.ref,
                        run_id,
                        "run cancelled while reserving the task",
                    )
                )
            )
        raise


def _instructions(repo: Path) -> tuple[str, str] | None:
    """The main checkout's ``AGENTS.md``, else ``CLAUDE.md``, verbatim and bounded; None without.

    Read from the main checkout rather than the task's worktree on purpose: the worktree is
    whatever the previous run's agent left there, and a file it edited must not come back
    framed as the project's own instructions for the next run or for the review stage.
    """
    for name in INSTRUCTION_FILES:
        candidate = repo / name
        if candidate.is_file():
            data = candidate.read_bytes()
            text = data[:INSTRUCTIONS_LIMIT].decode("utf-8", errors="replace")
            if len(data) > INSTRUCTIONS_LIMIT:
                text += f"\n[truncated: {name} exceeds {INSTRUCTIONS_LIMIT // 1024} KiB]"
            return str(candidate), text
    return None


def _prepare(
    paths: Paths, config: Config, job: Job, task: tasks.Task, run_id: str, prerun_output: str
) -> StageRun:
    """Worktree, packet, and prompt for a claimed task; raises when the worktree fails."""
    assert job.project is not None
    project = config.projects[job.project]
    definition = project.stage(task.stage)
    env = {"ENSO_TASK": task.ref}
    info = None
    instructions = None
    recovery = None
    if project.repo is not None:
        env["ENSO_PROJECT_REPO"] = str(project.repo)
        instructions = _instructions(project.repo)
        if definition is None or definition.worktree is not False:
            info = worktrees.prepare(paths, project, task.ref)
            env["ENSO_TASK_DIR"] = str(info.path)
            if info.dirty:
                recovery = "uncommitted changes in " + ", ".join(info.dirty)
    workflows.start(paths, config, task.ref, run_id)
    ctx = tasks.context(
        paths, config, task.ref, env={"ENSO_RUN_ID": run_id, "ENSO_JOB": job.dir_name}
    )
    parts = [
        tasks.render_task_block(
            ctx,
            working_dir=str(info.path) if info else None,
            main_checkout=str(project.repo) if info else None,
            branch=info.branch if info else None,
            base=info.base if info else None,
            recovery=recovery,
            project_instructions=instructions[0] if instructions else None,
        )
    ]
    if instructions:
        parts.append(f"[Project instructions — {instructions[0]}]\n{instructions[1]}")
    parts.append(job.prompt.replace("{{prerun_output}}", prerun_output))
    return StageRun(task, "\n\n".join(parts), env)


async def begin(
    paths: Paths, config: Config, job: Job, run_id: str, prerun_output: str
) -> StageRun | None:
    """Claim the readiest task for this run and prepare it; None when nothing waits.

    Preparation faults block the task with their diagnostic; cancellation waits until the
    actual setup worker stops before releasing ownership.
    """
    assert job.project is not None and job.stage is not None
    task = await _reserve(paths, config, job, run_id)
    if task is None:
        return None
    log.info("run=%s took %s (%s)", run_id, task.ref, job.stage)
    ownership = worktrees.execution_context(paths, task.ref)
    preparation: asyncio.Task[StageRun] | None = None
    entered = transferred = False
    try:
        ownership.__enter__()
        entered = True
        preparation = asyncio.create_task(
            asyncio.to_thread(_prepare, paths, config, job, task, run_id, prerun_output)
        )
        prepared = await asyncio.shield(preparation)
        transferred = True
        return replace(prepared, ownership=ownership)
    except worktrees.WorktreeBusyError as exc:
        # Acceptance can release its claim before its final hook/ownership cleanup. The
        # next stage may reserve the task in that gap, but must simply retry admission.
        await asyncio.to_thread(
            tasks.release,
            paths,
            task.ref,
            actor=tasks.ENSO_ACTOR,
            run_id=run_id,
            message=str(exc),
            reason="deferred",
        )
        return StageRun(task, error=str(exc), deferred=True)
    except (worktrees.WorktreeError, tasks.TaskError, OSError) as exc:
        message = f"could not prepare {task.ref}: {exc}"
        log.warning("run=%s %s", run_id, message)
        await asyncio.to_thread(workflows.interrupt, paths, config, task.ref, run_id, message)
        return StageRun(task, error=message)
    except BaseException as exc:
        # A cancelled await does not stop a worker thread. Keep the task reserved until
        # setup really finishes, so another run cannot start writing into the same tree.
        cancelled = isinstance(exc, asyncio.CancelledError)
        if cancelled and preparation is not None:
            with contextlib.suppress(Exception):
                await _join_worker(preparation)
        status = "cancelled" if cancelled else f"error: {type(exc).__name__}: {exc}"
        message = f"run {run_id} ended ({status}) while preparing {task.ref}"
        log.warning("run=%s %s", run_id, message)
        try:
            await asyncio.shield(
                asyncio.to_thread(workflows.interrupt, paths, config, task.ref, run_id, message)
            )
            if tasks.get(paths, task.ref).claim_run_id == run_id:
                await asyncio.shield(
                    asyncio.to_thread(_release, paths, config, task.ref, run_id, message)
                )
        except tasks.TaskError as release_exc:
            log.warning("run=%s could not release %s: %s", run_id, task.ref, release_exc)
        raise
    finally:
        if entered and not transferred:
            ownership.__exit__(None, None, None)


def end_ownership(stage: StageRun) -> StageRun:
    """Release only after setup, all writers, checks, settlement, and lifecycle work stop."""
    if stage.ownership is not None:
        stage.ownership.__exit__(None, None, None)
        return replace(stage, ownership=None)
    return stage


def _release(paths: Paths, config: Config, ref: str, run_id: str, message: str) -> tasks.Task:
    """Release a stale claim whose stage no longer permits workflow settlement."""
    return tasks.release(
        paths, ref, actor=tasks.ENSO_ACTOR, run_id=run_id, message=message, reason="run_ended"
    )


def _end(
    paths: Paths, config: Config, ref: str, run_id: str, status: str, error: str = ""
) -> tasks.Task:
    task = tasks.get(paths, ref)
    if task.claim_run_id != run_id:
        return task  # the agent handed off (or a person forced past the claim)
    reason = f"run {run_id} ended ({status})" + (f": {error}" if error else " without a handoff")
    workflows.interrupt(paths, config, ref, run_id, reason)
    task = tasks.get(paths, ref)
    if task.claim_run_id != run_id:
        return task
    return _release(paths, config, ref, run_id, f"run {run_id} ended ({status}) without a handoff")


async def end(
    paths: Paths, config: Config, stage: StageRun, run_id: str, status: str, error: str = ""
) -> tasks.Task:
    """Block an unaccepted transaction and release its claim after execution stopped."""
    return await asyncio.to_thread(_end, paths, config, stage.task.ref, run_id, status, error)


def release_orphans(paths: Paths, config: Config, run_id: str, message: str) -> list[str]:
    """Let go of every claim a run that never finished still holds; the refs released.

    The runner's recovery closes the run rows an earlier Enso left ``running``; the claims
    those runs took would otherwise point at a dead run forever, and a claimed task is never
    ready. Block interrupted work so restarting the scheduler cannot reset repair budgets.
    """
    released = []
    for task in tasks.claimed_by(paths, run_id):
        try:
            workflows.interrupt(paths, config, task.ref, run_id, message)
            if tasks.get(paths, task.ref).claim_run_id == run_id:
                _release(paths, config, task.ref, run_id, message)
        except tasks.TaskError as exc:
            log.warning("run=%s could not release %s: %s", run_id, task.ref, exc)
            continue
        released.append(task.ref)
    return released


async def sweep(paths: Paths, config: Config, key: str, failures: dict[str, str]) -> list[str]:
    """Remove finished, clean worktrees of a repo project; never raises.

    ``failures`` remembers the last failure per project between calls, so a sweep that fails
    the same way every tick is logged once at warning level rather than once a minute.
    """
    project = config.projects.get(key)
    if project is None or project.repo is None:
        return []
    try:
        removed = await asyncio.to_thread(worktrees.sweep, paths, project)
    except (worktrees.WorktreeError, OSError) as exc:
        message = str(exc)
        if failures.get(key) != message:
            log.warning("sweep %s failed: %s", key, message)
            failures[key] = message
        return []
    failures.pop(key, None)
    if removed:
        log.info("sweep %s removed %s", key, ", ".join(removed))
    return removed
