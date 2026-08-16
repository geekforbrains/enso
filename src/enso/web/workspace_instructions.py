"""Symlink-resistant viewing and editing of workspace ``AGENTS.md`` files.

Existing-file writes require the host's atomic name-exchange primitive. Enso
exchanges the staged and target names without a missing-file window, then
validates the intended bytes, displaced inode and revision, and complete pinned
path before discarding the old file. A detected one-shot race is exchanged
back. Platforms without atomic exchange fail before publication; a continuously
mutating same-user peer can still defeat rollback or change a file after the
last verification, in which case Enso reports failure without unlinking an
object whose identity is uncertain.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterator
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
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_CREATE_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_TEMP_PREFIX = ".enso-agents-"
_REVISION_RE = re.compile(r"[0-9a-fA-F]{64}")
_RENAME_EXCHANGE = 0x00000002


class AgentFileError(RuntimeError):
    """Base class for safe instruction-file failures."""


class UnsafeAgentPath(AgentFileError):  # noqa: N818 - public API uses concise domain names
    """The requested root or relative instruction path is not addressable."""


class AgentNotFound(AgentFileError):  # noqa: N818 - public API uses concise domain names
    """The requested root, parent directory, or instruction file is absent."""


class AgentIntegrityError(AgentFileError):
    """A path component or file failed ownership, mode, type, or link checks."""


class AgentConflict(AgentFileError):  # noqa: N818 - public API uses concise domain names
    """The instruction file changed since it was displayed or opened."""


class AgentTooLarge(AgentFileError):  # noqa: N818 - public API uses concise domain names
    """The instruction file exceeds the caller's byte limit."""


class AgentEncodingError(AgentFileError):
    """Instruction content is not safe UTF-8 text."""


class AgentFilesystemError(AgentFileError):
    """The filesystem could not complete a secure operation."""


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
    """One stable instruction revision opened through its workspace root."""

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


@dataclass(frozen=True, slots=True)
class _PinnedParent:
    root_path: str
    directory_parts: tuple[str, ...]
    descriptors: tuple[int, ...]
    identities: tuple[tuple[int, int], ...]
    filename: str

    @property
    def parent_fd(self) -> int:
        return self.descriptors[-1]


@dataclass(frozen=True, slots=True)
class _StagedFile:
    name: str
    descriptor: int
    file_stat: os.stat_result


@dataclass(slots=True)
class _ExchangeState:
    published_stat: os.stat_result | None = None
    displaced_stat: os.stat_result | None = None
    displaced_verified: bool = False


def _stat_identity(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_uid,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        file_stat.st_nlink,
    )


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _inode_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _validate_max_bytes(max_bytes: int) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")


def _canonical_root(root: str) -> str:
    if not isinstance(root, str) or not root or "\0" in root:
        raise UnsafeAgentPath("workspace root must be a non-empty absolute path")
    expanded = os.path.expanduser(root)
    if not os.path.isabs(expanded):
        raise UnsafeAgentPath("workspace root must be absolute")
    # Catalog paths are canonical before they reach this boundary. Resolving
    # again here would follow a workspace root replaced with a symlink after
    # configuration was loaded, bypassing the component-wise no-follow walk.
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


