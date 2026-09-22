"""Durable task worktrees, bounded preparation, and serialized Git integration.

The database records ownership and the selected target when a worktree is first used.
Changing project configuration never silently moves or retargets existing work. Worktrees
isolate files; they are not a security sandbox. See ``docs/tasks.md``.
"""

from __future__ import annotations

import contextlib
import ctypes
import fcntl
import hashlib
import json
import logging
import os
import re
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import db, locks, tasks
from .config import Paths, ProjectConfig, project_directory
from .execution import kill_process_group

log = logging.getLogger(__name__)
GIT_TIMEOUT = 120
OUTPUT_KEEP = 64 * 1024
BRANCH_PREFIX = "enso/"
_REF_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}-[0-9]{3,9}")


class WorktreeError(Exception):
    """A refused or failed worktree operation, with a user-facing explanation."""


class WorktreeBusyError(WorktreeError):
    """Another execution or lifecycle hook still owns this task's worktree."""


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str
    base: str
    created: bool
    dirty: tuple[str, ...]


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    keep: int = OUTPUT_KEEP,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Bound execution/output and clean up the command's owned process group."""
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    try:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=environment,
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
    try:
        with process.stdout as stream:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
                    raise WorktreeError(f"{cmd[0]} timed out after {timeout}s")
                chunk = os.read(stream.fileno(), 64 * 1024)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 2 * keep:
                    del buffer[:-keep]
        try:
            rc = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise WorktreeError(f"{cmd[0]} timed out after {timeout}s") from None
        return rc, bytes(buffer[-keep:]).decode(errors="replace")
    finally:
        # A script may have closed its pipes before starting a background writer.
        kill_process_group(process)


def _git(args: list[str], *, cwd: Path, check: bool = True) -> tuple[int, str]:
    rc, output = _run(["git", *args], cwd=cwd, timeout=GIT_TIMEOUT)
    if check and rc != 0:
        raise WorktreeError(f"git {args[0]} failed (exit {rc}): {output.strip()}")
    return rc, output


def _check_ref(ref: str) -> str:
    if not _REF_RE.fullmatch(ref):
        raise WorktreeError(f"{ref!r} is not a task reference")
    return ref


def _repo(project: ProjectConfig) -> Path:
    if project.repo is None:
        raise WorktreeError(f"project {project.key} has no repository; tasks get no worktree")
    return project.repo.resolve()


def lookup(paths: Paths, ref: str) -> dict[str, Any] | None:
    """Read the recorded identity, including retained and removed worktrees."""
    _check_ref(ref)
    try:
        with db.reader(paths) as con:
            row = con.execute("SELECT * FROM _enso_worktrees WHERE ref = ?", (ref,)).fetchone()
    except db.MissingDatabaseError:
        return None
    return dict(row) if row else None


def _root(project: ProjectConfig) -> Path:
    repo = _repo(project)
    selected = Path(project.worktree_root or ".worktrees").expanduser()
    root = (selected if selected.is_absolute() else repo / selected).resolve()
    _, common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo)
    if root.is_relative_to(Path(common.strip()).resolve()):
        raise WorktreeError("worktree_root must be outside Git's administrative directory")
    return root


def worktree_path(paths: Paths, project: ProjectConfig, ref: str) -> Path:
    record = lookup(paths, ref)
    if record is not None:
        return Path(record["path"])
    # Computing a display path needs no Git process; prepare validates the location.
    root = Path(project.worktree_root or ".worktrees").expanduser()
    return (root if root.is_absolute() else _repo(project) / root) / ref


def branch_name(ref: str) -> str:
    return BRANCH_PREFIX + _check_ref(ref)


def base_branch(repo: Path) -> str:
    rc, output = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=repo, check=False)
    if rc != 0 or not output.strip():
        raise WorktreeError(f"main checkout {repo} is detached; configure a base branch first")
    return output.strip()


