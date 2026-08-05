"""Tests for the SQLite-backed run history (enso.runs)."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from enso import runs


def _backdate(run_id: str, days: int) -> None:
    """Rewrite a run's started_at to ``days`` in the past (UTC)."""
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with sqlite3.connect(runs._db_path()) as conn:
        conn.execute("UPDATE runs SET started_at=? WHERE id=?", (old, run_id))


def _install_failing_run_insert(monkeypatch):
    """Make run insertion fail after SQLite has opened a write transaction."""
    real_connect = sqlite3.connect
    opened = []

    class FailingInsertConnection(sqlite3.Connection):
        was_closed = False
        in_transaction_at_close = None

        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.lstrip().upper().startswith("INSERT INTO RUNS"):
                raise sqlite3.OperationalError("injected run insert failure")
            return cursor

        def close(self):
            self.in_transaction_at_close = self.in_transaction
            self.was_closed = True
            super().close()

    def failing_connect(*args, **kwargs):
        kwargs["factory"] = FailingInsertConnection
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(runs.sqlite3, "connect", failing_connect)
    return real_connect, opened


def test_database_connections_are_operation_scoped_and_closed(tmp_enso, monkeypatch):
    """Each operation owns a fresh connection and closes it before returning."""
    real_connect = sqlite3.connect
    opened = []
    closed = set()

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.add(id(self))
            super().close()

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(runs.sqlite3, "connect", tracking_connect)

    before = len(opened)
    run_id = runs.create("job", "operation-scoped")
    created_connections = opened[before:]
    assert created_connections
    assert all(id(conn) in closed for conn in created_connections)

    before = len(opened)
    assert runs.get(run_id)["name"] == "operation-scoped"
    read_connections = opened[before:]
    assert read_connections
    assert all(id(conn) in closed for conn in read_connections)

    before = len(opened)
    assert [row["id"] for row in runs.list_runs()] == [run_id]
    list_connections = opened[before:]
    assert list_connections
    assert all(id(conn) in closed for conn in list_connections)


def test_worker_threads_do_not_share_run_database_connections(tmp_enso, monkeypatch):
    """A connection is created, used, and closed within one calling thread."""
    real_connect = sqlite3.connect
    created_on = {}
    selected_on = []
    closed = set()
    records_lock = threading.Lock()

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.lstrip().upper().startswith("SELECT * FROM RUNS"):
                with records_lock:
                    selected_on.append((id(self), threading.get_ident()))
            return super().execute(sql, parameters)

        def close(self):
            with records_lock:
                closed.add(id(self))
            super().close()

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        conn = real_connect(*args, **kwargs)
        with records_lock:
            created_on[id(conn)] = threading.get_ident()
        return conn

    monkeypatch.setattr(runs.sqlite3, "connect", tracking_connect)
    run_id = runs.create("job", "thread-owned")

    # Keep the calls sequential so this regression test diagnoses connection
    # ownership without invoking undefined concurrent access on the old shared
    # connection implementation.
    with ThreadPoolExecutor(max_workers=1) as pool:
        get_future = pool.submit(runs.get, run_id)
        assert get_future.result()["id"] == run_id
        list_future = pool.submit(runs.list_runs, limit=1)
        assert list_future.result()[0]["id"] == run_id

    assert len(selected_on) == 2
    assert len({connection_id for connection_id, _ in selected_on}) == 2
    assert all(created_on[connection_id] == thread_id for connection_id, thread_id in selected_on)
    assert all(connection_id in closed for connection_id, _ in selected_on)


def test_concurrent_first_writes_retry_wal_initialization_busy(tmp_enso, monkeypatch):
    """Both first writers survive a transient WAL-mode initialization race."""
    real_connect = sqlite3.connect
    initial_mode_reads = threading.Barrier(2)
    wal_initialized = threading.Event()
    state_lock = threading.Lock()
    mode_read_count = 0
    wal_attempt_count = 0

    class ConcurrentInitConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal mode_read_count, wal_attempt_count
            normalized = " ".join(sql.upper().split())
            if normalized == "PRAGMA JOURNAL_MODE":
                cursor = super().execute(sql, parameters)
                with state_lock:
                    mode_read_count += 1
                    synchronize = mode_read_count <= 2
                if synchronize:
                    initial_mode_reads.wait(timeout=2)
                return cursor
            if normalized == "PRAGMA JOURNAL_MODE=WAL":
                with state_lock:
                    wal_attempt_count += 1
                    attempt = wal_attempt_count
                if attempt == 1:
                    assert wal_initialized.wait(timeout=2)
                    error = sqlite3.OperationalError("database is locked")
                    error.sqlite_errorcode = 5  # SQLITE_BUSY; constant is absent on Python 3.10.
                    error.sqlite_errorname = "SQLITE_BUSY"
                    raise error
                cursor = super().execute(sql, parameters)
                wal_initialized.set()
                return cursor
            return super().execute(sql, parameters)

    def concurrent_connect(*args, **kwargs):
        kwargs["factory"] = ConcurrentInitConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(runs.sqlite3, "connect", concurrent_connect)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runs.create, "job", f"first-{index}") for index in range(2)]
        run_ids = [future.result(timeout=3) for future in futures]

    assert len(run_ids) == 2
    assert {row["id"] for row in runs.list_runs()} == set(run_ids)


