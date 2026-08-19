"""Safe local Git history for versionable Enso content."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
        ".snapshot.lock",
        ".snapshot.transaction.json",
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
_STRUCTURAL_IDENTIFIER_EXCEPTIONS = tuple(
    pattern
    for name in sorted(_PROTECTED_COMPONENTS - {".git"})
    for pattern in (
        f"!/jobs/{name}/",
        f"!/skills/{name}/",
        f"!/workspaces/{name}/",
        f"!/workspaces/*/skills/{name}/",
    )
)
_PROTECTIVE_GITIGNORE_PATTERNS = (
    "/.config.lock",
    "/.snapshot.lock",
    "/.snapshot.transaction.json",
    "/.snapshot-transaction-*.tmp",
    "/config.json",
    "/config.json.lock",
    "/enso.db*",
    "/messages.json",
    "/messages.json.lock",
    "/state.json",
    "/update.lock",
    "/update.json",
    "/update.json.lock",
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
    *_STRUCTURAL_IDENTIFIER_EXCEPTIONS,
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
    "GIT_ATTR_NOSYSTEM",
    "GIT_ATTR_SOURCE",
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
_SNAPSHOT_LOCK_NAME = ".snapshot.lock"
_SNAPSHOT_TRANSACTION_NAME = ".snapshot.transaction.json"
_SNAPSHOT_INDEX_RE = re.compile(r"^\.snapshot-index-[0-9a-f]{32}$")
_SNAPSHOT_MARKER_TEMP_RE = re.compile(
    r"^\.snapshot-transaction-[0-9a-f]{32}\.tmp$"
)
_SNAPSHOT_TRANSACTION_VERSION = 1
_MAX_TRANSACTION_BYTES = 16_384


@dataclass(frozen=True)
class _SnapshotTransaction:
    """Durable state spanning the atomic ref and native-index updates."""

    temp_index: str
    old_head: str | None
    old_tree: str
    old_index_sha256: str | None
    new_head: str | None = None
    new_tree: str | None = None
    new_index_sha256: str | None = None


@dataclass(frozen=True)
class _SnapshotIndexEntry:
    """One descriptor-read worktree entry for the alternate Git index."""

    path: str
    mode: str
    oid: str


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
    if (
        first in _PROTECTED_ROOT_FILES
        or first.startswith(".snapshot-transaction-")
        or first.startswith("enso.db")
    ):
        return True
    identifier_indexes: set[int] = set()
    if first in {"jobs", "skills", "workspaces"} and len(parts) > 1:
        identifier_indexes.add(1)
    if first == "workspaces" and len(parts) > 3 and parts[2] == "skills":
        identifier_indexes.add(3)
    if any(
        component in _PROTECTED_COMPONENTS
        and (component == ".git" or index not in identifier_indexes)
        for index, component in enumerate(parts)
    ):
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
        self._active_snapshot_lock_fd: int | None = None
        self._active_snapshot_root_identity: tuple[int, int] | None = None
        self._active_snapshot_root_dir_fd: int | None = None

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
        if os.path.lexists(self._snapshot_transaction_path) or self._has_marker_temp_residue():
            with self._snapshot_lock():
                self.validate()
                self._cleanup_marker_temp_residue()
                self._recover_snapshot_transaction()
                self._raise_if_native_index_locked()

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
        return protected_tracked_paths(self.tracked_paths())

    def has_head(self) -> bool:
        """Return whether this exact worktree currently has a commit at HEAD."""
        self.validate()
        return self._has_head()

    def commit_subject_paths(self, subject: str) -> tuple[str, ...] | None:
        """Return the recursive tree paths for the newest exact-subject ancestor."""
        commit_oid = self._commit_oid_with_subject(subject)
        if commit_oid is None:
            return None
        result = self._run_git(
            ["ls-tree", "-r", "--name-only", "-z", commit_oid],
            read_only=True,
            description="inspect the matching Enso commit tree",
        )
        return tuple(os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw)

    def tracked_paths(self) -> tuple[str, ...]:
        """Return every exact path currently present in the Git index."""
        self.validate()
        return self._tracked_paths()

    def _tracked_paths(
        self,
        *,
        index_file: str | None = None,
    ) -> tuple[str, ...]:
        """Read exact index paths after the caller establishes repository safety."""
        result = self._run_git(
            ["ls-files", "--cached", "-z"],
            read_only=True,
            index_file=index_file,
            worktree_free=True,
            description="inspect tracked Enso paths",
        )
        return tuple(dict.fromkeys(os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw))

    def ignored_paths(self, paths: Sequence[str]) -> tuple[str, ...]:
        """Return requested allowlisted paths ignored by effective Git rules."""
        if isinstance(paths, (str, bytes)):
            raise RepositoryError("ignored-path inspection requires a sequence of paths")
        self.validate()
        normalized = self._normalize_snapshot_paths(paths)
        if not normalized:
            return ()
        input_data = b"".join(os.fsencode(path) + b"\0" for path in normalized)
        result = self._run_git(
            ["check-ignore", "--no-index", "--stdin", "-z"],
            check=False,
            read_only=True,
            input_data=input_data,
            description="inspect effective Git ignore rules",
        )
        if result.returncode not in {0, 1}:
            self._raise_git_failure(result, "inspect effective Git ignore rules")
        ignored = tuple(os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw)
        unexpected = tuple(path for path in ignored if path not in normalized)
        if unexpected:
            listed = ", ".join(repr(path) for path in unexpected)
            raise RepositoryError(
                f"Git returned unexpected paths while inspecting effective ignore rules: {listed}"
            )
        ignored_set = set(ignored)
        return tuple(path for path in normalized if path in ignored_set)

    def _has_head(self) -> bool:
        result = self._run_git(
            ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            check=False,
            read_only=True,
            description="inspect repository history",
        )
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            self._raise_git_failure(result, "inspect repository history")
        symbolic = self._run_git(
            ["symbolic-ref", "--quiet", "HEAD"],
            check=False,
            read_only=True,
            description="inspect unborn repository HEAD",
        )
        if symbolic.returncode == 1:
            raise RepositoryError("repository HEAD is detached but does not resolve to a commit")
        if symbolic.returncode != 0:
            self._raise_git_failure(symbolic, "inspect unborn repository HEAD")
        target = symbolic.stdout.rstrip(b"\r\n")
        if (
            not target.startswith(b"refs/heads/")
            or b"\0" in target
            or b"\n" in target
            or b"\r" in target
        ):
            raise RepositoryError("repository HEAD is not a valid unborn branch reference")
        target_result = self._run_git(
            ["show-ref", "--verify", "--quiet", os.fsdecode(target)],
            check=False,
            read_only=True,
            description="inspect symbolic repository HEAD target",
        )
        if target_result.returncode == 0:
            raise RepositoryError("repository HEAD target does not resolve to a commit")
        if target_result.returncode != 1:
            self._raise_git_failure(target_result, "inspect symbolic repository HEAD target")
        return False

    def _commit_oid_with_subject(self, subject: str) -> str | None:
        if (
            not isinstance(subject, str)
            or not subject
            or "\0" in subject
            or "\n" in subject
            or "\r" in subject
        ):
            raise RepositoryError("commit subject must be a non-empty single-line string")
        self.validate()
        if not self._has_head():
            return None
        result = self._run_git(
            ["log", "--format=%H%x00%s", "-z", "HEAD"],
            read_only=True,
            description="inspect Enso commit subjects",
        )
        fields = result.stdout.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 2:
            raise RepositoryError("Git returned malformed commit metadata while inspecting history")
        for index in range(0, len(fields), 2):
            if os.fsdecode(fields[index + 1]) == subject:
                return fields[index].decode("ascii")
        return None

    def snapshot(
        self,
        paths: Sequence[str],
        message: str,
        *,
        caller_cwd: str | None = None,
    ) -> bool:
        """Commit only explicit allowlisted repository-relative or absolute paths.

        ``True`` means a commit was created. A clean request is a successful
        no-op and returns ``False``. Existing staged or tracked-sensitive state
        fails closed rather than being folded into Enso's commit. Relative public
        paths may be resolved from an explicit caller working directory; internal
        callers that omit it use repository-relative paths.
        """
        if isinstance(paths, (str, bytes)) or not paths:
            raise RepositoryError("a snapshot requires at least one explicit path")
        if not isinstance(message, str) or not message.strip() or "\0" in message:
            raise RepositoryError(
                "a snapshot requires a non-empty commit message without NUL bytes"
            )
        requested_paths = tuple(paths)
        if not requested_paths:
            raise RepositoryError("a snapshot requires at least one explicit path")

        # Reject malformed or unsafe requests without creating even Enso's lock
        # file. The authoritative checks are repeated under the lock so another
        # process cannot race repository, path, index, or HEAD state.
        self._normalize_snapshot_paths(requested_paths, caller_cwd=caller_cwd)
        self.validate()
        with self._snapshot_lock():
            self.validate()
            self._cleanup_marker_temp_residue()
            normalized = self._normalize_snapshot_paths(
                requested_paths,
                caller_cwd=caller_cwd,
            )
            self._recover_snapshot_transaction()
            self._raise_if_native_index_locked()
            tracked_paths = self._tracked_paths()
            protected = protected_tracked_paths(tracked_paths)
            if protected:
                listed = ", ".join(repr(path) for path in protected)
                raise RepositoryError(
                    "snapshots are blocked because protected paths are already "
                    f"tracked: {listed}"
                )

            old_head = self._current_head_oid()
            head_tree = self._head_tree_oid(old_head)
            old_index_sha256 = self._native_index_sha256()
            staged = self._run_git(
                [
                    "diff",
                    "--cached",
                    "--quiet",
                    "--exit-code",
                    "--ita-visible-in-index",
                    "--no-ext-diff",
                    "--no-textconv",
                    head_tree,
                    "--",
                ],
                check=False,
                read_only=True,
                worktree_free=True,
                description="verify that the native Git staging area is clean",
            )
            if staged.returncode == 1:
                raise RepositoryError(
                    "the Git staging area is not clean; unstage existing changes before "
                    "snapshotting"
                )
            if staged.returncode != 0:
                self._raise_git_failure(
                    staged,
                    "verify that the native Git staging area is clean",
                )
            if (
                self._current_head_oid() != old_head
                or self._native_index_sha256() != old_index_sha256
            ):
                raise RepositoryError(
                    "repository HEAD or native Git index changed while its clean staging "
                    "baseline was inspected"
                )
            old_tree = head_tree
            transaction = _SnapshotTransaction(
                temp_index=self._allocate_snapshot_index_name(),
                old_head=old_head,
                old_tree=head_tree,
                old_index_sha256=old_index_sha256,
            )
            self._write_snapshot_transaction(transaction)
            try:
                new_tree, staged_paths = self._prepare_temporary_index(
                    transaction,
                    normalized,
                )
                if new_tree == old_tree:
                    self._clear_snapshot_transaction(transaction)
                    return False
                if not staged_paths:
                    raise RepositoryError(
                        "the temporary snapshot index changed without any audited paths"
                    )

                new_head = self._create_snapshot_commit(
                    tree=new_tree,
                    parent=old_head,
                    message=message,
                )
                transaction = replace(
                    transaction,
                    new_head=new_head,
                    new_tree=new_tree,
                    new_index_sha256=self._snapshot_index_sha256(transaction),
                )
                self._write_snapshot_transaction(transaction)
                self._require_transaction_state(
                    transaction,
                    expected_head=old_head,
                    expected_tree=old_tree,
                    action="before advancing the snapshot ref",
                )
                self._acquire_native_index_lock(
                    transaction,
                    expected_head=old_head,
                )
                expected_old = old_head or ("0" * len(new_head))
                self._run_git(
                    ["update-ref", "HEAD", new_head, expected_old],
                    description="atomically advance the Enso snapshot ref",
                )
                self._require_locked_old_index_state(
                    transaction,
                    expected_head=new_head,
                    action="before aligning the native Git index",
                )
                self._install_transaction_index(transaction)
                self._require_transaction_state(
                    transaction,
                    expected_head=new_head,
                    expected_tree=new_tree,
                    action="after aligning the native Git index",
                )
            except BaseException:
                self._handle_snapshot_transaction_failure(transaction)
                raise
            self._clear_snapshot_transaction(transaction)
            return True

    def _prepare_temporary_index(
        self,
        transaction: _SnapshotTransaction,
        normalized: list[str],
    ) -> tuple[str, tuple[str, ...]]:
        """Build and audit the requested tree without touching Git's native index."""
        index_path = self._snapshot_index_path(transaction.temp_index)
        seed = (
            ["read-tree", transaction.old_head]
            if transaction.old_head
            else ["read-tree", "--empty"]
        )
        self._run_git(
            seed,
            index_file=index_path,
            worktree_free=True,
            description="seed the temporary Enso snapshot index",
        )
        self._harden_snapshot_index(index_path)
        indexed_before = self._tracked_paths(index_file=index_path)
        scoped_before = tuple(
            path
            for path in indexed_before
            if any(path == scope or path.startswith(f"{scope}/") for scope in normalized)
        )
        if scoped_before:
            self._run_git(
                [
                    "--literal-pathspecs",
                    "update-index",
                    "--force-remove",
                    "--",
                    *scoped_before,
                ],
                index_file=index_path,
                worktree_free=True,
                description="remove prior scoped paths from the snapshot index",
            )
        entries = self._snapshot_worktree_entries(normalized, frozenset(indexed_before))
        for entry in entries:
            self._run_git(
                [
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{entry.mode},{entry.oid},{entry.path}",
                ],
                index_file=index_path,
                worktree_free=True,
                description=f"index filter-free snapshot path {entry.path!r}",
            )
        tree = self._index_tree(index_file=index_path)
        self._harden_snapshot_index(index_path)
        staged_paths = self._tree_changed_paths(transaction.old_tree, tree)
        indexed_paths = self._tracked_paths(index_file=index_path)
        indexed_protected = protected_tracked_paths(indexed_paths)
        if indexed_protected:
            listed = ", ".join(repr(path) for path in indexed_protected)
            raise RepositoryError(
                f"protected paths were staged in the temporary snapshot index: {listed}"
            )
        indexed_unsupported = tuple(
            path
            for path in indexed_paths
            if classify_content_path(path) is PathDisposition.UNSUPPORTED
        )
        if indexed_unsupported:
            listed = ", ".join(repr(path) for path in indexed_unsupported)
            raise RepositoryError(
                "non-versionable paths entered the temporary snapshot index: "
                f"{listed}"
            )
        staged_unrelated = tuple(
            path
            for path in staged_paths
            if not any(path == scope or path.startswith(f"{scope}/") for scope in normalized)
        )
        if staged_unrelated:
            listed = ", ".join(repr(path) for path in staged_unrelated)
            raise RepositoryError(
                f"paths outside the explicit snapshot scopes entered the temporary index: {listed}"
            )
        return tree, staged_paths

    def _snapshot_worktree_entries(
        self,
        scopes: Sequence[str],
        tracked: frozenset[str],
    ) -> tuple[_SnapshotIndexEntry, ...]:
        collected: dict[str, _SnapshotIndexEntry] = {}
        for scope in scopes:
            self._assert_active_snapshot_root()
            parent_and_name = self._open_snapshot_scope_parent(scope)
            if parent_and_name is None:
                continue
            parent_descriptor, name = parent_and_name
            try:
                try:
                    path_stat = os.stat(
                        name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                self._require_exact_snapshot_entry(parent_descriptor, name, scope)
                tracked_scope = any(
                    candidate == scope or candidate.startswith(f"{scope}/")
                    for candidate in tracked
                )
                if self._path_is_ignored(scope) and not tracked_scope:
                    raise RepositoryError(f"snapshot path {scope!r} is ignored by Git")
                if stat.S_ISDIR(path_stat.st_mode):
                    directory_descriptor = self._open_snapshot_directory(
                        parent_descriptor,
                        name,
                        scope,
                        path_stat,
                    )
                    try:
                        self._walk_snapshot_directory(
                            directory_descriptor,
                            scope,
                            tracked,
                            collected,
                        )
                    finally:
                        os.close(directory_descriptor)
                else:
                    collected[scope] = self._snapshot_worktree_entry(
                        parent_descriptor,
                        name,
                        scope,
                        path_stat,
                    )
            except OSError as exc:
                raise RepositoryError(f"could not inspect snapshot path {scope!r}: {exc}") from exc
            finally:
                os.close(parent_descriptor)
        return tuple(collected[path] for path in sorted(collected))

    def _open_snapshot_scope_parent(self, relative: str) -> tuple[int, str] | None:
        root_descriptor = self._active_snapshot_root_dir_fd
        if root_descriptor is None:
            raise RepositoryError("snapshot path traversal requires the Enso snapshot lock")
        parts = relative.split("/")
        descriptor = os.dup(root_descriptor)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for index, component in enumerate(parts[:-1]):
                traversed = "/".join(parts[: index + 1])
                try:
                    before = os.stat(
                        component,
                        dir_fd=descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    os.close(descriptor)
                    return None
                self._require_exact_snapshot_entry(descriptor, component, traversed)
                if stat.S_ISLNK(before.st_mode):
                    raise RepositoryError(f"snapshot path {relative!r} is a symlink escape")
                if not stat.S_ISDIR(before.st_mode):
                    raise RepositoryError(
                        f"snapshot path ancestor {traversed!r} must be a directory"
                    )
                opened_descriptor = os.open(component, flags, dir_fd=descriptor)
                opened = os.fstat(opened_descriptor)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    os.close(opened_descriptor)
                    raise RepositoryError(
                        f"snapshot path ancestor {traversed!r} changed while it was opened"
                    )
                os.close(descriptor)
                descriptor = opened_descriptor
            return descriptor, parts[-1]
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _require_exact_snapshot_entry(
        parent_descriptor: int,
        name: str,
        relative: str,
    ) -> None:
        try:
            with os.scandir(parent_descriptor) as iterator:
                exact = any(entry.name == name for entry in iterator)
        except OSError as exc:
            raise RepositoryError(
                f"could not verify exact spelling for snapshot path {relative!r}: {exc}"
            ) from exc
        if not exact:
            raise RepositoryError(
                f"snapshot path {relative!r} must use the directory entry's exact spelling"
            )

    @staticmethod
    def _open_snapshot_directory(
        parent_descriptor: int,
        name: str,
        relative: str,
        before: os.stat_result,
    ) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise RepositoryError(
                    f"snapshot directory {relative!r} changed while it was opened"
                )
            return descriptor
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            raise

    def _walk_snapshot_directory(
        self,
        descriptor: int,
        relative: str,
        tracked: frozenset[str],
        collected: dict[str, _SnapshotIndexEntry],
    ) -> None:
        self._assert_active_snapshot_root()
        try:
            os.stat(".git", dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RepositoryError(
                f"could not inspect snapshot directory {relative!r}: {exc}"
            ) from exc
        else:
            raise RepositoryError(
                f"nested Git repositories cannot be snapshotted: {relative!r}"
            )
        try:
            with os.scandir(descriptor) as iterator:
                names = tuple(sorted(entry.name for entry in iterator))
        except OSError as exc:
            raise RepositoryError(
                f"could not inspect snapshot directory {relative!r}: {exc}"
            ) from exc

        for name in names:
            self._assert_active_snapshot_root()
            child = f"{relative}/{name}"
            try:
                child_stat = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise RepositoryError(f"could not inspect snapshot path {child!r}: {exc}") from exc
            tracked_child = child in tracked or any(
                candidate.startswith(f"{child}/") for candidate in tracked
            )
            if self._path_is_ignored(child) and not tracked_child:
                continue
            if stat.S_ISDIR(child_stat.st_mode):
                child_descriptor = self._open_snapshot_directory(
                    descriptor,
                    name,
                    child,
                    child_stat,
                )
                try:
                    self._walk_snapshot_directory(
                        child_descriptor,
                        child,
                        tracked,
                        collected,
                    )
                finally:
                    os.close(child_descriptor)
            else:
                collected[child] = self._snapshot_worktree_entry(
                    descriptor,
                    name,
                    child,
                    child_stat,
                )

    def _snapshot_worktree_entry(
        self,
        parent_descriptor: int,
        name: str,
        relative: str,
        path_stat: os.stat_result,
    ) -> _SnapshotIndexEntry:
        disposition = classify_content_path(relative)
        if disposition is PathDisposition.PROTECTED:
            raise RepositoryError(
                f"protected paths were staged in the temporary snapshot index: {relative!r}"
            )
        if disposition is not PathDisposition.VERSIONABLE:
            raise RepositoryError(
                f"non-versionable paths entered the temporary snapshot index: {relative!r}"
            )
        if path_stat.st_uid != os.getuid():
            raise RepositoryError(f"snapshot path {relative!r} must be owned by the current user")
        if path_stat.st_nlink != 1:
            raise RepositoryError(f"snapshot path {relative!r} must not have hard links")
        if stat.S_ISREG(path_stat.st_mode):
            data = self._read_snapshot_regular_file(
                parent_descriptor,
                name,
                relative,
                path_stat,
            )
            mode = "100755" if path_stat.st_mode & 0o111 else "100644"
        elif stat.S_ISLNK(path_stat.st_mode):
            data = self._read_snapshot_symlink(
                parent_descriptor,
                name,
                relative,
                path_stat,
            )
            mode = "120000"
        else:
            raise RepositoryError(
                f"snapshot path {relative!r} must be a regular file or symlink"
            )
        result = self._run_git(
            ["hash-object", "-w", "--no-filters", "--stdin"],
            input_data=data,
            worktree_free=True,
            description=f"hash filter-free snapshot path {relative!r}",
        )
        return _SnapshotIndexEntry(
            path=relative,
            mode=mode,
            oid=self._validated_oid(result.stdout, "snapshot blob"),
        )

    def _read_snapshot_regular_file(
        self,
        parent_descriptor: int,
        name: str,
        relative: str,
        before: os.stat_result,
    ) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
            ):
                raise RepositoryError(
                    f"snapshot path {relative!r} changed while it was opened"
                )
            with os.fdopen(descriptor, "rb") as file:
                descriptor = None
                data = file.read()
                after = os.fstat(file.fileno())
            current = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            identity = (before.st_dev, before.st_ino)
            if (
                (after.st_dev, after.st_ino) != identity
                or (current.st_dev, current.st_ino) != identity
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
            ):
                raise RepositoryError(f"snapshot path {relative!r} changed while it was read")
            return data
        except RepositoryError:
            raise
        except OSError as exc:
            raise RepositoryError(
                f"could not safely read snapshot path {relative!r}: {exc}"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read_snapshot_symlink(
        self,
        parent_descriptor: int,
        name: str,
        relative: str,
        before: os.stat_result,
    ) -> bytes:
        try:
            target = os.readlink(name, dir_fd=parent_descriptor)
            after = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise RepositoryError(
                f"could not safely read snapshot link {relative!r}: {exc}"
            ) from exc
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or not stat.S_ISLNK(after.st_mode)
            or after.st_uid != os.getuid()
            or after.st_nlink != 1
        ):
            raise RepositoryError(f"snapshot link {relative!r} changed while it was read")
        if os.path.isabs(target):
            raise RepositoryError(f"snapshot path {relative!r} is a symlink escape")
        anchored_parts = list(relative.split("/")[:-1])
        for component in target.split("/"):
            if component in {"", "."}:
                continue
            if component == "..":
                if not anchored_parts:
                    raise RepositoryError(
                        f"snapshot path {relative!r} is a symlink escape"
                    )
                anchored_parts.pop()
            else:
                anchored_parts.append(component)
        return os.fsencode(target)

    def _path_is_ignored(self, relative: str) -> bool:
        result = self._run_git(
            ["check-ignore", "--quiet", "--no-index", "--", relative],
            check=False,
            read_only=True,
            description=f"inspect ignore rules for snapshot path {relative!r}",
        )
        if result.returncode not in {0, 1}:
            self._raise_git_failure(result, f"inspect ignore rules for snapshot path {relative!r}")
        return result.returncode == 0

    def _tree_changed_paths(self, old_tree: str, new_tree: str) -> tuple[str, ...]:
        result = self._run_git(
            [
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "--no-ext-diff",
                "--no-textconv",
                "-r",
                "-z",
                old_tree,
                new_tree,
            ],
            read_only=True,
            worktree_free=True,
            description="audit changed snapshot tree paths",
        )
        return tuple(os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw)

    def _create_snapshot_commit(self, *, tree: str, parent: str | None, message: str) -> str:
        try:
            message_bytes = message.encode("utf-8") + b"\n"
        except UnicodeEncodeError as exc:
            raise RepositoryError("snapshot commit message must be valid UTF-8") from exc
        args = ["commit-tree", tree]
        if parent is not None:
            args.extend(("-p", parent))
        result = self._run_git(
            args,
            input_data=message_bytes,
            description="create the local Enso snapshot commit",
        )
        return self._validated_oid(result.stdout, "snapshot commit")

    def _current_head_oid(self) -> str | None:
        if not self._has_head():
            return None
        result = self._run_git(
            ["rev-parse", "--verify", "HEAD^{commit}"],
            read_only=True,
            description="resolve the current Enso snapshot HEAD",
        )
        return self._validated_oid(result.stdout, "repository HEAD")

    def _head_tree_oid(self, head: str | None) -> str:
        if head is None:
            result = self._run_git(
                ["mktree"],
                input_data=b"",
                worktree_free=True,
                description="resolve the empty snapshot tree",
            )
        else:
            result = self._run_git(
                ["rev-parse", "--verify", f"{head}^{{tree}}"],
                read_only=True,
                worktree_free=True,
                description="resolve the snapshot HEAD tree",
            )
        return self._validated_oid(result.stdout, "snapshot HEAD tree")

    def _index_tree(
        self,
        *,
        index_file: str,
    ) -> str:
        result = self._run_git(
            ["write-tree"],
            index_file=index_file,
            worktree_free=True,
            description="inspect the Enso snapshot index tree",
        )
        return self._validated_oid(result.stdout, "snapshot index tree")

    @staticmethod
    def _validated_oid(raw: bytes, description: str) -> str:
        try:
            oid = raw.rstrip(b"\r\n").decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise RepositoryError(
                f"Git returned an invalid {description} object ID"
            ) from exc
        if len(oid) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in oid
        ):
            raise RepositoryError(f"Git returned an invalid {description} object ID")
        return oid

    def _allocate_snapshot_index_name(self) -> str:
        for _attempt in range(16):
            name = f".snapshot-index-{secrets.token_hex(16)}"
            if not os.path.lexists(self._snapshot_index_path(name)):
                return name
        raise RepositoryError("could not allocate a unique temporary snapshot index")

    def _snapshot_index_path(self, name: str) -> str:
        if _SNAPSHOT_INDEX_RE.fullmatch(name) is None or os.path.basename(name) != name:
            raise RepositoryError("snapshot transaction names an unsafe temporary index")
        return os.path.join(os.path.dirname(self._git_path("index")), name)

    @staticmethod
    def _harden_snapshot_index(path: str) -> None:
        with EnsoRepository._verified_regular_file(
            path,
            "temporary snapshot index",
            expected_links=1,
        ) as descriptor:
            try:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                    raise RepositoryError(
                        "temporary snapshot index did not become owner-only"
                    )
            except RepositoryError:
                raise
            except OSError as exc:
                raise RepositoryError(
                    f"could not secure temporary snapshot index: {exc}"
                ) from exc

    @property
    def _snapshot_transaction_path(self) -> str:
        return os.path.join(self.root, _SNAPSHOT_TRANSACTION_NAME)

    def _has_marker_temp_residue(self) -> bool:
        try:
            return any(_SNAPSHOT_MARKER_TEMP_RE.fullmatch(name) for name in os.listdir(self.root))
        except OSError as exc:
            raise RepositoryError(f"could not inspect snapshot transaction residue: {exc}") from exc

    def _cleanup_marker_temp_residue(self) -> None:
        self._assert_active_snapshot_root()
        removed = False
        root_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        root_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_descriptor = os.open(self.root, root_flags)
        except OSError as exc:
            raise RepositoryError(
                f"could not anchor snapshot transaction residue cleanup: {exc}"
            ) from exc
        try:
            self._validate_snapshot_root_descriptor(root_descriptor)
            try:
                names = tuple(os.listdir(root_descriptor))
            except OSError as exc:
                raise RepositoryError(
                    f"could not inspect snapshot transaction residue: {exc}"
                ) from exc
            for name in names:
                if _SNAPSHOT_MARKER_TEMP_RE.fullmatch(name) is None:
                    continue
                path = os.path.join(self.root, name)
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor: int | None = None
                try:
                    before = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_uid != os.getuid()
                        or before.st_nlink != 1
                        or stat.S_IMODE(before.st_mode) & 0o077
                    ):
                        raise RepositoryError(
                            "snapshot transaction residue is unsafe and was preserved: "
                            f"{path}"
                        )
                    descriptor = os.open(name, flags, dir_fd=root_descriptor)
                    opened = os.fstat(descriptor)
                    current = os.stat(
                        name,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        (before.st_dev, before.st_ino)
                        != (opened.st_dev, opened.st_ino)
                        or (opened.st_dev, opened.st_ino)
                        != (current.st_dev, current.st_ino)
                    ):
                        raise RepositoryError(
                            "snapshot transaction residue changed while it was inspected; "
                            f"it was preserved: {path}"
                        )
                    os.unlink(name, dir_fd=root_descriptor)
                    removed = True
                except RepositoryError:
                    raise
                except OSError as exc:
                    raise RepositoryError(
                        f"could not clear snapshot transaction residue {path}: {exc}"
                    ) from exc
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
        finally:
            os.close(root_descriptor)
        if removed:
            self._fsync_snapshot_root()

    def _write_snapshot_transaction(self, transaction: _SnapshotTransaction) -> None:
        self._assert_active_snapshot_root()
        payload = {
            "version": _SNAPSHOT_TRANSACTION_VERSION,
            "temp_index": transaction.temp_index,
            "old_head": transaction.old_head,
            "old_tree": transaction.old_tree,
            "old_index_sha256": transaction.old_index_sha256,
            "new_head": transaction.new_head,
            "new_tree": transaction.new_tree,
            "new_index_sha256": transaction.new_index_sha256,
        }
        temp_name = f".snapshot-transaction-{secrets.token_hex(16)}.tmp"
        temp_path = os.path.join(self.root, temp_name)
        descriptor: int | None = None
        try:
            content = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temp_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as file:
                descriptor = None
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self._snapshot_transaction_path)
            self._fsync_snapshot_root()
        except (OSError, TypeError, ValueError) as exc:
            raise RepositoryError(f"could not persist the snapshot transaction: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_path)

    def _read_snapshot_transaction(self) -> _SnapshotTransaction | None:
        self._assert_active_snapshot_root()
        path = self._snapshot_transaction_path
        if not os.path.lexists(path):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            before = os.lstat(path)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) & 0o077
            ):
                raise RepositoryError(
                    "snapshot transaction marker must be an owner-only regular file"
                )
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(descriptor)
                raise RepositoryError("snapshot transaction marker changed while opening")
            with os.fdopen(descriptor, "rb") as file:
                raw = file.read(_MAX_TRANSACTION_BYTES + 1)
        except RepositoryError:
            raise
        except OSError as exc:
            raise RepositoryError(f"could not read snapshot transaction marker: {exc}") from exc
        if len(raw) > _MAX_TRANSACTION_BYTES:
            raise RepositoryError("snapshot transaction marker is unexpectedly large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RepositoryError(f"snapshot transaction marker is invalid: {exc}") from exc
        expected_keys = {
            "version",
            "temp_index",
            "old_head",
            "old_tree",
            "old_index_sha256",
            "new_head",
            "new_tree",
            "new_index_sha256",
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise RepositoryError("snapshot transaction marker has an invalid schema")
        if payload["version"] != _SNAPSHOT_TRANSACTION_VERSION:
            raise RepositoryError("snapshot transaction marker has an unsupported version")
        temp_index = payload["temp_index"]
        if not isinstance(temp_index, str):
            raise RepositoryError("snapshot transaction marker has an invalid temporary index")
        self._snapshot_index_path(temp_index)
        old_head = self._optional_transaction_oid(payload["old_head"], "old HEAD")
        old_tree = self._required_transaction_oid(payload["old_tree"], "old tree")
        new_head = self._optional_transaction_oid(payload["new_head"], "new HEAD")
        new_tree = self._optional_transaction_oid(payload["new_tree"], "new tree")
        new_checksum = payload["new_index_sha256"]
        if (new_head is None) != (new_tree is None) or (new_head is None) != (
            new_checksum is None
        ):
            raise RepositoryError("snapshot transaction marker has an incomplete new state")
        checksum = payload["old_index_sha256"]
        if checksum is not None and (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)
        ):
            raise RepositoryError("snapshot transaction marker has an invalid index checksum")
        if new_checksum is not None and (
            not isinstance(new_checksum, str)
            or len(new_checksum) != 64
            or any(character not in "0123456789abcdef" for character in new_checksum)
        ):
            raise RepositoryError(
                "snapshot transaction marker has an invalid new-index checksum"
            )
        transaction = _SnapshotTransaction(
            temp_index=temp_index,
            old_head=old_head,
            old_tree=old_tree,
            old_index_sha256=checksum,
            new_head=new_head,
            new_tree=new_tree,
            new_index_sha256=new_checksum,
        )
        if transaction.new_head is not None:
            self._validate_transaction_commit(transaction)
        return transaction

    def _validate_transaction_commit(self, transaction: _SnapshotTransaction) -> None:
        if transaction.new_head is None or transaction.new_tree is None:
            raise RepositoryError("snapshot transaction marker has an incomplete commit")
        tree_result = self._run_git(
            ["rev-parse", "--verify", f"{transaction.new_head}^{{tree}}"],
            read_only=True,
            description="validate the interrupted snapshot commit tree",
        )
        actual_tree = self._validated_oid(tree_result.stdout, "snapshot commit tree")
        parents_result = self._run_git(
            ["rev-list", "--parents", "-n", "1", transaction.new_head],
            read_only=True,
            description="validate the interrupted snapshot commit parent",
        )
        try:
            history = parents_result.stdout.decode("ascii").strip().split()
        except UnicodeDecodeError as exc:
            raise RepositoryError("Git returned invalid snapshot commit ancestry") from exc
        expected_history = [transaction.new_head]
        if transaction.old_head is not None:
            expected_history.append(transaction.old_head)
        if actual_tree != transaction.new_tree or history != expected_history:
            raise RepositoryError(
                "snapshot transaction commit does not match its recorded tree and parent"
            )

    def _recover_snapshot_transaction(self) -> None:
        transaction = self._read_snapshot_transaction()
        if transaction is None:
            return
        native_lock_matches = self._matching_transaction_index_lock(transaction)
        actual_head = self._current_head_oid()
        actual_index_sha256 = self._native_index_sha256()
        old_index_matches = actual_index_sha256 == transaction.old_index_sha256
        if actual_head == transaction.old_head and old_index_matches:
            if native_lock_matches:
                self._remove_transaction_index_lock(transaction)
            self._clear_snapshot_transaction(transaction)
            return
        if transaction.new_head is not None and transaction.new_tree is not None:
            if actual_head == transaction.new_head and old_index_matches:
                if not native_lock_matches:
                    self._acquire_native_index_lock(
                        transaction,
                        expected_head=transaction.new_head,
                    )
                self._install_transaction_index(transaction)
                self._require_transaction_state(
                    transaction,
                    expected_head=transaction.new_head,
                    expected_tree=transaction.new_tree,
                    action="while recovering an interrupted snapshot",
                )
                self._clear_snapshot_transaction(transaction)
                return
            if (
                actual_head == transaction.new_head
                and actual_index_sha256 == transaction.new_index_sha256
            ):
                if native_lock_matches:
                    self._remove_transaction_index_lock(transaction)
                self._clear_snapshot_transaction(transaction)
                return
        if native_lock_matches and old_index_matches:
            self._remove_transaction_index_lock(transaction)
        raise RepositoryError(
            "interrupted snapshot state diverged from its durable transaction; "
            "HEAD, native staging, the temporary index, and the marker were preserved "
            "for manual inspection"
        )

    def _acquire_native_index_lock(
        self,
        transaction: _SnapshotTransaction,
        *,
        expected_head: str | None,
    ) -> None:
        if transaction.new_index_sha256 is None:
            raise RepositoryError("snapshot transaction is missing its new native index")
        source = self._snapshot_index_path(transaction.temp_index)
        lock_path = self._git_path("index.lock")
        created_lock: os.stat_result | None = None
        try:
            os.link(source, lock_path, follow_symlinks=False)
            created_lock = os.lstat(lock_path)
        except FileExistsError as exc:
            raise RepositoryError(
                f"Git index lock {lock_path} already exists; wait for active Git operations "
                "to finish, or inspect it manually if it is stale"
            ) from exc
        except OSError as exc:
            raise RepositoryError(f"could not acquire the native Git index lock: {exc}") from exc
        try:
            if not self._matching_transaction_index_lock(transaction):
                raise RepositoryError("Enso's native Git index lock did not persist")
            self._require_locked_old_index_state(
                transaction,
                expected_head=expected_head,
                action="after acquiring the native Git index lock",
            )
            self._fsync_directory(os.path.dirname(lock_path))
        except BaseException:
            if created_lock is not None:
                with contextlib.suppress(OSError):
                    current = os.lstat(lock_path)
                    if (current.st_dev, current.st_ino) == (
                        created_lock.st_dev,
                        created_lock.st_ino,
                    ):
                        os.unlink(lock_path)
                        self._fsync_directory(os.path.dirname(lock_path))
            raise

    def _install_transaction_index(self, transaction: _SnapshotTransaction) -> None:
        if transaction.new_head is None or transaction.new_tree is None:
            raise RepositoryError("snapshot transaction has no new index state to install")
        if not self._matching_transaction_index_lock(transaction):
            raise RepositoryError("Enso's native Git index lock is missing")
        self._require_locked_old_index_state(
            transaction,
            expected_head=transaction.new_head,
            action="before installing the audited native Git index",
        )
        index_path = self._git_path("index")
        lock_path = self._git_path("index.lock")
        with self._verified_regular_file(
            lock_path,
            "Enso native Git index lock",
            expected_links=2,
        ) as descriptor:
            try:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
                os.replace(lock_path, index_path)
                installed = os.lstat(index_path)
                opened = os.fstat(descriptor)
                if (
                    (installed.st_dev, installed.st_ino)
                    != (opened.st_dev, opened.st_ino)
                    or stat.S_IMODE(installed.st_mode) != 0o600
                ):
                    raise RepositoryError(
                        "the audited native Git index changed while it was installed"
                    )
                self._fsync_directory(os.path.dirname(index_path))
            except RepositoryError:
                raise
            except OSError as exc:
                raise RepositoryError(
                    f"could not install the audited native Git index: {exc}"
                ) from exc

    def _matching_transaction_index_lock(self, transaction: _SnapshotTransaction) -> bool:
        lock_path = self._git_path("index.lock")
        if not os.path.lexists(lock_path):
            return False
        if transaction.new_index_sha256 is None:
            raise RepositoryError(
                "a native Git index lock exists before this snapshot prepared an index; "
                "it was preserved"
            )
        source = self._snapshot_index_path(transaction.temp_index)
        try:
            source_stat = os.lstat(source)
            lock_stat = os.lstat(lock_path)
        except OSError as exc:
            raise RepositoryError(
                f"could not verify Enso's interrupted native index lock: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or not stat.S_ISREG(lock_stat.st_mode)
            or source_stat.st_uid != os.getuid()
            or lock_stat.st_uid != os.getuid()
            or source_stat.st_nlink != 2
            or lock_stat.st_nlink != 2
            or stat.S_IMODE(source_stat.st_mode) & 0o077
            or stat.S_IMODE(lock_stat.st_mode) & 0o077
            or (source_stat.st_dev, source_stat.st_ino)
            != (lock_stat.st_dev, lock_stat.st_ino)
            or self._file_sha256(lock_path, "native Git index lock")
            != transaction.new_index_sha256
        ):
            raise RepositoryError(
                "the native Git index lock does not match Enso's durable transaction; "
                "it was preserved for manual inspection"
            )
        return True

    def _remove_transaction_index_lock(self, transaction: _SnapshotTransaction) -> None:
        if not self._matching_transaction_index_lock(transaction):
            return
        lock_path = self._git_path("index.lock")
        try:
            os.unlink(lock_path)
            self._fsync_directory(os.path.dirname(lock_path))
        except OSError as exc:
            raise RepositoryError(f"could not clear Enso's native index lock: {exc}") from exc

    def _require_transaction_state(
        self,
        transaction: _SnapshotTransaction,
        *,
        expected_head: str | None,
        expected_tree: str,
        action: str,
    ) -> None:
        actual_head = self._current_head_oid()
        if actual_head != expected_head:
            raise RepositoryError(f"snapshot state changed unexpectedly {action}")
        if self._head_tree_oid(expected_head) != expected_tree:
            raise RepositoryError(f"snapshot HEAD tree changed unexpectedly {action}")
        expected_checksum = (
            transaction.old_index_sha256
            if expected_tree == transaction.old_tree
            else transaction.new_index_sha256
        )
        if self._native_index_sha256() != expected_checksum:
            raise RepositoryError(f"native Git index changed unexpectedly {action}")

    def _require_locked_old_index_state(
        self,
        transaction: _SnapshotTransaction,
        *,
        expected_head: str | None,
        action: str,
    ) -> None:
        if self._current_head_oid() != expected_head:
            raise RepositoryError(f"snapshot HEAD changed unexpectedly {action}")
        if self._native_index_sha256() != transaction.old_index_sha256:
            raise RepositoryError(f"native Git index changed unexpectedly {action}")

    def _handle_snapshot_transaction_failure(
        self,
        transaction: _SnapshotTransaction,
    ) -> None:
        """Remove only a transaction that provably never advanced or staged natively."""
        try:
            native_lock_matches = self._matching_transaction_index_lock(transaction)
            if native_lock_matches:
                if self._native_index_sha256() != transaction.old_index_sha256:
                    return
                self._remove_transaction_index_lock(transaction)
            self._require_transaction_state(
                transaction,
                expected_head=transaction.old_head,
                expected_tree=transaction.old_tree,
                action="while cleaning a failed snapshot",
            )
        except (OSError, RepositoryError):
            return
        self._clear_snapshot_transaction(transaction)

    def _clear_snapshot_transaction(self, transaction: _SnapshotTransaction) -> None:
        self._assert_active_snapshot_root()
        index_path = self._snapshot_index_path(transaction.temp_index)
        removed_index = False
        for path in (f"{index_path}.lock", index_path):
            try:
                os.unlink(path)
                removed_index = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RepositoryError(
                    f"could not remove Enso's temporary snapshot index {path}: {exc}"
                ) from exc
        if removed_index:
            try:
                self._fsync_directory(os.path.dirname(index_path))
            except OSError as exc:
                raise RepositoryError(
                    f"could not persist temporary snapshot index cleanup: {exc}"
                ) from exc
        try:
            os.unlink(self._snapshot_transaction_path)
            self._fsync_snapshot_root()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RepositoryError(f"could not clear snapshot transaction marker: {exc}") from exc

    def _native_index_sha256(self) -> str | None:
        path = self._git_path("index")
        if not os.path.lexists(path):
            return None
        return self._file_sha256(path, "native Git index")

    def _snapshot_index_sha256(self, transaction: _SnapshotTransaction) -> str:
        return self._file_sha256(
            self._snapshot_index_path(transaction.temp_index),
            "temporary snapshot index",
        )

    @staticmethod
    def _file_sha256(path: str, description: str) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode):
                raise RepositoryError(f"{description} must be a regular file")
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(descriptor)
                raise RepositoryError(f"{description} changed while opening")
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb") as file:
                for chunk in iter(lambda: file.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except RepositoryError:
            raise
        except OSError as exc:
            raise RepositoryError(f"could not safely hash {description}: {exc}") from exc

    @staticmethod
    @contextmanager
    def _verified_regular_file(
        path: str,
        description: str,
        *,
        expected_links: int,
    ) -> Iterator[int]:
        """Open one exact non-symlink file and bind checks to its descriptor."""
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != expected_links:
                raise RepositoryError(
                    f"{description} must be a regular file with the expected link count"
                )
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != expected_links
            ):
                raise RepositoryError(f"{description} changed while it was opened")
            yield descriptor
        except RepositoryError:
            raise
        except OSError as exc:
            raise RepositoryError(f"could not safely open {description}: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _git_path(self, name: str) -> str:
        result = self._run_git(
            ["rev-parse", "--git-path", name],
            read_only=True,
            description=f"locate the native Git {name}",
        )
        raw_path = result.stdout.rstrip(b"\r\n")
        if not raw_path or b"\0" in raw_path or b"\n" in raw_path or b"\r" in raw_path:
            raise RepositoryError(f"Git returned an invalid native {name} path")
        reported = os.fsdecode(raw_path)
        return reported if os.path.isabs(reported) else os.path.join(self.root, reported)

    @staticmethod
    def _required_transaction_oid(value: object, description: str) -> str:
        if not isinstance(value, str):
            raise RepositoryError(f"snapshot transaction marker has an invalid {description}")
        try:
            raw = value.encode("ascii", errors="strict")
        except UnicodeEncodeError as exc:
            raise RepositoryError(
                f"snapshot transaction marker has an invalid {description}"
            ) from exc
        return EnsoRepository._validated_oid(raw, description)

    @staticmethod
    def _optional_transaction_oid(value: object, description: str) -> str | None:
        if value is None:
            return None
        return EnsoRepository._required_transaction_oid(value, description)

    def _fsync_snapshot_root(self) -> None:
        self._fsync_directory(self.root)

    @staticmethod
    def _fsync_directory(path: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def _snapshot_lock(self) -> Iterator[None]:
        """Serialize every Enso-owned index and HEAD mutation across processes."""
        root_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        root_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            root_descriptor = os.open(self.root, root_flags)
        except OSError as exc:
            raise RepositoryError(
                f"could not anchor the Enso snapshot lock to its repository root: {exc}"
            ) from exc

        descriptor: int | None = None
        locked = False
        try:
            root_stat = self._validate_snapshot_root_descriptor(root_descriptor)
            descriptor = self._open_snapshot_lock(root_descriptor)

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            locked = True
            locked_stat = os.fstat(descriptor)
            self._validate_snapshot_lock_stat(locked_stat)
            current_root_stat = self._validate_snapshot_root_descriptor(root_descriptor)
            if (root_stat.st_dev, root_stat.st_ino) != (
                current_root_stat.st_dev,
                current_root_stat.st_ino,
            ):
                raise RepositoryError("the Enso repository root changed while locking snapshots")
            try:
                path_stat = os.stat(
                    _SNAPSHOT_LOCK_NAME,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise RepositoryError(
                    f"could not inspect the Enso snapshot lock after acquiring it: {exc}"
                ) from exc
            if (locked_stat.st_dev, locked_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise RepositoryError("the Enso snapshot lock changed while it was acquired")
            if self._active_snapshot_lock_fd is not None:
                raise RepositoryError("the Enso snapshot lock cannot be entered recursively")
            self._active_snapshot_lock_fd = descriptor
            self._active_snapshot_root_identity = (root_stat.st_dev, root_stat.st_ino)
            self._active_snapshot_root_dir_fd = root_descriptor
            yield
        except RepositoryError:
            raise
        except OSError as exc:
            raise RepositoryError(f"could not secure the Enso snapshot lock: {exc}") from exc
        finally:
            if self._active_snapshot_lock_fd == descriptor:
                self._active_snapshot_lock_fd = None
                self._active_snapshot_root_identity = None
                self._active_snapshot_root_dir_fd = None
            if locked and descriptor is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            if descriptor is not None:
                os.close(descriptor)
            os.close(root_descriptor)

    def _assert_active_snapshot_root(self) -> None:
        identity = self._active_snapshot_root_identity
        if identity is None:
            return
        try:
            current = os.lstat(self.root)
        except OSError as exc:
            raise RepositoryError(
                f"the locked Enso repository root is no longer accessible: {exc}"
            ) from exc
        if (
            (current.st_dev, current.st_ino) != identity
            or not stat.S_ISDIR(current.st_mode)
            or os.path.realpath(self.root) != self.root
        ):
            raise RepositoryError("the locked Enso repository root changed during a snapshot")

    def _open_snapshot_lock(self, root_descriptor: int) -> int:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(
                    _SNAPSHOT_LOCK_NAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=root_descriptor,
                )
            except FileExistsError:
                descriptor = os.open(
                    _SNAPSHOT_LOCK_NAME,
                    flags,
                    dir_fd=root_descriptor,
                )
            else:
                os.fchmod(descriptor, 0o600)
        except OSError as exc:
            raise RepositoryError(
                f"could not open the Enso snapshot lock safely: {exc}"
            ) from exc

        try:
            opened_stat = os.fstat(descriptor)
            self._validate_snapshot_lock_stat(opened_stat)
            path_stat = os.stat(
                _SNAPSHOT_LOCK_NAME,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (opened_stat.st_dev, opened_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise RepositoryError("the Enso snapshot lock changed while it was opened")
            os.fchmod(descriptor, 0o600)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_snapshot_root_descriptor(self, descriptor: int) -> os.stat_result:
        """Bind the lock's directory descriptor to the validated root path."""
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode):
            raise RepositoryError("the Enso snapshot lock root must be a physical directory")
        try:
            path_stat = os.lstat(self.root)
        except OSError as exc:
            raise RepositoryError(
                f"could not verify the Enso snapshot lock repository root: {exc}"
            ) from exc
        if (
            (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
            or os.path.realpath(self.root) != self.root
        ):
            raise RepositoryError(
                "the Enso snapshot lock is not anchored to the physical repository root"
            )
        return opened_stat

    @staticmethod
    def _validate_snapshot_lock_stat(lock_stat: os.stat_result) -> None:
        if not stat.S_ISREG(lock_stat.st_mode):
            raise RepositoryError("the Enso snapshot lock must be a regular file")
        if lock_stat.st_uid != os.getuid():
            raise RepositoryError("the Enso snapshot lock must be owned by the current user")
        if lock_stat.st_nlink != 1:
            raise RepositoryError("the Enso snapshot lock must not have additional hard links")

    def _raise_if_native_index_locked(self) -> None:
        """Fail without deleting Git's own active-or-stale index lock."""
        lock_path = self._git_path("index.lock")
        if os.path.lexists(lock_path):
            raise RepositoryError(
                f"Git index lock {lock_path} already exists; wait for active Git operations "
                "to finish, or inspect and remove it manually if it is stale"
            )

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
        self._raise_if_partial_clone()
        if stat.S_ISREG(os.lstat(self._git_entry_path).st_mode):
            self._validate_gitfile_binding()

    def _raise_if_partial_clone(self) -> None:
        extension = self._run_git(
            ["config", "--get", "extensions.partialClone"],
            check=False,
            read_only=True,
            description="inspect partial-clone repository configuration",
        )
        if extension.returncode == 0 and extension.stdout.strip():
            raise RepositoryError(
                "partial-clone/promisor repositories are unsupported because Enso "
                "snapshots never contact remotes"
            )
        if extension.returncode not in {0, 1}:
            self._raise_git_failure(extension, "inspect partial-clone repository configuration")
        promisors = self._run_git(
            ["config", "--bool", "--get-regexp", r"^remote\..*\.promisor$"],
            check=False,
            read_only=True,
            description="inspect promisor remote configuration",
        )
        if promisors.returncode not in {0, 1}:
            self._raise_git_failure(promisors, "inspect promisor remote configuration")
        for line in promisors.stdout.splitlines():
            fields = line.rsplit(maxsplit=1)
            if len(fields) == 2 and fields[1].lower() == b"true":
                raise RepositoryError(
                    "partial-clone/promisor repositories are unsupported because Enso "
                    "snapshots never contact remotes"
                )

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

    def _normalize_snapshot_paths(
        self,
        paths: Sequence[str],
        *,
        caller_cwd: str | None = None,
    ) -> list[str]:
        base = self._snapshot_base(caller_cwd)

        normalized: list[str] = []
        seen: set[str] = set()
        for requested in paths:
            if not isinstance(requested, str) or not requested or "\0" in requested:
                raise RepositoryError("snapshot paths must be non-empty strings")
            portable_requested = requested.replace(os.sep, "/")
            if os.altsep:
                portable_requested = portable_requested.replace(os.altsep, "/")
            if ".." in PurePosixPath(portable_requested).parts:
                raise RepositoryError(
                    f"snapshot path {requested!r} contains forbidden traversal and must "
                    f"remain beneath {self.root}"
                )
            candidate = (
                os.path.abspath(os.path.expanduser(requested))
                if os.path.isabs(requested)
                else os.path.abspath(os.path.join(base, requested))
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

    def _snapshot_base(self, caller_cwd: str | None) -> str:
        """Return the physical base used for caller-relative snapshot paths."""
        if caller_cwd is None:
            return self.root
        if (
            not isinstance(caller_cwd, str)
            or not caller_cwd
            or "\0" in caller_cwd
            or not os.path.isabs(caller_cwd)
        ):
            raise RepositoryError("caller_cwd must be a non-empty absolute directory path")
        base = os.path.abspath(caller_cwd)
        try:
            base_stat = os.stat(base)
        except OSError as exc:
            raise RepositoryError(
                f"caller working directory {caller_cwd!r} is not accessible: {exc}"
            ) from exc
        if not stat.S_ISDIR(base_stat.st_mode):
            raise RepositoryError(f"caller working directory {caller_cwd!r} must be a directory")
        return os.path.realpath(base)

    def _run_git(
        self,
        args: list[str],
        *,
        check: bool = True,
        read_only: bool = False,
        input_data: bytes | None = None,
        index_file: str | None = None,
        worktree_free: bool = False,
        description: str,
    ) -> subprocess.CompletedProcess[bytes]:
        self._assert_active_snapshot_root()
        env = os.environ.copy()
        for key in _REPOSITORY_ENV_KEYS:
            env.pop(key, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ALLOW_PROTOCOL"] = ""
        env["GIT_NO_LAZY_FETCH"] = "1"
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        if index_file is not None:
            if not os.path.isabs(index_file):
                raise RepositoryError("temporary Git index path must be absolute")
            env["GIT_INDEX_FILE"] = index_file
        if read_only:
            env["GIT_OPTIONAL_LOCKS"] = "0"
        command = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.splitIndex=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "protocol.allow=never",
        ]
        if worktree_free:
            git_dir = (
                os.path.dirname(index_file)
                if index_file is not None
                else os.path.dirname(self._git_path("index"))
            )
            command.extend(("-c", "core.bare=true", f"--git-dir={git_dir}"))
        else:
            command.extend(("-C", self.root))
        command.extend(args)
        pass_fds = (
            (self._active_snapshot_lock_fd,)
            if self._active_snapshot_lock_fd is not None
            else ()
        )
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL if input_data is None else None,
                input=input_data,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=env,
                pass_fds=pass_fds,
                umask=0o077,
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
    def _raise_git_failure(
        result: subprocess.CompletedProcess[bytes], description: str
    ) -> NoReturn:
        detail = os.fsdecode(result.stderr).strip() or os.fsdecode(result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RepositoryError(f"could not {description}{suffix}")