def _registrations(repo: Path) -> dict[Path, str]:
    _, output = _git(["worktree", "list", "--porcelain", "-z"], cwd=repo)
    found: dict[Path, str] = {}
    for block in output.split("\0\0"):
        path = None
        branch = ""
        for item in block.split("\0"):
            if item.startswith("worktree "):
                path = Path(item[9:]).resolve()
            elif item.startswith("branch refs/heads/"):
                branch = item[len("branch refs/heads/") :]
        if path is not None:
            found[path] = branch
    return found


def _registered(repo: Path) -> set[Path]:
    return set(_registrations(repo))


def _porcelain_files(output: str) -> tuple[str, ...]:
    # -z emits destination first, then source for renamed entries.
    result = []
    entries = iter(output.split("\0"))
    for entry in entries:
        if len(entry) > 3:
            result.append(entry[3:])
            if entry[0] in "RC" or entry[1] in "RC":
                next(entries, None)
    return tuple(result)


def _dirty(cwd: Path) -> tuple[str, ...]:
    _, output = _git(["status", "--porcelain", "-z", "--untracked-files=no"], cwd=cwd)
    return _porcelain_files(output)


def _unclean(cwd: Path) -> tuple[str, ...]:
    _, output = _git(["status", "--porcelain", "-z"], cwd=cwd)
    return _porcelain_files(output)


def _branch_exists(repo: Path, branch: str) -> bool:
    rc, _ = _git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False)
    return rc == 0


def _revision(repo: Path, ref: str) -> str:
    return _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo)[1].strip()


def _event(
    paths: Paths,
    ref: str,
    kind: str,
    message: str,
    *,
    payload: dict[str, Any] | None = None,
    attention: bool = False,
) -> None:
    with db.transaction(paths) as con:
        row = con.execute("SELECT id FROM _enso_tasks WHERE ref = ?", (ref,)).fetchone()
        if row is None:
            return
        con.execute(
            "INSERT INTO _enso_task_events (task_id,kind,actor,message,payload,created_at) "
            "VALUES (?,?,'enso:worktrees',?,?,?)",
            (row["id"], kind, message, json.dumps(payload or {}), db.now()),
        )
        if attention:
            con.execute("UPDATE _enso_tasks SET attention=1 WHERE id=?", (row["id"],))


def _state(paths: Paths, ref: str, status: str, error: str = "") -> None:
    with db.transaction(paths) as con:
        con.execute(
            "UPDATE _enso_worktrees SET status=?,error=?,updated_at=? WHERE ref=?",
            (status, error[-OUTPUT_KEEP:], db.now(), ref),
        )


def _record(
    paths: Paths,
    project: ProjectConfig,
    ref: str,
    repo: Path,
    path: Path,
    branch: str,
    base: str,
    start: str,
    status: str,
) -> dict[str, Any]:
    stamp = db.now()
    with db.transaction(paths) as con:
        con.execute(
            "INSERT INTO _enso_worktrees "
            "(ref,project,repo,path,branch,base,start_revision,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ref, project.key, str(repo), str(path), branch, base, start, status, stamp, stamp),
        )
    result = lookup(paths, ref)
    assert result is not None
    return result


def _verify(record: dict[str, Any]) -> tuple[Path, Path]:
    repo, path = Path(record["repo"]), Path(record["path"])
    if path.is_symlink() or not (path / ".git").is_file():
        raise WorktreeError(f"{record['ref']} has no worktree at {path}")
    if _registrations(repo).get(path.resolve()) != record["branch"]:
        raise WorktreeError(
            f"{path} is not the registered {record['branch']} worktree; left untouched"
        )
    if base_branch(path) != record["branch"]:
        raise WorktreeError(f"{path} is on a different branch; left untouched")
    return repo, path


def dirty_files(paths: Paths, project: ProjectConfig, ref: str) -> tuple[str, ...]:
    if project.repo is None and lookup(paths, ref) is None:
        return ()
    path = worktree_path(paths, project, ref)
    if not (path / ".git").exists():
        return ()
    return _dirty(path)


