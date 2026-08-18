"""Safe local Git history for versionable Enso content."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Sequence
from enum import Enum
from pathlib import PurePosixPath
from typing import NoReturn

from . import config
from .fsutil import atomic_write_text


class RepositoryError(RuntimeError):
    """Enso's local repository cannot be used without risking user data."""


class PathDisposition(str, Enum):
    """Whether a path may enter Enso's local content history."""

    VERSIONABLE = "versionable"
    PROTECTED = "protected"
    UNSUPPORTED = "unsupported"


_PROTECTED_COMPONENTS = frozenset(
    {
        ".deleted",
        ".git",
        ".runtime",
        "audits",
        "cache",
        "caches",
        "drafts",
        "logs",
        "policies",
        "runtime",
        "runs",
        "secrets",
        "uploads",
    }
)
_PROTECTED_ROOT_FILES = frozenset(
    {
        ".config.lock",
        "config.json",
        "config.json.lock",
        "messages.json",
        "messages.json.lock",
        "state.json",
        "update.lock",
        "update.json",
        "update.json.lock",
    }
)
_VERSIONABLE_ROOT_FILES = frozenset({".gitignore", "AGENTS.md", "CLAUDE.md"})
_VERSIONABLE_ROOT_DIRS = frozenset({"docs", "skills"})
_WORKSPACE_ROOT_FILES = frozenset({"AGENTS.md", "CLAUDE.md"})
_WORKSPACE_DIRS = frozenset({"knowledge", "skills"})
_DISCOVERY_LINKS = frozenset({(".agents", "skills"), (".claude", "skills")})
_JOB_SUPPORT_FILES = frozenset({"JOB.md", "prerun.py", "prerun.sh"})
_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_DATABASE_SIDECARS = ("-journal", "-shm", "-wal")

_GITIGNORE_START = "# >>> Enso protected paths (managed; do not edit) >>>"
_GITIGNORE_END = "# <<< Enso protected paths (managed; do not edit) <<<"
_PROTECTIVE_GITIGNORE_PATTERNS = (
    "/.config.lock",
    "/config.json",
    "/config.json.lock",
    "/enso.db*",
    "/messages.json",
    "/messages.json.lock",
    "/state.json",
    "/update.lock",
    "/update.json",
    "/update.json.lock",
    "**/.deleted/",
    "**/.runtime/",
    "**/audits/",
    "**/cache/",
    "**/caches/",
    "**/drafts/",
    "**/logs/",
    "**/policies/",
    "**/runtime/",
    "**/runs/",
    "**/secrets/",
    "**/uploads/",
    "**/auth.json",
    "**/.run.lock",
    "**/.env",
    "**/.env.*",
    "**/*.env",
    "**/*.db",
    "**/*.db-journal",
    "**/*.db-shm",
    "**/*.db-wal",
    "**/*.log",
    "**/*.sqlite",
    "**/*.sqlite-journal",
    "**/*.sqlite-shm",
    "**/*.sqlite-wal",
    "**/*.sqlite3",
    "**/*.sqlite3-journal",
    "**/*.sqlite3-shm",
    "**/*.sqlite3-wal",
    "/jobs/*/output/",
    "/jobs/*/tmp/",
)
_PROTECTIVE_GITIGNORE_BLOCK = "\n".join(
    (
        _GITIGNORE_START,
        "# Enso keeps local history for allowlisted, human-authored content only.",
        "# These runtime and potentially sensitive paths must never be snapshotted.",
        *_PROTECTIVE_GITIGNORE_PATTERNS,
        _GITIGNORE_END,
        "",
    )
)

_REPOSITORY_ENV_KEYS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_WORK_TREE",
)
_GIT_TIMEOUT_SECONDS = 30
_FALLBACK_AUTHOR_NAME = "Enso Local History"
_FALLBACK_AUTHOR_EMAIL = "enso@localhost"


def _parts(path: str) -> tuple[str, ...] | None:
    if not isinstance(path, str) or not path or "\0" in path or path.startswith("/"):
        return None
    raw_parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in raw_parts):
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute():
        return None
    return pure.parts


