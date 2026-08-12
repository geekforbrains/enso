"""Short-lived, one-time Slack persistent-surface drafts.

The model can prepare a validated Canvas or App Home payload, but only an
atomically claimed Slack confirmation may expose that payload to the publisher.
Pending drafts survive service restarts; interrupted publications never replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from . import config
from .outbound import (
    AppHomePublication,
    CanvasPublication,
    SurfacePublication,
    deserialize_surface_publication,
    serialize_surface_publication,
)
from .sqlite_store import database_exists, read_connection, write_connection

DEFAULT_TTL_SECONDS = 15 * 60
RETENTION_DAYS = 7

DraftAction = Literal["publish", "cancel"]
TerminalStatus = Literal["published", "failed", "partial", "unknown"]

_TERMINAL_STATUSES = frozenset(
    {"published", "failed", "partial", "unknown", "cancelled", "expired", "superseded", "revoked"}
)

_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS _enso_surface_drafts (
    draft_id          TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    route_kind        TEXT NOT NULL,
    workspace_id      TEXT NOT NULL,
    access_profile    TEXT NOT NULL,
    route_audit       INTEGER NOT NULL,
    user_id           TEXT NOT NULL,
    channel_id        TEXT NOT NULL,
    thread_ts         TEXT,
    conversation_type TEXT NOT NULL,
    audit_turn_id     TEXT,
    target_key        TEXT NOT NULL,
    publication_hash  TEXT NOT NULL,
    publication_json  TEXT,
    target_hash       TEXT,
    target_json       TEXT,
    source_text       TEXT,
    status            TEXT NOT NULL,
    message_ts        TEXT,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    completed_at      TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_surface_drafts_expiry "
    "ON _enso_surface_drafts (status, expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_surface_drafts_publishing_target "
    "ON _enso_surface_drafts (target_key) WHERE status='publishing'",
)

_MIGRATION_COLUMNS = {
    "target_hash": "TEXT",
    "target_json": "TEXT",
}


@dataclass(frozen=True, slots=True)
class SurfaceDraftOrigin:
    """Trusted Slack routing metadata captured before model execution."""

    account_id: str
    route_id: str
    route_kind: str
    workspace_id: str
    access_profile: str
    route_audit: bool
    user_id: str
    channel_id: str
    thread_ts: str | None = None
    conversation_type: str = ""
    audit_turn_id: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.account_id,
            self.route_id,
            self.workspace_id,
            self.access_profile,
            self.user_id,
            self.channel_id,
        )
        if not all(type(value) is str and value for value in required):
            raise ValueError("surface draft origin requires complete routing metadata")
        if self.route_kind not in {"dm", "channel"}:
            raise ValueError("surface draft route_kind must be dm or channel")
        if type(self.route_audit) is not bool:
            raise ValueError("surface draft route_audit must be a boolean")
        if self.thread_ts is not None and type(self.thread_ts) is not str:
            raise ValueError("surface draft thread_ts must be a string or null")
        if type(self.conversation_type) is not str:
            raise ValueError("surface draft conversation_type must be a string")
        if self.audit_turn_id is not None and type(self.audit_turn_id) is not str:
            raise ValueError("surface draft audit_turn_id must be a string or null")


@dataclass(frozen=True, slots=True)
class ChannelCanvasTarget:
    """Trusted create-or-replace decision resolved from the origin channel."""

    operation: Literal["create", "replace"]
    canvas_id: str | None = None
    title: str | None = None
    permalink: str | None = None
    edit_timestamp: int | None = None

    def __post_init__(self) -> None:
        if self.operation == "create":
            if any(
                value is not None
                for value in (
                    self.canvas_id,
                    self.title,
                    self.permalink,
                    self.edit_timestamp,
                )
            ):
                raise ValueError("new channel Canvas target cannot name an existing Canvas")
            return
        if self.operation != "replace":
            raise ValueError("channel Canvas operation must be create or replace")
        if not all(
            type(value) is str and value.strip()
            for value in (self.canvas_id, self.title, self.permalink)
        ):
            raise ValueError("replacement channel Canvas target is incomplete")
        if self.edit_timestamp is not None and (
            type(self.edit_timestamp) is not int or self.edit_timestamp < 0
        ):
            raise ValueError("replacement channel Canvas revision is invalid")


@dataclass(frozen=True, slots=True)
class SurfaceDraft:
    """A validated draft and the trusted Slack origin allowed to confirm it."""

    draft_id: str
    publication: SurfacePublication
    source_text: str
    origin: SurfaceDraftOrigin
    status: str
    message_ts: str | None
    target_key: str
    channel_canvas_target: ChannelCanvasTarget | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SurfaceDraftScope:
    """Trusted selectors and lifecycle state without exposing draft content."""

    origin: SurfaceDraftOrigin
    status: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _db_path() -> str:
    return os.path.join(config.CONFIG_DIR, "enso.db")


def _ensure_schema(connection: sqlite3.Connection) -> None:
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(_enso_surface_drafts)")
    }
    for name, definition in _MIGRATION_COLUMNS.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE _enso_surface_drafts ADD COLUMN {name} {definition}"
            )


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("surface draft timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _origin_from_row(row: sqlite3.Row) -> SurfaceDraftOrigin:
    return SurfaceDraftOrigin(
        account_id=row["account_id"],
        route_id=row["route_id"],
        route_kind=row["route_kind"],
        workspace_id=row["workspace_id"],
        access_profile=row["access_profile"],
        route_audit=bool(row["route_audit"]),
        user_id=row["user_id"],
        channel_id=row["channel_id"],
        thread_ts=row["thread_ts"],
        conversation_type=row["conversation_type"],
        audit_turn_id=row["audit_turn_id"],
    )


def _publication_from_row(row: sqlite3.Row) -> SurfacePublication | None:
    raw_publication = row["publication_json"]
    expected_hash = row["publication_hash"]
    if type(raw_publication) is not str or type(expected_hash) is not str:
        return None
    actual_hash = hashlib.sha256(raw_publication.encode()).hexdigest()
    if actual_hash != expected_hash:
        return None
    return deserialize_surface_publication(raw_publication)


def _serialize_channel_canvas_target(target: ChannelCanvasTarget) -> str:
    payload: dict[str, object] = {
        "version": 1,
        "operation": target.operation,
    }
    if target.operation == "replace":
        payload.update(
            canvas_id=target.canvas_id,
            title=target.title,
            permalink=target.permalink,
            edit_timestamp=target.edit_timestamp,
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _deserialize_channel_canvas_target(raw: str) -> ChannelCanvasTarget | None:
    try:
        payload = json.loads(raw)
        if type(payload) is not dict or payload.get("version") != 1:
            return None
        if payload.get("operation") == "create" and set(payload) == {
            "version",
            "operation",
        }:
            return ChannelCanvasTarget(operation="create")
        if payload.get("operation") == "replace" and set(payload) == {
            "version",
            "operation",
            "canvas_id",
            "title",
            "permalink",
            "edit_timestamp",
        }:
            return ChannelCanvasTarget(
                operation="replace",
                canvas_id=payload["canvas_id"],
                title=payload["title"],
                permalink=payload["permalink"],
                edit_timestamp=payload["edit_timestamp"],
            )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _target_from_row(row: sqlite3.Row) -> ChannelCanvasTarget | None:
    raw_target = row["target_json"]
    expected_hash = row["target_hash"]
    if raw_target is None and expected_hash is None:
        return None
    if type(raw_target) is not str or type(expected_hash) is not str:
        return None
    if hashlib.sha256(raw_target.encode()).hexdigest() != expected_hash:
        return None
    return _deserialize_channel_canvas_target(raw_target)


def _draft_from_row(
    row: sqlite3.Row,
    *,
    publication: SurfacePublication | None = None,
    status: str | None = None,
) -> SurfaceDraft | None:
    restored = publication or _publication_from_row(row)
    if restored is None or row["source_text"] is None:
        return None
    channel_canvas_target = _target_from_row(row)
    is_channel_canvas = (
        isinstance(restored, CanvasPublication) and restored.placement == "channel"
    )
    if is_channel_canvas != (channel_canvas_target is not None):
        return None
    return SurfaceDraft(
        draft_id=row["draft_id"],
        publication=restored,
        source_text=row["source_text"],
        origin=_origin_from_row(row),
        status=status or row["status"],
        message_ts=row["message_ts"],
        target_key=row["target_key"],
        channel_canvas_target=channel_canvas_target,
        created_at=_parse_time(row["created_at"]),
        expires_at=_parse_time(row["expires_at"]),
    )


def _target_key(
    publication: SurfacePublication,
    origin: SurfaceDraftOrigin,
    draft_id: str,
) -> str:
    if isinstance(publication, AppHomePublication):
        return f"app_home:{origin.account_id}:{origin.user_id}"
    if isinstance(publication, CanvasPublication) and publication.placement == "channel":
        return f"channel_canvas:{origin.account_id}:{origin.channel_id}"
    return f"standalone_canvas:{draft_id}"


def create(
    publication: SurfacePublication,
    *,
    source_text: str,
    origin: SurfaceDraftOrigin,
    channel_canvas_target: ChannelCanvasTarget | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> SurfaceDraft:
    """Persist one immutable, unbound draft and return its opaque handle."""
    if not isinstance(publication, (CanvasPublication, AppHomePublication)):
        raise TypeError("unsupported surface draft publication")
    if type(source_text) is not str or not source_text:
        raise ValueError("surface draft source_text must be non-empty")
    if type(ttl_seconds) is not int or ttl_seconds <= 0:
        raise ValueError("surface draft ttl_seconds must be a positive integer")
    is_channel_canvas = (
        isinstance(publication, CanvasPublication) and publication.placement == "channel"
    )
    if is_channel_canvas != (channel_canvas_target is not None):
        raise ValueError("channel Canvas target must match the publication placement")

    publication_json = serialize_surface_publication(publication)
    if deserialize_surface_publication(publication_json) != publication:
        raise ValueError("surface publication cannot be restored exactly")
    now = _as_utc(_utc_now())
    expires_at = now + timedelta(seconds=ttl_seconds)
    draft_id = secrets.token_urlsafe(24)
    target_key = _target_key(publication, origin, draft_id)
    publication_hash = hashlib.sha256(publication_json.encode()).hexdigest()
    target_json = (
        _serialize_channel_canvas_target(channel_canvas_target)
        if channel_canvas_target is not None
        else None
    )
    target_hash = (
        hashlib.sha256(target_json.encode()).hexdigest()
        if target_json is not None
        else None
    )

    with write_connection(_db_path()) as connection:
        _ensure_schema(connection)
        connection.execute(
            "INSERT INTO _enso_surface_drafts "
            "(draft_id, account_id, route_id, route_kind, workspace_id, "
            "access_profile, route_audit, user_id, channel_id, thread_ts, "
            "conversation_type, audit_turn_id, target_key, publication_hash, "
            "publication_json, target_hash, target_json, source_text, status, "
            "created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'pending', ?, ?)",
            (
                draft_id,
                origin.account_id,
                origin.route_id,
                origin.route_kind,
                origin.workspace_id,
                origin.access_profile,
                int(origin.route_audit),
                origin.user_id,
                origin.channel_id,
                origin.thread_ts,
                origin.conversation_type,
                origin.audit_turn_id,
                target_key,
                publication_hash,
                publication_json,
                target_hash,
                target_json,
                source_text,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )

    return SurfaceDraft(
        draft_id=draft_id,
        publication=publication,
        source_text=source_text,
        origin=origin,
        status="pending",
        message_ts=None,
        target_key=target_key,
        channel_canvas_target=channel_canvas_target,
        created_at=now,
        expires_at=expires_at,
    )


def bind_message(draft_id: str, *, message_ts: str) -> bool:
    """Bind a draft exactly once to the bot message containing its buttons."""
    if type(draft_id) is not str or not draft_id:
        return False
    if type(message_ts) is not str or not message_ts:
        return False
    now = _as_utc(_utc_now())
    with write_connection(_db_path()) as connection:
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT target_key, user_id FROM _enso_surface_drafts WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        cursor = connection.execute(
            "UPDATE _enso_surface_drafts SET message_ts=? "
            "WHERE draft_id=? AND status='pending' AND message_ts IS NULL",
            (message_ts, draft_id),
        )
        if (
            cursor.rowcount == 1
            and row is not None
            and not row["target_key"].startswith("standalone_canvas:")
        ):
            connection.execute(
                "UPDATE _enso_surface_drafts SET status='superseded', "
                "publication_json=NULL, target_json=NULL, source_text=NULL, "
                "completed_at=? "
                "WHERE target_key=? AND user_id=? AND draft_id<>? "
                "AND status='pending'",
                (
                    now.isoformat(),
                    row["target_key"],
                    row["user_id"],
                    draft_id,
                ),
            )
    return cursor.rowcount == 1


def get_scoped(
    draft_id: str,
    *,
    account_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
) -> SurfaceDraft | None:
    """Read a draft only when every trusted Slack selector matches."""
    path = _db_path()
    if not database_exists(path):
        return None
    with read_connection(path) as connection:
        row = connection.execute(
            "SELECT * FROM _enso_surface_drafts WHERE draft_id=? "
            "AND account_id=? AND user_id=? AND channel_id=? AND message_ts=?",
            (draft_id, account_id, user_id, channel_id, message_ts),
        ).fetchone()
    return _draft_from_row(row) if row is not None else None


def get_origin_scoped(
    draft_id: str,
    *,
    account_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
) -> SurfaceDraftScope | None:
    """Read only trusted origin metadata before atomically validating content."""
    path = _db_path()
    if not database_exists(path):
        return None
    with read_connection(path) as connection:
        row = connection.execute(
            "SELECT * FROM _enso_surface_drafts WHERE draft_id=? "
            "AND account_id=? AND user_id=? AND channel_id=? AND message_ts=?",
            (draft_id, account_id, user_id, channel_id, message_ts),
        ).fetchone()
    if row is None:
        return None
    return SurfaceDraftScope(origin=_origin_from_row(row), status=row["status"])


def claim(
    draft_id: str,
    *,
    action: DraftAction,
    account_id: str,
    user_id: str,
    channel_id: str,
    message_ts: str,
) -> SurfaceDraft | None:
    """Atomically consume a correctly scoped pending draft once."""
    if action not in {"publish", "cancel"}:
        raise ValueError("surface draft action must be publish or cancel")
    now = _as_utc(_utc_now())
    with write_connection(_db_path()) as connection:
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT * FROM _enso_surface_drafts WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        if row is None or any(
            row[key] != value
            for key, value in (
                ("account_id", account_id),
                ("user_id", user_id),
                ("channel_id", channel_id),
                ("message_ts", message_ts),
            )
        ):
            return None
        if row["status"] != "pending" or row["message_ts"] is None:
            return None
        if _parse_time(row["expires_at"]) <= now:
            connection.execute(
                "UPDATE _enso_surface_drafts SET status='expired', "
                "publication_json=NULL, target_json=NULL, source_text=NULL, "
                "completed_at=? "
                "WHERE draft_id=? AND status='pending'",
                (now.isoformat(), draft_id),
            )
            return None

        publication = _publication_from_row(row)
        restored = (
            _draft_from_row(row, publication=publication)
            if publication is not None
            else None
        )
        if restored is None:
            connection.execute(
                "UPDATE _enso_surface_drafts SET status='failed', "
                "publication_json=NULL, target_json=NULL, source_text=NULL, "
                "completed_at=? "
                "WHERE draft_id=? AND status='pending'",
                (now.isoformat(), draft_id),
            )
            return None

        next_status = "publishing" if action == "publish" else "cancelled"
        try:
            cursor = connection.execute(
                "UPDATE _enso_surface_drafts SET status=?, "
                "publication_json=CASE WHEN ?='cancelled' THEN NULL ELSE publication_json END, "
                "target_json=CASE WHEN ?='cancelled' THEN NULL ELSE target_json END, "
                "source_text=CASE WHEN ?='cancelled' THEN NULL ELSE source_text END, "
                "completed_at=CASE WHEN ?='cancelled' THEN ? ELSE completed_at END "
                "WHERE draft_id=? AND status='pending'",
                (
                    next_status,
                    next_status,
                    next_status,
                    next_status,
                    next_status,
                    now.isoformat(),
                    draft_id,
                ),
            )
        except sqlite3.IntegrityError:
            return None
        if cursor.rowcount != 1:
            return None
        updated_row = row
        if next_status == "publishing":
            updated_row = connection.execute(
                "SELECT * FROM _enso_surface_drafts WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
        return _draft_from_row(
            updated_row,
            publication=publication,
            status=next_status,
        )


def finish(draft_id: str, *, status: TerminalStatus) -> bool:
    """Finish one claimed publication and scrub its complete content."""
    if status not in {"published", "failed", "partial", "unknown"}:
        raise ValueError("invalid terminal surface draft status")
    now = _as_utc(_utc_now())
    with write_connection(_db_path()) as connection:
        _ensure_schema(connection)
        row = connection.execute(
            "SELECT target_key FROM _enso_surface_drafts "
            "WHERE draft_id=? AND status='publishing'",
            (draft_id,),
        ).fetchone()
        cursor = connection.execute(
            "UPDATE _enso_surface_drafts SET status=?, publication_json=NULL, "
            "target_json=NULL, source_text=NULL, completed_at=? "
            "WHERE draft_id=? AND status='publishing'",
            (status, now.isoformat(), draft_id),
        )
        if (
            cursor.rowcount == 1
            and row is not None
            and status in {"published", "partial", "unknown"}
        ):
            connection.execute(
                "UPDATE _enso_surface_drafts SET status='superseded', "
                "publication_json=NULL, target_json=NULL, source_text=NULL, "
                "completed_at=? WHERE target_key=? AND draft_id<>? "
                "AND status='pending'",
                (now.isoformat(), row["target_key"], draft_id),
            )
    return cursor.rowcount == 1


def revoke(draft_id: str) -> bool:
    """Invalidate a pending draft whose trusted route is no longer authorized."""
    now = _as_utc(_utc_now())
    with write_connection(_db_path()) as connection:
        _ensure_schema(connection)
        cursor = connection.execute(
            "UPDATE _enso_surface_drafts SET status='revoked', "
            "publication_json=NULL, target_json=NULL, source_text=NULL, "
            "completed_at=? "
            "WHERE draft_id=? AND status='pending'",
            (now.isoformat(), draft_id),
        )
    return cursor.rowcount == 1


def maintain() -> dict[str, int]:
    """Expire idle pending drafts and prune old terminal metadata.

    This live-maintenance path deliberately never changes ``publishing`` rows.
    Only startup reconciliation may close a publication interrupted by process
    death, so a slow live Slack API call can never lose its target lease.
    """
    path = _db_path()
    if not database_exists(path):
        return {"expired": 0, "pruned": 0}
    now = _as_utc(_utc_now())
    retention_cutoff = now - timedelta(days=RETENTION_DAYS)
    with write_connection(path) as connection:
        _ensure_schema(connection)
        expired = connection.execute(
            "UPDATE _enso_surface_drafts SET status='expired', "
            "publication_json=NULL, target_json=NULL, source_text=NULL, "
            "completed_at=? "
            "WHERE status='pending' AND expires_at<=?",
            (now.isoformat(), now.isoformat()),
        ).rowcount
        placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
        pruned = connection.execute(
            f"DELETE FROM _enso_surface_drafts WHERE status IN ({placeholders}) "
            "AND completed_at<?",
            (*sorted(_TERMINAL_STATUSES), retention_cutoff.isoformat()),
        ).rowcount
    return {"expired": expired, "pruned": pruned}


def reconcile() -> dict[str, int]:
    """Close unsafe crash leftovers, expire old drafts, and prune metadata."""
    path = _db_path()
    if not database_exists(path):
        return {"unknown": 0, "expired": 0, "failed": 0, "pruned": 0}
    now = _as_utc(_utc_now())
    retention_cutoff = now - timedelta(days=RETENTION_DAYS)
    with write_connection(path) as connection:
        _ensure_schema(connection)
        unknown = connection.execute(
            "UPDATE _enso_surface_drafts SET status='unknown', "
            "publication_json=NULL, target_json=NULL, source_text=NULL, "
            "completed_at=? "
            "WHERE status='publishing'",
            (now.isoformat(),),
        ).rowcount
        expired = connection.execute(
            "UPDATE _enso_surface_drafts SET status='expired', "
            "publication_json=NULL, target_json=NULL, source_text=NULL, "
            "completed_at=? "
            "WHERE status='pending' AND expires_at<=?",
            (now.isoformat(), now.isoformat()),
        ).rowcount
        failed = connection.execute(
            "UPDATE _enso_surface_drafts SET status='failed', "
            "publication_json=NULL, target_json=NULL, source_text=NULL, "
            "completed_at=? "
            "WHERE status='pending' AND (message_ts IS NULL OR "
            "(target_key LIKE 'channel_canvas:%' AND target_json IS NULL))",
            (now.isoformat(),),
        ).rowcount
        placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
        pruned = connection.execute(
            f"DELETE FROM _enso_surface_drafts WHERE status IN ({placeholders}) "
            "AND completed_at<?",
            (*sorted(_TERMINAL_STATUSES), retention_cutoff.isoformat()),
        ).rowcount
    return {"unknown": unknown, "expired": expired, "failed": failed, "pruned": pruned}