@contextlib.contextmanager
def execution_context(paths: Paths, ref: str) -> Iterator[None]:
    """The owner holds this from preparation through hooks; sweep uses the same lock."""
    try:
        fd = locks.acquire(paths.lock("worktrees", _check_ref(ref)))
    except BlockingIOError:
        raise WorktreeBusyError(f"{ref} worktree is in use") from None
    except OSError as exc:
        raise WorktreeError(str(exc)) from exc
    try:
        yield
    finally:
        os.close(fd)


def _ignore_location(repo: Path, path: Path) -> None:
    """Keep a configured nested worktree out of the repository without changing tracked files."""
    if not path.is_relative_to(repo):
        return
    _, common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo)
    exclude = Path(common.strip()) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    # Escape Git ignore metacharacters, including spaces at the end of a path component.
    relative = path.relative_to(repo).as_posix()
    pattern = "/" + re.sub(r"([\\*?\[\]#! ])", r"\\\1", relative) + "/"
    previous = exclude.read_text() if exclude.exists() else ""
    if pattern not in previous.splitlines():
        with exclude.open("a") as file:
            file.write(("\n" if previous and not previous.endswith("\n") else "") + pattern + "\n")


def _matches(relative: str, pattern: str) -> bool:
    pattern = pattern.removeprefix("/").rstrip("/")
    path = PurePosixPath(relative)
    return path.full_match(pattern) or any(
        parent.as_posix() != "." and parent.full_match(pattern) for parent in path.parents
    )


def _clone_file(source: Path, target: Path) -> None:
    """Copy-on-write when the filesystem supports it; callers provide the ordinary fallback."""
    if sys.platform == "darwin":
        library = ctypes.CDLL(None, use_errno=True)
        clone = library.clonefile
        clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clone.restype = ctypes.c_int
        if clone(os.fsencode(source), os.fsencode(target), 0) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    elif sys.platform.startswith("linux"):
        try:
            with source.open("rb") as src, target.open("xb") as dst:
                fcntl.ioctl(dst.fileno(), 0x40049409, src.fileno())  # FICLONE
        except OSError:
            target.unlink(missing_ok=True)
            raise
    else:
        raise OSError("copy-on-write is unavailable on this platform")
    shutil.copystat(source, target)


def _copy_patterns(repo: Path, items: tuple[str, ...]) -> tuple[list[str], list[str]]:
    includes = list(items)
    excludes: list[str] = []
    manifest = repo / ".worktreeinclude"
    if manifest.is_file() and not manifest.is_symlink():
        for line in manifest.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                (excludes if line.startswith("!") else includes).append(line.removeprefix("!"))
    for pattern in (*includes, *excludes):
        if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise WorktreeError(f"copy pattern must stay relative to the repository: {pattern!r}")
    return includes, excludes


def _symlink_below(path: Path, root: Path) -> bool:
    return path.is_symlink() or any(
        parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)
    )


