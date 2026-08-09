"""Turn-based Slack audit trail — the opt-in plain-text safety record.

One row per human-facing turn in ``_enso_audit``: the triggering request,
the routing decision, and the final user-visible response with its delivery
state. Fetched context, status edits, tool calls, and reasoning are never
stored. See docs/specs/data-model.md § Audit log.

Unlike run telemetry, audit writes are not best-effort: failures propagate
so the caller can apply ``audit.on_failure`` (block the turn by default).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from . import config
from .sqlite_store import database_exists, read_connection, write_connection

log = logging.getLogger(__name__)

DECISIONS = ("accepted", "ignored", "unconfigured", "denied")

# Intake decisions that are terminal at creation, and the outcome they get.
_TERMINAL_AT_INTAKE = {"ignored": "ignored", "unconfigured": "blocked", "denied": "blocked"}

_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS _enso_audit (
    id                TEXT PRIMARY KEY,
    received_at       TEXT NOT NULL,
    completed_at      TEXT,
    transport         TEXT NOT NULL,
    account_id        TEXT NOT NULL,
    delivery_id       TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    channel_id        TEXT NOT NULL,
    thread_id         TEXT,
    source_message_id TEXT NOT NULL,
    conversation_id   TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    user_name         TEXT,
    groups_json       TEXT NOT NULL,
    authorized_groups_json TEXT,
    workspace_id      TEXT,
    binding_revision  TEXT,
    policy_revision   TEXT,
    kind              TEXT,
    decision          TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    terminal_reason   TEXT,
    delivery_status   TEXT NOT NULL,
    provider          TEXT,
    model             TEXT,
    request_text      TEXT NOT NULL,
    response_text     TEXT
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_source "
    "ON _enso_audit (account_id, delivery_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_received ON _enso_audit (received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_route "
    "ON _enso_audit (route_id, received_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_user "
    "ON _enso_audit (user_id, received_at DESC)",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    return os.path.join(config.CONFIG_DIR, "enso.db")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def _schema_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='_enso_audit'"
    ).fetchone() is not None


def create_turn(
    *,
    account_id: str,
    delivery_id: str,
    route_id: str,
    channel_id: str,
    source_message_id: str,
    conversation_id: str,
    user_id: str,
    request_text: str,
    decision: str,
    thread_id: str | None = None,
    user_name: str | None = None,
    groups: Sequence[str] = (),
    authorized_groups: Sequence[str] | None = None,
    workspace_id: str | None = None,
    binding_revision: str | None = None,
    policy_revision: str | None = None,
    kind: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    response_text: str | None = None,
    transport: str = "slack",
) -> str:
    """Insert the turn row before any command, context fetch, or spawn.

    ``accepted`` turns start ``pending``. ``ignored`` turns are terminal
    immediately; ``unconfigured``/``denied`` turns are terminal as ``blocked``
    but may carry the refusal text, whose delivery is then recorded like any
    response. Raises on storage failure — the caller applies on_failure.
    """
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    turn_id = uuid.uuid4().hex
    now = _utc_now()
    terminal_outcome = _TERMINAL_AT_INTAKE.get(decision)
    outcome = terminal_outcome or "pending"
    completed_at = now if terminal_outcome else None
    delivery_status = "pending" if response_text is not None else "not_attempted"
    with write_connection(_db_path()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "INSERT INTO _enso_audit "
            "(id, received_at, completed_at, transport, account_id, delivery_id, "
            "route_id, channel_id, thread_id, source_message_id, conversation_id, "
            "user_id, user_name, groups_json, authorized_groups_json, workspace_id, "
            "binding_revision, policy_revision, kind, decision, outcome, "
            "delivery_status, provider, model, request_text, response_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                turn_id, now, completed_at, transport, account_id, delivery_id,
                route_id, channel_id, thread_id, source_message_id, conversation_id,
                user_id, user_name, json.dumps(list(groups)),
                None if authorized_groups is None else json.dumps(list(authorized_groups)),
                workspace_id, binding_revision, policy_revision, kind, decision,
                outcome, delivery_status, provider, model, request_text, response_text,
            ),
        )
    log.info(
        "audit turn created id=%s route=%s decision=%s", turn_id, route_id, decision
    )
    return turn_id


def record_response(turn_id: str, response_text: str) -> None:
    """Store the final user-visible text before attempting delivery."""
    with write_connection(_db_path()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE _enso_audit SET response_text=?, delivery_status='pending' "
            "WHERE id=?",
            (response_text, turn_id),
        )


def record_delivery(turn_id: str, *, ok: bool) -> None:
    """Record whether Slack delivery of the stored response succeeded."""
    status = "delivered" if ok else "failed"
    with write_connection(_db_path()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE _enso_audit SET delivery_status=? WHERE id=?", (status, turn_id)
        )


def complete_turn(
    turn_id: str, outcome: str, *, terminal_reason: str | None = None
) -> None:
    """Mark an accepted turn terminal with its outcome and stable reason.

    Idempotent: an already-terminal row is never rewritten, so a refusal
    recorded at revalidation survives the generic completion that follows.
    """
    with write_connection(_db_path()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE _enso_audit SET outcome=?, terminal_reason=?, completed_at=? "
            "WHERE id=? AND completed_at IS NULL",
            (outcome, terminal_reason, _utc_now(), turn_id),
        )


def close_abandoned(turn_id: str) -> None:
    """Close a turn orphaned by a crash: error/service_restart.

    Preserves a delivery status the crash left legitimately recorded — a
    reply that reached Slack before the crash stays ``delivered``; only a
    still-unresolved delivery becomes ``not_attempted``.
    """
    with write_connection(_db_path()) as conn:
        _ensure_schema(conn)
        conn.execute(
            "UPDATE _enso_audit SET outcome='error', terminal_reason='service_restart', "
            "completed_at=?, "
            "delivery_status=CASE WHEN delivery_status IN ('delivered', 'failed') "
            "THEN delivery_status ELSE 'not_attempted' END "
            "WHERE id=? AND completed_at IS NULL",
            (_utc_now(), turn_id),
        )


def get(turn_id: str) -> dict | None:
    """Return one turn row as a dict, or None."""
    path = _db_path()
    if not database_exists(path):
        return None
    with read_connection(path) as conn:
        if not _schema_exists(conn):
            return None
        row = conn.execute("SELECT * FROM _enso_audit WHERE id=?", (turn_id,)).fetchone()
        return dict(row) if row is not None else None


def list_turns(
    route_id: str | None = None,
    user_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return a bounded page of turns, newest first, optionally filtered."""
    path = _db_path()
    if not database_exists(path):
        return []
    clauses: list[str] = []
    params: list[object] = []
    if route_id is not None:
        clauses.append("route_id=?")
        params.append(route_id)
    if user_id is not None:
        clauses.append("user_id=?")
        params.append(user_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend((limit, offset))
    with read_connection(path) as conn:
        if not _schema_exists(conn):
            return []
        cur = conn.execute(
            f"SELECT * FROM _enso_audit{where} "
            "ORDER BY received_at DESC, rowid DESC LIMIT ? OFFSET ?",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def prune(max_age_days: int) -> None:
    """Drop turns older than the cap. ``0`` selects indefinite retention.

    Age uses ``completed_at`` when present and ``received_at`` otherwise.
    Disabling a route's audit never deletes existing evidence — only this
    explicit retention cap does.
    """
    if max_age_days <= 0:
        return
    path = _db_path()
    if not database_exists(path):
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with write_connection(path) as conn:
        _ensure_schema(conn)
        conn.execute(
            "DELETE FROM _enso_audit WHERE COALESCE(completed_at, received_at) < ?",
            (cutoff,),
        )
