"""Schema creation, the sessions table, and job state."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from enso import db
from enso.config import Paths


def test_sessions_round_trip_and_prune(enso_home: Paths) -> None:
    db.migrate(enso_home)
    db.migrate(enso_home)  # idempotent
    assert db.get_session(enso_home, "slack:D1", "claude") is None
    db.set_session(enso_home, "slack:D1", "claude", "s1", "default")
    db.set_session(enso_home, "slack:D1", "codex", "t1", "default")
    db.set_session(enso_home, "slack:D1", "claude", "s2", "other")
    session = db.get_session(enso_home, "slack:D1", "claude")
    assert session is not None and (session.session_id, session.workspace) == ("s2", "other")
    assert [s.provider for s in db.get_sessions(enso_home, "slack:D1")] == ["claude", "codex"]
    assert db.prune_sessions(enso_home, max_age_days=30) == 0
    with db.transaction(enso_home) as con:
        con.execute("UPDATE sessions SET last_active = '2020-01-01T00:00:00+00:00'")
    db.touch_session(enso_home, "slack:D1", "codex")
    assert db.prune_sessions(enso_home, max_age_days=30) == 1
    assert db.delete_session(enso_home, "slack:D1", "codex") is not None
    assert db.delete_session(enso_home, "slack:D1", "codex") is None
    db.set_session(enso_home, "slack:D1", "claude", "s3", "default")
    assert [s.session_id for s in db.delete_sessions(enso_home, "slack:D1")] == ["s3"]
    assert db.get_sessions(enso_home, "slack:D1") == []


def test_fresh_database_is_created_at_current_schema_version(enso_home: Paths) -> None:
    db.migrate(enso_home)
    with db.transaction(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 7
        objects = {
            row["name"]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        assert objects >= {
            "runs",
            "messages",
            "sessions",
            "job_state",
            "_enso_tables",
            "_enso_run_attempts",
            "_enso_tasks",
            "_enso_task_events",
            "_enso_task_refs",
            "_enso_beats",
            "_enso_beat_events",
            "_enso_beat_runs",
            "_enso_memory_turns",
            "_enso_memory_batches",
            "_enso_memories",
            "_enso_memory_sources",
        }
        assert objects >= {"runs_job", "messages_target"}
        columns = {row["name"]: row for row in con.execute("PRAGMA table_info(sessions)")}
        assert columns["workspace"]["notnull"] == 1
        assert columns["workspace"]["dflt_value"] is None
    # No default means nothing can manufacture a session row without a workspace.
    stamp = "2026-01-01T00:00:00+00:00"
    with pytest.raises(sqlite3.IntegrityError), db.transaction(enso_home) as con:
        con.execute(
            "INSERT INTO sessions (conversation, provider, session_id, created_at, last_active)"
            " VALUES ('slack:D1', 'claude', 's1', ?, ?)",
            (stamp, stamp),
        )
    db.migrate(enso_home)  # a database already at the current version is left alone
    with db.transaction(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_v2_migration_is_atomic_and_preserves_v1_history(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    enso_home.home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(enso_home.db) as con:
        con.executescript(db._SCHEMA_V1)
        con.execute(
            """INSERT INTO runs
                 (id, job, workspace, provider, model, effort, trigger, started_at,
                  status, output, error)
               VALUES ('old', 'nightly', 'default', 'claude', 'sonnet', 'high', 'manual',
                       '2026-09-01T00:00:00+00:00', 'error', 'saved output', 'saved error')"""
        )
        con.execute("CREATE TABLE user_data (note TEXT)")
        con.execute("INSERT INTO user_data VALUES ('preserved')")
    schema = db._SCHEMA_V2
    monkeypatch.setattr(db, "_SCHEMA_V2", schema + "\nTHIS IS INVALID SQL;\n")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(enso_home)
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "session_id" not in {row["name"] for row in con.execute("PRAGMA table_info(runs)")}
        assert con.execute("SELECT output, error FROM runs").fetchone()[:] == (
            "saved output",
            "saved error",
        )
    monkeypatch.setattr(db, "_SCHEMA_V2", schema)
    db.migrate(enso_home)
    db.migrate(enso_home)
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert con.execute("SELECT output, error, session_id, postrun_error FROM runs").fetchone()[
            :
        ] == ("saved output", "saved error", None, None)
        assert con.execute("SELECT note FROM user_data").fetchone()[0] == "preserved"
        assert con.execute("SELECT count(*) FROM _enso_run_attempts").fetchone()[0] == 0


def test_job_state_round_trip(enso_home: Paths) -> None:
    db.migrate(enso_home)
    assert db.job_state(enso_home, "nightly") == db.JobState("nightly")
    db.set_last_run(enso_home, "nightly", "2026-09-01T09:00:00+00:00")
    db.set_failure(enso_home, "nightly", "fp", "2026-09-01T09:01:00+00:00")
    state = db.job_state(enso_home, "nightly")
    assert (state.last_run, state.failure_fingerprint) == ("2026-09-01T09:00:00+00:00", "fp")
    db.set_failure(enso_home, "nightly", None, None)
    state = db.job_state(enso_home, "nightly")
    assert (state.last_run, state.failure_fingerprint, state.failure_alerted_at) == (
        "2026-09-01T09:00:00+00:00",
        None,
        None,
    )
    assert list(db.job_states(enso_home)) == ["nightly"]


def test_v4_migration_rolls_back_atomically_and_preserves_existing_tables(
    enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    with sqlite3.connect(enso_home.db) as con:
        con.executescript(db._SCHEMA_V1 + db._SCHEMA_V2 + db._SCHEMA_V3)
        con.execute("CREATE TABLE user_data (value TEXT)")
        con.execute("INSERT INTO user_data VALUES ('keep me')")
    schema = db._SCHEMA_V4
    monkeypatch.setattr(db, "_SCHEMA_V4", schema + "\nINVALID SQL;\n")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(enso_home)
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
        assert con.execute("SELECT value FROM user_data").fetchone()[0] == "keep me"
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name = '_enso_beats'").fetchone()
    monkeypatch.setattr(db, "_SCHEMA_V4", schema)
    db.migrate(enso_home)
    db.migrate(enso_home)
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert con.execute("SELECT value FROM user_data").fetchone()[0] == "keep me"


def test_transaction_holds_the_write_lock_for_its_whole_body(enso_home: Paths) -> None:
    """A deferred BEGIN takes no lock until a statement runs; IMMEDIATE takes it up front.

    Read-then-write callers depend on that: upgrading a deferred transaction under WAL
    fails with SQLITE_BUSY immediately, outside the busy timeout's protection.
    """
    db.migrate(enso_home)
    with db.transaction(enso_home):
        other = sqlite3.connect(enso_home.db, timeout=0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


def test_failing_transaction_propagates_its_own_exception(enso_home: Paths) -> None:
    """A rollback that cannot run must not replace the failure that explains the problem."""
    db.migrate(enso_home)
    with pytest.raises(RuntimeError, match="the real failure"), db.transaction(enso_home) as con:
        con.close()  # the rollback now fails too
        raise RuntimeError("the real failure")


def _newer_database(paths: Paths) -> dict[str, Any]:
    """A ``enso.db`` a newer Enso wrote: sentinel schema, rows, and a non-WAL journal.

    Journal mode stays ``delete`` so a rejected open that got as far as
    ``PRAGMA journal_mode=WAL`` would show up in the file itself, and so no ``-wal`` or
    ``-shm`` sidecar can predate the attempt.
    """
    paths.home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(paths.db, isolation_level=None)
    try:
        con.executescript(
            "CREATE TABLE later (id INTEGER PRIMARY KEY, note TEXT);"
            "CREATE INDEX later_note ON later (note);"
            "CREATE TRIGGER later_guard AFTER INSERT ON later BEGIN SELECT 1; END;"
            "INSERT INTO later (id, note) VALUES (1, 'from a newer enso');"
            f"PRAGMA user_version = {db.SCHEMA_VERSION + 1};"
        )
    finally:
        con.close()
    return _snapshot(paths)


def _snapshot(paths: Paths) -> dict[str, Any]:
    """Everything about the file a rejected write must leave alone."""
    con = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, isolation_level=None)
    try:
        return {
            "version": con.execute("PRAGMA user_version").fetchone()[0],
            "journal": con.execute("PRAGMA journal_mode").fetchone()[0],
            "objects": sorted(con.execute("SELECT type, name, sql FROM sqlite_master")),
            "rows": con.execute("SELECT id, note FROM later").fetchall(),
            "bytes": paths.db.read_bytes(),
        }
    finally:
        con.close()


def test_a_newer_schema_is_refused_by_every_writable_path(enso_home: Paths) -> None:
    """Downgrading Enso against a newer home fails closed; it never migrates or repairs it."""
    before = _newer_database(enso_home)
    assert before["version"] == db.SCHEMA_VERSION + 1 and before["journal"] == "delete"

    def opened() -> None:
        db.connect(enso_home).close()

    def transacted() -> None:
        with db.transaction(enso_home) as con:
            con.execute("INSERT INTO later (note) VALUES ('should never land')")

    for attempt in (
        opened,
        transacted,
        lambda: db.migrate(enso_home),
        lambda: db.set_session(enso_home, "slack:D1", "claude", "s1", "default"),
        lambda: db.set_last_run(enso_home, "nightly", "2026-09-01T09:00:00+00:00"),
        lambda: db.prune_sessions(enso_home),
        lambda: db.job_states(enso_home),
    ):
        with pytest.raises(db.UnsupportedDatabaseError, match="upgrade Enso"):
            attempt()

    after = _snapshot(enso_home)
    assert after == before  # version, schema, indexes, triggers, rows, journal mode, every byte
    assert {name for _, name, _ in after["objects"]} == {"later", "later_note", "later_guard"}
    # Every rejected connection was closed, so SQLite left no sidecar behind either.
    assert [path.name for path in sorted(enso_home.home.glob("enso.db*"))] == ["enso.db"]


def test_a_newer_schema_stays_readable_for_inspection(enso_home: Paths) -> None:
    """The reader's own forward-version contract is untouched by the writable guard."""
    _newer_database(enso_home)
    version = db.SCHEMA_VERSION + 1
    with (
        pytest.raises(db.UnreadableDatabaseError, match=f"schema version {version}"),
        db.reader(enso_home),
    ):
        pass
    con = db.read_connect(enso_home)
    try:
        assert con.execute("SELECT note FROM later").fetchone()[0] == "from a newer enso"
    finally:
        con.close()


