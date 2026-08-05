"""Shared SQLite connection and failure-handling policy.

SQLite is a synchronous library.  Callers that run inside an event loop must
execute complete storage operations in a worker thread; each connection below
is deliberately owned by one operation and never crosses a thread boundary.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from .fsutil import harden_sqlite_files, prepare_private_sqlite_file

READ_TIMEOUT_SECONDS = 0.5
WRITE_TIMEOUT_SECONDS = 5.0

DatabaseErrorKind = Literal["busy", "unavailable"]


def database_exists(path: str) -> bool:
    """Return whether *path* is a regular file, propagating access failures."""
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(file_stat.st_mode):
        raise OSError(f"SQLite path is not a regular file: {path}")
    return True


def database_error_kind(error: BaseException) -> DatabaseErrorKind:
    """Classify transient lock contention separately from persistent failures.

    ``sqlite_errorcode`` arrived in Python 3.11, while Enso supports Python
    3.10.  The narrow message fallback keeps lock handling useful on 3.10
    without treating arbitrary operational errors as retryable.
    """
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in (5, 6):  # SQLITE_BUSY / LOCKED
        return "busy"
    if isinstance(error, OSError) and error.errno in (errno.EAGAIN, errno.EBUSY):
        return "busy"
    message = str(error).casefold()
    if "database is locked" in message or "database table is locked" in message:
        return "busy"
    return "unavailable"


def _timeout_ms(timeout: float) -> int:
    return max(0, round(timeout * 1000))


def _ensure_wal_mode(conn: sqlite3.Connection, deadline: float) -> None:
    """Enable WAL, retrying the transient race between first-time writers.

    SQLite's journal-mode pragma may return ``SQLITE_BUSY`` immediately while
    another connection changes the mode, even when a busy timeout is active.
    Once that connection succeeds, the next check normally observes WAL and
    can continue without attempting another mode change.
    """
    delay = 0.01
    last_error = sqlite3.OperationalError("database is locked while enabling WAL mode")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise last_error
        conn.execute(f"PRAGMA busy_timeout={_timeout_ms(remaining)}")
        try:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(journal_mode).casefold() == "wal":
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise last_error
            conn.execute(f"PRAGMA busy_timeout={_timeout_ms(remaining)}")
            journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal_mode).casefold() == "wal":
                return
            # SQLite can report the unchanged mode instead of raising when it
            # cannot obtain the lock required to switch journals.
            last_error = sqlite3.OperationalError("database is locked while enabling WAL mode")
        except sqlite3.OperationalError as exc:
            if database_error_kind(exc) != "busy":
                raise
            last_error = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise last_error
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.1)


@contextlib.contextmanager
def read_connection(
    path: str,
    *,
    timeout: float = READ_TIMEOUT_SECONDS,
) -> Iterator[sqlite3.Connection]:
    """Yield a short-lived, read-only connection with a bounded busy wait."""
    if not database_exists(path):
        raise FileNotFoundError(path)
    harden_sqlite_files(path)
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(
        uri,
        uri=True,
        timeout=timeout,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute(f"PRAGMA busy_timeout={_timeout_ms(timeout)}")
        yield conn
    finally:
        conn.close()
        harden_sqlite_files(path)


@contextlib.contextmanager
def write_connection(
    path: str,
    *,
    timeout: float = WRITE_TIMEOUT_SECONDS,
) -> Iterator[sqlite3.Connection]:
    """Yield one short explicit write transaction and always close it cleanly."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    prepare_private_sqlite_file(path)
    conn = sqlite3.connect(
        path,
        timeout=timeout,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    try:
        deadline = time.monotonic() + timeout
        conn.execute(f"PRAGMA busy_timeout={_timeout_ms(timeout)}")
        _ensure_wal_mode(conn, deadline)
        conn.execute("PRAGMA synchronous=NORMAL")
        harden_sqlite_files(path)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise sqlite3.OperationalError("database is locked before write transaction")
        conn.execute(f"PRAGMA busy_timeout={_timeout_ms(remaining)}")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"PRAGMA busy_timeout={_timeout_ms(timeout)}")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
        harden_sqlite_files(path)