def _is_protected(parts: tuple[str, ...]) -> bool:
    first = parts[0]
    basename = parts[-1]
    if first in _PROTECTED_ROOT_FILES or first.startswith("enso.db"):
        return True
    if any(component in _PROTECTED_COMPONENTS for component in parts):
        return True
    if basename == ".run.lock" or basename == "auth.json":
        return True
    if basename == ".env" or basename.startswith(".env.") or basename.endswith(".env"):
        return True
    if basename.endswith(".log"):
        return True
    if any(
        basename.endswith(suffix)
        or any(basename.endswith(f"{suffix}{sidecar}") for sidecar in _DATABASE_SIDECARS)
        for suffix in _DATABASE_SUFFIXES
    ):
        return True
    return len(parts) >= 3 and parts[0] == "jobs" and parts[2] in {"output", "tmp"}


def classify_content_path(path: str) -> PathDisposition:
    """Classify one Git-style path relative to ``~/.enso``.

    The allowlist is intentionally narrow. Unknown paths are not assumed safe,
    and protected paths win even when nested below an otherwise versionable root.
    """
    parts = _parts(path)
    if parts is None:
        return PathDisposition.UNSUPPORTED
    if _is_protected(parts):
        return PathDisposition.PROTECTED
    if len(parts) == 1 and parts[0] in _VERSIONABLE_ROOT_FILES | _VERSIONABLE_ROOT_DIRS:
        return PathDisposition.VERSIONABLE
    if parts[0] in _VERSIONABLE_ROOT_DIRS:
        return PathDisposition.VERSIONABLE
    if tuple(parts) in _DISCOVERY_LINKS:
        return PathDisposition.VERSIONABLE
    if len(parts) == 3 and parts[0] == "jobs" and parts[2] in _JOB_SUPPORT_FILES:
        return PathDisposition.VERSIONABLE
    if len(parts) >= 3 and parts[0] == "workspaces":
        workspace_parts = parts[2:]
        if len(workspace_parts) == 1 and workspace_parts[0] in (
            _WORKSPACE_ROOT_FILES | _WORKSPACE_DIRS
        ):
            return PathDisposition.VERSIONABLE
        if workspace_parts[0] in _WORKSPACE_DIRS:
            return PathDisposition.VERSIONABLE
        if tuple(workspace_parts) in _DISCOVERY_LINKS:
            return PathDisposition.VERSIONABLE
    return PathDisposition.UNSUPPORTED


