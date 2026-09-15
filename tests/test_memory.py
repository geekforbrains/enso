"""Source identity, atomic refinement, historical recall, and deliberate forgetting."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from enso import db, memory
from enso.config import parse_config


def capture(
    paths,
    *,
    ident="m1",
    workspace="personal",
    received="2026-09-14T15:00:00Z",
    request="Please pause deployment.",
    response="Deployment is paused.",
    **extra,
):
    db.migrate(paths)
    values = dict(
        conversation="slack:C1:thread1",
        workspace=workspace,
        provider="claude",
        model="sonnet",
        effort="high",
        transport="slack",
        channel="C1",
        channel_name="personal",
        thread="thread1",
        message_id=ident,
        user_id="U1",
        user_name="Operator",
        request=request,
        files=[],
        received_at=received,
    )
    values.update(extra)
    turn = memory.start_turn(paths, **values)
    if turn is not None and response is not None:
        memory.finish_turn(paths, turn, response=response)
    return turn


def record(paths, turn_ids, *, batch="batch1", summary="Operator paused deployment."):
    prepared = memory.prepare_batch(paths, batch_id=batch)
    assert prepared is not None
    return memory.record_batch(paths, batch, [{"summary": summary, "source_ids": turn_ids}])[0]


def test_capture_is_deduplicated_and_truncation_is_explicit(enso_home):
    ident = capture(enso_home, request="é" * memory.MAX_TEXT_BYTES)
    assert capture(enso_home, request="changed duplicate") is None
    entry = record(enso_home, [ident])
    source = memory.sources(enso_home, entry.ref)[0]
    assert source.request_truncated and not source.response_truncated
    assert len(source.request.encode()) == memory.MAX_TEXT_BYTES
    assert source.request == "é" * (memory.MAX_TEXT_BYTES // 2)
    assert source.received_at == "2026-09-14T15:00:00.000000+00:00"
    assert source.response == "Deployment is paused."
    assert memory.status(enso_home)["turns"] == 1


def test_atomic_refinement_rejects_cross_context_without_acknowledging_any_source(enso_home):
    one = capture(enso_home)
    two = capture(enso_home, ident="m2", workspace="work")
    memory.prepare_batch(enso_home, batch_id="batch")
    payload = [
        {"summary": "valid first", "source_ids": [one]},
        {"summary": "mixed scopes", "source_ids": [one, two]},
    ]
    with pytest.raises(memory.MemoryError, match="one workspace"):
        memory.record_batch(enso_home, "batch", payload)
    state = memory.status(enso_home)
    assert state["entries"] == 0 and state["pending_turns"] == 2
    assert memory.batch_status(enso_home, "batch")["status"] == "pending"


@pytest.mark.parametrize(
    "bad",
    [
        [{"summary": "Invented", "source_ids": [999]}],
        [{"summary": "Invented", "source_ids": [True]}],
        [{"summary": "Invented", "source_ids": [1, 1]}],
        [{"summary": "Invented", "source_ids": [1], "occurred_at": "2020-01-01"}],
        [{"summary": "", "source_ids": [1]}],
    ],
)
def test_model_cannot_supply_origin_time_or_sources_outside_batch(enso_home, bad):
    capture(enso_home)
    memory.prepare_batch(enso_home, batch_id="batch")
    with pytest.raises(memory.MemoryError):
        memory.record_batch(enso_home, "batch", bad)
    assert memory.status(enso_home)["pending_turns"] == 1


def test_exact_retry_is_idempotent_and_changed_retry_is_rejected(enso_home):
    one = capture(enso_home)
    entry = record(enso_home, [one])
    repeated = memory.record_batch(
        enso_home, "batch1", [{"summary": "Operator paused deployment.", "source_ids": [one]}]
    )
    assert repeated == (entry,)
    with pytest.raises(memory.MemoryError, match="different memories"):
        memory.record_batch(enso_home, "batch1", [{"summary": "changed", "source_ids": [one]}])
    assert memory.status(enso_home)["entries"] == 1
    assert memory.prepare_batch(enso_home, batch_id="next") is None


def test_empty_refinement_acknowledges_only_its_snapshot(enso_home):
    capture(enso_home)
    first = memory.prepare_batch(enso_home, batch_id="first")
    assert first is not None and len(first.turns) == 1
    two = capture(enso_home, ident="m2")
    assert memory.record_batch(enso_home, "first", []) == ()
    assert memory.record_batch(enso_home, "first", []) == ()
    next_batch = memory.prepare_batch(enso_home, batch_id="next")
    assert next_batch is not None and [turn.id for turn in next_batch.turns] == [two]
    assert memory.status(enso_home)["pending_turns"] == 1


def test_abandoned_batch_can_retry_without_losing_inputs(enso_home):
    one = capture(enso_home)
    first = memory.prepare_batch(enso_home, batch_id="failed")
    again = memory.prepare_batch(enso_home, batch_id="failed")
    assert first == again
    later = memory.prepare_batch(enso_home, batch_id="retry")
    assert later is not None and [turn.id for turn in later.turns] == [one]
    with pytest.raises(memory.MemoryError, match="no prepared batch"):
        memory.record_batch(enso_home, "failed", [])
    memory.record_batch(enso_home, "retry", [])
    assert memory.status(enso_home)["pending_turns"] == 0


def test_running_turn_is_not_refined_and_recovery_makes_interruption_visible(enso_home):
    one = capture(enso_home, response=None)
    assert memory.prepare_batch(enso_home, batch_id="before") is None
    assert memory.recover_interrupted(enso_home) == 1
    assert memory.recover_interrupted(enso_home) == 0
    batch = memory.prepare_batch(enso_home, batch_id="after")
    assert batch is not None and batch.turns[0].id == one
    assert batch.turns[0].status == "stopped" and batch.turns[0].completed_at


def test_batch_is_bounded_and_preserves_remaining_inputs(enso_home):
    first = capture(
        enso_home, request="\x00" * memory.MAX_TEXT_BYTES, response="\x00" * memory.MAX_TEXT_BYTES
    )
    second = capture(
        enso_home,
        ident="m2",
        request="\x00" * memory.MAX_TEXT_BYTES,
        response="\x00" * memory.MAX_TEXT_BYTES,
    )
    batch = memory.prepare_batch(enso_home, batch_id="full")
    assert batch is not None and [turn.id for turn in batch.turns] == [first]
    assert len(json.dumps(batch.as_dict(), indent=2).encode()) <= memory.MAX_BATCH_BYTES
    assert batch.turns[0].request_truncated and batch.turns[0].response_truncated
    assert memory.prepare_batch(enso_home, batch_id="full") == batch
    with db.reader(enso_home) as con:
        assert (
            len(
                con.execute(
                    "SELECT request FROM _enso_memory_turns WHERE id=?", (first,)
                ).fetchone()[0]
            )
            == memory.MAX_TEXT_BYTES
        )
    memory.record_batch(enso_home, "full", [])
    next_batch = memory.prepare_batch(enso_home, batch_id="rest")
    assert next_batch is not None and [turn.id for turn in next_batch.turns] == [second]


def test_recall_filters_event_time_not_summary_creation_and_escapes_like(enso_home):
    one = capture(enso_home, received="2026-09-01T12:00:00-07:00")
    older = record(enso_home, [one], batch="old", summary="Keep 10% of plan_A.")
    two = capture(enso_home, ident="m2", received="2026-09-14T15:00:00Z", workspace="work")
    newer = record(enso_home, [two], batch="new", summary="Deploy work release.")
    assert [entry.id for entry in memory.list_entries(enso_home).entries] == [newer.id, older.id]
    assert memory.list_entries(enso_home, workspace="personal").entries == (older,)
    assert memory.list_entries(enso_home, query="10%").entries == (older,)
    assert memory.list_entries(enso_home, query="plan_").entries == (older,)
    assert memory.list_entries(enso_home, query="DEPLOY").entries == (newer,)
    assert memory.list_entries(enso_home, since="2026-09-10T00:00:00Z").entries == (newer,)
    assert memory.list_entries(enso_home, until="2026-09-10T00:00:00Z").entries == (older,)
    assert memory.list_entries(enso_home, channel="missing").total == 0
    page = memory.list_entries(enso_home, limit=1, offset=1)
    assert page.total == 2 and page.entries == (older,)
    assert older.created_at > older.occurred_at
    assert memory.facets(enso_home)["channel_names"] == {"C1": "personal"}


def test_calendar_filters_use_named_timezone_and_dst_boundaries():
    now = datetime(2026, 3, 10, 0, 0, tzinfo=UTC)
    # Monday starts after the Vancouver spring-forward transition, UTC-7.
    assert (
        memory.parse_since("week", "America/Vancouver", now) == "2026-03-09T07:00:00.000000+00:00"
    )
    # First of the month precedes it, UTC-8.
    assert (
        memory.parse_since("month", "America/Vancouver", now) == "2026-03-01T08:00:00.000000+00:00"
    )
    assert (
        memory.parse_since("2026-03-01", "America/Vancouver", now)
        == "2026-03-01T08:00:00.000000+00:00"
    )
    assert memory.parse_since("7d", "America/Vancouver", now) == "2026-03-03T00:00:00.000000+00:00"
    with pytest.raises(memory.MemoryError, match="timezone"):
        memory.parse_since("2026-03-01T12:00:00", "America/Vancouver", now)
    with pytest.raises(memory.MemoryError, match="too large"):
        memory.parse_since("999999w", "America/Vancouver", now)


def test_forget_removes_shared_sources_and_prevents_old_batch_replay(enso_home):
    one = capture(enso_home)
    two = capture(enso_home, ident="m2")
    memory.prepare_batch(enso_home, batch_id="shared")
    payload = [
        {"summary": "one", "source_ids": [one]},
        {"summary": "both", "source_ids": [one, two]},
        {"summary": "two", "source_ids": [two]},
    ]
    entries = memory.record_batch(enso_home, "shared", payload)
    assert memory.forget(enso_home, entries[0].ref)
    assert not memory.forget(enso_home, entries[0].ref)
    assert memory.status(enso_home)["entries"] == 0
    assert memory.status(enso_home)["turns"] == 0
    assert memory.record_batch(enso_home, "shared", payload) == ()
    assert memory.prepare_batch(enso_home, batch_id="later") is None
    next_turn = capture(enso_home, ident="m3")
    next_entry = record(enso_home, [next_turn], batch="new")
    assert next_entry.id > entries[-1].id


def test_reads_do_not_create_or_upgrade_a_database(enso_home):
    assert memory.list_entries(enso_home).total == 0
    assert memory.get_entry(enso_home, "MEM-1") is None
    assert not enso_home.db.exists()
    with sqlite3.connect(enso_home.db) as con:
        con.executescript(db._SCHEMA_V1)
    assert memory.status(enso_home)["entries"] == 0
    with sqlite3.connect(enso_home.db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1


def test_out_of_range_offset_has_a_domain_error(enso_home):
    with pytest.raises(memory.MemoryError, match="offset"):
        memory.list_entries(enso_home, offset=10**25)


def test_memory_migration_is_atomic_and_preserves_prior_data(enso_home, monkeypatch):
    schema = db._SCHEMA_V7
    with sqlite3.connect(enso_home.db) as con:
        con.executescript(
            db._SCHEMA_V1
            + db._SCHEMA_V2
            + db._SCHEMA_V3
            + db._SCHEMA_V4
            + db._SCHEMA_V5
            + db._SCHEMA_V6
        )
        con.execute("CREATE TABLE personal_records (value TEXT)")
        con.execute("INSERT INTO personal_records VALUES ('preserved')")
    monkeypatch.setattr(db, "_SCHEMA_V7", schema + "\nINVALID SQL;\n")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(enso_home)
    with db.reader(enso_home) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 6
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='_enso_memories'").fetchone()
    monkeypatch.setattr(db, "_SCHEMA_V7", schema)
    db.migrate(enso_home)
    db.migrate(enso_home)
    with db.reader(enso_home) as con:
        assert con.execute("SELECT value FROM personal_records").fetchone()[0] == "preserved"


def test_memory_configuration_is_validated(enso_home, raw_config):
    raw_config["memory"] = {"enabled": False, "timezone": "America/Vancouver"}
    config, problems, _ = parse_config(raw_config, enso_home)
    assert not problems and config is not None and not config.memory.enabled
    raw_config["memory"] = {"enabled": "false", "timezone": "Not/AZone"}
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and len([p for p in problems if p.startswith("memory.")]) == 2