@contextlib.contextmanager
def _open_root(root: str) -> Iterator[tuple[int, str]]:
    canonical = _canonical_root(root)
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise AgentFilesystemError("secure directory opening is unavailable")

    descriptor = -1
    try:
        descriptor = os.open(os.path.sep, _DIRECTORY_FLAGS)
        for component in (part for part in canonical.split(os.path.sep) if part):
            try:
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as exc:
                raise _open_error(exc, "workspace root") from exc
            os.close(descriptor)
            descriptor = next_descriptor
        _validate_directory_stat(os.fstat(descriptor), "workspace root")
        yield descriptor, canonical
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextlib.contextmanager
def _open_parent(root: str, rel_path: str) -> Iterator[_PinnedParent]:
    parts = _agent_parts(rel_path)
    with _open_root(root) as (root_fd, canonical):
        child_descriptors: list[int] = []
        current_fd = root_fd
        try:
            for index, component in enumerate(parts[:-1]):
                try:
                    before = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
                    next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                except OSError as exc:
                    raise _open_error(exc, f"instruction directory {component!r}") from exc
                try:
                    opened = os.fstat(next_fd)
                    _validate_directory_stat(opened, f"instruction directory {component!r}")
                    if not _same_file(before, opened):
                        raise AgentConflict("instruction directory changed while it was opened")
                except BaseException:
                    os.close(next_fd)
                    raise
                child_descriptors.append(next_fd)
                current_fd = next_fd
                if index + 1 > MAX_DISCOVERY_DEPTH:  # defence beyond _agent_parts
                    raise UnsafeAgentPath("instruction path is too deep")
            descriptors = (root_fd, *child_descriptors)
            yield _PinnedParent(
                root_path=canonical,
                directory_parts=parts[:-1],
                descriptors=descriptors,
                identities=tuple(
                    _inode_identity(os.fstat(descriptor)) for descriptor in descriptors
                ),
                filename=parts[-1],
            )
        finally:
            for descriptor in reversed(child_descriptors):
                os.close(descriptor)


def _revalidate_parent(pinned: _PinnedParent) -> None:
    """Require every configured root-to-parent name to retain its pinned inode."""
    for descriptor, identity in zip(pinned.descriptors, pinned.identities, strict=True):
        held = os.fstat(descriptor)
        _validate_directory_stat(held, "instruction directory")
        if (held.st_dev, held.st_ino) != identity:
            raise AgentConflict("instruction directory identity changed during save")

    reopened_children: list[int] = []
    try:
        with _open_root(pinned.root_path) as (root_fd, _):
            if not _same_file(os.fstat(root_fd), os.fstat(pinned.descriptors[0])):
                raise AgentConflict("workspace root moved during save")
            current_fd = root_fd
            for index, component in enumerate(pinned.directory_parts, start=1):
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
                reopened_children.append(child_fd)
                opened = os.fstat(child_fd)
                _validate_directory_stat(opened, "instruction directory")
                if not _same_file(opened, os.fstat(pinned.descriptors[index])):
                    raise AgentConflict("instruction directory moved during save")
                current_fd = child_fd
    except AgentConflict:
        raise
    except (AgentFileError, OSError) as exc:
        raise AgentConflict("configured instruction path changed during save") from exc
    finally:
        for descriptor in reversed(reopened_children):
            os.close(descriptor)


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