@dataclass
class _Copy:
    repo: Path
    worktree: Path
    excludes: list[str]
    excluded_roots: set[Path]
    counts: dict[str, int] = field(default_factory=lambda: {"cloned": 0, "copied": 0, "skipped": 0})
    seen: set[Path] = field(default_factory=set)

    def ignored(self, path: Path) -> bool:
        rc, _ = _git(
            ["check-ignore", "--quiet", "--", path.relative_to(self.repo).as_posix()],
            cwd=self.repo,
            check=False,
        )
        if rc not in (0, 1):
            raise WorktreeError(f"could not check whether {path} is ignored")
        return rc == 0

    def file(self, source: Path, target: Path) -> None:
        # Check before mkdir: a tracked symlink can redirect even directory creation.
        if target.exists() or _symlink_below(target, self.worktree):
            self.counts["skipped"] += 1
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            _clone_file(source, target)
            self.counts["cloned"] += 1
        except OSError:
            shutil.copy2(source, target, follow_symlinks=False)
            self.counts["copied"] += 1

    def copy(self, source: Path, parent_ignored: bool = False) -> None:
        if source in self.seen:
            return
        self.seen.add(source)
        relative = source.relative_to(self.repo).as_posix()
        if (
            _symlink_below(source, self.repo)
            or ".git" in source.relative_to(self.repo).parts
            or any(source.is_relative_to(root) for root in self.excluded_roots)
            or any(_matches(relative, pattern) for pattern in self.excludes)
        ):
            self.counts["skipped"] += 1
            return
        is_ignored = parent_ignored or self.ignored(source)
        if source.is_dir():
            if (source / ".git").exists():
                self.counts["skipped"] += 1
                return
            for child in sorted(source.iterdir()):
                self.copy(child, is_ignored)
        elif source.is_file() and is_ignored:
            self.file(source, self.worktree / source.relative_to(self.repo))


def _copy_into(repo: Path, worktree: Path, items: tuple[str, ...]) -> dict[str, int]:
    """Copy selected ignored paths without following symlinks or overwriting files.

    .worktreeinclude adds root-relative globs and ! exclusions to the configured copy paths.
    Directories are recursive; .git, registered worktrees and the nested worktree root
    are always excluded. Dependencies receive independent copies, never mutable links.
    """
    includes, excludes = _copy_patterns(repo, items)
    excluded_roots = _registered(repo) - {repo}
    excluded_roots.add(repo / ".worktrees")
    excluded_roots.add(worktree.resolve())
    if worktree.parent != repo and worktree.parent.is_relative_to(repo):
        excluded_roots.add(worktree.parent)
    copier = _Copy(repo, worktree, excludes, excluded_roots)
    for pattern in includes:
        for source in sorted(repo.glob(pattern.rstrip("/"))):
            copier.copy(source)
    return copier.counts


def _hook_history(paths: Paths, ref: str, event: str, event_id: str) -> tuple[int, bool]:
    with contextlib.closing(db.read_connect(paths)) as con:
        rows = con.execute(
            "SELECT kind,payload FROM _enso_task_events e JOIN _enso_tasks t ON t.id=e.task_id "
            "WHERE t.ref=? AND e.kind IN (?, 'workflow_reset') ORDER BY e.id",
            (ref, f"worktree_{event}"),
        ).fetchall()
    attempts, completed = 0, False
    for row in rows:
        if row["kind"] == "workflow_reset":
            attempts = 0
            continue
        data = json.loads(row["payload"])
        if data.get("event_id") == event_id:
            attempts += 1
            completed = completed or data.get("status") == "passed"
    return attempts, completed


def _context(paths: Paths, record: dict[str, Any], event: str, event_id: str) -> dict[str, str]:
    try:
        task = tasks.get(paths, record["ref"])
        stage, run_id = task.stage, task.claim_run_id or ""
    except tasks.TaskError:
        stage, run_id = "", ""
    attempts, _ = _hook_history(paths, record["ref"], event, event_id)
    return {
        "ENSO_TASK": record["ref"],
        "ENSO_PROJECT": record["project"],
        "ENSO_REPO": record["repo"],
        "ENSO_PROJECT_REPO": record["repo"],
        "ENSO_TASK_DIR": record["path"],
        "ENSO_WORKTREE": record["path"],
        "ENSO_BRANCH": record["branch"],
        "ENSO_BASE": record["base"],
        "ENSO_FROM_STAGE": stage,
        "ENSO_TO_STAGE": stage,
        "ENSO_RUN_ID": run_id,
        "ENSO_EVENT": event,
        "ENSO_EVENT_ID": event_id,
        "ENSO_ATTEMPT": str(attempts + 1),
        "ENSO_LIFECYCLE": "1",
    }


