"""Schema creation, the sessions table, and job state."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from conftest import session_for

from enso import db
from enso.config import Paths


def test_sessions_round_trip_and_prune(enso_home: Paths) -> None:
    db.initialize(enso_home)
    db.initialize(enso_home)  # idempotent
    assert session_for(enso_home, "slack:D1", "claude") is None
    db.set_session(enso_home, "slack:D1", "claude", "s1", "default")
    db.set_session(enso_home, "slack:D1", "codex", "t1", "default")
    db.set_session(enso_home, "slack:D1", "claude", "s2", "other")
    session = session_for(enso_home, "slack:D1", "claude")
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
    db.initialize(enso_home)
    with db.transaction(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 4
        for table in ("sessions", "messages"):
            columns = {row["name"]: row for row in con.execute(f"PRAGMA table_info({table})")}
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
    with pytest.raises(sqlite3.IntegrityError), db.transaction(enso_home) as con:
        con.execute(
            "INSERT INTO messages (created_at, transport, target, text, source, status) "
            "VALUES (?, 'slack', 'C1', 'hello', 'cli', 'sent')",
            (stamp,),
        )
    db.initialize(enso_home)  # a database already at the current version is left alone
    with db.transaction(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_job_state_round_trip(enso_home: Paths) -> None:
    db.initialize(enso_home)
    assert db.job_state(enso_home, "default:nightly") == db.JobState("default", "nightly")
    db.set_last_run(enso_home, "default:nightly", "2026-09-01T09:00:00+00:00")
    db.set_failure(enso_home, "default:nightly", "fp", "2026-09-01T09:01:00+00:00")
    state = db.job_state(enso_home, "default:nightly")
    assert (state.last_run, state.failure_fingerprint) == ("2026-09-01T09:00:00+00:00", "fp")
    db.set_failure(enso_home, "default:nightly", None, None)
    state = db.job_state(enso_home, "default:nightly")
    assert (state.last_run, state.failure_fingerprint, state.failure_alerted_at) == (
        "2026-09-01T09:00:00+00:00",
        None,
        None,
    )
    assert list(db.job_states(enso_home)) == ["default:nightly"]


def test_transaction_holds_the_write_lock_for_its_whole_body(enso_home: Paths) -> None:
    """A deferred BEGIN takes no lock until a statement runs; IMMEDIATE takes it up front.

    Read-then-write callers depend on that: upgrading a deferred transaction under WAL
    fails with SQLITE_BUSY immediately, outside the busy timeout's protection.
    """
    db.initialize(enso_home)
    with db.transaction(enso_home):
        other = sqlite3.connect(enso_home.db, timeout=0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()


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
            f"PRAGMA application_id = {db.APPLICATION_ID};"
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
        lambda: db.initialize(enso_home),
        lambda: db.set_session(enso_home, "slack:D1", "claude", "s1", "default"),
        lambda: db.set_last_run(enso_home, "default:nightly", "2026-09-01T09:00:00+00:00"),
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


@pytest.mark.parametrize("version", [0, 6])
def test_legacy_database_is_refused_without_modifying_it(enso_home, version):
    from enso.config import LEGACY_HOME_MESSAGE

    with sqlite3.connect(enso_home.db) as con:
        con.executescript(
            "CREATE TABLE preserved (value TEXT); INSERT INTO preserved VALUES ('keep');"
            f"PRAGMA user_version = {version};"
        )
    before = enso_home.db.read_bytes()
    with pytest.raises(db.UnsupportedDatabaseError, match=r"predates 0\.2\.0") as error:
        db.initialize(enso_home)
    assert str(error.value) == LEGACY_HOME_MESSAGE
    with pytest.raises(db.UnreadableDatabaseError, match=r"predates 0\.2\.0"), db.reader(enso_home):
        pass
    assert enso_home.db.read_bytes() == before
    assert not enso_home.db.with_name("enso.db-wal").exists()


def test_old_job_directory_is_refused_before_creating_state(enso_home, raw_config):
    from enso.config import LEGACY_HOME_MESSAGE, parse_config
    from enso.initialization import initialize_home

    (enso_home.home / "jobs").mkdir()
    assert parse_config(raw_config, enso_home)[1] == [LEGACY_HOME_MESSAGE]
    assert initialize_home(enso_home)["problems"] == [LEGACY_HOME_MESSAGE]
    with pytest.raises(db.UnsupportedDatabaseError, match=r"predates 0\.2\.0"):
        db.initialize(enso_home)
    assert not enso_home.db.exists()


def test_concurrent_initializers_share_one_fresh_schema(enso_home):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(4)

    def initialize():
        barrier.wait(timeout=5)
        db.initialize(enso_home)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(initialize) for _ in range(4)]
        for future in futures:
            future.result(timeout=10)
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_journal_mode_retries_only_busy_lock_upgrades(enso_home, monkeypatch):
    original = sqlite3.connect
    calls = []

    class Contended(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "PRAGMA journal_mode=WAL":
                calls.append(sql)
                if len(calls) == 1:
                    error = sqlite3.OperationalError("database is locked")
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: original(*a, **k, factory=Contended))
    monkeypatch.setattr(db.time, "sleep", lambda _: None)
    db.initialize(enso_home)
    assert len(calls) == 2
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
