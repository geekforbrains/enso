"""SQLite-backed run history for jobs.

Machine-generated run telemetry lives in a single ``runs`` table at
``CONFIG_DIR/enso.db`` (WAL mode so concurrent readers never block the
writer), while the captured output for each run is an append-only log file
at ``CONFIG_DIR/runs/<id>.log``. Keeping the transcript on disk keeps the
DB small, keeps output greppable, and makes retention a matter of deleting
a row and unlinking a file.

The DB is opened lazily and the schema is created with
``CREATE TABLE IF NOT EXISTS`` on connect, so existing installs need no
migration. See docs/specs/data-model.md for the governing design.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import config

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    title        TEXT,
    trigger      TEXT NOT NULL,
    status       TEXT NOT NULL,
    exit_code    INTEGER,
    provider     TEXT,
    model        TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    duration_ms  INTEGER,
    output_path  TEXT,
    output_bytes INTEGER
);

CREATE INDEX IF NOT EXISTS idx_runs_started   ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_kind_name ON runs (kind, name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status    ON runs (status);
"""

# One cached connection per DB path. Keying by path (rather than a single
# global) keeps tests isolated: each temp ~/.enso gets its own connection.
_connections: dict[str, sqlite3.Connection] = {}
_lock = threading.Lock()


def _utc_now() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    """Return the absolute path to the SQLite database."""
    return os.path.join(config.CONFIG_DIR, "enso.db")


def runs_dir() -> str:
    """Return the directory holding per-run output logs."""
    return os.path.join(config.CONFIG_DIR, "runs")


def log_path(run_id: str) -> str:
    """Return the absolute path to a run's captured-output log file."""
    return os.path.join(runs_dir(), f"{run_id}.log")


def _connect() -> sqlite3.Connection:
    """Return the cached connection for the active DB path, opening it lazily.

    Enables WAL mode and applies the schema (all ``IF NOT EXISTS``) the first
    time a path is opened, so existing databases are left untouched.
    """
    path = _db_path()
    conn = _connections.get(path)
    if conn is not None:
        return conn
    os.makedirs(config.CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    _connections[path] = conn
    log.debug("Opened runs DB at %s", path)
    return conn


def create(
    kind: str,
    name: str,
    title: str | None = None,
    trigger: str = "manual",
    provider: str | None = None,
    model: str | None = None,
    started_at: str | None = None,
) -> str:
    """Insert a ``running`` run row and return its id (uuid4 hex).

    ``started_at`` defaults to now (ISO-8601 UTC); callers that begin useful
    work before creating the row may pass the original timestamp. The caller
    opens ``log_path(run_id)`` for captured output and later calls ``finish``.
    """
    run_id = uuid.uuid4().hex
    conn = _connect()
    with _lock:
        conn.execute(
            "INSERT INTO runs "
            "(id, kind, name, title, trigger, status, provider, model, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)",
            (
                run_id, kind, name, title, trigger, provider, model,
                started_at or _utc_now(),
            ),
        )
        conn.commit()
    log.info("run created id=%s kind=%s name=%s trigger=%s", run_id, kind, name, trigger)
    return run_id


def append_output(run_id: str, text: str) -> None:
    """Append captured output to the run's log file (append-only).

    Also records ``output_path`` on the row the first time output is written
    so the log is discoverable from the DB while the run is still active.
    """
    os.makedirs(runs_dir(), exist_ok=True)
    path = log_path(run_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    conn = _connect()
    with _lock:
        conn.execute(
            "UPDATE runs SET output_path=? WHERE id=? AND output_path IS NULL",
            (path, run_id),
        )
        conn.commit()


def finish(run_id: str, exit_code: int, status: str) -> None:
    """Mark a run terminal: set ended_at, duration_ms, exit_code, status, bytes.

    Job statuses include ``ok`` | ``error`` | ``timeout`` plus
    ``prerun_error`` | ``prerun_timeout``. ``duration_ms`` is derived from the
    stored ``started_at``; ``output_bytes`` from the log file size
    (``output_path`` is backfilled if output exists but wasn't recorded).
    """
    conn = _connect()
    row = get(run_id)
    if row is None:
        log.warning("finish called for unknown run id=%s", run_id)
        return

    ended_at = _utc_now()
    duration_ms: int | None = None
    started = row.get("started_at")
    if started:
        try:
            delta = datetime.fromisoformat(ended_at) - datetime.fromisoformat(started)
            duration_ms = int(delta.total_seconds() * 1000)
        except ValueError:
            log.warning("Could not parse started_at=%r for run %s", started, run_id)

    path = log_path(run_id)
    output_path = row.get("output_path")
    output_bytes: int | None = None
    if os.path.exists(path):
        output_bytes = os.path.getsize(path)
        if output_path is None:
            output_path = path

    with _lock:
        conn.execute(
            "UPDATE runs SET ended_at=?, duration_ms=?, exit_code=?, status=?, "
            "output_path=?, output_bytes=? WHERE id=?",
            (ended_at, duration_ms, exit_code, status, output_path, output_bytes, run_id),
        )
        conn.commit()
    log.info(
        "run finished id=%s status=%s exit=%s duration_ms=%s bytes=%s",
        run_id, status, exit_code, duration_ms, output_bytes,
    )


def get(run_id: str) -> dict | None:
    """Return a run row as a dict, or None if no such run exists."""
    conn = _connect()
    cur = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def list_runs(
    kind: str | None = None,
    name: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return run rows newest first, optionally filtered by kind/name/status."""
    conn = _connect()
    clauses: list[str] = []
    params: list[object] = []
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)
    if name is not None:
        clauses.append("name=?")
        params.append(name)
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    cur = conn.execute(
        f"SELECT * FROM runs{where} ORDER BY started_at DESC, rowid DESC LIMIT ?",
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def read_output(run_id: str, max_bytes: int | None = None) -> str:
    """Return a run's captured output, at most ``max_bytes`` bytes if given.

    Returns an empty string when no log file exists. Bytes are decoded as
    UTF-8 with replacement so a truncated multibyte tail never raises.
    """
    path = log_path(run_id)
    try:
        with open(path, "rb") as f:
            data = f.read() if max_bytes is None else f.read(max_bytes)
    except FileNotFoundError:
        return ""
    return data.decode("utf-8", errors="replace")


def prune(keep: int = 500, max_age_days: int = 30) -> None:
    """Enforce retention caps, deleting run rows and their log files.

    Drops rows beyond the newest ``keep`` and rows older than
    ``max_age_days``. A ``running`` row is never pruned (it may be an active
    or crashed run). Both caps apply; a row failing either is removed.
    """
    conn = _connect()

    to_delete: set[str] = set()

    # Age cap: anything terminal whose start predates the cutoff.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    cur = conn.execute(
        "SELECT id FROM runs WHERE status != 'running' AND started_at < ?",
        (cutoff,),
    )
    to_delete.update(r["id"] for r in cur.fetchall())

    # Count cap: keep only the newest ``keep`` terminal rows.
    cur = conn.execute(
        "SELECT id FROM runs WHERE status != 'running' "
        "ORDER BY started_at DESC, rowid DESC"
    )
    terminal_ids = [r["id"] for r in cur.fetchall()]
    to_delete.update(terminal_ids[max(keep, 0):])

    if not to_delete:
        return

    with _lock:
        conn.executemany("DELETE FROM runs WHERE id=?", [(i,) for i in to_delete])
        conn.commit()
    for run_id in to_delete:
        with contextlib.suppress(OSError):
            os.remove(log_path(run_id))
    log.info("Pruned %d run(s) (keep=%d, max_age_days=%d)", len(to_delete), keep, max_age_days)
