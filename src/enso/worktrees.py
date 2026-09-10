"""Per-task Git worktrees for repo projects: prepare, land, and sweep.

A repo project keeps each task's work in ``worktrees/<KEY>/<REF>`` on a ``enso/<REF>``
branch, based on whatever branch the main checkout has out. The main checkout is never
edited, committed to, or switched here: ``land`` only fast-forwards it, and only after the
task branch has been rebased in its own worktree and the checkout was found clean. Every Git
call is an argument array with an explicit working directory, a timeout, and bounded output;
task references are validated before they become a path segment or a branch name, so they
never reach a shell. See ``docs/tasks.md``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import tasks
from .config import Paths, ProjectConfig

log = logging.getLogger(__name__)

GIT_TIMEOUT = 120
SETUP_TIMEOUT = 600
OUTPUT_KEEP = 64 * 1024
BRANCH_PREFIX = "enso/"
_REF_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}-[0-9]{3,9}")


class WorktreeError(Exception):
    """A worktree operation the module refuses or Git failed; the message is user-facing."""


@dataclass(frozen=True)
class WorktreeInfo:
    """Where a task's work lives, and what ``prepare`` found or made there."""

    path: Path
    branch: str
    base: str
    created: bool
    dirty: tuple[str, ...]  # tracked files with uncommitted changes


# -- Processes ----------------------------------------------------------------


def _run(cmd: list[str], *, cwd: Path, timeout: int, keep: int = OUTPUT_KEEP) -> tuple[int, str]:
    """Run to completion or ``timeout`` seconds, keeping only the output tail, both streams.

    The child gets its own process group so a timeout kills whatever a setup command spawned,
    and ``GIT_TERMINAL_PROMPT=0`` keeps Git from waiting on a prompt nobody will answer.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise WorktreeError(f"could not start {cmd[0]}: {exc}") from exc
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    timed_out = False
    with process.stdout as stream:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([stream], [], [], remaining)
            if not ready:
                timed_out = True
                break
            chunk = os.read(stream.fileno(), 64 * 1024)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 2 * keep:
                del buffer[:-keep]
    if timed_out:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise WorktreeError(f"{cmd[0]} timed out after {timeout}s")
    rc = process.wait()
    return rc, bytes(buffer[-keep:]).decode(errors="replace")


def _git(args: list[str], *, cwd: Path, check: bool = True) -> tuple[int, str]:
    rc, output = _run(["git", *args], cwd=cwd, timeout=GIT_TIMEOUT)
    if check and rc != 0:
        raise WorktreeError(f"git {args[0]} failed (exit {rc}): {output.strip()}")
    return rc, output


# -- Locations ----------------------------------------------------------------


def _check_ref(ref: str) -> str:
    if not _REF_RE.fullmatch(ref):
        raise WorktreeError(f"{ref!r} is not a task reference")
    return ref


def _repo(project: ProjectConfig) -> Path:
    if project.repo is None:
        raise WorktreeError(f"project {project.key} has no repository; tasks get no worktree")
    return project.repo


def worktree_path(paths: Paths, project: ProjectConfig, ref: str) -> Path:
    return paths.worktrees / project.key / _check_ref(ref)


def branch_name(ref: str) -> str:
    return BRANCH_PREFIX + _check_ref(ref)


def base_branch(repo: Path) -> str:
    """The main checkout's current branch; a detached HEAD has no branch to base on."""
    rc, output = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=repo, check=False)
    if rc != 0 or not output.strip():
        raise WorktreeError(f"main checkout {repo} is detached; check out a branch there first")
    return output.strip()


def _registered(repo: Path) -> set[Path]:
    """Every worktree path Git knows about, resolved so symlinked homes still compare."""
    _, output = _git(["worktree", "list", "--porcelain"], cwd=repo)
    found = set()
    for line in output.splitlines():
        if line.startswith("worktree "):
            found.add(Path(line[len("worktree ") :]).resolve())
    return found