def _read_at(parent_fd: int, filename: str, rel_path: str, max_bytes: int) -> _OpenedDocument:
    _validate_directory_stat(os.fstat(parent_fd), "instruction directory")
    try:
        before = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise _open_error(exc, "instruction file") from exc
    _validate_file_stat(before)
    if before.st_size > max_bytes:
        raise AgentTooLarge(f"instruction file exceeds the {max_bytes}-byte limit")

    try:
        descriptor = os.open(filename, _READ_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _open_error(exc, "instruction file") from exc
    try:
        opened = os.fstat(descriptor)
        _validate_file_stat(opened)
        if _stat_identity(before) != _stat_identity(opened):
            raise AgentConflict("instruction file changed while it was opened")
        raw = _read_bytes(descriptor, max_bytes)
        final = os.fstat(descriptor)
        try:
            current = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise AgentConflict("instruction file changed while it was read") from exc
        if (
            _stat_identity(opened) != _stat_identity(final)
            or _stat_identity(final) != _stat_identity(current)
            or len(raw) != final.st_size
        ):
            raise AgentConflict("instruction file changed while it was read")
        _validate_directory_stat(os.fstat(parent_fd), "instruction directory")
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
    """Read one stable, owner-protected ``AGENTS.md`` beneath ``root``."""
    _validate_max_bytes(max_bytes)
    normalized = "/".join(_agent_parts(rel_path))
    with _open_parent(root, normalized) as pinned:
        return _read_at(
            pinned.parent_fd,
            pinned.filename,
            normalized,
            max_bytes,
        ).document


def _record_discovery_error(state: _DiscoveryState, rel_path: str, reason: str) -> None:
    if len(state.errors) < MAX_DISCOVERY_ERRORS:
        state.errors.append(AgentDiscoveryError(rel_path=rel_path, reason=reason))
    else:
        state.truncated = True


def _discovery_entries(directory_fd: int, state: _DiscoveryState) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
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


def _descendable_directory(name: str) -> bool:
    return (
        _safe_segment(name)
        and not name.startswith(".")
        and name.casefold() not in _EXCLUDED_DIRECTORIES
    )


def _discover_file(
    directory_fd: int,
    rel_path: str,
    file_stat: os.stat_result,
    state: _DiscoveryState,
) -> None:
    try:
        _validate_file_stat(file_stat)
    except AgentIntegrityError as exc:
        _record_discovery_error(state, rel_path, str(exc))
        return
    if len(state.files) >= MAX_DISCOVERY_FILES:
        state.truncated = True
        return
    # A second no-following lookup closes the common stat/list race. The detail
    # read repeats the complete open/fstat/identity validation.
    try:
        current = os.stat(AGENT_FILENAME, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        _record_discovery_error(state, rel_path, "instruction file changed during discovery")
        return
    if _stat_identity(file_stat) != _stat_identity(current):
        _record_discovery_error(state, rel_path, "instruction file changed during discovery")
        return
    state.files.append(AgentEntry(rel_path=rel_path))


def _walk_agents(
    directory_fd: int,
    relative_parts: tuple[str, ...],
    depth: int,
    state: _DiscoveryState,
) -> None:
    state.directories += 1
    names = _discovery_entries(directory_fd, state)
    for name in names:
        rel_path = "/".join((*relative_parts, name))
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            if name == AGENT_FILENAME:
                _record_discovery_error(state, rel_path, "could not inspect instruction file")
            continue

        if name == AGENT_FILENAME:
            _discover_file(directory_fd, rel_path, entry_stat, state)
            continue
        if not stat.S_ISDIR(entry_stat.st_mode) or not _descendable_directory(name):
            continue
        if depth >= MAX_DISCOVERY_DEPTH or state.entry_budget_spent:
            state.truncated = True
            continue
        if state.directories >= MAX_DISCOVERY_DIRECTORIES:
            state.truncated = True
            continue
        child_fd = -1
        try:
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            opened = os.fstat(child_fd)
            _validate_directory_stat(opened, f"instruction directory {name!r}")
            if not _same_file(entry_stat, opened):
                raise AgentConflict("instruction directory changed during discovery")
        except (OSError, AgentFileError) as exc:
            if child_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(child_fd)
            _record_discovery_error(state, rel_path, str(exc) or "unsafe directory")
            continue
        try:
            _walk_agents(child_fd, (*relative_parts, name), depth + 1, state)
        finally:
            os.close(child_fd)


def discover_agents(root: str) -> AgentListing:
    """Return a bounded, symlink-free inventory of ``AGENTS.md`` files."""
    state = _DiscoveryState()
    with _open_root(root) as (root_fd, _):
        _walk_agents(root_fd, (), 0, state)
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


def _stage_file(parent_fd: int, raw: bytes, mode: int) -> _StagedFile:
    for _ in range(10):
        name = f"{_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=parent_fd)
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
        file_stat = os.fstat(descriptor)
        _validate_file_stat(file_stat)
    except BaseException as exc:
        os.close(descriptor)
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=parent_fd)
        if isinstance(exc, OSError):
            raise AgentFilesystemError("could not stage instruction content") from exc
        raise
    return _StagedFile(name=name, descriptor=descriptor, file_stat=file_stat)


def _verify_staged_name(parent_fd: int, staged: _StagedFile) -> None:
    """Require the reserved staging name to identify the still-open file."""
    opened = os.fstat(staged.descriptor)
    _validate_file_stat(opened)
    try:
        current = os.stat(staged.name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise AgentConflict("staged instruction file changed before publication") from exc
    try:
        _validate_file_stat(current)
    except AgentIntegrityError as exc:
        raise AgentConflict("staged instruction file changed before publication") from exc
    if _stat_identity(opened) != _stat_identity(staged.file_stat) or _stat_identity(
        current
    ) != _stat_identity(opened):
        raise AgentConflict("staged instruction file changed before publication")


def _staged_metadata(file_stat: os.stat_result) -> tuple[int, ...]:
    """Return staged properties that namespace publication must not alter."""
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_uid,
        file_stat.st_gid,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _verify_staged_content(
    staged: _StagedFile,
    raw: bytes,
    *,
    expected_links: int,
) -> os.stat_result:
    """Require the held inode to retain the exact staged bytes and metadata."""
    if not hasattr(os, "pread"):
        raise AgentFilesystemError("secure staged-content verification is unavailable")
    try:
        before = os.fstat(staged.descriptor)
        content = bytearray()
        while len(content) <= len(raw):
            chunk = os.pread(
                staged.descriptor,
                min(64 * 1024, len(raw) + 1 - len(content)),
                len(content),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(staged.descriptor)
    except OSError as exc:
        raise AgentConflict("staged instruction content changed during publication") from exc

    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_uid != os.getuid()
        or stat.S_IMODE(after.st_mode) & 0o022
        or after.st_nlink != expected_links
        or _staged_metadata(after) != _staged_metadata(staged.file_stat)
        or _stat_identity(before) != _stat_identity(after)
        or bytes(content) != raw
    ):
        raise AgentConflict("staged instruction content changed during publication")
    return after


def _verify_linked_stage(
    parent_fd: int,
    staged: _StagedFile,
    target_name: str,
) -> None:
    """Require staging and target to be the only two links to the held inode."""
    try:
        held = os.fstat(staged.descriptor)
        staging = os.stat(staged.name, dir_fd=parent_fd, follow_symlinks=False)
        target = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise AgentConflict("new instruction links changed during publication") from exc
    for candidate in (held, staging, target):
        if (
            not stat.S_ISREG(candidate.st_mode)
            or candidate.st_uid != os.getuid()
            or stat.S_IMODE(candidate.st_mode) & 0o022
            or candidate.st_nlink != 2
        ):
            raise AgentConflict("new instruction links changed during publication")
    if not _same_file(held, staging) or not _same_file(held, target):
        raise AgentConflict("new instruction links changed during publication")


def _discard_staged(parent_fd: int, staged: _StagedFile, *, scrub: bool) -> None:
    """Remove the reserved name and close a failed staged-file transaction."""
    if scrub:
        with contextlib.suppress(OSError):
            os.ftruncate(staged.descriptor, 0)
            os.fsync(staged.descriptor)
    with contextlib.suppress(OSError):
        os.unlink(staged.name, dir_fd=parent_fd)
    with contextlib.suppress(OSError):
        os.close(staged.descriptor)


def _before_publication(parent_fd: int, staged_name: str, target_name: str) -> None:
    """Test hook at the final adversarial window before a namespace mutation."""


def _exchange_names(parent_fd: int, first: str, second: str) -> None:
    """Atomically exchange two names, failing closed when the OS cannot do so."""
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(library, "renameatx_np", None)
    elif sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
    else:
        function = None
    if function is None:
        raise AgentFilesystemError("atomic instruction exchange is unavailable")

    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        parent_fd,
        os.fsencode(first),
        parent_fd,
        os.fsencode(second),
        _RENAME_EXCHANGE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _name_stat(parent_fd: int, name: str, description: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise AgentConflict(f"{description} changed during publication") from exc


def _unlink_name_if_same(parent_fd: int, name: str, expected: os.stat_result) -> bool:
    """Remove ``name`` only while it identifies ``expected`` and verify detachment."""
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not _same_file(current, expected):
        return True
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        return False
    try:
        rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not _same_file(rebound, expected)


def _rollback_new_publication(
    parent_fd: int,
    staged: _StagedFile,
    target_name: str,
    published_stat: os.stat_result | None,
) -> None:
    """Remove a failed create, or preserve its bytes if target removal is impossible."""
    held = os.fstat(staged.descriptor)
    expected_target = held if published_stat is None else published_stat
    if _unlink_name_if_same(parent_fd, target_name, expected_target):
        _discard_staged(parent_fd, staged, scrub=True)
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise AgentFilesystemError("could not sync new-instruction rollback") from exc
        return

    # The target still identifies the held inode. Detach only the temporary
    # link, and never scrub through the descriptor that remains published.
    _unlink_name_if_same(parent_fd, staged.name, held)
    with contextlib.suppress(OSError):
        os.close(staged.descriptor)
    with contextlib.suppress(OSError):
        os.fsync(parent_fd)
    raise AgentFilesystemError("could not safely roll back new instruction publication")


def _rollback_exchange(
    parent_fd: int,
    staged: _StagedFile,
    filename: str,
    published_stat: os.stat_result,
    displaced_stat: os.stat_result,
) -> None:
    """Restore the two exact objects observed immediately after an exchange."""
    current_target = _name_stat(parent_fd, filename, "instruction file")
    current_staging = _name_stat(parent_fd, staged.name, "staged instruction file")
    held = os.fstat(staged.descriptor)
    target_is_held = _same_file(current_target, held)
    if target_is_held:
        try:
            _validate_file_stat(current_target)
        except AgentIntegrityError:
            target_is_held = False
    if (
        _stat_identity(current_target) != _stat_identity(published_stat) and not target_is_held
    ) or _stat_identity(current_staging) != _stat_identity(displaced_stat):
        raise AgentFilesystemError(
            "could not safely roll back a concurrently changed instruction file"
        )
    try:
        _exchange_names(parent_fd, staged.name, filename)
    except (AgentFileError, OSError) as exc:
        raise AgentFilesystemError("could not roll back instruction publication") from exc
    restored = _name_stat(parent_fd, filename, "instruction file")
    returned_stage = _name_stat(parent_fd, staged.name, "staged instruction file")
    if not _same_file(restored, displaced_stat) or not _same_file(returned_stage, published_stat):
        raise AgentFilesystemError("instruction publication rollback was not stable")


def _restore_previous_copy(
    pinned: _PinnedParent,
    staged: _StagedFile,
    recovery: _StagedFile,
    opened: _OpenedDocument,
) -> None:
    """Restore verified prior bytes when the displaced original name was lost."""
    parent_fd = pinned.parent_fd
    recovery_exchanged = False
    restored_held: os.stat_result | None = None
    displaced_new: os.stat_result | None = None
    try:
        current_target = _name_stat(parent_fd, pinned.filename, "published instruction file")
        held_new = os.fstat(staged.descriptor)
        _validate_file_stat(current_target)
        _validate_file_stat(held_new)
        if not _same_file(current_target, held_new):
            raise AgentConflict("published instruction identity changed before recovery")
        _verify_staged_name(parent_fd, recovery)
        _verify_staged_content(recovery, opened.raw, expected_links=1)
        _exchange_names(parent_fd, recovery.name, pinned.filename)
        recovery_exchanged = True

        restored_target = _name_stat(parent_fd, pinned.filename, "restored instruction file")
        displaced_new = _name_stat(parent_fd, recovery.name, "rejected instruction file")
        restored_held = _verify_staged_content(recovery, opened.raw, expected_links=1)
        current_new = os.fstat(staged.descriptor)
        _validate_file_stat(current_new)
        if _stat_identity(restored_target) != _stat_identity(restored_held) or not _same_file(
            displaced_new, current_new
        ):
            raise AgentConflict("instruction recovery exchange was not stable")
    except (AgentFileError, OSError) as exc:
        if recovery_exchanged and restored_held is not None and displaced_new is not None:
            with contextlib.suppress(AgentFileError, OSError):
                current_target = _name_stat(parent_fd, pinned.filename, "restored instruction file")
                current_rejected = _name_stat(parent_fd, recovery.name, "rejected instruction file")
                if _same_file(current_target, restored_held) and _same_file(
                    current_rejected, displaced_new
                ):
                    _exchange_names(parent_fd, recovery.name, pinned.filename)
        with contextlib.suppress(OSError):
            os.close(staged.descriptor)
        with contextlib.suppress(OSError):
            os.close(recovery.descriptor)
        raise AgentFilesystemError("could not restore the previous instruction revision") from exc

    rejected_removed = _unlink_name_if_same(parent_fd, recovery.name, displaced_new)
    original_removed = _unlink_name_if_same(parent_fd, staged.name, opened.file_stat)
    if rejected_removed:
        with contextlib.suppress(OSError):
            rejected_stat = os.fstat(staged.descriptor)
            if rejected_stat.st_nlink == 0:
                os.ftruncate(staged.descriptor, 0)
                os.fsync(staged.descriptor)
    with contextlib.suppress(OSError):
        os.close(staged.descriptor)

    try:
        final_held = _verify_staged_content(recovery, opened.raw, expected_links=1)
        final_target = _name_stat(parent_fd, pinned.filename, "restored instruction file")
        if _stat_identity(final_target) != _stat_identity(final_held):
            raise AgentConflict("restored instruction changed during rollback")
        os.fsync(parent_fd)
        synced_held = _verify_staged_content(recovery, opened.raw, expected_links=1)
        synced_target = _name_stat(parent_fd, pinned.filename, "restored instruction file")
        if _stat_identity(synced_target) != _stat_identity(synced_held):
            raise AgentConflict("restored instruction changed during rollback")
    except (AgentFileError, OSError) as exc:
        raise AgentFilesystemError("could not verify the restored instruction revision") from exc
    finally:
        with contextlib.suppress(OSError):
            os.close(recovery.descriptor)

    if not rejected_removed or not original_removed:
        raise AgentFilesystemError("could not clean up instruction rollback files")


def _prepare_existing_exchange(
    pinned: _PinnedParent,
    staged: _StagedFile,
    recovery: _StagedFile,
    opened: _OpenedDocument,
    raw: bytes,
) -> None:
    parent_fd = pinned.parent_fd
    current = _name_stat(parent_fd, pinned.filename, "instruction file")
    _validate_file_stat(current)
    if _stat_identity(current) != _stat_identity(opened.file_stat):
        raise AgentConflict("instruction file changed during save")
    _verify_staged_name(parent_fd, staged)
    _verify_staged_content(staged, raw, expected_links=1)
    _verify_staged_name(parent_fd, recovery)
    _verify_staged_content(recovery, opened.raw, expected_links=1)
    _revalidate_parent(pinned)
    _before_publication(parent_fd, staged.name, pinned.filename)
    final_current = _name_stat(parent_fd, pinned.filename, "instruction file")
    _validate_file_stat(final_current)
    if _stat_identity(final_current) != _stat_identity(opened.file_stat):
        raise AgentConflict("instruction file changed immediately before publication")
    _verify_staged_name(parent_fd, staged)
    _verify_staged_content(staged, raw, expected_links=1)
    _verify_staged_name(parent_fd, recovery)
    _verify_staged_content(recovery, opened.raw, expected_links=1)


def _commit_existing_exchange(
    pinned: _PinnedParent,
    staged: _StagedFile,
    opened: _OpenedDocument,
    raw: bytes,
    max_bytes: int,
    state: _ExchangeState,
) -> None:
    parent_fd = pinned.parent_fd
    state.published_stat = _name_stat(parent_fd, pinned.filename, "instruction file")
    state.displaced_stat = _name_stat(parent_fd, staged.name, "displaced instruction file")
    published_held = _verify_staged_content(staged, raw, expected_links=1)
    if _stat_identity(state.published_stat) != _stat_identity(published_held):
        raise AgentConflict("staged instruction file changed during publication")
    displaced = _read_at(
        parent_fd,
        staged.name,
        opened.document.rel_path,
        max_bytes,
    )
    if (
        not _same_file(displaced.file_stat, opened.file_stat)
        or displaced.document.revision != opened.document.revision
    ):
        raise AgentConflict("instruction file changed during publication")
    state.displaced_verified = True
    _revalidate_parent(pinned)

    current_displaced = _name_stat(parent_fd, staged.name, "displaced instruction file")
    current_published = _name_stat(parent_fd, pinned.filename, "published instruction file")
    held_before_commit = _verify_staged_content(staged, raw, expected_links=1)
    if _stat_identity(current_displaced) != _stat_identity(state.displaced_stat) or _stat_identity(
        current_published
    ) != _stat_identity(held_before_commit):
        raise AgentConflict("displaced instruction file changed before commit")
    os.unlink(staged.name, dir_fd=parent_fd)
    final_target = _name_stat(parent_fd, pinned.filename, "published instruction file")
    final_held = _verify_staged_content(staged, raw, expected_links=1)
    _validate_file_stat(final_target)
    if _stat_identity(final_target) != _stat_identity(final_held):
        raise AgentConflict("published instruction file changed during commit")
    os.fsync(parent_fd)
    synced_held = _verify_staged_content(staged, raw, expected_links=1)
    synced_target = _name_stat(parent_fd, pinned.filename, "published instruction file")
    if _stat_identity(synced_target) != _stat_identity(synced_held):
        raise AgentConflict("published instruction file changed during commit")


def _recover_existing_exchange(
    pinned: _PinnedParent,
    staged: _StagedFile,
    recovery: _StagedFile,
    opened: _OpenedDocument,
    state: _ExchangeState,
) -> None:
    rolled_back = False
    if (
        state.displaced_verified
        and state.published_stat is not None
        and state.displaced_stat is not None
    ):
        try:
            _rollback_exchange(
                pinned.parent_fd,
                staged,
                pinned.filename,
                state.published_stat,
                state.displaced_stat,
            )
            rolled_back = True
        except AgentFileError:
            pass
    if rolled_back:
        _discard_staged(pinned.parent_fd, staged, scrub=True)
        _discard_staged(pinned.parent_fd, recovery, scrub=True)
        return
    _restore_previous_copy(pinned, staged, recovery, opened)


def _replace_existing(
    pinned: _PinnedParent,
    raw: bytes,
    opened: _OpenedDocument,
    max_bytes: int,
) -> None:
    parent_fd = pinned.parent_fd
    staged = _stage_file(parent_fd, raw, opened.document.mode)
    try:
        recovery = _stage_file(parent_fd, opened.raw, opened.document.mode)
    except BaseException:
        _discard_staged(parent_fd, staged, scrub=True)
        raise

    try:
        _prepare_existing_exchange(pinned, staged, recovery, opened, raw)
    except BaseException:
        _discard_staged(parent_fd, staged, scrub=True)
        _discard_staged(parent_fd, recovery, scrub=True)
        raise

    try:
        _exchange_names(parent_fd, staged.name, pinned.filename)
    except (AgentFileError, OSError) as exc:
        _discard_staged(parent_fd, staged, scrub=True)
        _discard_staged(parent_fd, recovery, scrub=True)
        if isinstance(exc, AgentFileError):
            raise
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            raise AgentConflict("instruction file changed during publication") from exc
        raise AgentFilesystemError("could not exchange instruction file safely") from exc

    state = _ExchangeState()
    try:
        _commit_existing_exchange(pinned, staged, opened, raw, max_bytes, state)
    except BaseException as exc:
        try:
            _recover_existing_exchange(pinned, staged, recovery, opened, state)
        except AgentFilesystemError as rollback_error:
            raise rollback_error from exc
        if isinstance(exc, AgentFileError):
            raise
        if isinstance(exc, OSError):
            raise AgentFilesystemError("could not commit instruction publication") from exc
        raise

    _discard_staged(parent_fd, recovery, scrub=True)
    with contextlib.suppress(OSError):
        os.close(staged.descriptor)
    try:
        os.fsync(parent_fd)
    except OSError as exc:
        raise AgentFilesystemError("could not sync instruction directory") from exc


def _publish_new_root(pinned: _PinnedParent, raw: bytes) -> None:
    parent_fd = pinned.parent_fd
    staged = _stage_file(parent_fd, raw, 0o644)
    try:
        _verify_staged_name(parent_fd, staged)
        _verify_staged_content(staged, raw, expected_links=1)
        _revalidate_parent(pinned)
        _before_publication(parent_fd, staged.name, pinned.filename)
    except BaseException:
        _discard_staged(parent_fd, staged, scrub=True)
        raise
    try:
        os.link(
            staged.name,
            pinned.filename,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except (FileExistsError, FileNotFoundError) as exc:
        _discard_staged(parent_fd, staged, scrub=True)
        raise AgentConflict("instruction file appeared during save") from exc
    except OSError as exc:
        _discard_staged(parent_fd, staged, scrub=True)
        raise AgentFilesystemError("could not create instruction file safely") from exc

    target: os.stat_result | None = None
    try:
        candidate = _name_stat(parent_fd, pinned.filename, "instruction file")
        if not _same_file(candidate, os.fstat(staged.descriptor)):
            raise AgentConflict("staged instruction file changed during publication")
        target = candidate
        _revalidate_parent(pinned)
        _verify_linked_stage(parent_fd, staged, pinned.filename)
        linked_held = _verify_staged_content(staged, raw, expected_links=2)
        if _stat_identity(target) != _stat_identity(linked_held):
            raise AgentConflict("new instruction content changed during publication")
        _verify_staged_content(staged, raw, expected_links=2)
        os.unlink(staged.name, dir_fd=parent_fd)
        final_held = _verify_staged_content(staged, raw, expected_links=1)
        final_target = _name_stat(parent_fd, pinned.filename, "instruction file")
        _validate_file_stat(final_target)
        if _stat_identity(final_target) != _stat_identity(final_held):
            raise AgentConflict("new instruction file changed during commit")
        os.fsync(parent_fd)
        synced_held = _verify_staged_content(staged, raw, expected_links=1)
        synced_target = _name_stat(parent_fd, pinned.filename, "instruction file")
        if _stat_identity(synced_target) != _stat_identity(synced_held):
            raise AgentConflict("new instruction file changed during commit")
    except (AgentFileError, OSError) as exc:
        try:
            _rollback_new_publication(parent_fd, staged, pinned.filename, target)
        except AgentFilesystemError as rollback_error:
            raise rollback_error from exc
        if isinstance(exc, AgentFileError):
            raise
        raise AgentFilesystemError("could not finalize instruction file safely") from exc

    with contextlib.suppress(OSError):
        os.close(staged.descriptor)


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
    """Publish a checked revision, requiring atomic exchange for replacements."""
    _validate_max_bytes(max_bytes)
    normalized = "/".join(_agent_parts(rel_path))
    raw = _encode_submission(content, max_bytes)
    expected_revision = _normalize_expected_revision(expected_revision)
    with _open_parent(root, normalized) as pinned:
        parent_fd = pinned.parent_fd
        try:
            opened = _read_at(parent_fd, pinned.filename, normalized, max_bytes)
        except AgentNotFound:
            if not allow_create_root or normalized != AGENT_FILENAME:
                raise
            if expected_revision is not None:
                raise AgentConflict("missing instruction file has no matching revision") from None
            _validate_directory_stat(os.fstat(parent_fd), "workspace root")
            _publish_new_root(pinned, raw)
            return hashlib.sha256(raw).hexdigest()

        if expected_revision is None or not secrets.compare_digest(
            expected_revision, opened.document.revision
        ):
            raise AgentConflict("instruction file has changed since it was displayed")
        _replace_existing(pinned, raw, opened, max_bytes)
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