def _prepare_record(paths: Paths, project: ProjectConfig, ref: str) -> dict[str, Any]:
    if record := lookup(paths, ref):
        if record["project"] != project.key:
            raise WorktreeError(f"{ref} belongs to recorded project {record['project']}")
        return record
    repo = _repo(project)
    path = _root(project) / ref
    if path.is_symlink():
        raise WorktreeError(f"worktree path must not be a symbolic link: {path}")
    path = path.resolve()
    branch = branch_name(ref)
    base = (project.base or base_branch(repo)).removeprefix("refs/heads/")
    if base.startswith("-") or not _branch_exists(repo, base):
        raise WorktreeError(f"base {base!r} is not an existing local branch")
    registrations = _registrations(repo)
    if path in registrations:
        if registrations[path] != branch:
            raise WorktreeError(f"{path} is registered to a different branch; left untouched")
        start = _git(["merge-base", base, branch], cwd=repo)[1].strip()
        record = _record(paths, project, ref, repo, path, branch, base, start, "ready")
        _event(paths, ref, "worktree_adopted", f"Retained existing worktree at {path}")
        return record
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise WorktreeError(f"{path} exists but is not a registered worktree; move it aside")
    if branch in registrations.values():
        raise WorktreeError(f"{branch} is already checked out elsewhere; left untouched")
    start = _revision(repo, f"refs/heads/{base}")
    if _branch_exists(repo, branch):
        start = _git(["merge-base", base, branch], cwd=repo)[1].strip()
    return _record(paths, project, ref, repo, path, branch, base, start, "creating")


def _prepare_checkout(paths: Paths, record: dict[str, Any]) -> bool:
    repo, path = Path(record["repo"]), Path(record["path"])
    if (path / ".git").exists():
        return False
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise WorktreeError(f"{path} exists but is not a registered worktree; left untouched")
    path.parent.mkdir(parents=True, exist_ok=True)
    _ignore_location(repo, path.parent if path.parent != repo else path)
    branch = record["branch"]
    args = (
        ["worktree", "add", str(path), branch]
        if _branch_exists(repo, branch)
        else ["worktree", "add", "-b", branch, str(path), record["start_revision"]]
    )
    try:
        _git(args, cwd=repo)
    except WorktreeError as exc:
        _state(paths, record["ref"], "setup_failed", str(exc))
        raise
    _state(paths, record["ref"], "setup_running")
    return True


def _setup(paths: Paths, project: ProjectConfig, record: dict[str, Any]) -> None:
    repo, path, ref = Path(record["repo"]), Path(record["path"]), record["ref"]
    event_id = f"worktree:{ref}:{record['created_at']}:setup"
    _state(paths, ref, "setup_running")
    try:
        counts = _copy_into(repo, path, project.copy)
        output = ""
        if project.setup:
            rc, output = _run(
                ["bash", "-c", project.setup],
                cwd=project_directory(paths, project.workspace, project.key),
                timeout=project.script_timeout,
                env={
                    "ENSO_HOME": str(paths.home),
                    "ENSO_WORKSPACE": project.workspace,
                    **_context(paths, record, "setup", event_id),
                },
            )
            if rc != 0:
                raise WorktreeError(f"setup failed (exit {rc}) in {path}: {output.strip()[-2000:]}")
        _state(paths, ref, "ready")
        _event(
            paths,
            ref,
            "worktree_setup",
            f"Worktree ready at {path}",
            payload={
                "event_id": event_id,
                "copy": counts,
                "output": output,
                "status": "passed",
            },
        )
        if counts["copied"]:
            log.info(
                "%s: copy-on-write unavailable for %d files; used independent copies",
                ref,
                counts["copied"],
            )
    except (OSError, ValueError, WorktreeError) as exc:
        _state(paths, ref, "setup_failed", str(exc))
        _event(
            paths,
            ref,
            "worktree_setup",
            f"Setup failed; preserved {path}: {exc}",
            payload={"event_id": event_id, "status": "failed"},
            attention=True,
        )
        raise WorktreeError(str(exc)) from exc


