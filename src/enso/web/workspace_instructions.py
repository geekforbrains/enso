"""Bounded viewing and editing of workspace ``AGENTS.md`` files.

Paths are validated against a strict relative grammar, every ancestor must be
an existing physical, owner-protected directory, and instruction files must be
owner-owned regular single-link files without group/other write bits, holding
UTF-8 no larger than the caller's byte limit. A save is revision-checked
against the content hash the form displayed, staged beside the target, and
published with one atomic ``os.replace``. Concurrent same-user writers are
detected best-effort only: the revision check plus one pre-publish identity
check catch human-scale races, not a continuously mutating peer. That is
deliberate — agents edit the same files directly with ordinary tools, so the
durable safety boundary is scoped Git history and the protective ignore rules,
not this module.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass, field

AGENT_FILENAME = "AGENTS.md"
MAX_DISCOVERY_DEPTH = 6
MAX_DISCOVERY_FILES = 100
MAX_DISCOVERY_DIRECTORIES = 2_000
MAX_DISCOVERY_ENTRIES = 20_000
MAX_DISCOVERY_ERRORS = 100

_EXCLUDED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        "build",
        "cache",
        "caches",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "runtime",
        "target",
        "temp",
        "tmp",
        "uploads",
        "vendor",
        "vendors",
    }
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_TEMP_PREFIX = ".enso-agents-"
_REVISION_RE = re.compile(r"[0-9a-fA-F]{64}")


class AgentFileError(RuntimeError):
    """Base class for safe instruction-file failures."""


class UnsafeAgentPath(AgentFileError):  # noqa: N818 - public API uses concise domain names
    """The requested instruction path is not addressable."""


class AgentNotFound(AgentFileError):  # noqa: N818 - public API uses concise domain names
    """The instruction file does not exist."""


class AgentIntegrityError(AgentFileError):
    """The instruction file or its path violates the safety contract."""


class AgentConflict(AgentFileError):  # noqa: N818 - public API uses concise domain names
    """The instruction file changed relative to what the caller saw."""


class AgentTooLarge(AgentFileError):  # noqa: N818 - public API uses concise domain names
    """The instruction file or submission exceeds the byte limit."""


class AgentEncodingError(AgentFileError):
    """The instruction content is not clean UTF-8 text."""


class AgentFilesystemError(AgentFileError):
    """The filesystem operation failed or is unavailable."""


@dataclass(frozen=True, slots=True)
class AgentEntry:
    """One safely discovered instruction file, relative to its workspace."""

    rel_path: str


@dataclass(frozen=True, slots=True)
class AgentDiscoveryError:
    """One skipped candidate that could not be inspected safely."""

    rel_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class AgentListing:
    """A bounded instruction inventory."""

    files: tuple[AgentEntry, ...]
    truncated: bool
    errors: tuple[AgentDiscoveryError, ...]


@dataclass(frozen=True, slots=True)
class AgentDocument:
    """One instruction revision opened through its workspace root."""

    rel_path: str
    content: str
    revision: str
    mode: int


@dataclass(slots=True)
class _DiscoveryState:
    files: list[AgentEntry] = field(default_factory=list)
    errors: list[AgentDiscoveryError] = field(default_factory=list)
    directories: int = 0
    entries: int = 0
    truncated: bool = False
    entry_budget_spent: bool = False


@dataclass(frozen=True, slots=True)
class _OpenedDocument:
    document: AgentDocument
    file_stat: os.stat_result
    raw: bytes


def _stat_identity(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _validate_max_bytes(max_bytes: int) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")


def _canonical_root(root: str) -> str:
    if not isinstance(root, str) or not root or "\0" in root:
        raise UnsafeAgentPath("workspace root must be a non-empty absolute path")
    expanded = os.path.expanduser(root)
    if not os.path.isabs(expanded):
        raise UnsafeAgentPath("workspace root must be absolute")
    return os.path.normpath(expanded)


def _safe_segment(segment: str) -> bool:
    if (
        not segment
        or segment in {".", ".."}
        or "/" in segment
        or "\\" in segment
        or any(ord(character) < 32 for character in segment)
    ):
        return False
    try:
        return len(segment.encode("utf-8")) <= 255
    except UnicodeEncodeError:
        return False


def _agent_parts(rel_path: str) -> tuple[str, ...]:
    if not isinstance(rel_path, str) or not rel_path or "\\" in rel_path or os.path.isabs(rel_path):
        raise UnsafeAgentPath("instruction path must be a non-empty relative path")
    parts = tuple(rel_path.split("/"))
    if not parts or parts[-1] != AGENT_FILENAME or not all(_safe_segment(part) for part in parts):
        raise UnsafeAgentPath(f"instruction path must end in {AGENT_FILENAME}")
    directories = parts[:-1]
    if len(directories) > MAX_DISCOVERY_DEPTH:
        raise UnsafeAgentPath(
            f"instruction path is too deep (max {MAX_DISCOVERY_DEPTH} directories)"
        )
    if any(
        part.startswith(".") or part.casefold() in _EXCLUDED_DIRECTORIES for part in directories
    ):
        raise UnsafeAgentPath("instruction path is inside an excluded directory")
    return parts


def _validate_directory_stat(directory_stat: os.stat_result, description: str) -> None:
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise AgentIntegrityError(f"{description} must be a directory")
    if directory_stat.st_uid != os.getuid():
        raise AgentIntegrityError(f"{description} must be owned by the current user")
    if stat.S_IMODE(directory_stat.st_mode) & 0o022:
        raise AgentIntegrityError(f"{description} must not be group- or other-writable")


def _validate_file_stat(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise AgentIntegrityError("instruction file must be a regular non-symlink file")
    if file_stat.st_uid != os.getuid():
        raise AgentIntegrityError("instruction file must be owned by the current user")
    if file_stat.st_nlink != 1:
        raise AgentIntegrityError("instruction file must not have additional hard links")
    if stat.S_IMODE(file_stat.st_mode) & 0o022:
        raise AgentIntegrityError("instruction file must not be group- or other-writable")


def _open_error(exc: OSError, description: str) -> AgentFileError:
    if exc.errno == errno.ENOENT:
        return AgentNotFound(f"{description} was not found")
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return AgentIntegrityError(f"{description} contains a symlink or non-directory")
    return AgentFilesystemError(f"could not open {description} safely")


def _resolve_parent(root: str, parts: tuple[str, ...]) -> str:
    """Validate root-to-parent directories and return the parent's path.

    Every ancestor must be an existing, owner-protected physical directory;
    the realpath comparison rejects symlinks anywhere in the chain, including
    a workspace root replaced with a symlink after configuration was loaded.
    """
    current = _canonical_root(root)
    for index in range(len(parts)):
        description = (
            "workspace root" if index == 0 else f"instruction directory {parts[index - 1]!r}"
        )
        try:
            directory_stat = os.lstat(current)
        except FileNotFoundError as exc:
            raise AgentNotFound(f"{description} was not found") from exc
        except OSError as exc:
            raise AgentFilesystemError(f"could not inspect {description}") from exc
        if not stat.S_ISDIR(directory_stat.st_mode) or os.path.realpath(current) != current:
            raise AgentIntegrityError(f"{description} contains a symlink or non-directory")
        _validate_directory_stat(directory_stat, description)
        if index < len(parts) - 1:
            current = os.path.join(current, parts[index])
    return current


def _read_bytes(descriptor: int, max_bytes: int) -> bytes:
    content = bytearray()
    while len(content) <= max_bytes:
        chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    if len(content) > max_bytes:
        raise AgentTooLarge(f"instruction file exceeds the {max_bytes}-byte limit")
    return bytes(content)


def _decode_content(raw: bytes) -> str:
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentEncodingError("instruction file must contain valid UTF-8") from exc
    if "\0" in content:
        raise AgentEncodingError("instruction file must not contain NUL bytes")
    return content


def _open_document(parent: str, filename: str, rel_path: str, max_bytes: int) -> _OpenedDocument:
    path = os.path.join(parent, filename)
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        raise AgentNotFound("instruction file was not found") from exc
    except OSError as exc:
        raise AgentFilesystemError("could not inspect instruction file") from exc
    _validate_file_stat(before)
    if before.st_size > max_bytes:
        raise AgentTooLarge(f"instruction file exceeds the {max_bytes}-byte limit")

    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as exc:
        raise _open_error(exc, "instruction file") from exc
    try:
        opened = os.fstat(descriptor)
        _validate_file_stat(opened)
        if not _same_file(before, opened):
            raise AgentConflict("instruction file changed while it was opened")
        raw = _read_bytes(descriptor, max_bytes)
        final = os.fstat(descriptor)
        if len(raw) != final.st_size:
            raise AgentConflict("instruction file changed while it was read")
    finally:
        os.close(descriptor)

    content = _decode_content(raw)
    return _OpenedDocument(
        document=AgentDocument(
            rel_path=rel_path,
            content=content,
            revision=hashlib.sha256(raw).hexdigest(),
            mode=stat.S_IMODE(final.st_mode),
        ),
        file_stat=final,
        raw=raw,
    )


def read_agent(root: str, rel_path: str, max_bytes: int) -> AgentDocument:
    """Read one owner-protected ``AGENTS.md`` beneath ``root``."""
    _validate_max_bytes(max_bytes)
    parts = _agent_parts(rel_path)
    parent = _resolve_parent(root, parts)
    return _open_document(parent, parts[-1], "/".join(parts), max_bytes).document


def _record_discovery_error(state: _DiscoveryState, rel_path: str, reason: str) -> None:
    if len(state.errors) < MAX_DISCOVERY_ERRORS:
        state.errors.append(AgentDiscoveryError(rel_path=rel_path, reason=reason))
    else:
        state.truncated = True


def _descendable_directory(name: str) -> bool:
    return (
        _safe_segment(name)
        and not name.startswith(".")
        and name.casefold() not in _EXCLUDED_DIRECTORIES
    )


def _discovery_entries(directory: str, state: _DiscoveryState) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if state.entries >= MAX_DISCOVERY_ENTRIES:
                    state.truncated = True
                    state.entry_budget_spent = True
                    break
                state.entries += 1
                names.append(entry.name)
    except OSError:
        state.truncated = True
    return sorted(names)


def _walk_agents(
    directory: str,
    relative_parts: tuple[str, ...],
    depth: int,
    state: _DiscoveryState,
) -> None:
    state.directories += 1
    for name in _discovery_entries(directory, state):
        rel_path = "/".join((*relative_parts, name))
        path = os.path.join(directory, name)
        try:
            entry_stat = os.lstat(path)
        except OSError:
            if name == AGENT_FILENAME:
                _record_discovery_error(state, rel_path, "could not inspect instruction file")
            continue

        if name == AGENT_FILENAME:
            try:
                _validate_file_stat(entry_stat)
            except AgentIntegrityError as exc:
                _record_discovery_error(state, rel_path, str(exc))
                continue
            if len(state.files) >= MAX_DISCOVERY_FILES:
                state.truncated = True
                continue
            state.files.append(AgentEntry(rel_path=rel_path))
            continue
        if not stat.S_ISDIR(entry_stat.st_mode) or not _descendable_directory(name):
            continue
        if depth >= MAX_DISCOVERY_DEPTH or state.entry_budget_spent:
            state.truncated = True
            continue
        if state.directories >= MAX_DISCOVERY_DIRECTORIES:
            state.truncated = True
            continue
        try:
            _validate_directory_stat(entry_stat, f"instruction directory {name!r}")
        except AgentIntegrityError as exc:
            _record_discovery_error(state, rel_path, str(exc))
            continue
        _walk_agents(path, (*relative_parts, name), depth + 1, state)


def discover_agents(root: str) -> AgentListing:
    """Return a bounded, symlink-free inventory of ``AGENTS.md`` files."""
    canonical = _resolve_parent(root, (AGENT_FILENAME,))
    state = _DiscoveryState()
    _walk_agents(canonical, (), 0, state)
    return AgentListing(
        files=tuple(sorted(state.files, key=lambda item: item.rel_path)),
        truncated=state.truncated,
        errors=tuple(sorted(state.errors, key=lambda item: item.rel_path)),
    )


def _encode_submission(content: str, max_bytes: int) -> bytes:
    if not isinstance(content, str):
        raise AgentEncodingError("instruction content must be text")
    normalized = content.replace("\r\n", "\n")
    if "\0" in normalized:
        raise AgentEncodingError("instruction content must not contain NUL bytes")
    try:
        raw = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AgentEncodingError("instruction content must contain valid UTF-8") from exc
    if len(raw) > max_bytes:
        raise AgentTooLarge(f"instruction content exceeds the {max_bytes}-byte limit")
    return raw


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("could not write instruction file")
        remaining = remaining[written:]


def _stage_file(parent: str, raw: bytes, mode: int) -> str:
    """Write the submission to a synced temporary file beside its target."""
    for _ in range(10):
        path = os.path.join(parent, f"{_TEMP_PREFIX}{secrets.token_hex(16)}.tmp")
        try:
            descriptor = os.open(path, _CREATE_FLAGS, 0o600)
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise AgentFilesystemError("could not stage instruction file safely") from exc
    else:
        raise AgentFilesystemError("could not allocate an instruction temporary file")

    try:
        _write_all(descriptor, raw)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except BaseException as exc:
        os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(path)
        if isinstance(exc, OSError):
            raise AgentFilesystemError("could not stage instruction content") from exc
        raise
    os.close(descriptor)
    return path


def _fsync_directory(parent: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError as exc:
        raise AgentFilesystemError("could not sync instruction directory") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise AgentFilesystemError("could not sync instruction directory") from exc
    finally:
        os.close(descriptor)


def _replace_existing(parent: str, filename: str, raw: bytes, opened: _OpenedDocument) -> None:
    target = os.path.join(parent, filename)
    staged = _stage_file(parent, raw, opened.document.mode)
    try:
        try:
            current = os.lstat(target)
        except OSError as exc:
            raise AgentConflict("instruction file changed during save") from exc
        if not stat.S_ISREG(current.st_mode) or _stat_identity(current) != _stat_identity(
            opened.file_stat
        ):
            raise AgentConflict("instruction file changed during save")
        os.replace(staged, target)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        if isinstance(exc, AgentFileError) or not isinstance(exc, OSError):
            raise
        raise AgentFilesystemError("could not publish instruction file safely") from exc
    _fsync_directory(parent)


def _publish_new_root(parent: str, raw: bytes) -> None:
    staged = _stage_file(parent, raw, 0o644)
    try:
        os.link(staged, os.path.join(parent, AGENT_FILENAME))
    except FileExistsError as exc:
        raise AgentConflict("instruction file appeared during save") from exc
    except OSError as exc:
        raise AgentFilesystemError("could not create instruction file safely") from exc
    finally:
        with contextlib.suppress(OSError):
            os.unlink(staged)
    _fsync_directory(parent)


def _normalize_expected_revision(expected_revision: object) -> str | None:
    if expected_revision is None:
        return None
    if not isinstance(expected_revision, str) or not _REVISION_RE.fullmatch(expected_revision):
        raise AgentConflict("expected instruction revision must be a 64-character hex hash")
    return expected_revision.lower()


def write_agent(
    root: str,
    rel_path: str,
    content: str,
    expected_revision: str | None,
    max_bytes: int,
    allow_create_root: bool = False,
) -> str:
    """Publish a revision-checked edit with one atomic replacement."""
    _validate_max_bytes(max_bytes)
    parts = _agent_parts(rel_path)
    normalized = "/".join(parts)
    raw = _encode_submission(content, max_bytes)
    expected_revision = _normalize_expected_revision(expected_revision)
    parent = _resolve_parent(root, parts)
    try:
        opened = _open_document(parent, parts[-1], normalized, max_bytes)
    except AgentNotFound:
        if not allow_create_root or normalized != AGENT_FILENAME:
            raise
        if expected_revision is not None:
            raise AgentConflict("missing instruction file has no matching revision") from None
        _publish_new_root(parent, raw)
        return hashlib.sha256(raw).hexdigest()

    if expected_revision is None or not secrets.compare_digest(
        expected_revision, opened.document.revision
    ):
        raise AgentConflict("instruction file has changed since it was displayed")
    _replace_existing(parent, parts[-1], raw, opened)
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "AGENT_FILENAME",
    "MAX_DISCOVERY_DEPTH",
    "MAX_DISCOVERY_DIRECTORIES",
    "MAX_DISCOVERY_ENTRIES",
    "MAX_DISCOVERY_FILES",
    "AgentConflict",
    "AgentDiscoveryError",
    "AgentDocument",
    "AgentEncodingError",
    "AgentEntry",
    "AgentFileError",
    "AgentFilesystemError",
    "AgentIntegrityError",
    "AgentListing",
    "AgentNotFound",
    "AgentTooLarge",
    "UnsafeAgentPath",
    "discover_agents",
    "read_agent",
    "write_agent",
]
