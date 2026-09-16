"""Bounded read-only heartbeat and mixed run summaries for the viewer.

The viewer joins the two kinds of run at read time. It never invents jobs for beats,
loads provider output into list pages, or migrates the home it is inspecting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .. import db, runs
from ..config import Paths
from ..heartbeat.models import CLOSED_STATES

PAGE_SIZE = 50
EVENT_PREVIEW = 8000
RUN_STATUSES = (*runs.STATUSES, "cancelled")


@dataclass(frozen=True)
class ActivityRun(runs.RunSummary):
    source: str
    title: str

    @property
    def href(self) -> str:
        prefix = "/heartbeats/runs/" if self.source == "heartbeat" else "/runs/"
        return prefix + self.id


@dataclass(frozen=True)
class BeatRow:
    id: int
    title: str
    workspace: str
    state: str
    attention: bool
    at: str | None
    schedule: str | None
    timezone: str
    next_check_at: str | None
    last_check_at: str | None
    last_check_status: str | None
    updated_at: str
    closed_at: str | None
    pending: int

    @property
    def ref(self) -> str:
        return f"HB-{self.id:03d}"


_ACTIVITY_SELECT = f"""SELECT id, workspace || ':' || job AS job, workspace,
        provider, model, effort, trigger, started_at,
        ended_at, duration_ms, status, exit_code,
        substr(error, 1, {runs.ERROR_PREVIEW}) AS error_preview,
        output IS NOT NULL AS has_output, 'jobs' AS source, workspace || ':' || job AS title
        FROM runs
        UNION ALL SELECT id, printf('HB-%03d', beat_id),
        json_extract(definition, '$.workspace'),
        json_extract(definition, '$.agent.provider'),
        json_extract(definition, '$.agent.model'),
        json_extract(definition, '$.agent.effort'),
        trigger, started_at, ended_at, duration_ms, status, exit_code,
        substr(error, 1, {runs.ERROR_PREVIEW}), output != '', 'heartbeat',
        substr(json_extract(definition, '$.title'), 1, 240) FROM _enso_beat_runs"""


def _run_filters(
    source: str,
    job: str | None,
    status: str | None,
    statuses: Sequence[str] | None,
    beat_ref: str | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    values: list[object] = []
    if source in ("jobs", "heartbeat"):
        clauses.append("source = ?")
        values.append(source)
    if job:
        clauses.append("source = 'jobs' AND job = ?")
        values.append(job)
    if beat_ref:
        clauses.append("source = 'heartbeat' AND job = ?")
        values.append(beat_ref)
    if status:
        clauses.append("status = ?")
        values.append(status)
    if statuses is not None:
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")" if statuses else "0")
        values.extend(statuses)
    return " AND ".join(clauses) or "1", values


def activity(
    paths: Paths,
    *,
    source: str = "any",
    job: str | None = None,
    status: str | None = None,
    statuses: Sequence[str] | None = None,
    beat_ref: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[ActivityRun]:
    where, values = _run_filters(source, job, status, statuses, beat_ref)
    try:
        with db.reader(paths) as con:
            rows = con.execute(
                f"SELECT * FROM ({_ACTIVITY_SELECT}) WHERE {where} "
                "ORDER BY started_at DESC, source, id DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
    except db.MissingDatabaseError:
        return []
    return [ActivityRun(**dict(row)) for row in rows]


def activity_count(
    paths: Paths,
    *,
    source: str = "any",
    job: str | None = None,
    status: str | None = None,
    statuses: Sequence[str] | None = None,
    beat_ref: str | None = None,
) -> int:
    where, values = _run_filters(source, job, status, statuses, beat_ref)
    try:
        with db.reader(paths) as con:
            return con.execute(
                f"SELECT count(*) FROM ({_ACTIVITY_SELECT}) WHERE {where}", values
            ).fetchone()[0]
    except db.MissingDatabaseError:
        return 0


def _beat_filter(
    view: str, state: str | None, attention: bool, upcoming: bool
) -> tuple[str, list[object]]:
    clauses = [
        "state IN ('fulfilled', 'cancelled', 'expired')"
        if view == "previous"
        else "state IN ('active', 'paused')"
    ]
    values: list[object] = []
    if state:
        clauses.append("state = ?")
        values.append(state)
    if attention:
        clauses.append("attention = 1")
    if upcoming:
        clauses.append("state = 'active' AND next_check_at IS NOT NULL")
    return " AND ".join(clauses), values


def beat_count(
    paths: Paths, *, view: str = "current", state: str | None = None, attention: bool = False
) -> int:
    where, values = _beat_filter(view, state, attention, False)
    try:
        with db.reader(paths) as con:
            return con.execute(
                f"SELECT count(*) FROM _enso_beats WHERE {where}", values
            ).fetchone()[0]
    except db.MissingDatabaseError:
        return 0


def beat_rows(
    paths: Paths,
    *,
    view: str = "current",
    state: str | None = None,
    attention: bool = False,
    upcoming: bool = False,
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> list[BeatRow]:
    where, values = _beat_filter(view, state, attention, upcoming)
    order = (
        "next_check_at, id"
        if upcoming
        else (
            "closed_at DESC, id DESC"
            if view == "previous"
            else "attention DESC, updated_at DESC, id DESC"
        )
    )
    try:
        with db.reader(paths) as con:
            rows = con.execute(
                f"""SELECT id, substr(json_extract(definition, '$.title'), 1, 240) AS title,
                    json_extract(definition, '$.workspace') AS workspace, state, attention,
                    json_extract(definition, '$.at') AS at,
                    json_extract(definition, '$.schedule') AS schedule,
                    json_extract(definition, '$.timezone') AS timezone,
                    next_check_at, last_check_at, last_check_status, updated_at, closed_at,
                    (SELECT count(*) FROM _enso_beat_events e WHERE e.beat_id = b.id
                     AND e.requires_handling = 1 AND e.id > b.handled_event_id) AS pending
                    FROM _enso_beats b WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""",
                (*values, limit, offset),
            ).fetchall()
    except db.MissingDatabaseError:
        return []
    return [BeatRow(**dict(row)) for row in rows]


def events(
    paths: Paths, beat_id: int, *, limit: int = PAGE_SIZE, offset: int = 0
) -> tuple[int, list[dict]]:
    """One history page, including receipts; long event data is visibly clipped for the viewer."""
    try:
        with db.reader(paths) as con:
            where = "beat_id = ?"
            total = con.execute(
                f"SELECT count(*) FROM _enso_beat_events WHERE {where}", (beat_id,)
            ).fetchone()[0]
            rows = con.execute(
                f"""SELECT id, kind, actor, run_id, created_at, requires_handling,
                    substr(message, 1, {EVENT_PREVIEW}) AS message,
                    length(message) > {EVENT_PREVIEW} AS message_clipped,
                    json_extract(payload, '$.occurred_at') AS occurred_at,
                    substr(payload, 1, {EVENT_PREVIEW}) AS payload_text,
                    length(payload) > {EVENT_PREVIEW} AS payload_clipped,
                    action_key, action_status, substr(receipt, 1, {EVENT_PREVIEW}) AS receipt,
                    length(receipt) > {EVENT_PREVIEW} AS receipt_clipped
                    FROM _enso_beat_events WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?""",
                (beat_id, limit, offset),
            ).fetchall()
    except db.MissingDatabaseError:
        return 0, []
    return total, [{**dict(row), "ref": f"HB-{beat_id:03d}"} for row in rows]


def state_choices(view: str) -> tuple[str, ...]:
    return CLOSED_STATES if view == "previous" else ("active", "paused")