def test_version_5_renames_dandy_tables_and_keeps_their_rows(enso_home: Paths) -> None:
    """A home written before the rename has ``_dandy_*`` tables; migrating keeps every row."""
    enso_home.home.mkdir(parents=True, exist_ok=True)
    legacy = "".join((db._SCHEMA_V1, db._SCHEMA_V2, db._SCHEMA_V3, db._SCHEMA_V4)).replace(
        "_enso_", "_dandy_"
    )
    stamp = "2026-09-01T00:00:00+00:00"
    with sqlite3.connect(enso_home.db) as con:
        con.executescript(legacy)
        con.execute(
            """INSERT INTO runs
                 (id, job, workspace, provider, model, effort, trigger, started_at, status)
               VALUES ('r1', 'nightly', 'default', 'claude', 'sonnet', 'high', 'manual',
                       ?, 'ok')""",
            (stamp,),
        )
        con.execute(
            """INSERT INTO _dandy_run_attempts
                 (run_id, number, status, exit_code, output, error, postrun_output, postrun_error)
               VALUES ('r1', 0, 'ok', 0, '', '', '', '')"""
        )
        con.execute(
            """INSERT INTO _dandy_tasks
                 (project, number, ref, title, stage, entered_stage_at, created_at, updated_at)
               VALUES ('DD', 1, 'DD-001', 'first', 'todo', ?, ?, ?)""",
            (stamp, stamp, stamp),
        )
        con.execute(
            """INSERT INTO _dandy_beats (definition, state, created_at, updated_at)
               VALUES ('{}', 'active', ?, ?)""",
            (stamp, stamp),
        )
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4

    db.migrate(enso_home)
    db.migrate(enso_home)  # nothing left to rename

    with db.transaction(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        names = {
            row["name"]
            for row in con.execute("SELECT name FROM sqlite_master WHERE name LIKE '%dandy%'")
        }
        assert names == set()
        assert con.execute("SELECT ref FROM _enso_tasks").fetchone()["ref"] == "DD-001"
        assert con.execute("SELECT count(*) FROM _enso_run_attempts").fetchone()[0] == 1
        # The AUTOINCREMENT high-water mark moved with the table.
        assert (
            con.execute("SELECT seq FROM sqlite_sequence WHERE name = '_enso_beats'").fetchone()[0]
            == 1
        )
        # The recreated triggers protect the renamed tables.
        con.execute("DELETE FROM runs WHERE id = 'r1'")
        assert con.execute("SELECT count(*) FROM _enso_run_attempts").fetchone()[0] == 0
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
