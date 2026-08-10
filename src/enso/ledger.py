"""Slack delivery ledger — at-most-once dispatch, independent of auditing.

Slack routing claims every inbound delivery in ``_enso_slack_events``
before routing. Both event types for one message (``message`` and
``app_mention``) and every Slack retry derive the same opaque ``delivery_id``,
so a duplicate claim acknowledges the retry without executing anything twice.
Failure to claim blocks execution; it never degrades to at-least-once.

The ledger is metadata-only: an opaque digest and timestamps, never user IDs,
channel IDs, or text. See docs/specs/data-model.md § Slack delivery ledger.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config
from .sqlite_store import database_exists, write_connection

log = logging.getLogger(__name__)

RETENTION_DAYS = 7

_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS _enso_slack_events (
    account_id      TEXT NOT NULL,
    delivery_id     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    completed_at    TEXT,
    status          TEXT NOT NULL,
    audit_turn_id   TEXT,
    PRIMARY KEY (account_id, delivery_id)
)""",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    return os.path.join(config.CONFIG_DIR, "enso.db")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def delivery_id(account_id: str, channel_id: str, source_ts: str) -> str:
    """Stable delivery ID: SHA-256 of a versioned, length-prefixed tuple.

    Length prefixes keep field boundaries unambiguous, so shifting characters
    between fields can never produce a colliding ID.
    """
    payload = b"v1"
    for part in (account_id, channel_id, source_ts):
        encoded = part.encode()
        payload += len(encoded).to_bytes(4, "big") + encoded
    return hashlib.sha256(payload).hexdigest()


def claim(account_id: str, delivery_id: str) -> bool:
    """Atomically claim one delivery. False means it was already claimed.

    Any claim — pending, completed, or abandoned — suppresses the retry.
    Storage failure propagates so the caller blocks execution.
    """
    with write_connection(_db_path()) as conn:
        _ensure_schema(conn)
        try:
            conn.execute(
                "INSERT INTO _enso_slack_events "
                "(account_id, delivery_id, received_at, status) "
                "VALUES (?, ?, ?, 'pending')",
                (account_id, delivery_id, _utc_now()),
            )
        except sqlite3.IntegrityError:
            return False
    return True


def link_audit_turn(account_id: str, delivery_id: str, audit_turn_id: str) -> None:
    """Record the audit turn created for a claimed delivery."""
    with write_connection(_db_path()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE _enso_slack_events SET audit_turn_id=? "
            "WHERE account_id=? AND delivery_id=?",
            (audit_turn_id, account_id, delivery_id),
        )


def complete(
    account_id: str, delivery_id: str, *, audit_turn_id: str | None = None
) -> None:
    """Mark a claim terminal. Every normal terminal path calls this."""
    with write_connection(_db_path()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE _enso_slack_events SET status='completed', completed_at=?, "
            "audit_turn_id=COALESCE(?, audit_turn_id) "
            "WHERE account_id=? AND delivery_id=?",
            (_utc_now(), audit_turn_id, account_id, delivery_id),
        )


def abandon_pending() -> list[dict]:
    """Mark claims left ``pending`` by a crash as ``abandoned``.

    Returns the abandoned rows so linked audit turns can be closed. An
    abandoned claim still suppresses the original event if Slack retries it;
    it is never replayed.
    """
    path = _db_path()
    if not database_exists(path):
        return []
    with write_connection(path) as conn:
        _ensure_schema(conn)
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT account_id, delivery_id, audit_turn_id "
                "FROM _enso_slack_events WHERE status='pending'"
            )
        ]
        if rows:
            conn.execute(
                "UPDATE _enso_slack_events SET status='abandoned', completed_at=? "
                "WHERE status='pending'",
                (_utc_now(),),
            )
    if rows:
        log.warning("Abandoned %d pending Slack delivery claim(s) from a prior run", len(rows))
    return rows


def prune(days: int = RETENTION_DAYS) -> None:
    """Drop claims received more than *days* ago."""
    path = _db_path()
    if not database_exists(path):
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with write_connection(path) as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM _enso_slack_events WHERE received_at < ?", (cutoff,))