def prepare(paths: Paths, project: ProjectConfig, ref: str) -> WorktreeInfo:
    """Create or recover a task's durable worktree; failed setup never deletes its work."""
    _check_ref(ref)
    record = _prepare_record(paths, project, ref)
    created = _prepare_checkout(paths, record)
    _verify(record)
    if created or record["status"] in ("creating", "setup_running", "setup_failed"):
        _setup(paths, project, record)
    path = Path(record["path"])
    return WorktreeInfo(path, record["branch"], record["base"], created, _dirty(path))


@contextlib.contextmanager
def landing_context(paths: Paths, project: ProjectConfig, ref: str) -> Iterator[None]:
    record = lookup(paths, ref)
    repo = Path(record["repo"]) if record else _repo(project)
    _, common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo)
    # The lock lives with Git so two Enso homes still serialize this repository.
    name = hashlib.sha256(str(Path(common.strip()).resolve()).encode()).hexdigest()[:16]
    lock_path = Path(common.strip()) / f"enso-land-{name}.lock"
    try:
        fd = locks.acquire(lock_path)
    except BlockingIOError:
        raise WorktreeError(
            "another task is integrating into this repository; retry later"
        ) from None
    except OSError as exc:
        raise WorktreeError(str(exc)) from exc
    try:
        yield
    finally:
        os.close(fd)


def _landing_record(paths: Paths, project: ProjectConfig, ref: str) -> dict[str, Any]:
    record = lookup(paths, ref)
    if record is None:
        path = worktree_path(paths, project, ref)
        if not (path / ".git").exists():
            raise WorktreeError(f"{ref} has no worktree at {path}")
        prepare(paths, project, ref)
        record = lookup(paths, ref)
    assert record is not None
    _verify(record)
    return record


def _target_clean(record: dict[str, Any]) -> bool:
    repo = Path(record["repo"])
    checked_out = [
        path for path, branch in _registrations(repo).items() if branch == record["base"]
    ]
    if checked_out and checked_out != [repo.resolve()]:
        raise WorktreeError(
            f"target {record['base']} is checked out at {checked_out[0]}; "
            "leave that checkout untouched"
        )
    if checked_out and _unclean(repo):
        raise WorktreeError("main checkout is dirty; block the task until it is clean")
    return bool(checked_out)


def _never_committed(repo: Path, branch: str) -> bool:
    rc, output = _git(["reflog", "show", "--format=%H", branch], cwd=repo, check=False)
    return rc == 0 and len(output.split()) <= 1


def prepare_land(
    paths: Paths,
    project: ProjectConfig,
    ref: str,
    *,
    recovery_candidate: str | None = None,
) -> tuple[str, str]:
    """Rebase onto the pinned target's current revision; return candidate and target SHAs.

    Caller holds landing_context until finish_land, including all checks of this candidate.
    A conflict or timeout is aborted and leaves the task's prior commits available.
    Only the engine supplies recovery_candidate, after validating durable checked intent.
    It permits rechecking an already-integrated candidate against the current target.
    """
    record = _landing_record(paths, project, ref)
    repo, path = Path(record["repo"]), Path(record["path"])
    branch, base = record["branch"], record["base"]
    if dirty := _unclean(path):
        raise WorktreeError(f"worktree {path} has uncommitted changes: {', '.join(dirty)}")
    _target_clean(record)
    target = _revision(repo, f"refs/heads/{base}")
    rc, _ = _git(["merge-base", "--is-ancestor", branch, target], cwd=repo, check=False)
    if rc == 0 and _revision(path, "HEAD") != recovery_candidate:
        why = "was never committed to" if _never_committed(repo, branch) else "is already landed"
        raise WorktreeError(f"{branch} has nothing {base} lacks; it {why}")
    try:
        rc, output = _git(["rebase", target], cwd=path, check=False)
    except WorktreeError as exc:
        _git(["rebase", "--abort"], cwd=path, check=False)
        raise WorktreeError(f"rebasing {branch} onto {base}: {exc}; rebase aborted") from exc
    if rc != 0:
        _, conflicts = _git(["diff", "--name-only", "--diff-filter=U"], cwd=path, check=False)
        _git(["rebase", "--abort"], cwd=path, check=False)
        files = ", ".join(conflicts.split()) or output.strip()[-500:]
        raise WorktreeError(f"rebasing {branch} onto {base} conflicts: {files}")
    return _revision(path, "HEAD"), target


