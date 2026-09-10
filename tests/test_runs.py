"""The runs table: lifecycle, prefix lookup, listing, retention."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from conftest import load_job, write_job

from enso import db, runs
from enso.config import Config, Paths


def test_attempt_history_is_durable_bounded_and_retained_with_its_run(
    enso_home: Paths, config: Config
) -> None:
    db.migrate(enso_home)
    write_job(enso_home)
    run_id = runs.start(enso_home, load_job(enso_home, config), "manual", effort="high")
    attempt = {
        "number": 1,
        "status": "ok",
        "exit_code": 0,
        "output": "discard" + "x" * runs.OUTPUT_KEEP,
        "error": "",
        "session_id": "same-session",
        "duration_ms": 12,
    }
    runs.record_attempt(enso_home, run_id, **attempt)
    running = runs.get(enso_home, run_id)
    assert running is not None and running.status == "running" and running.ended_at is None
    saved = runs.attempts(enso_home, run_id)
    assert len(saved) == 1 and saved[0].output == "x" * runs.OUTPUT_KEEP
    assert saved[0].session_id == "same-session" and saved[0].postrun_exit_code is None
    runs.record_attempt(
        enso_home,
        run_id,
        **attempt,
        postrun_exit_code=10,
        postrun_output="Commit the changes.",
        postrun_error="",
    )
    runs.record_attempt(
        enso_home, run_id, **(attempt | {"number": 2, "output": "Committed."}), postrun_exit_code=0
    )
    saved = runs.attempts(enso_home, run_id)
    assert [item.number for item in saved] == [1, 2]
    assert saved[0].postrun_output == "Commit the changes."
    assert saved[0].duration_ms == 12 and saved[1].postrun_exit_code == 0
    assert "attempts" not in runs.list_runs(enso_home)[0].as_dict()
    assert "attempts" not in runs.list_summaries(enso_home)[0].as_dict()
    runs.finish(
        enso_home,
        run_id,
        status="error",
        output="Committed.",
        session_id="same-session",
        error="Validation failed",
        postrun_error="Validation failed",
    )
    final = runs.get(enso_home, run_id)
    assert final is not None and final.session_id == "same-session"
    assert final.postrun_error == "Validation failed" and final.ended_at is not None
    assert runs.prune(enso_home, keep=0, max_age_days=30) == 1
    assert runs.attempts(enso_home, run_id) == []
    runs.record_attempt(enso_home, run_id, **attempt)
    assert runs.attempts(enso_home, run_id) == []  # a late writer cannot leave an orphan


def test_attempt_zero_and_plain_sqlite_deletion(enso_home: Paths, config: Config) -> None:
    db.migrate(enso_home)
    write_job(enso_home)
    run_id = runs.start(enso_home, load_job(enso_home, config), "manual", effort="high")
    runs.record_attempt(
        enso_home,
        run_id,
        number=0,
        status="no_work",
        exit_code=None,
        output="",
        error="",
        session_id=None,
        postrun_exit_code=2,
        postrun_error="cleanup failed",
    )
    assert runs.attempts(enso_home, run_id)[0].number == 0
    # SQLite clients do not have to enable foreign-key enforcement for cleanup to work.
    with sqlite3.connect(enso_home.db) as con:
        con.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        assert con.execute("SELECT count(*) FROM _enso_run_attempts").fetchone()[0] == 0


def test_v1_history_can_be_read_without_migration(enso_home: Paths) -> None:
    assert runs.attempts(enso_home, "old") == [] and not enso_home.db.exists()
    enso_home.home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(enso_home.db) as con:
        con.executescript(db._SCHEMA_V1)
        con.execute(
            """INSERT INTO runs
                 (id, job, workspace, provider, model, effort, trigger, started_at, status, output)
               VALUES ('old', 'nightly', 'default', 'claude', 'sonnet', 'high', 'manual',
                       '2026-09-01T00:00:00+00:00', 'ok', 'saved output')"""
        )
    old = runs.get(enso_home, "old")
    assert old is not None and old.output == "saved output"
    assert old.session_id is None and old.postrun_error is None
    assert runs.attempts(enso_home, "old") == []
    assert runs.list_summaries(enso_home)[0].id == "old"
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1


def test_run_lifecycle_and_retention(enso_home: Paths, config: Config) -> None:
    db.migrate(enso_home)
    write_job(enso_home)
    job = load_job(enso_home, config)

    before = datetime.now(UTC)
    first = runs.start(enso_home, job, "manual", effort="low")  # not the job's ``high``
    started = runs.get(enso_home, first)
    assert started is not None and (started.status, started.trigger, started.effort) == (
        "running",
        "manual",
        "low",
    )
    assert len(first) == runs.ID_LENGTH and started.ended_at is None
    runs.finish(enso_home, first, status="ok", exit_code=0, output="x" * (runs.OUTPUT_KEEP + 5))
    after = datetime.now(UTC)
    run = runs.get(enso_home, first[:6])
    assert run is not None and (run.status, run.exit_code, run.error) == ("ok", 0, None)
    assert run.output is not None and len(run.output) == runs.OUTPUT_KEEP
    assert run.ended_at is not None
    assert before <= datetime.fromisoformat(run.started_at) <= datetime.fromisoformat(run.ended_at)
    assert datetime.fromisoformat(run.ended_at) <= after
    assert run.duration_ms is not None
    assert run.duration_ms <= int((after - before).total_seconds() * 1000)

    second = runs.start(enso_home, job, "schedule", effort=job.effort)
    with db.transaction(enso_home) as con:  # a clock step backwards must not go negative
        future = (datetime.now(UTC) + timedelta(seconds=10)).isoformat(timespec="microseconds")
        con.execute("UPDATE runs SET started_at = ? WHERE id = ?", (future, second))
    runs.finish(enso_home, second, status="error", exit_code=1, error="boom")
    errored = runs.get(enso_home, second)
    assert errored is not None and errored.duration_ms == 0
    assert not runs.abandon(enso_home, second, "late")  # a closed row keeps its outcome
    assert [r.id for r in runs.list_runs(enso_home)] == [second, first]
    assert [r.id for r in runs.list_runs(enso_home, job="nightly", limit=1)] == [second]
    assert runs.list_runs(enso_home, job="other") == []
    assert runs.latest(enso_home)["nightly"].id == second
    assert runs.get(enso_home, "nope") is None

    running = runs.start(enso_home, job, "schedule", effort=job.effort)
    assert runs.prune(enso_home, keep=1, max_age_days=30) == 1
    assert {r.id for r in runs.list_runs(enso_home)} == {second, running}
    with db.transaction(enso_home) as con:
        con.execute("UPDATE runs SET started_at = '2020-01-01T00:00:00+00:00'")
    assert runs.prune(enso_home, keep=10, max_age_days=30) == 1  # the running row stays
    assert [r.id for r in runs.list_runs(enso_home)] == [running]
    assert runs.abandon(enso_home, running, "interrupted")
    orphan = runs.get(enso_home, running)
    assert orphan is not None and (orphan.status, orphan.error, orphan.ended_at) == (
        "error",
        "interrupted",
        None,
    )


def test_prefix_lookup_treats_wildcards_as_literal(enso_home: Paths, config: Config) -> None:
    """An id is matched as literal text: ``%``, ``_``, and ``\\`` are never wildcards."""
    db.migrate(enso_home)
    write_job(enso_home)
    job = load_job(enso_home, config)

    first = runs.start(enso_home, job, "manual", effort="low")
    with db.transaction(enso_home) as con:  # a known id, so the substitutions are exact
        con.execute("UPDATE runs SET id = 'abcdef012345' WHERE id = ?", (first,))

    # One row stored, so an ambiguous match cannot mask a wildcard that matched it.
    assert runs.get(enso_home, "%") is None
    assert runs.get(enso_home, "abc_ef") is None
    assert runs.get(enso_home, "\\") is None  # the escape character is literal too
    assert runs.get(enso_home, "abcdef012345") is not None
    assert runs.get(enso_home, "abcdef0") is not None

    second = runs.start(enso_home, job, "manual", effort="low")
    with db.transaction(enso_home) as con:
        con.execute("UPDATE runs SET id = 'abcdef987654' WHERE id = ?", (second,))
    assert runs.get(enso_home, "abcdef") is None  # still ambiguous
    run = runs.get(enso_home, "abcdef0")
    assert run is not None and run.id == "abcdef012345"
