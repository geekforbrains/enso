"""SQLite state store: short-lived WAL connections and ``user_version`` migrations.

Two ways in. ``connect``/``transaction`` create the home and the database, set WAL mode, and are
what every write path uses; ``connect`` is the only place a writable handle is opened and
``transaction`` and ``migrate`` are its only callers, so the forward-version guard it performs
cannot be walked around by another call site. Every ``transaction`` begins IMMEDIATE, read-only
helpers included, so the write lock is held for its whole body: a deferred transaction that
reads and then writes has to upgrade a snapshot another writer may have moved on from, which
under WAL fails at once with ``SQLITE_BUSY`` and never waits on the busy timeout. Enso has one
writer and its transactions last microseconds, so serialising them costs nothing worth
measuring. ``reader`` opens the file read-only (SQLite's ``mode=ro`` plus ``PRAGMA query_only``)
and never creates or migrates anything: it is the web viewer's only route to the database, so a
request cannot change Enso state even by accident, and a WAL reader never blocks
``enso serve``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .config import Paths

SCHEMA_VERSION = 7

_SCHEMA_V7 = """
CREATE TABLE _enso_memory_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation TEXT NOT NULL, workspace TEXT NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
  transport TEXT NOT NULL, channel TEXT NOT NULL, channel_name TEXT NOT NULL,
  thread TEXT, message_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
  request TEXT NOT NULL, response TEXT NOT NULL DEFAULT '', files TEXT NOT NULL,
  received_at TEXT NOT NULL, completed_at TEXT, session_id TEXT,
  status TEXT NOT NULL DEFAULT 'running', error TEXT NOT NULL DEFAULT '',
  request_truncated INTEGER NOT NULL DEFAULT 0, response_truncated INTEGER NOT NULL DEFAULT 0,
  processed_at TEXT);
CREATE UNIQUE INDEX _enso_memory_source_identity
  ON _enso_memory_turns (transport, channel, message_id) WHERE message_id != '';
CREATE INDEX _enso_memory_pending ON _enso_memory_turns (received_at, id)
  WHERE processed_at IS NULL AND completed_at IS NOT NULL;
CREATE TABLE _enso_memory_batches (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, turn_ids TEXT NOT NULL,
  recorded_at TEXT, payload_hash TEXT);
CREATE TABLE _enso_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
  summary TEXT NOT NULL, occurred_at TEXT NOT NULL, ended_at TEXT NOT NULL,
  created_at TEXT NOT NULL, workspace TEXT NOT NULL, transport TEXT NOT NULL,
  channel TEXT NOT NULL, channel_name TEXT NOT NULL, thread TEXT, conversation TEXT NOT NULL);
CREATE INDEX _enso_memory_recent ON _enso_memories (occurred_at DESC, id DESC);
CREATE INDEX _enso_memory_workspace ON _enso_memories (workspace, occurred_at DESC, id DESC);
CREATE INDEX _enso_memory_location
  ON _enso_memories (transport, channel, occurred_at DESC, id DESC);
CREATE TABLE _enso_memory_sources (
  memory_id INTEGER NOT NULL, turn_id INTEGER NOT NULL,
  PRIMARY KEY (memory_id, turn_id));
CREATE INDEX _enso_memory_source_turn ON _enso_memory_sources (turn_id);
CREATE TRIGGER _enso_memory_delete_sources AFTER DELETE ON _enso_memories
BEGIN
  DELETE FROM _enso_memory_sources WHERE memory_id = OLD.id;