def finish_land(
    paths: Paths, project: ProjectConfig, ref: str, candidate: str, target_sha: str
) -> str:
    """Land exactly the checked candidate while the recorded target remains unchanged."""
    record = _landing_record(paths, project, ref)
    repo, path = Path(record["repo"]), Path(record["path"])
    branch, base = record["branch"], record["base"]
    if _revision(path, "HEAD") != candidate or _revision(repo, f"refs/heads/{branch}") != candidate:
        raise WorktreeError(
            "candidate changed after checks; verify the new candidate before integrating"
        )
    if _revision(repo, f"refs/heads/{base}") != target_sha:
        raise WorktreeError("target changed after checks; rebase and verify again")
    if _unclean(path):
        raise WorktreeError("worktree changed after checks; verify a clean candidate again")
    if _git(["merge-base", "--is-ancestor", target_sha, candidate], cwd=repo, check=False)[0] != 0:
        raise WorktreeError("candidate is not based on the checked target")
    checked_out = _target_clean(record)
    if candidate != target_sha:
        if checked_out:
            _git(["merge", "--ff-only", candidate], cwd=repo)
        else:
            _git(["update-ref", f"refs/heads/{base}", candidate, target_sha], cwd=repo)
    if _revision(repo, f"refs/heads/{base}") != candidate:
        raise WorktreeError(
            "target moved during integration; inspect the repository before retrying"
        )
    _state(paths, ref, "landed")
    return candidate


def land(paths: Paths, project: ProjectConfig, ref: str) -> str:
    """Legacy no-check landing; configured workflow checks use the split acceptance API."""
    with landing_context(paths, project, ref):
        candidate, target = prepare_land(paths, project, ref)
        return finish_land(paths, project, ref, candidate, target)


def _pending(paths: Paths, ref: str) -> bool:
    with contextlib.closing(db.read_connect(paths)) as con:
        return bool(
            con.execute(
                "SELECT 1 FROM _enso_workflow_events WHERE task_ref=? "
                "AND status IN ('pending','running','failed') LIMIT 1",
                (ref,),
            ).fetchone()
        )


def _teardown(paths: Paths, project: ProjectConfig, record: dict[str, Any]) -> None:
    command = project.hooks.get("teardown")
    if not command:
        return
    ref = record["ref"]
    event_id = f"worktree:{ref}:{record['created_at']}:teardown"
    attempts, completed = _hook_history(paths, ref, "teardown", event_id)
    if completed:
        return
    if attempts >= 3:
        raise WorktreeError(
            "teardown retry limit exhausted; review its failure and run enso workflow retry"
        )
    try:
        rc, output = _run(
            ["bash", "-c", command],
            cwd=project_directory(paths, project.workspace, project.key),
            timeout=project.script_timeout,
            env={
                "ENSO_HOME": str(paths.home),
                "ENSO_WORKSPACE": project.workspace,
                **_context(paths, record, "teardown", event_id),
            },
        )
    except (ValueError, WorktreeError) as exc:
        rc, output = -1, str(exc)
    _event(
        paths,
        ref,
        "worktree_teardown",
        f"Teardown {'passed' if rc == 0 else 'failed'}",
        payload={
            "event_id": event_id,
            "status": "passed" if rc == 0 else "failed",
            "exit_code": rc,
            "output": output,
        },
        attention=rc != 0,
    )
    if rc != 0:
        raise WorktreeError(
            f"teardown failed (exit {rc}); preserved {record['path']}: {output.strip()[-2000:]}"
        )


