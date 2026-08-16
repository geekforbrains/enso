"""Secure loading and stable snapshots for shared agent instructions."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass

from .config import CONFIG_DIR

# Codex and Agy carry this content in one argv element. Linux limits one
# execve argument to 128 KiB, and JSON/TOML encoding can expand an ASCII
# control byte to six bytes. Keep enough headroom for provider wrappers.
MAX_SHARED_INSTRUCTION_BYTES = 20 * 1024

_DIRECTORY_MODE = 0o700
_SNAPSHOT_MODE = 0o400
_READ_CHUNK_SIZE = 64 * 1024


class InstructionError(RuntimeError):
    """Raised when shared instructions cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class ValidatedInstructions:
    """One validated shared-instruction revision, without filesystem changes."""

    source_path: str
    content: str
    revision: str


@dataclass(frozen=True, slots=True)
class InstructionBundle(ValidatedInstructions):
    """One validated shared-instruction revision and its stable snapshot."""

    snapshot_path: str


def _stat_identity(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
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


def _validate_file_stat(
    file_stat: os.stat_result,
    *,
    description: str,
    size_limit: int,
) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise InstructionError(f"{description} must be a regular, non-symlink file")
    if file_stat.st_uid != os.getuid():
        raise InstructionError(f"{description} must be owned by the current user")
    if file_stat.st_size > size_limit:
        raise InstructionError(f"{description} exceeds the {size_limit}-byte limit")


def _read_owned_regular_file(
    path: str,
    *,
    description: str,
    missing_message: str,
    size_limit: int,
) -> tuple[bytes, os.stat_result]:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError as exc:
        raise InstructionError(missing_message) from exc
    except OSError as exc:
        raise InstructionError(f"could not inspect {description}: {exc}") from exc

    _validate_file_stat(path_stat, description=description, size_limit=size_limit)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise InstructionError(f"{description} changed while it was being read") from exc
    except OSError as exc:
        raise InstructionError(f"could not open {description} safely: {exc}") from exc

    try:
        opened_stat = os.fstat(descriptor)
        _validate_file_stat(opened_stat, description=description, size_limit=size_limit)
        if not _same_file(path_stat, opened_stat):
            raise InstructionError(f"{description} changed while it was being read")

        content = bytearray()
        while len(content) <= size_limit:
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_SIZE, size_limit + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > size_limit:
            raise InstructionError(f"{description} exceeds the {size_limit}-byte limit")

        final_stat = os.fstat(descriptor)
        try:
            final_path_stat = os.lstat(path)
        except OSError as exc:
            raise InstructionError(f"{description} changed while it was being read") from exc
        if (
            _stat_identity(opened_stat) != _stat_identity(final_stat)
            or _stat_identity(final_stat) != _stat_identity(final_path_stat)
            or len(content) != final_stat.st_size
        ):
            raise InstructionError(f"{description} changed while it was being read")
        return bytes(content), final_stat
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: str, *, description: str) -> None:
    try:
        os.mkdir(path, _DIRECTORY_MODE)
    except FileExistsError:
        pass
    except OSError as exc:
        raise InstructionError(f"could not create {description}: {exc}") from exc

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstructionError(f"could not open {description} safely: {exc}") from exc
    try:
        directory_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise InstructionError(f"{description} must be a directory")
        if directory_stat.st_uid != os.getuid():
            raise InstructionError(f"{description} must be owned by the current user")
        os.fchmod(descriptor, _DIRECTORY_MODE)
    except OSError as exc:
        raise InstructionError(f"could not secure {description}: {exc}") from exc
    finally:
        os.close(descriptor)


def _verify_snapshot(path: str, expected: bytes) -> None:
    content, snapshot_stat = _read_owned_regular_file(
        path,
        description="shared instruction snapshot",
        missing_message="shared instruction snapshot is missing",
        size_limit=MAX_SHARED_INSTRUCTION_BYTES,
    )
    if content != expected:
        raise InstructionError("shared instruction snapshot does not match its revision")
    if stat.S_IMODE(snapshot_stat.st_mode) != _SNAPSHOT_MODE:
        raise InstructionError("shared instruction snapshot permissions must be owner-read-only")
    if snapshot_stat.st_nlink != 1:
        raise InstructionError("shared instruction snapshot must not have additional hard links")


