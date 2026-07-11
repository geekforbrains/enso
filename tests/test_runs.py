"""Tests for the SQLite-backed run history (enso.runs)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from enso import runs


def _backdate(run_id: str, days: int) -> None:
    """Rewrite a run's started_at to ``days`` in the past (UTC)."""
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = runs._connect()
    conn.execute("UPDATE runs SET started_at=? WHERE id=?", (old, run_id))
    conn.commit()


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