def _finish_removal(paths: Paths, record: dict[str, Any]) -> None:
    repo, path, ref = Path(record["repo"]), Path(record["path"]), record["ref"]
    branch, base = record["branch"], record["base"]
    if path.exists() or path.is_symlink():
        raise WorktreeError(f"cleanup path still exists: {path}")
    registrations = _registrations(repo)
    if path in registrations:
        if registrations[path] != branch:
            raise WorktreeError("cleanup registration changed; left untouched")
        _git(["worktree", "remove", str(path)], cwd=repo)
    if _branch_exists(repo, branch):
        head = _revision(repo, f"refs/heads/{branch}")
        if _git(["merge-base", "--is-ancestor", head, f"refs/heads/{base}"], cwd=repo, check=False)[
            0
        ]:
            raise WorktreeError(f"cleanup retained branch {branch}: not integrated into {base}")
        _git(["update-ref", "-d", f"refs/heads/{branch}", head], cwd=repo)
    _state(paths, ref, "removed")
    _event(
        paths,
        ref,
        "worktree_removed",
        f"Removed clean worktree {path}; ignored local files were removed too",
    )


def _remove_owned(paths: Paths, project: ProjectConfig, record: dict[str, Any]) -> bool:
    ref = record["ref"]
    task = tasks.get(paths, ref, workspace=project.workspace)
    if not task.finished or task.claim_run_id or _pending(paths, ref):
        return False
    path = Path(record["path"])
    if record["status"] == "removing" and not path.exists():
        _finish_removal(paths, record)
        return True
    repo, path = _verify(record)
    if dirty := _unclean(path):
        raise WorktreeError(f"cleanup retained uncommitted files: {', '.join(dirty)}")
    branch, base = record["branch"], record["base"]
    head = _revision(repo, f"refs/heads/{branch}")
    if (
        _git(["merge-base", "--is-ancestor", head, f"refs/heads/{base}"], cwd=repo, check=False)[0]
        != 0
    ):
        raise WorktreeError(
            f"cleanup retained {branch}: not integrated into recorded target {base}"
        )
    _teardown(paths, project, record)
    if _unclean(path):
        raise WorktreeError("cleanup retained files created during teardown")
    _state(paths, ref, "removing")
    record["status"] = "removing"
    _git(["worktree", "remove", str(path)], cwd=repo)
    _finish_removal(paths, record)
    return True


def sweep(paths: Paths, project: ProjectConfig) -> list[str]:
    """Reconcile only owned terminal worktrees; never force away dirty or unmerged data.

    Ignored files are removed with a clean worktree; teardown can archive valuable local
    artifacts first. Dirty, unmerged, failed-hook and actively owned worktrees stay visible.
    """
    try:
        with db.reader(paths) as con:
            records = [
                dict(row)
                for row in con.execute(
                    "SELECT * FROM _enso_worktrees WHERE project=? "
                    "AND status!='removed' ORDER BY ref",
                    (project.key,),
                )
            ]
    except db.MissingDatabaseError:
        return []
    removed = []
    for record in records:
        ref = record["ref"]
        try:
            with execution_context(paths, ref), landing_context(paths, project, ref):
                if _remove_owned(paths, project, record):
                    removed.append(ref)
        except tasks.TaskError:
            continue
        except WorktreeError as exc:
            if str(exc).endswith("worktree is in use") or str(exc).startswith(
                "another task is integrating"
            ):
                continue
            if record["error"] != str(exc):
                status = "removing" if record["status"] == "removing" else "cleanup_failed"
                _state(paths, ref, status, str(exc))
                _event(paths, ref, "worktree_cleanup", str(exc), attention=True)
    return removed