def test_failed_write_rolls_back_and_closes_its_connection(tmp_enso, monkeypatch):
    """A write exception cannot leave an open transaction behind."""
    _, opened = _install_failing_run_insert(monkeypatch)

    with pytest.raises(sqlite3.OperationalError, match="injected run insert failure"):
        runs.create("job", "failed-write")

    failed_connection = opened[-1]
    assert failed_connection.was_closed
    assert failed_connection.in_transaction_at_close is False


def test_failed_write_does_not_retain_database_lock(tmp_enso, monkeypatch):
    """Another writer can proceed immediately after a run write fails."""
    real_connect, _ = _install_failing_run_insert(monkeypatch)

    with pytest.raises(sqlite3.OperationalError, match="injected run insert failure"):
        runs.create("job", "failed-write")

    with real_connect(runs._db_path(), timeout=0) as probe:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()


def test_reads_and_writes_use_distinct_bounded_busy_timeouts(tmp_enso, monkeypatch):
    """Reads fail quickly while telemetry writes tolerate brief contention."""
    real_connect = sqlite3.connect
    connection_records = []

    class TimeoutTrackingConnection(sqlite3.Connection):
        record = None

        def close(self):
            self.record["busy_timeout_ms"] = super().execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0]
            super().close()

    def tracking_connect(*args, **kwargs):
        timeout = kwargs.get("timeout")
        kwargs["factory"] = TimeoutTrackingConnection
        conn = real_connect(*args, **kwargs)
        record = {"timeout_seconds": timeout, "busy_timeout_ms": None}
        conn.record = record
        connection_records.append(record)
        return conn

    monkeypatch.setattr(runs.sqlite3, "connect", tracking_connect)

    before = len(connection_records)
    run_id = runs.create("job", "timeouts")
    write_records = connection_records[before:]
    assert write_records
    assert all(record == {
        "timeout_seconds": 5.0,
        "busy_timeout_ms": 5000,
    } for record in write_records)

    before = len(connection_records)
    assert runs.get(run_id) is not None
    read_records = connection_records[before:]
    assert read_records
    assert all(record == {
        "timeout_seconds": 0.5,
        "busy_timeout_ms": 500,
    } for record in read_records)


def test_database_and_wal_files_are_created_private(tmp_enso):
    previous_umask = os.umask(0)
    try:
        runs.create("job", "private-store")
    finally:
        os.umask(previous_umask)

    database = runs._db_path()
    assert os.path.isfile(database)
    sqlite_files = [database, f"{database}-wal", f"{database}-shm"]
    existing_files = [path for path in sqlite_files if os.path.exists(path)]
    assert all(os.path.isfile(path) for path in existing_files)
    assert all(stat.S_IMODE(os.stat(path).st_mode) == 0o600 for path in existing_files)


def test_create_finish_lifecycle(tmp_enso):
    """A created run starts 'running' and finish fills terminal fields."""
    run_id = runs.create(
        "job", "digest", title="Daily Digest", trigger="schedule",
        provider="claude", model="sonnet",
    )
    assert isinstance(run_id, str) and len(run_id) == 32

    row = runs.get(run_id)
    assert row is not None
    assert row["kind"] == "job"
    assert row["name"] == "digest"
    assert row["title"] == "Daily Digest"
    assert row["trigger"] == "schedule"
    assert row["provider"] == "claude"
    assert row["model"] == "sonnet"
    assert row["status"] == "running"
    assert row["started_at"]
    assert row["ended_at"] is None
    assert row["exit_code"] is None
    assert row["duration_ms"] is None

    runs.append_output(run_id, "hello ")
    runs.append_output(run_id, "world\n")
    runs.finish(run_id, exit_code=0, status="ok")

    row = runs.get(run_id)
    assert row["status"] == "ok"
    assert row["exit_code"] == 0
    assert row["ended_at"]
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    assert row["output_bytes"] == len("hello world\n")
    assert row["output_path"] == runs.log_path(run_id)


def test_get_unknown_returns_none(tmp_enso):
    """get() returns None for an id that was never created."""
    assert runs.get("deadbeef") is None


