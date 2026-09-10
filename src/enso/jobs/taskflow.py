"""Stage job runs: claim a task, prepare its worktree, frame the prompt, release at the end.

The runner calls ``begin`` after the prerun and the group lock, runs the provider exactly as
it does for any other job with the prompt and environment ``begin`` hands back, and calls
``end`` however the run stopped. The task's own rules stay in ``enso.tasks``: this module
only decides what Enso itself does around a run, which is to claim, to frame, and to let go
of a claim the agent never handed off. See ``docs/tasks.md`` and ``docs/concepts.md``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import tasks, worktrees
from ..config import Config, Paths
from . import Job

log = logging.getLogger(__name__)

INSTRUCTIONS_LIMIT = 64 * 1024
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
TWO_STRIKES = "Two runs ended without a handoff; needs a look"


@dataclass(frozen=True)
class StageRun:
    """One claimed task and everything the provider turn needs to work on it."""

    task: tasks.Task
    prompt: str | None = None  # None when preparation failed; ``error`` says why
    env: dict[str, str] = field(default_factory=dict)
    error: str = ""


def actor(job: Job) -> str:
    return f"job:{job.dir_name}"


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
    env = {"ENSO_TASK": task.ref}
    info = None
    instructions = None
    recovery = None
    if project.repo is not None:
        info = worktrees.prepare(paths, project, task.ref)
        env["ENSO_TASK_DIR"] = str(info.path)
        instructions = _instructions(project.repo)
        if info.dirty:
            recovery = "uncommitted changes in " + ", ".join(info.dirty)
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

    A worktree that cannot be prepared releases the claim at once with the fault as the
    message, so the timeline says why the run failed and the two-strikes rule still applies.
    """
    assert job.project is not None and job.stage is not None
    task = await asyncio.to_thread(
        tasks.take, paths, config, job.project, job.stage, run_id=run_id, actor=actor(job)
    )
    if task is None:
        return None
    log.info("run=%s took %s (%s)", run_id, task.ref, job.stage)
    try:
        return await asyncio.to_thread(_prepare, paths, config, job, task, run_id, prerun_output)
    except (worktrees.WorktreeError, tasks.TaskError, OSError) as exc:
        message = f"could not prepare {task.ref}: {exc}"
        log.warning("run=%s %s", run_id, message)
        await asyncio.to_thread(_release, paths, config, task.ref, run_id, message)
        return StageRun(task, error=message)
    except BaseException as exc:
        # A stop during a long setup, or a fault nobody expected: the claim must not outlive
        # the run, or the task silently never becomes ready again. The runner never sees a
        # StageRun for this claim, so it cannot settle it; release here and re-raise.
        cancelled = isinstance(exc, asyncio.CancelledError)
        status = "cancelled" if cancelled else f"error: {type(exc).__name__}: {exc}"
        message = f"run {run_id} ended ({status}) while preparing {task.ref}"
        log.warning("run=%s %s", run_id, message)
        try:
            await asyncio.shield(
                asyncio.to_thread(_release, paths, config, task.ref, run_id, message)
            )
        except tasks.TaskError as release_exc:
            log.warning("run=%s could not release %s: %s", run_id, task.ref, release_exc)
        raise


def _release(paths: Paths, config: Config, ref: str, run_id: str, message: str) -> tasks.Task:
    """Let go of a claim the run still holds; two such releases in a row block the task."""
    released = tasks.release(
        paths, ref, actor=tasks.ENSO_ACTOR, run_id=run_id, message=message, reason="run_ended"
    )
    previous = None
    for event in tasks.events(paths, ref)[1:]:  # [0] is the release just written
        if event.run_id == run_id:
            continue  # this run's own claim, refs, and notes are not a person's look
        previous = event
        break
    if (
        previous is not None
        and previous.kind == "released"
        and previous.payload.get("reason") == "run_ended"
    ):
        log.warning("%s: %s", ref, TWO_STRIKES)
        # Enso's own move, like an automatic resume: the claim is already gone, and a run
        # may only move the task it holds, so this is not done in the run's name.
        return tasks.move(
            paths,
            config,
            ref,
            "block",
            actor=tasks.ENSO_ACTOR,
            run_id=None,
            message=TWO_STRIKES,
            attention=True,
        )
    return released


def _end(paths: Paths, config: Config, ref: str, run_id: str, status: str) -> tasks.Task:
    task = tasks.get(paths, ref)
    if task.claim_run_id != run_id:
        return task  # the agent handed off (or a person forced past the claim)
    return _release(paths, config, ref, run_id, f"run {run_id} ended ({status}) without a handoff")


async def end(
    paths: Paths, config: Config, stage: StageRun, run_id: str, status: str
) -> tasks.Task:
    """Release the claim when the run still holds it; the task as it stands afterwards."""
    return await asyncio.to_thread(_end, paths, config, stage.task.ref, run_id, status)


def release_orphans(paths: Paths, config: Config, run_id: str, message: str) -> list[str]:
    """Let go of every claim a run that never finished still holds; the refs released.

    The runner's recovery closes the run rows an earlier Enso left ``running``; the claims
    those runs took would otherwise point at a dead run forever, and a claimed task is never
    ready. The two-strikes rule applies as for any run that ended without a handoff.
    """
    released = []
    for task in tasks.claimed_by(paths, run_id):
        try:
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