END;
PRAGMA user_version = 7;
"""

_SCHEMA_V6 = """
CREATE TABLE _enso_workflow_transactions (
  id TEXT PRIMARY KEY, task_ref TEXT NOT NULL, run_id TEXT NOT NULL,
  stage TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX _enso_workflow_task ON _enso_workflow_transactions (task_ref);
CREATE TABLE _enso_workflow_events (
  id TEXT PRIMARY KEY, task_ref TEXT NOT NULL, transaction_id TEXT,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE INDEX _enso_workflow_events_pending ON _enso_workflow_events (status, task_ref);
CREATE TABLE _enso_worktrees (
  ref TEXT PRIMARY KEY, project TEXT NOT NULL, repo TEXT NOT NULL, path TEXT NOT NULL,
  branch TEXT NOT NULL, base TEXT NOT NULL, start_revision TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ready', error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
PRAGMA user_version = 6;
"""

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, job TEXT NOT NULL, workspace TEXT NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
  trigger TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, duration_ms INTEGER,
  status TEXT NOT NULL,
  exit_code INTEGER, output TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS runs_job ON runs (job, started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
  transport TEXT NOT NULL, target TEXT NOT NULL, thread TEXT,
  text TEXT NOT NULL, source TEXT NOT NULL,
  status TEXT NOT NULL, message_id TEXT,
  consumed_at TEXT);
CREATE INDEX IF NOT EXISTS messages_target ON messages (transport, target, thread, consumed_at);

-- A session belongs to the workspace it was created in: Claude and Grok keep the
-- transcript under that directory even when the session is resumed elsewhere.
CREATE TABLE IF NOT EXISTS sessions (
  conversation TEXT NOT NULL, provider TEXT NOT NULL, session_id TEXT NOT NULL,
  workspace TEXT NOT NULL, created_at TEXT NOT NULL, last_active TEXT NOT NULL,
  PRIMARY KEY (conversation, provider));

CREATE TABLE IF NOT EXISTS job_state (
  job TEXT PRIMARY KEY, last_run TEXT,
  failure_fingerprint TEXT, failure_alerted_at TEXT);

CREATE TABLE IF NOT EXISTS _enso_tables (
  table_name TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

PRAGMA user_version = 1;
"""

_SCHEMA_V2 = """
ALTER TABLE runs ADD COLUMN session_id TEXT;
ALTER TABLE runs ADD COLUMN postrun_error TEXT;

CREATE TABLE _enso_run_attempts (
  run_id TEXT NOT NULL, number INTEGER NOT NULL CHECK (number >= 0),
  status TEXT NOT NULL, exit_code INTEGER, output TEXT NOT NULL, error TEXT NOT NULL,
  session_id TEXT, duration_ms INTEGER,
  postrun_exit_code INTEGER, postrun_output TEXT NOT NULL, postrun_error TEXT NOT NULL,
  PRIMARY KEY (run_id, number));

-- A trigger also protects deletes made through ordinary SQLite connections without
-- changing foreign-key enforcement for existing user-authored tables.
CREATE TRIGGER _enso_runs_delete_attempts AFTER DELETE ON runs
BEGIN
  DELETE FROM _enso_run_attempts WHERE run_id = OLD.id;
END;

PRAGMA user_version = 2;
"""

# Tasks, their append-only events, and attached refs. No REFERENCES clauses: Enso does not
# rely on foreign-key enforcement, so integrity lives in ``tasks.py``. Rows are never pruned.
_SCHEMA_V3 = """
CREATE TABLE _enso_tasks (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  number INTEGER NOT NULL CHECK (number > 0),
  ref TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL,
  previous_stage TEXT,
  priority INTEGER NOT NULL DEFAULT 0,
  attention INTEGER NOT NULL DEFAULT 0 CHECK (attention IN (0, 1)),
  after_ref TEXT,
  from_ref TEXT,
  claim_run_id TEXT,
  claim_actor TEXT,
  claim_at TEXT,
  entered_stage_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (project, number),
  CHECK ((claim_run_id IS NULL AND claim_actor IS NULL AND claim_at IS NULL)
      OR (claim_run_id IS NOT NULL AND claim_actor IS NOT NULL AND claim_at IS NOT NULL))
);
CREATE INDEX _enso_tasks_stage ON _enso_tasks (project, stage, priority DESC, created_at, number);
CREATE INDEX _enso_tasks_claim ON _enso_tasks (claim_run_id);

CREATE TABLE _enso_task_events (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  run_id TEXT,
  from_stage TEXT,
  to_stage TEXT,
  message TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX _enso_task_events_task ON _enso_task_events (task_id, id DESC);

CREATE TABLE _enso_task_refs (
  task_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  actor TEXT NOT NULL,
  run_id TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, kind, value)
);

PRAGMA user_version = 3;
"""

# AUTOINCREMENT retains its high-water mark in SQLite's own sequence table after pruning.
# A retired HB reference must never identify a different situation or gate directory.
_SCHEMA_V4 = """
CREATE TABLE _enso_beats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  definition TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'fulfilled', 'cancelled', 'expired')),
  revision INTEGER NOT NULL DEFAULT 1,
  attention INTEGER NOT NULL DEFAULT 0 CHECK (attention IN (0, 1)),
  at_consumed INTEGER NOT NULL DEFAULT 0 CHECK (at_consumed IN (0, 1)),
  checkpoint TEXT NOT NULL DEFAULT '{}',
  handled_event_id INTEGER NOT NULL DEFAULT 0,
  next_check_at TEXT,
  last_check_at TEXT,
  last_check_status TEXT,
  last_check_error TEXT,
  last_success_at TEXT,
  claim_run_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE INDEX _enso_beats_due ON _enso_beats (state, next_check_at);

CREATE TABLE _enso_beat_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  beat_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT NOT NULL,
  run_id TEXT,
  message TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  requires_handling INTEGER NOT NULL DEFAULT 0 CHECK (requires_handling IN (0, 1)),
  action_key TEXT,
  action_status TEXT,
  receipt TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX _enso_beat_events_history ON _enso_beat_events (beat_id, id DESC);
CREATE INDEX _enso_beat_events_actions ON _enso_beat_events (beat_id, action_key, id DESC);

CREATE TABLE _enso_beat_runs (
  id TEXT PRIMARY KEY,
  beat_id INTEGER NOT NULL,
  definition TEXT NOT NULL,
  definition_revision INTEGER NOT NULL,
  input_cutoff INTEGER NOT NULL,
  trigger TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  status TEXT NOT NULL,
  settlement TEXT,
  duration_ms INTEGER,
  exit_code INTEGER,
  session_id TEXT,
  output TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX _enso_beat_runs_history ON _enso_beat_runs (beat_id, started_at DESC, id);
CREATE UNIQUE INDEX _enso_beat_runs_active ON _enso_beat_runs (beat_id)
  WHERE status = 'running';

CREATE TRIGGER _enso_beats_delete_history AFTER DELETE ON _enso_beats
BEGIN
  DELETE FROM _enso_beat_events WHERE beat_id = OLD.id;
  DELETE FROM _enso_beat_runs WHERE beat_id = OLD.id;
END;

PRAGMA user_version = 4;
"""

# Version 5 is the Dandy-to-Enso rename. Tables are renamed in place, which keeps their rows,
# their ``sqlite_sequence`` entries, and the trigger bodies that name them; indexes and
# triggers keep their old names through a table rename, so ``_rename_legacy_objects`` drops
# them and this script recreates them under their Enso names. A database created at this
# version already has every object below, so each statement is conditional.
_LEGACY_PREFIX = "_dandy_"
_SCHEMA_V5 = """
CREATE INDEX IF NOT EXISTS _enso_tasks_stage
  ON _enso_tasks (project, stage, priority DESC, created_at, number);
CREATE INDEX IF NOT EXISTS _enso_tasks_claim ON _enso_tasks (claim_run_id);
CREATE INDEX IF NOT EXISTS _enso_task_events_task ON _enso_task_events (task_id, id DESC);
CREATE INDEX IF NOT EXISTS _enso_beats_due ON _enso_beats (state, next_check_at);
CREATE INDEX IF NOT EXISTS _enso_beat_events_history ON _enso_beat_events (beat_id, id DESC);
CREATE INDEX IF NOT EXISTS _enso_beat_events_actions
  ON _enso_beat_events (beat_id, action_key, id DESC);
CREATE INDEX IF NOT EXISTS _enso_beat_runs_history
  ON _enso_beat_runs (beat_id, started_at DESC, id);
CREATE UNIQUE INDEX IF NOT EXISTS _enso_beat_runs_active ON _enso_beat_runs (beat_id)
  WHERE status = 'running';

CREATE TRIGGER IF NOT EXISTS _enso_runs_delete_attempts AFTER DELETE ON runs
BEGIN
  DELETE FROM _enso_run_attempts WHERE run_id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS _enso_beats_delete_history AFTER DELETE ON _enso_beats
BEGIN
  DELETE FROM _enso_beat_events WHERE beat_id = OLD.id;
  DELETE FROM _enso_beat_runs WHERE beat_id = OLD.id;
END;

PRAGMA user_version = 5;
"""


def _rename_legacy_objects(con: sqlite3.Connection) -> None:
    """Move every ``_dandy_*`` table to its ``_enso_*`` name; drop the indexes and triggers.

    Runs inside the version 5 migration transaction, so an interruption leaves the old
    names in place with the old version. Nothing to do on a database without them.
    """
    rows = con.execute(
        "SELECT type, name FROM sqlite_master WHERE name LIKE ? ESCAPE '\\'"
        " AND type IN ('table', 'index', 'trigger')",
        (_LEGACY_PREFIX.replace("_", "\\_") + "%",),
    ).fetchall()
    # Object names come from sqlite_master, not from input; quoting keeps them literal.
    for row in rows:
        if row["type"] != "table":
            con.execute(f'DROP {row["type"].upper()} "{row["name"]}"')
    for row in rows:
        if row["type"] == "table":
            renamed = "_enso_" + row["name"].removeprefix(_LEGACY_PREFIX)
            con.execute(f'ALTER TABLE "{row["name"]}" RENAME TO "{renamed}"')


class MissingDatabaseError(Exception):
    """``enso.db`` does not exist, or has never been migrated: there is no history yet."""


class UnreadableDatabaseError(Exception):
    """``enso.db`` exists but cannot be read, or its schema is newer than this Enso."""


class UnsupportedDatabaseError(Exception):
    """``enso.db`` is at a schema this Enso does not know, so it must not be written to."""


def now() -> str:
    """UTC timestamp in the form every table stores."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def connect(paths: Paths) -> sqlite3.Connection:
    """Open a WAL connection in autocommit mode; callers own the transaction.

    Raises ``UnsupportedDatabaseError`` when the file is at a schema above this Enso's.
    That check reads ``user_version`` and nothing else, before the journal-mode pragma or
    any statement, so a database a newer Enso wrote is closed again exactly as found: no
    schema, no migration, no conversion to WAL, and no downgrade is ever attempted.
    """
    paths.home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(paths.db, timeout=30, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise UnsupportedDatabaseError(
                f"{paths.db} is schema version {version}; this Enso knows up to "
                f"{SCHEMA_VERSION}: upgrade Enso to the version that wrote it"
            )
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
    except BaseException:
        con.close()
        raise
    return con


@contextmanager
def transaction(paths: Paths) -> Iterator[sqlite3.Connection]:
    """One short-lived connection holding the write lock for the whole transaction."""
    con = connect(paths)
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        # A broken connection cannot roll back, and that secondary error would displace
        # the one that actually explains the failure.
        with suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def read_connect(paths: Paths) -> sqlite3.Connection:
    """A read-only connection to an existing ``enso.db``; never creates, migrates, or writes.

    ``mode=ro`` refuses writes at the file level and ``query_only`` at the statement level,
    so even a bug in a caller cannot change anything. The busy timeout is kept so a reader
    waits for a checkpoint instead of failing.
    """
    if not paths.db.is_file():
        raise MissingDatabaseError(f"{paths.db} does not exist")
    uri = f"file:{quote(str(paths.db))}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        raise UnreadableDatabaseError(f"could not open {paths.db}: {exc}") from exc
    return con


@contextmanager
def reader(paths: Paths) -> Iterator[sqlite3.Connection]:
    """One short-lived read-only connection to a database at a schema this Enso knows.

    Raises ``MissingDatabaseError`` when the file is absent or was never migrated (nothing has
    happened yet) and ``UnreadableDatabaseError`` when it cannot be read or was written by a
    newer Enso. Every other ``sqlite3.Error`` is also reported as ``UnreadableDatabaseError``.
    """
    con = read_connect(paths)
    try:
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as exc:
            raise UnreadableDatabaseError(f"could not read {paths.db}: {exc}") from exc
        if version == 0:
            raise MissingDatabaseError(f"{paths.db} has not been initialised by `enso serve`")
        if version > SCHEMA_VERSION:
            raise UnreadableDatabaseError(
                f"{paths.db} is schema version {version}; this Enso knows up to {SCHEMA_VERSION}"
            )
        try:
            yield con
        except sqlite3.Error as exc:
            raise UnreadableDatabaseError(f"could not read {paths.db}: {exc}") from exc
    finally:
        con.close()


def migrate(paths: Paths) -> None:
    """Create or upgrade the schema; safe to call at every start.

    Raises ``UnsupportedDatabaseError`` when the file was written by a newer Enso, before
    anything about it has changed.
    """
    con = connect(paths)  # refuses anything above SCHEMA_VERSION before this touches it
    try:
        # Check the version under the same lock as the upgrade: concurrent starts cannot
        # both apply a migration, and interruption rolls back its schema and version together.
        con.execute("BEGIN IMMEDIATE")
        version = con.execute("PRAGMA user_version").fetchone()[0]
        for target, schema in (
            (1, _SCHEMA_V1),
            (2, _SCHEMA_V2),
            (3, _SCHEMA_V3),
            (4, _SCHEMA_V4),
            (5, _SCHEMA_V5),
            (6, _SCHEMA_V6),
            (7, _SCHEMA_V7),
        ):
            if version >= target:
                continue
            if target == 5:
                _rename_legacy_objects(con)
            # executescript commits an existing transaction first. Execute complete SQL
            # statements individually so the migration stays atomic, including triggers.
            statement = ""
            for line in schema.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    con.execute(statement)
                    statement = ""
        con.execute("COMMIT")
    except BaseException:
        with suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


# -- Sessions -----------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    conversation: str
    provider: str
    session_id: str
    workspace: str  # the workspace it was created in
    created_at: str
    last_active: str


def _session(row: sqlite3.Row) -> Session:
    return Session(**{key: row[key] for key in Session.__dataclass_fields__})


def get_sessions(paths: Paths, conversation: str) -> list[Session]:
    with transaction(paths) as con:
        rows = con.execute(
            "SELECT * FROM sessions WHERE conversation = ? ORDER BY last_active DESC",
            (conversation,),
        ).fetchall()
    return [_session(row) for row in rows]


def get_session(paths: Paths, conversation: str, provider: str) -> Session | None:
    with transaction(paths) as con:
        row = con.execute(
            "SELECT * FROM sessions WHERE conversation = ? AND provider = ?",
            (conversation, provider),
        ).fetchone()
    return _session(row) if row else None


def set_session(
    paths: Paths, conversation: str, provider: str, session_id: str, workspace: str
) -> None:
    """Insert or replace the provider session for a conversation."""
    stamp = now()
    with transaction(paths) as con:
        con.execute(
            """INSERT INTO sessions
                 (conversation, provider, session_id, workspace, created_at, last_active)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (conversation, provider)
               DO UPDATE SET session_id = excluded.session_id,
                             workspace = excluded.workspace,
                             last_active = excluded.last_active""",
            (conversation, provider, session_id, workspace, stamp, stamp),
        )


def touch_session(paths: Paths, conversation: str, provider: str) -> None:
    """Refresh ``last_active`` so idle pruning measures real use."""
    with transaction(paths) as con:
        con.execute(
            "UPDATE sessions SET last_active = ? WHERE conversation = ? AND provider = ?",
            (now(), conversation, provider),
        )


def delete_session(paths: Paths, conversation: str, provider: str) -> Session | None:
    """Remove one provider's session for a conversation, returning what was there."""
    with transaction(paths) as con:
        row = con.execute(
            "SELECT * FROM sessions WHERE conversation = ? AND provider = ?",
            (conversation, provider),
        ).fetchone()
        con.execute(
            "DELETE FROM sessions WHERE conversation = ? AND provider = ?", (conversation, provider)
        )
    return _session(row) if row else None


def delete_sessions(paths: Paths, conversation: str) -> list[Session]:
    """Remove every provider session for a conversation, returning what was there."""
    with transaction(paths) as con:
        rows = con.execute(
            "SELECT * FROM sessions WHERE conversation = ?", (conversation,)
        ).fetchall()
        con.execute("DELETE FROM sessions WHERE conversation = ?", (conversation,))
    return [_session(row) for row in rows]


def prune_sessions(paths: Paths, max_age_days: int = 30) -> int:
    """Drop sessions idle for longer than ``max_age_days``."""
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    with transaction(paths) as con:
        cursor = con.execute("DELETE FROM sessions WHERE last_active < ?", (cutoff,))
        return cursor.rowcount


# -- Job state ----------------------------------------------------------------


@dataclass(frozen=True)
class JobState:
    """What the scheduler remembers about one job between ticks and restarts."""

    job: str
    last_run: str | None = None  # local ISO timestamp of the last scheduled dispatch
    failure_fingerprint: str | None = None  # the prerun failure last alerted on
    failure_alerted_at: str | None = None


def job_states(paths: Paths) -> dict[str, JobState]:
    with transaction(paths) as con:
        rows = con.execute("SELECT * FROM job_state").fetchall()
    return {
        row["job"]: JobState(**{key: row[key] for key in JobState.__dataclass_fields__})
        for row in rows
    }


def job_state(paths: Paths, job: str) -> JobState:
    return job_states(paths).get(job, JobState(job))


def set_last_run(paths: Paths, job: str, stamp: str) -> None:
    with transaction(paths) as con:
        con.execute(
            """INSERT INTO job_state (job, last_run) VALUES (?, ?)
               ON CONFLICT (job) DO UPDATE SET last_run = excluded.last_run""",
            (job, stamp),
        )


def set_failure(paths: Paths, job: str, fingerprint: str | None, alerted_at: str | None) -> None:
    """Record the prerun failure that was alerted on (or clear it after a recovery)."""
    with transaction(paths) as con:
        con.execute(
            """INSERT INTO job_state (job, failure_fingerprint, failure_alerted_at)
               VALUES (?, ?, ?)
               ON CONFLICT (job) DO UPDATE SET
                 failure_fingerprint = excluded.failure_fingerprint,
                 failure_alerted_at = excluded.failure_alerted_at""",
            (job, fingerprint, alerted_at),
        )
