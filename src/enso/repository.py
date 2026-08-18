"""Safety policy for Enso's local, content-only Git history."""

from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath


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


def _parts(path: str) -> tuple[str, ...] | None:
    if not isinstance(path, str) or not path or path.startswith("/"):
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
    if basename.endswith((".db", ".log", ".sqlite", ".sqlite3")):
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
        sorted(
            path
            for path in paths
            if classify_content_path(path) is PathDisposition.PROTECTED
        )
    )
