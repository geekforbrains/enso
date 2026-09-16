"""Advisory file locks: one hardened open for every lock file Enso takes.

A lock file may sit in a job directory the operator edits by hand or that a crashed
process left behind (``web.pid``), so it is opened without following a symbolic link
and refused unless it is a regular file. Callers own what contention means: they map
``BlockingIOError`` to their own result or error and close the descriptor when they are done,
which releases the lock. This module imports no other Enso module.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path
from typing import IO


class LockPathError(OSError):
    """The lock path is a symbolic link or something other than a regular file."""


def open_lock(path: Path, *, create: bool = True) -> int:
    """Open ``path`` for locking; refuse a symbolic link or a special file.

    ``O_NONBLOCK`` keeps a FIFO planted at the path from stalling the open and has no effect
    on a regular file. A missing file when ``create`` is false, a permission problem, and any
    other ``OSError`` propagate unchanged.
    """
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LockPathError(f"lock must not be a symbolic link: {path}") from None
        if exc.errno == errno.EISDIR:
            raise LockPathError(f"lock must be a regular file: {path}") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LockPathError(f"lock must be a regular file: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def acquire(path: Path, *, shared: bool = False) -> int:
    """Open ``path`` and take its advisory lock without waiting; the descriptor holds it.

    Raises ``BlockingIOError`` when another process holds the lock, leaving nothing open.
    """
    fd = open_lock(path)
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def acquire_file_lock(path: Path) -> IO[str] | None:
    """Take ``path``'s exclusive lock as a file object, or None when another process holds it."""
    try:
        fd = acquire(path)
    except BlockingIOError:
        return None
    return os.fdopen(fd, "a", encoding="utf-8")