def _validate_publish_lock(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise InstructionError("snapshot publish lock must be a regular file")
    if file_stat.st_uid != os.getuid():
        raise InstructionError("snapshot publish lock must be owned by the current user")
    if file_stat.st_nlink != 1:
        raise InstructionError("snapshot publish lock must not have additional hard links")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise InstructionError("snapshot publish lock permissions must be owner-only")


@contextlib.contextmanager
def _snapshot_publish_lock(snapshot_dir: str) -> Iterator[None]:
    lock_path = os.path.join(snapshot_dir, ".publish.lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise InstructionError(f"could not open snapshot publish lock safely: {exc}") from exc

    locked = False
    try:
        lock_stat = os.fstat(descriptor)
        _validate_publish_lock(lock_stat)
        try:
            path_stat = os.lstat(lock_path)
        except OSError as exc:
            raise InstructionError("snapshot publish lock changed while it was opened") from exc
        if not _same_file(lock_stat, path_stat):
            raise InstructionError("snapshot publish lock changed while it was opened")

        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        try:
            locked_path_stat = os.lstat(lock_path)
        except OSError as exc:
            raise InstructionError("snapshot publish lock changed while it was acquired") from exc
        locked_stat = os.fstat(descriptor)
        _validate_publish_lock(locked_stat)
        if not _same_file(locked_stat, locked_path_stat):
            raise InstructionError("snapshot publish lock changed while it was acquired")
        os.fchmod(descriptor, 0o600)
        hardened_stat = os.fstat(descriptor)
        if (
            _stat_identity(hardened_stat) != _stat_identity(os.lstat(lock_path))
            or stat.S_IMODE(hardened_stat.st_mode) != 0o600
        ):
            raise InstructionError("snapshot publish lock could not be made owner-only")
        yield
    except OSError as exc:
        raise InstructionError(f"could not secure snapshot publish lock: {exc}") from exc
    finally:
        if locked:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("could not write shared instruction snapshot")
        remaining = remaining[written:]


def _publish_snapshot_locked(content: bytes, revision: str, snapshot_dir: str) -> str:
    snapshot_path = os.path.join(snapshot_dir, f"{revision}.md")

    if os.path.lexists(snapshot_path):
        _verify_snapshot(snapshot_path, content)
        return snapshot_path

    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{revision[:12]}-", dir=snapshot_dir)
        _write_all(descriptor, content)
        os.fchmod(descriptor, _SNAPSHOT_MODE)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, snapshot_path)
        except FileExistsError:
            pass
        except OSError as exc:
            raise InstructionError(f"could not publish shared instruction snapshot: {exc}") from exc
    except InstructionError:
        raise
    except OSError as exc:
        raise InstructionError(f"could not stage shared instruction snapshot: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            with contextlib.suppress(FileNotFoundError):
                os.remove(temporary)

    _verify_snapshot(snapshot_path, content)
    return snapshot_path


def _publish_snapshot(content: bytes, revision: str) -> str:
    runtime_dir = os.path.join(CONFIG_DIR, "runtime")
    snapshot_dir = os.path.join(runtime_dir, "instructions")
    _ensure_private_directory(runtime_dir, description="instruction runtime directory")
    _ensure_private_directory(snapshot_dir, description="instruction snapshot directory")
    with _snapshot_publish_lock(snapshot_dir):
        return _publish_snapshot_locked(content, revision, snapshot_dir)


def validate_shared_instructions() -> ValidatedInstructions:
    """Validate ``~/.enso/AGENTS.md`` without changing runtime state."""
    source_path = os.path.join(CONFIG_DIR, "AGENTS.md")
    raw, source_stat = _read_owned_regular_file(
        source_path,
        description="shared instruction file",
        missing_message=f"shared instruction file is missing: {source_path}",
        size_limit=MAX_SHARED_INSTRUCTION_BYTES,
    )
    if stat.S_IMODE(source_stat.st_mode) & 0o022:
        raise InstructionError(
            "shared instruction file must not be group- or other-writable"
        )
    if source_stat.st_nlink != 1:
        raise InstructionError("shared instruction file must not have additional hard links")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstructionError("shared instruction file must contain valid UTF-8") from exc
    if "\x00" in content:
        raise InstructionError("shared instruction file must not contain NUL bytes")

    revision = hashlib.sha256(raw).hexdigest()
    return ValidatedInstructions(
        source_path=source_path,
        content=content,
        revision=revision,
    )


def load_shared_instructions() -> InstructionBundle:
    """Validate shared instructions and publish a stable provider snapshot.

    The source is limited to 20 KiB and must be a stable, owner-owned regular
    UTF-8 file without NUL bytes. The returned snapshot is content-addressed
    and owner-read-only so subprocesses can consume the exact validated bytes.
    """
    validated = validate_shared_instructions()
    raw = validated.content.encode("utf-8")
    snapshot_path = _publish_snapshot(raw, validated.revision)
    return InstructionBundle(
        source_path=validated.source_path,
        content=validated.content,
        revision=validated.revision,
        snapshot_path=snapshot_path,
    )