def protected_tracked_paths(paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Return sorted already-tracked paths that block automatic snapshots."""
    return tuple(
        sorted(path for path in paths if classify_content_path(path) is PathDisposition.PROTECTED)
    )


def _append_managed_ignore_block(content: str) -> str:
    """Return user rules followed by the authoritative Enso-owned block."""
    lines = content.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == _GITIGNORE_START]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == _GITIGNORE_END]
    if bool(starts) != bool(ends) or len(starts) > 1 or len(ends) > 1:
        raise RepositoryError(".gitignore has ambiguous Enso managed block markers")
    if starts and starts[0] >= ends[0]:
        raise RepositoryError(".gitignore has ambiguous Enso managed block markers")

    unmanaged = content
    if starts:
        unmanaged = "".join(lines[: starts[0]] + lines[ends[0] + 1 :])
    if unmanaged:
        if not unmanaged.endswith(("\n", "\r")):
            unmanaged += "\n"
        if not unmanaged.endswith(("\n\n", "\r\n\r\n")):
            unmanaged += "\n"
    return unmanaged + _PROTECTIVE_GITIGNORE_BLOCK


class EnsoRepository:
    """One exact, local-only Git repository rooted at Enso's config directory."""

    def __init__(self, root: str | None = None) -> None:
        configured_root = config.CONFIG_DIR if root is None else root
        if not isinstance(configured_root, str) or not configured_root:
            raise RepositoryError("repository root must be a non-empty path")
        self.root = os.path.abspath(os.path.expanduser(configured_root))
        if self.root == os.path.abspath(os.sep):
            raise RepositoryError("repository root may not be the filesystem root")

    @property
    def _gitignore_path(self) -> str:
        return os.path.join(self.root, ".gitignore")

    @property
    def _git_entry_path(self) -> str:
        return os.path.join(self.root, ".git")

    def ensure(self) -> None:
        """Create or conservatively repair the exact local Enso repository."""
        self._ensure_physical_root()
        if os.path.lexists(self._git_entry_path):
            self._validate_git_entry()
            self._validate_exact_worktree()
            self._ensure_protective_ignore()
        else:
            outer = self._discovered_worktree_root()
            if outer is None:
                outer = self._outer_git_entry()
            if outer is not None:
                raise RepositoryError(
                    f"{self.root} is inside outer Git repository {outer}; "
                    "refusing to create an ambiguous nested Enso repository"
                )
            # This ordering is a safety boundary: Git must never see a new Enso
            # repository before its sensitive/runtime exclusions are in place.
            self._ensure_protective_ignore()
            self._run_git(
                ["init", "--quiet", "--initial-branch=main", "--template="],
                description="initialize the Enso repository",
            )
            self._validate_git_entry()
            self._validate_exact_worktree()
        self._ensure_local_fallback_identity()

    def validate(self) -> None:
        """Confirm the exact repository and ignore boundary without writing."""
        self._validate_physical_root()
        if not os.path.lexists(self._git_entry_path):
            outer = self._discovered_worktree_root()
            if outer is None:
                outer = self._outer_git_entry()
            if outer is not None:
                raise RepositoryError(
                    f"{self.root} is inside outer Git repository {outer}, not an exact Git root"
                )
            raise RepositoryError(f"{self.root} is missing its required .git entry")
        self._validate_git_entry()
        self._validate_exact_worktree()
        content, _mode = self._read_gitignore()
        if _append_managed_ignore_block(content) != content:
            raise RepositoryError(
                f"{self._gitignore_path} does not contain the current protective .gitignore block"
            )

    def tracked_protected_paths(self) -> tuple[str, ...]:
        """Return every tracked path that blocks automatic Enso snapshots."""
        self.validate()
        result = self._run_git(
            ["ls-files", "--cached", "-z"],
            read_only=True,
            description="inspect tracked Enso paths",
        )
        paths = [os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw]
        return protected_tracked_paths(paths)

    def snapshot(self, paths: Sequence[str], message: str) -> bool:
        """Commit only explicit allowlisted repository-relative or absolute paths.

        ``True`` means a commit was created. A clean request is a successful
        no-op and returns ``False``. Existing staged or tracked-sensitive state
        fails closed rather than being folded into Enso's commit.
        """
        if isinstance(paths, (str, bytes)) or not paths:
            raise RepositoryError("a snapshot requires at least one explicit path")
        if not isinstance(message, str) or not message.strip():
            raise RepositoryError("a snapshot requires a non-empty commit message")

        self.ensure()
        normalized = self._normalize_snapshot_paths(paths)
        protected = self.tracked_protected_paths()
        if protected:
            listed = ", ".join(repr(path) for path in protected)
            raise RepositoryError(
                "automatic snapshots are blocked because protected paths are already "
                f"tracked: {listed}"
            )

        staged = self._run_git(
            ["diff", "--cached", "--quiet", "--exit-code"],
            check=False,
            read_only=True,
            description="inspect the Git staging area",
        )
        if staged.returncode == 1:
            raise RepositoryError(
                "the Git staging area is not clean; unstage existing changes before snapshotting"
            )
        if staged.returncode != 0:
            self._raise_git_failure(staged, "inspect the Git staging area")

        had_head = (
            self._run_git(
                ["rev-parse", "--verify", "--quiet", "HEAD"],
                check=False,
                read_only=True,
                description="inspect repository history",
            ).returncode
            == 0
        )
        if not self._stage_snapshot_paths(normalized, had_head):
            return False

        try:
            self._run_git(
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgSign=false",
                    "--literal-pathspecs",
                    "commit",
                    "--quiet",
                    "--no-verify",
                    "-m",
                    message,
                    "--",
                    *normalized,
                ],
                description="commit the requested Enso snapshot",
            )
        except RepositoryError as exc:
            self._fail_snapshot(exc, normalized, had_head)
        return True

    def _stage_snapshot_paths(self, normalized: list[str], had_head: bool) -> bool:
        """Stage, audit, and diff explicit paths while cleaning every failure."""
        literal_paths = ["--literal-pathspecs", "add", "--all", "--", *normalized]
        try:
            self._run_git(literal_paths, description="stage the requested Enso paths")
        except RepositoryError as exc:
            self._fail_snapshot(exc, normalized, had_head)
        try:
            staged_paths_result = self._run_git(
                ["diff", "--cached", "--name-only", "-z"],
                read_only=True,
                description="verify staged Enso paths",
            )
        except RepositoryError as exc:
            self._fail_snapshot(exc, normalized, had_head)
        staged_paths = [os.fsdecode(raw) for raw in staged_paths_result.stdout.split(b"\0") if raw]
        staged_protected = protected_tracked_paths(staged_paths)
        if staged_protected:
            listed = ", ".join(repr(path) for path in staged_protected)
            self._fail_snapshot(
                RepositoryError(
                    f"protected paths were staged despite the ignore boundary: {listed}"
                ),
                normalized,
                had_head,
            )
        changed = self._run_git(
            [
                "--literal-pathspecs",
                "diff",
                "--cached",
                "--quiet",
                "--exit-code",
                "--",
                *normalized,
            ],
            check=False,
            read_only=True,
            description="inspect the requested snapshot",
        )
        if changed.returncode == 0:
            return False
        if changed.returncode != 1:
            try:
                self._raise_git_failure(changed, "inspect the requested snapshot")
            except RepositoryError as exc:
                self._fail_snapshot(exc, normalized, had_head)
        return True

    def _fail_snapshot(self, error: RepositoryError, paths: list[str], had_head: bool) -> NoReturn:
        cleanup_error = self._unstage_after_failure(paths, had_head)
        if cleanup_error is not None:
            raise RepositoryError(
                f"{error}; additionally, staging cleanup failed: {cleanup_error}"
            ) from error
        raise error

    def _ensure_physical_root(self) -> None:
        if os.path.lexists(self.root):
            self._validate_physical_root()
            return
        parent = os.path.dirname(self.root)
        try:
            parent_stat = os.lstat(parent)
        except OSError as exc:
            raise RepositoryError(
                f"repository parent {parent} must already be a physical directory: {exc}"
            ) from exc
        if not stat.S_ISDIR(parent_stat.st_mode) or os.path.realpath(parent) != parent:
            raise RepositoryError(f"repository parent {parent} must be a physical directory")
        try:
            os.mkdir(self.root, mode=0o700)
        except OSError as exc:
            raise RepositoryError(f"could not create repository root {self.root}: {exc}") from exc
        self._validate_physical_root()

    def _validate_physical_root(self) -> None:
        try:
            root_stat = os.lstat(self.root)
        except OSError as exc:
            raise RepositoryError(
                f"repository root {self.root} must be an existing physical directory: {exc}"
            ) from exc
        if not stat.S_ISDIR(root_stat.st_mode) or os.path.realpath(self.root) != self.root:
            raise RepositoryError(f"repository root {self.root} must be a physical directory")

    def _validate_git_entry(self) -> None:
        try:
            entry_stat = os.lstat(self._git_entry_path)
        except OSError as exc:
            raise RepositoryError(f"could not inspect {self._git_entry_path}: {exc}") from exc
        if not (stat.S_ISDIR(entry_stat.st_mode) or stat.S_ISREG(entry_stat.st_mode)):
            raise RepositoryError(
                f"{self._git_entry_path} must be a directory or regular gitfile, not a symlink"
            )

    def _validate_exact_worktree(self) -> None:
        try:
            inside = self._run_git(
                ["rev-parse", "--is-inside-work-tree"],
                read_only=True,
                description="validate the Enso Git repository",
            )
            top = self._run_git(
                ["rev-parse", "--show-toplevel"],
                read_only=True,
                description="find the Enso Git worktree root",
            )
        except RepositoryError as exc:
            raise RepositoryError(
                f"{self.root} does not contain a valid Git repository: {exc}"
            ) from exc
        if inside.stdout.strip() != b"true":
            raise RepositoryError(f"{self.root} does not contain a valid Git worktree")
        worktree = os.path.realpath(os.fsdecode(top.stdout.rstrip(b"\r\n")))
        if worktree != self.root:
            raise RepositoryError(
                f"Git worktree {worktree} is not the exact worktree root {self.root}"
            )
        if stat.S_ISREG(os.lstat(self._git_entry_path).st_mode):
            self._validate_gitfile_binding()

    def _validate_gitfile_binding(self) -> None:
        """Reject a gitfile that merely reinterprets another checkout's gitdir."""
        gitdir_result = self._run_git(
            ["rev-parse", "--absolute-git-dir"],
            read_only=True,
            description="resolve the Enso gitfile",
        )
        gitdir = os.fsdecode(gitdir_result.stdout.rstrip(b"\r\n"))
        try:
            gitdir_stat = os.lstat(gitdir)
        except OSError as exc:
            raise RepositoryError(
                f"Enso gitfile target is not a valid Git directory: {exc}"
            ) from exc
        if not stat.S_ISDIR(gitdir_stat.st_mode) or os.path.realpath(gitdir) != gitdir:
            raise RepositoryError("Enso gitfile target must be a physical Git directory")

        back_reference = os.path.join(gitdir, "gitdir")
        try:
            with open(back_reference, encoding="utf-8") as file:
                referenced_gitfile = file.read().strip()
        except (OSError, UnicodeError):
            referenced_gitfile = ""
        if referenced_gitfile and not os.path.isabs(referenced_gitfile):
            referenced_gitfile = os.path.join(gitdir, referenced_gitfile)
        if referenced_gitfile and os.path.realpath(referenced_gitfile) == self._git_entry_path:
            return

        configured_worktree = self._run_git(
            ["config", "--local", "--path", "--get", "core.worktree"],
            check=False,
            read_only=True,
            description="inspect the gitfile worktree binding",
        )
        if configured_worktree.returncode == 0:
            configured = os.fsdecode(configured_worktree.stdout.rstrip(b"\r\n"))
            if not os.path.isabs(configured):
                configured = os.path.join(gitdir, configured)
            if os.path.realpath(configured) == self.root:
                return
        raise RepositoryError(
            f"gitfile does not bind its Git directory to the exact worktree root {self.root}"
        )

    def _discovered_worktree_root(self) -> str | None:
        result = self._run_git(
            ["rev-parse", "--show-toplevel"],
            check=False,
            read_only=True,
            description="inspect enclosing Git repositories",
        )
        if result.returncode != 0:
            return None
        return os.path.realpath(os.fsdecode(result.stdout.rstrip(b"\r\n")))

    def _outer_git_entry(self) -> str | None:
        """Find a valid, corrupt, or otherwise ambiguous ancestor .git entry."""
        ancestor = os.path.dirname(self.root)
        while True:
            if os.path.lexists(os.path.join(ancestor, ".git")):
                return ancestor
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                return None
            ancestor = parent

    def _read_gitignore(self) -> tuple[str, int]:
        try:
            ignore_stat = os.lstat(self._gitignore_path)
        except FileNotFoundError as exc:
            raise RepositoryError(f"{self._gitignore_path} must be a regular file") from exc
        except OSError as exc:
            raise RepositoryError(f"could not inspect {self._gitignore_path}: {exc}") from exc
        if not stat.S_ISREG(ignore_stat.st_mode):
            raise RepositoryError(f"{self._gitignore_path} must be a regular file, not a symlink")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._gitignore_path, flags)
            opened_stat = os.fstat(descriptor)
            if (opened_stat.st_dev, opened_stat.st_ino) != (
                ignore_stat.st_dev,
                ignore_stat.st_ino,
            ):
                os.close(descriptor)
                raise RepositoryError(f"{self._gitignore_path} changed while it was opened")
            with os.fdopen(descriptor, encoding="utf-8", newline="") as file:
                content = file.read()
        except RepositoryError:
            raise
        except (OSError, UnicodeError) as exc:
            raise RepositoryError(f"could not safely read {self._gitignore_path}: {exc}") from exc
        return content, stat.S_IMODE(ignore_stat.st_mode)

    def _ensure_protective_ignore(self) -> None:
        if os.path.lexists(self._gitignore_path):
            content, mode = self._read_gitignore()
        else:
            content, mode = "", 0o600
        desired = _append_managed_ignore_block(content)
        if desired == content:
            return
        try:
            atomic_write_text(self._gitignore_path, desired, mode=mode, newline="")
        except OSError as exc:
            raise RepositoryError(
                f"could not write protective .gitignore at {self._gitignore_path}: {exc}"
            ) from exc

    def _ensure_local_fallback_identity(self) -> None:
        identity = self._run_git(
            ["var", "GIT_AUTHOR_IDENT"],
            check=False,
            read_only=True,
            description="inspect the effective Git author",
        )
        if identity.returncode == 0:
            return
        self._run_git(
            ["config", "--local", "user.name", _FALLBACK_AUTHOR_NAME],
            description="set the repository-local fallback author name",
        )
        self._run_git(
            ["config", "--local", "user.email", _FALLBACK_AUTHOR_EMAIL],
            description="set the repository-local fallback author email",
        )
        verified = self._run_git(
            ["var", "GIT_AUTHOR_IDENT"],
            check=False,
            read_only=True,
            description="verify the effective Git author",
        )
        if verified.returncode != 0:
            self._raise_git_failure(verified, "establish a repository-local Git author")

    def _normalize_snapshot_paths(self, paths: Sequence[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for requested in paths:
            if not isinstance(requested, str) or not requested or "\0" in requested:
                raise RepositoryError("snapshot paths must be non-empty strings")
            if not os.path.isabs(requested) and ".." in PurePosixPath(requested).parts:
                raise RepositoryError(
                    f"snapshot path {requested!r} must remain beneath {self.root}"
                )
            candidate = (
                os.path.abspath(requested)
                if os.path.isabs(requested)
                else os.path.abspath(os.path.join(self.root, requested))
            )
            try:
                within = os.path.commonpath((self.root, candidate)) == self.root
            except ValueError:
                within = False
            if not within or candidate == self.root:
                raise RepositoryError(
                    f"snapshot path {requested!r} must remain beneath {self.root}"
                )
            relative = os.path.relpath(candidate, self.root).replace(os.sep, "/")
            disposition = classify_content_path(relative)
            if disposition is PathDisposition.PROTECTED:
                raise RepositoryError(f"snapshot path {relative!r} is protected")
            if disposition is not PathDisposition.VERSIONABLE:
                raise RepositoryError(f"snapshot path {relative!r} is not allowlisted")
            resolved = os.path.realpath(candidate)
            try:
                resolved_within = os.path.commonpath((self.root, resolved)) == self.root
            except ValueError:
                resolved_within = False
            if not resolved_within:
                raise RepositoryError(f"snapshot path {relative!r} is a symlink escape")
            if relative not in seen:
                normalized.append(relative)
                seen.add(relative)
        return normalized

    def _unstage_after_failure(self, paths: list[str], had_head: bool) -> RepositoryError | None:
        args = (
            ["--literal-pathspecs", "reset", "--quiet", "HEAD", "--", *paths]
            if had_head
            else [
                "--literal-pathspecs",
                "rm",
                "--cached",
                "--quiet",
                "--force",
                "-r",
                "--ignore-unmatch",
                "--",
                *paths,
            ]
        )
        try:
            self._run_git(args, description="clean up failed snapshot staging")
        except RepositoryError as exc:
            return exc
        return None

    def _run_git(
        self,
        args: list[str],
        *,
        check: bool = True,
        read_only: bool = False,
        description: str,
    ) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        for key in _REPOSITORY_ENV_KEYS:
            env.pop(key, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        if read_only:
            env["GIT_OPTIONAL_LOCKS"] = "0"
        command = ["git", "-C", self.root, *args]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RepositoryError("Git is required for Enso local history") from exc
        except subprocess.TimeoutExpired as exc:
            raise RepositoryError(f"timed out while attempting to {description}") from exc
        except OSError as exc:
            raise RepositoryError(f"could not {description}: {exc}") from exc
        if check and result.returncode != 0:
            self._raise_git_failure(result, description)
        return result

    @staticmethod
    def _raise_git_failure(result: subprocess.CompletedProcess[bytes], description: str) -> None:
        detail = os.fsdecode(result.stderr).strip() or os.fsdecode(result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RepositoryError(f"could not {description}{suffix}")