def _porcelain_files(output: str) -> tuple[str, ...]:
    files = []
    for line in output.splitlines():
        if len(line) > 3:
            path = line[3:]
            files.append(path.split(" -> ")[-1] if line[0] in "RC" else path)
    return tuple(files)


def _dirty(cwd: Path) -> tuple[str, ...]:
    """Tracked files with uncommitted changes; untracked files do not block a handoff or a land."""
    _, output = _git(["status", "--porcelain", "--untracked-files=no"], cwd=cwd)
    return _porcelain_files(output)


def _unclean(cwd: Path) -> tuple[str, ...]:
    """Every uncommitted file, untracked ones included; ignored files (a copied ``.env``) are not.

    The sweep deletes a worktree, so a new file the agent never ``git add``-ed counts here even
    though it did not stop the task from advancing: the branch never held it, and nothing else
    does.
    """
    _, output = _git(["status", "--porcelain"], cwd=cwd)
    return _porcelain_files(output)


def _branch_exists(repo: Path, branch: str) -> bool:
    rc, _ = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False
    )
    return rc == 0


def dirty_files(paths: Paths, project: ProjectConfig, ref: str) -> tuple[str, ...]:
    """Uncommitted tracked files in the task's worktree; empty when there is no worktree."""
    if project.repo is None:
        return ()
    path = worktree_path(paths, project, ref)
    if not (path / ".git").exists():
        return ()
    return _dirty(path)


# -- Prepare ------------------------------------------------------------------


def _copy_into(repo: Path, worktree: Path, items: tuple[str, ...]) -> None:
    for item in items:
        source = repo / item
        if not source.exists():
            continue
        target = worktree / item
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)


def _remove(repo: Path, path: Path, branch: str | None) -> None:
    """Undo a worktree whose setup failed so the next run creates it afresh."""
    _git(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)
    if branch is not None:
        _git(["branch", "-D", branch], cwd=repo, check=False)


def prepare(paths: Paths, project: ProjectConfig, ref: str) -> WorktreeInfo:
    """The task's worktree, created on first use, with ``copy`` and ``setup`` applied then."""
    repo = _repo(project)
    path = worktree_path(paths, project, ref)
    branch = branch_name(ref)
    base = base_branch(repo)
    _git(["worktree", "prune"], cwd=repo)
    if path.resolve() in _registered(repo) and path.is_dir():
        return WorktreeInfo(path, branch, base, created=False, dirty=_dirty(path))
    if path.exists():
        if any(path.iterdir()):
            raise WorktreeError(f"{path} exists but is not a registered worktree; move it aside")
        path.rmdir()
    path.parent.mkdir(parents=True, exist_ok=True)
    attach = _branch_exists(repo, branch)
    if attach:
        _git(["worktree", "add", str(path), branch], cwd=repo)
    else:
        _git(["worktree", "add", "-b", branch, str(path), base], cwd=repo)
    try:
        _copy_into(repo, path, project.copy)
        if project.setup:
            log.info("%s: running setup in %s", ref, path)
            rc, output = _run(["bash", "-c", project.setup], cwd=path, timeout=SETUP_TIMEOUT)
            if rc != 0:
                tail = output.strip()[-2000:]
                raise WorktreeError(f"setup failed (exit {rc}) in {path}: {tail}")
    except (WorktreeError, OSError) as exc:
        _remove(repo, path, None if attach else branch)
        if isinstance(exc, OSError):
            raise WorktreeError(f"could not prepare {path}: {exc}") from exc
        raise
    return WorktreeInfo(path, branch, base, created=True, dirty=_dirty(path))


# -- Land ---------------------------------------------------------------------


def _never_committed(repo: Path, branch: str) -> bool:
    """Whether the branch's reflog holds nothing but its creation."""
    rc, output = _git(["reflog", "show", "--format=%H", branch], cwd=repo, check=False)
    return rc == 0 and len(output.split()) <= 1


