"""Tests for the Slack delivery ledger (_enso_slack_events)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from enso import ledger


def _rows(tmp_enso):
    db = Path(tmp_enso) / "enso.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM _enso_slack_events")]
    finally:
        conn.close()


# -- delivery_id --


def test_delivery_id_is_sha256_hex():
    did = ledger.delivery_id("T1", "C1", "123.456")
    assert len(did) == 64
    assert did == ledger.delivery_id("T1", "C1", "123.456")


def test_delivery_id_identical_for_both_event_types():
    """message and app_mention for one Slack message share account/channel/ts."""
    assert ledger.delivery_id("T1", "C1", "1.2") == ledger.delivery_id("T1", "C1", "1.2")


@pytest.mark.parametrize(
    "a, b",
    [
        (("T1", "C1", "1.2"), ("T2", "C1", "1.2")),
        (("T1", "C1", "1.2"), ("T1", "C2", "1.2")),
        (("T1", "C1", "1.2"), ("T1", "C1", "1.3")),
        # Length-prefixing: shifting a boundary must not collide.
        (("T1", "C1ab", "1.2"), ("T1", "C1", "ab1.2")),
    ],
)
def test_delivery_id_distinct(a, b):
    assert ledger.delivery_id(*a) != ledger.delivery_id(*b)


# -- claim / complete --


def test_first_claim_succeeds(tmp_enso):
    assert ledger.claim("T1", "d1") is True
    (row,) = _rows(tmp_enso)
    assert row["status"] == "pending"
    assert row["completed_at"] is None


def test_duplicate_claim_is_rejected(tmp_enso):
    assert ledger.claim("T1", "d1") is True
    assert ledger.claim("T1", "d1") is False


def test_claims_are_scoped_by_account(tmp_enso):
    assert ledger.claim("T1", "d1") is True
    assert ledger.claim("T2", "d1") is True


def test_complete_marks_claim_terminal(tmp_enso):
    ledger.claim("T1", "d1")
    ledger.complete("T1", "d1", audit_turn_id="turn9")
    (row,) = _rows(tmp_enso)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert row["audit_turn_id"] == "turn9"


def test_completed_claim_still_blocks_retry(tmp_enso):
    ledger.claim("T1", "d1")
    ledger.complete("T1", "d1")
    assert ledger.claim("T1", "d1") is False


def test_claim_failure_propagates(tmp_enso, monkeypatch):
    """A ledger failure must block execution, never degrade to at-least-once."""

    def boom(path, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("enso.ledger.write_connection", boom)
    with pytest.raises(sqlite3.OperationalError):
        ledger.claim("T1", "d1")


# -- crash recovery --


def test_abandon_pending_marks_crashed_claims(tmp_enso):
    ledger.claim("T1", "d1")
    ledger.claim("T1", "d2")
    ledger.complete("T1", "d2")
    abandoned = ledger.abandon_pending()
    assert [(r["account_id"], r["delivery_id"]) for r in abandoned] == [("T1", "d1")]
    rows = {r["delivery_id"]: r["status"] for r in _rows(tmp_enso)}
    assert rows == {"d1": "abandoned", "d2": "completed"}


def test_abandoned_claim_suppresses_retry(tmp_enso):
    ledger.claim("T1", "d1")
    ledger.abandon_pending()
    assert ledger.claim("T1", "d1") is False


def test_abandon_pending_returns_audit_link(tmp_enso):
    ledger.claim("T1", "d1")
    ledger.link_audit_turn("T1", "d1", "turn5")
    (row,) = ledger.abandon_pending()
    assert row["audit_turn_id"] == "turn5"


def test_abandon_pending_without_db_is_noop(tmp_enso):
    assert ledger.abandon_pending() == []


# -- prune --


def test_prune_removes_old_claims(tmp_enso):
    ledger.claim("T1", "old")
    ledger.complete("T1", "old")
    ledger.claim("T1", "new")
    db = Path(tmp_enso) / "enso.db"
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE _enso_slack_events SET received_at=? WHERE delivery_id='old'", (old,)
    )
    conn.commit()
    conn.close()
    ledger.prune()
    assert [r["delivery_id"] for r in _rows(tmp_enso)] == ["new"]


def test_prune_without_db_is_noop(tmp_enso):
    ledger.prune()
