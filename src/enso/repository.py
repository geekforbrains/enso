"""Safe local Git root for Enso's managed content tree.

Enso keeps ``~/.enso`` as an exact, local-only Git repository for three
reasons:

- It is the discovery boundary that lets providers with native ancestor
  discovery (Claude, Codex) see root instructions and skills from inside a
  managed workspace without duplicate injection.
- Its managed protective ``.gitignore`` block keeps configuration,
  credentials, databases, and runtime state out of content history, so
  ordinary scoped ``git commit`` calls made by agents cannot capture them.
- A repository-local fallback identity lets headless machines commit without
  editing the user's global Git configuration.

History itself is ordinary Git: agents record scoped commits directly with
``git add <paths>``/``git commit`` as described in the root instructions, and
fresh setup records one baseline commit of the seeded content. Enso exposes
no snapshot, restore, reset, or history-management commands, and it never
creates a remote, pushes, pulls, or fetches.
"""

from __future__ import annotations

import os
import stat
import subprocess
from typing import NoReturn

from . import config
from .fsutil import atomic_write_text


class RepositoryError(RuntimeError):
    """Enso's local repository cannot be used without risking user data."""


# Directory names that are runtime-only anywhere in the tree. The managed
# ignore block excludes them recursively while re-allowing the same words in
# structural identifier slots (a job, skill, or workspace legitimately named
# "logs" must not vanish from history).
_PROTECTED_COMPONENTS = frozenset(
    {
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

_GITIGNORE_START = "# >>> Enso protected paths (managed; do not edit) >>>"
_GITIGNORE_END = "# <<< Enso protected paths (managed; do not edit) <<<"
_STRUCTURAL_IDENTIFIER_EXCEPTIONS = tuple(
    pattern
    for name in sorted(_PROTECTED_COMPONENTS)
    for pattern in (
        f"!/jobs/{name}/",
        f"!/skills/{name}/",
        f"!/workspaces/{name}/",
        f"!/workspaces/*/skills/{name}/",
    )
)
_PROTECTIVE_GITIGNORE_PATTERNS = (
    "/config.json",
    "/config.json.lock",
    "/enso.db*",
    "/messages.json",
    "/messages.json.lock",
    "/state.json",
    "/update.lock",
    "/update.json",
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
        "# Enso keeps local history for human-authored content only. These",
        "# runtime and potentially sensitive paths must never enter history.",
        *_PROTECTIVE_GITIGNORE_PATTERNS,
        _GITIGNORE_END,
        "",
    )
)

# Ambient Git environment that could redirect commands at another repository
# or index; scrubbed from every invocation.
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

    def has_head(self) -> bool:
        """Return whether this exact worktree currently has a commit at HEAD."""
        self.validate()
        result = self._run_git(
            ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            check=False,
            read_only=True,
            description="inspect repository history",
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        self._raise_git_failure(result, "inspect repository history")

    def commit_all(self, message: str) -> bool:
        """Stage and commit every non-ignored change; ``False`` when clean.

        Fresh setup uses this once to record the seeded baseline. The managed
        protective ignore block is what keeps runtime and credential paths out
        of that commit; the caller must have run :meth:`ensure` first.
        """
        if not isinstance(message, str) or not message.strip() or "\0" in message:
            raise RepositoryError("a commit requires a non-empty message without NUL bytes")
        self.validate()
        self._run_git(
            ["add", "--all"],
            description="stage the Enso content baseline",
        )
        staged = self._run_git(
            ["diff", "--cached", "--quiet"],
            check=False,
            read_only=True,
            description="check for staged Enso content",
        )
        if staged.returncode == 0:
            return False
        if staged.returncode != 1:
            self._raise_git_failure(staged, "check for staged Enso content")
        self._run_git(
            ["commit", "--quiet", "-m", message],
            description="commit the Enso content baseline",
        )
        return True

    def tracked_protected_paths(self) -> tuple[str, ...]:
        """Return tracked paths the protective ignore rules would exclude.

        Once a path is tracked, ``.gitignore`` stops shielding it: every later
        commit that includes it keeps updating it silently. This diagnostic
        reports such paths; repairing tracking is a deliberate operator action.
        """
        self.validate()
        result = self._run_git(
            ["ls-files", "--cached", "-i", "--exclude-standard", "-z"],
            read_only=True,
            description="inspect tracked protected Enso paths",
        )
        return tuple(
            sorted(os.fsdecode(raw) for raw in result.stdout.split(b"\0") if raw)
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
        env["GIT_ALLOW_PROTOCOL"] = ""
        env["GIT_NO_LAZY_FETCH"] = "1"
        env["GIT_NO_REPLACE_OBJECTS"] = "1"
        if read_only:
            env["GIT_OPTIONAL_LOCKS"] = "0"
        command = [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "protocol.allow=never",
            "-C",
            self.root,
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=env,
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