def land(paths: Paths, project: ProjectConfig, ref: str) -> str:
    """Rebase ``enso/<REF>`` onto the base and fast-forward the main checkout; the new HEAD."""
    repo = _repo(project)
    path = worktree_path(paths, project, ref)
    branch = branch_name(ref)
    _git(["worktree", "prune"], cwd=repo)
    if not (path / ".git").exists() or path.resolve() not in _registered(repo):
        raise WorktreeError(f"{ref} has no worktree at {path}")
    dirty = _dirty(path)
    if dirty:
        raise WorktreeError(f"worktree {path} has uncommitted changes: {', '.join(dirty)}")
    base = base_branch(repo)
    rc, _ = _git(["merge-base", "--is-ancestor", branch, base], cwd=repo, check=False)
    if rc == 0:
        why = "was never committed to" if _never_committed(repo, branch) else "is already landed"
        raise WorktreeError(f"{branch} has nothing {base} lacks; it {why}")
    try:
        rc, output = _git(["rebase", base], cwd=path, check=False)
    except WorktreeError as exc:
        # A timed-out rebase (a slow hook, a huge rewrite) must not leave the worktree
        # mid-rebase for the next run to find.
        _git(["rebase", "--abort"], cwd=path, check=False)
        raise WorktreeError(f"rebasing {branch} onto {base}: {exc}; rebase aborted") from exc
    if rc != 0:
        _, conflicts = _git(["diff", "--name-only", "--diff-filter=U"], cwd=path, check=False)
        _git(["rebase", "--abort"], cwd=path, check=False)
        files = ", ".join(conflicts.split()) or output.strip()[-500:]
        raise WorktreeError(f"rebasing {branch} onto {base} conflicts: {files}")
    if _dirty(repo):
        raise WorktreeError("main checkout is dirty; block the task until it is clean")
    _git(["merge", "--ff-only", branch], cwd=repo)
    _, head = _git(["rev-parse", "HEAD"], cwd=repo)
    return head.strip()


# -- Sweep --------------------------------------------------------------------


def sweep(paths: Paths, project: ProjectConfig) -> list[str]:
    """Remove the worktrees of finished, clean tasks; delete their branches once merged.

    Clean means nothing uncommitted at all, untracked files included: a sweep deletes the
    directory, so a file the agent forgot to add would otherwise be gone for good. Git is not
    asked anything while ``worktrees/<KEY>`` is missing or empty, so an idle home costs the
    scheduler nothing per tick.
    """
    repo = _repo(project)
    root = paths.worktrees / project.key
    if not root.is_dir() or not os.listdir(root):
        return []
    _git(["worktree", "prune"], cwd=repo)
    registered = _registered(repo)
    base = base_branch(repo)
    removed = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not _REF_RE.fullmatch(entry.name):
            continue
        try:
            task = tasks.get(paths, entry.name)
        except tasks.TaskError:
            continue  # not one of ours to judge
        if not task.finished:
            continue
        if entry.resolve() in registered:
            if _unclean(entry):
                continue
            # Never --force here: Git's own refusal of a tree that became unclean since the
            # check, or that another sweep already removed, is a skip and not a failure.
            rc, output = _git(["worktree", "remove", str(entry)], cwd=repo, check=False)
            if rc != 0:
                log.info("sweep %s: left %s alone: %s", project.key, entry.name, output.strip())
                continue
        elif any(entry.iterdir()):
            continue  # an unregistered directory with content is not ours to delete
        else:
            entry.rmdir()
        branch = branch_name(entry.name)
        if _branch_exists(repo, branch):
            rc, _ = _git(["merge-base", "--is-ancestor", branch, base], cwd=repo, check=False)
            if rc == 0:
                _git(["branch", "-D", branch], cwd=repo)
        removed.append(entry.name)
    return removed