def test_finish_defaults_provider_model_none(tmp_enso):
    """Optional provider/model default to NULL and status flows to error."""
    run_id = runs.create("job", "research-ofx")
    row = runs.get(run_id)
    assert row["trigger"] == "manual"
    assert row["provider"] is None
    assert row["model"] is None
    assert row["title"] is None

    runs.finish(run_id, exit_code=2, status="error")
    row = runs.get(run_id)
    assert row["status"] == "error"
    assert row["exit_code"] == 2
    # No output was written, so output stays unrecorded.
    assert row["output_bytes"] is None
    assert row["output_path"] is None


def test_create_accepts_original_start_time(tmp_enso):
    """Callers can include work that happened before the run row was created."""
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    run_id = runs.create("job", "gated", started_at=started_at)

    runs.finish(run_id, exit_code=2, status="prerun_error")

    row = runs.get(run_id)
    assert row["started_at"] == started_at
    assert row["duration_ms"] >= 1900


def test_list_filters_and_ordering(tmp_enso):
    """list_runs filters by kind/name/status and returns newest first."""
    a = runs.create("job", "alpha", trigger="schedule")
    b = runs.create("job", "beta", trigger="schedule")
    c = runs.create("chat", "alpha", trigger="manual")
    runs.finish(b, exit_code=0, status="ok")

    # Newest first: c, b, a (insertion order).
    ids = [r["id"] for r in runs.list_runs()]
    assert ids == [c, b, a]

    job_ids = {r["id"] for r in runs.list_runs(kind="job")}
    assert job_ids == {a, b}

    alpha_ids = {r["id"] for r in runs.list_runs(name="alpha")}
    assert alpha_ids == {a, c}

    ok_ids = [r["id"] for r in runs.list_runs(status="ok")]
    assert ok_ids == [b]

    # Combined filter + limit.
    combined = runs.list_runs(kind="chat", name="alpha")
    assert [r["id"] for r in combined] == [c]

    limited = runs.list_runs(limit=1)
    assert [r["id"] for r in limited] == [c]

    second = runs.list_runs(limit=1, offset=1)
    assert [r["id"] for r in second] == [b]


def test_output_append_and_read(tmp_enso):
    """Output appends accumulate; read honours max_bytes."""
    run_id = runs.create("job", "notes")
    assert runs.read_output(run_id) == ""  # no log yet

    runs.append_output(run_id, "line one\n")
    runs.append_output(run_id, "line two\n")
    assert runs.read_output(run_id) == "line one\nline two\n"
    assert runs.read_output(run_id, max_bytes=8) == "line one"

    # output_path is recorded on first append even before finish.
    assert runs.get(run_id)["output_path"] == runs.log_path(run_id)


def test_prune_by_count(tmp_enso):
    """prune(keep=N) drops the oldest terminal rows and their logs."""
    ids = []
    for i in range(5):
        rid = runs.create("job", "spammy", trigger="schedule")
        runs.append_output(rid, f"run {i}\n")
        runs.finish(rid, exit_code=0, status="ok")
        ids.append(rid)

    logs = [runs.log_path(rid) for rid in ids]
    assert all(os.path.exists(p) for p in logs)

    runs.prune(keep=2, max_age_days=9999)

    remaining = [r["id"] for r in runs.list_runs()]
    assert remaining == [ids[4], ids[3]]  # newest two kept

    for rid in ids[:3]:
        assert not os.path.exists(runs.log_path(rid))  # pruned logs unlinked
    for rid in ids[3:]:
        assert os.path.exists(runs.log_path(rid))


def test_prune_by_age(tmp_enso):
    """prune(max_age_days) drops rows older than the cutoff, keeps fresh ones."""
    old = runs.create("job", "old", trigger="schedule")
    runs.finish(old, exit_code=0, status="ok")
    _backdate(old, days=60)

    fresh = runs.create("job", "fresh", trigger="schedule")
    runs.finish(fresh, exit_code=0, status="ok")

    runs.prune(keep=500, max_age_days=30)

    ids = {r["id"] for r in runs.list_runs()}
    assert ids == {fresh}
    assert runs.get(old) is None


def test_prune_never_deletes_running(tmp_enso):
    """A running row is retained even when it violates both caps."""
    running = runs.create("job", "stuck", trigger="schedule")
    _backdate(running, days=999)

    # Also add finished rows to blow past the count cap.
    for _ in range(3):
        rid = runs.create("job", "done", trigger="schedule")
        runs.finish(rid, exit_code=0, status="ok")

    runs.prune(keep=0, max_age_days=1)

    assert runs.get(running) is not None
    assert runs.get(running)["status"] == "running"
    # All terminal rows were pruned.
    assert [r["id"] for r in runs.list_runs()] == [running]
