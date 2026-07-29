"""Shared filesystem helpers used across the package."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile

_PRIVATE_SQLITE_MODE = 0o600
_SQLITE_FILE_SUFFIXES = ("", "-wal", "-shm", "-journal")


def atomic_write_text(
    path: str,
    text: str,
    *,
    mode: int | None = None,
    newline: str | None = None,
) -> None:
    """Fsync a temporary UTF-8 file, then atomically replace ``path``.

    ``mode`` applies restrictive permissions (e.g. ``0o600``) before the
    replace so the final file never exists with looser bits. ``newline``
    is passed through to ``open`` for callers that must preserve exact
    line endings.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def is_within(base: str, target: str) -> bool:
    """True when ``target`` resolves to ``base`` or a path beneath it."""
    base_r = os.path.realpath(base)
    tgt_r = os.path.realpath(target)
    return tgt_r == base_r or tgt_r.startswith(base_r + os.sep)


def regular_file_sha256(path: str) -> str | None:
    """Hash a regular, non-symlink file, or return ``None`` safely."""
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def prepare_private_sqlite_file(path: str) -> None:
    """Create a SQLite database placeholder privately, or harden an existing one.

    Pre-creating with ``O_EXCL`` avoids the brief world-readable window that
    ``sqlite3.connect`` would otherwise introduce under a permissive umask.
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, _PRIVATE_SQLITE_MODE)
    except FileExistsError:
        pass
    else:
        try:
            os.fchmod(fd, _PRIVATE_SQLITE_MODE)
        finally:
            os.close(fd)
    harden_sqlite_files(path)


def harden_sqlite_files(path: str) -> None:
    """Set a SQLite database and its data-bearing sidecars to owner-only access."""
    for suffix in _SQLITE_FILE_SUFFIXES:
        candidate = f"{path}{suffix}"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(candidate, flags)
        except FileNotFoundError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"SQLite path is not a regular file: {candidate}")
            os.fchmod(fd, _PRIVATE_SQLITE_MODE)
        finally:
            os.close(fd)
