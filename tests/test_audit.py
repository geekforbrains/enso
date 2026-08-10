"""Tests for the turn-based Slack audit trail (_enso_audit)."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enso import audit


def _turn(**overrides) -> dict:
    fields = {
        "account_id": "T1",
        "delivery_id": "d1",
        "route_id": "slack.channel.C1",
        "channel_id": "C1",
        "thread_id": None,
        "source_message_id": "111.222",
        "conversation_id": "C1:111.222",
        "user_id": "U1",
        "user_name": "alex",
        "groups": ("team",),
        "authorized_groups": ("team",),
        "workspace_id": "acme",
        "binding_revision": "b" * 64,
        "policy_revision": "p" * 64,
        "decision": "accepted",
        "kind": "provider",
        "provider": "claude",
        "model": "opus",
        "request_text": "hello enso",
    }
    fields.update(overrides)
    return fields


def _rows(tmp_enso):
    db = Path(tmp_enso) / "enso.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM _enso_audit")]
    finally:
        conn.close()


# -- create_turn --


def test_accepted_turn_starts_pending(tmp_enso):
    turn_id = audit.create_turn(**_turn())
    (row,) = _rows(tmp_enso)
    assert row["id"] == turn_id
    assert row["decision"] == "accepted"
    assert row["outcome"] == "pending"
    assert row["delivery_status"] == "not_attempted"
    assert row["completed_at"] is None
    assert row["request_text"] == "hello enso"
    assert row["groups_json"] == '["team"]'


def test_ignored_turn_is_terminal_immediately(tmp_enso):
    audit.create_turn(**_turn(decision="ignored", kind=None, provider=None, model=None))
    (row,) = _rows(tmp_enso)
    assert row["outcome"] == "ignored"
    assert row["completed_at"] is not None
    assert row["delivery_status"] == "not_attempted"
    assert row["response_text"] is None


@pytest.mark.parametrize("decision", ["unconfigured", "denied"])
def test_refused_turn_is_blocked_with_response(tmp_enso, decision):
    audit.create_turn(
        **_turn(decision=decision, kind=None), response_text="Route is not configured."
    )
    (row,) = _rows(tmp_enso)
    assert row["outcome"] == "blocked"
    assert row["completed_at"] is not None
    assert row["response_text"] == "Route is not configured."
    assert row["delivery_status"] == "pending"


def test_duplicate_delivery_is_rejected(tmp_enso):
    audit.create_turn(**_turn())
    with pytest.raises(sqlite3.IntegrityError):
        audit.create_turn(**_turn())


def test_create_failure_propagates(tmp_enso, monkeypatch):
    """Audit storage failure must be visible so on_failure can block the turn."""

    def boom(path, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("enso.audit.write_connection", boom)
    with pytest.raises(sqlite3.OperationalError):
        audit.create_turn(**_turn())


# -- response / delivery / completion --


def test_record_response_before_delivery(tmp_enso):
    turn_id = audit.create_turn(**_turn())
    audit.record_response(turn_id, "the answer")
    (row,) = _rows(tmp_enso)
    assert row["response_text"] == "the answer"
    assert row["delivery_status"] == "pending"
    assert row["outcome"] == "pending"  # not terminal yet


def test_record_delivery_outcomes(tmp_enso):
    turn_id = audit.create_turn(**_turn())
    audit.record_response(turn_id, "x")
    audit.record_delivery(turn_id, ok=True)
    (row,) = _rows(tmp_enso)
    assert row["delivery_status"] == "delivered"
    audit.record_delivery(turn_id, ok=False)
    (row,) = _rows(tmp_enso)
    assert row["delivery_status"] == "failed"


def test_complete_turn_sets_terminal_state(tmp_enso):
    turn_id = audit.create_turn(**_turn())
    audit.record_response(turn_id, "done")
    audit.record_delivery(turn_id, ok=True)
    audit.complete_turn(turn_id, "completed")
    (row,) = _rows(tmp_enso)
    assert row["outcome"] == "completed"
    assert row["completed_at"] is not None
    assert row["terminal_reason"] is None


def test_complete_turn_with_reason(tmp_enso):
    turn_id = audit.create_turn(**_turn())
    audit.complete_turn(turn_id, "blocked", terminal_reason="resolution_changed")
    (row,) = _rows(tmp_enso)
    assert row["outcome"] == "blocked"
    assert row["terminal_reason"] == "resolution_changed"


def test_close_abandoned_turn(tmp_enso):
    """Startup closes rows orphaned by a crash as error/service_restart."""
    turn_id = audit.create_turn(**_turn())
    audit.close_abandoned(turn_id)
    (row,) = _rows(tmp_enso)
    assert row["outcome"] == "error"
    assert row["terminal_reason"] == "service_restart"
    assert row["delivery_status"] == "not_attempted"


def test_close_abandoned_preserves_delivered_status(tmp_enso):
    """A reply that reached Slack before the crash stays 'delivered'."""
    turn_id = audit.create_turn(**_turn())
    audit.record_response(turn_id, "answer")
    audit.record_delivery(turn_id, ok=True)
    audit.close_abandoned(turn_id)
    (row,) = _rows(tmp_enso)
    assert row["outcome"] == "error"
    assert row["delivery_status"] == "delivered"


def test_close_all_pending_reaches_unlinked_turns(tmp_enso):
    """Every pending turn is closed at startup, even one no ledger row references."""
    a = audit.create_turn(**_turn(delivery_id="d1"))
    audit.create_turn(**_turn(delivery_id="d2", decision="ignored", kind=None))  # terminal
    audit.create_turn(**_turn(delivery_id="d3"))  # left pending
    audit.complete_turn(a, "completed")
    assert audit.close_all_pending() == 1  # only c was still pending
    rows = {r["delivery_id"]: r for r in _rows(tmp_enso)}
    assert rows["d1"]["outcome"] == "completed"  # already terminal, untouched
    assert rows["d2"]["outcome"] == "ignored"
    assert rows["d3"]["outcome"] == "error"
    assert rows["d3"]["terminal_reason"] == "service_restart"


# -- queries --


def test_get_and_list(tmp_enso):
    turn_id = audit.create_turn(**_turn())
    audit.create_turn(**_turn(delivery_id="d2", user_id="U2", request_text="second"))
    assert audit.get(turn_id)["user_id"] == "U1"
    assert audit.get("missing") is None
    assert len(audit.list_turns()) == 2
    assert [t["user_id"] for t in audit.list_turns(user_id="U2")] == ["U2"]
    assert len(audit.list_turns(limit=1)) == 1


def test_list_without_db_is_empty(tmp_enso):
    assert audit.list_turns() == []


# -- retention --


def _backdate(tmp_enso, turn_id, days):
    db = Path(tmp_enso) / "enso.db"
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE _enso_audit SET received_at=?, completed_at=? WHERE id=?",
        (old, old, turn_id),
    )
    conn.commit()
    conn.close()


def test_prune_drops_old_turns(tmp_enso):
    old_id = audit.create_turn(**_turn())
    audit.complete_turn(old_id, "completed")
    _backdate(tmp_enso, old_id, 400)
    audit.create_turn(**_turn(delivery_id="d2"))
    audit.prune(max_age_days=365)
    assert [r["delivery_id"] for r in _rows(tmp_enso)] == ["d2"]


def test_prune_zero_keeps_everything(tmp_enso):
    turn_id = audit.create_turn(**_turn())
    audit.complete_turn(turn_id, "completed")
    _backdate(tmp_enso, turn_id, 4000)
    audit.prune(max_age_days=0)
    assert len(_rows(tmp_enso)) == 1


def test_db_file_is_private(tmp_enso):
    audit.create_turn(**_turn())
    mode = stat.S_IMODE(os.stat(Path(tmp_enso) / "enso.db").st_mode)
    assert mode == 0o600
