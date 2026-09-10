"""Short transactional heartbeat operations, isolated from scripts and provider execution.

Definitions, pending observations, action receipts, and execution claims have one durable home.
Reads never migrate an older database; writes use revision checks so an agent cannot finish
work against instructions that were changed or cancelled while it was running.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import IO, Any

from .. import db
from ..config import Agent, Config, Paths
from .models import CLOSED_STATES, STATES, Beat, BeatEvent, BeatRun, Definition, HeartbeatError
from .validation import (
    DEFINITION_KEYS,
    json_object,
    next_check,
    parse_ref,
    timestamp,
    utc,
    validate_definition,
    validate_gate,
)

MAX_OUTPUT = 1024 * 1024
NOTICE_KINDS = (
    "check_failed",
    "check_recovered",
    "run_failed",
    "run_recovered",
    "expired",
    "one_shot_not_ready",
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _definition(text: str) -> Definition:
    values = json.loads(text)
    return Definition(**{**values, "agent": Agent(**values["agent"])})


def _beat(row: sqlite3.Row) -> Beat:
    fields = dict(row)
    definition = _definition(fields.pop("definition"))
    fields["checkpoint"] = json.loads(fields["checkpoint"])
    fields["attention"] = bool(fields["attention"])
    fields["at_consumed"] = bool(fields["at_consumed"])
    return Beat(**{key: getattr(definition, key) for key in DEFINITION_KEYS}, **fields)


def _event(row: sqlite3.Row) -> BeatEvent:
    fields = dict(row)
    fields["payload"] = json.loads(fields["payload"])
    fields["requires_handling"] = bool(fields["requires_handling"])
    return BeatEvent(**fields)


def _run(row: sqlite3.Row) -> BeatRun:
    fields = dict(row)
    fields["definition"] = _definition(fields["definition"])
    return BeatRun(**fields)


def _load(con: sqlite3.Connection, ref: str) -> Beat:
    row = con.execute("SELECT * FROM _enso_beats WHERE id = ?", (parse_ref(ref),)).fetchone()
    if row is None:
        raise HeartbeatError(f"no beat {ref.upper()}")
    return _beat(row)


def _load_run(con: sqlite3.Connection, run_id: str) -> BeatRun:
    row = con.execute("SELECT * FROM _enso_beat_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HeartbeatError(f"no heartbeat run {run_id}")
    return _run(row)


@contextmanager
def _reader(paths: Paths) -> Iterator[sqlite3.Connection | None]:
    try:
        with db.reader(paths) as con:
            if con.execute("PRAGMA user_version").fetchone()[0] < 4:
                yield None
            else:
                con.execute("BEGIN")
                yield con
    except db.MissingDatabaseError:
        yield None


def _enabled(config: Config) -> None:
    if not config.heartbeat.enabled:
        raise HeartbeatError("heartbeat is disabled in config.json; existing beats are preserved")


def _open(beat: Beat) -> None:
    if beat.closed:
        raise HeartbeatError(
            f"{beat.ref} is {beat.state}; closed beats only take notes or action receipts"
        )


def _revision(beat: Beat, expected: int | None) -> None:
    if expected is not None and beat.revision != expected:
        raise HeartbeatError(f"{beat.ref} changed; read its current instructions before continuing")


def _owned(con: sqlite3.Connection, beat: Beat, run_id: str) -> BeatRun:
    run = _load_run(con, run_id)
    if run.beat_id != beat.id or beat.claim_run_id != run_id or run.status != "running":
        raise HeartbeatError(f"run {run_id} does not hold {beat.ref}")
    _revision(beat, run.definition_revision)
    if beat.state != "active":
        raise HeartbeatError(f"{beat.ref} is {beat.state}; its run may not continue")
    if beat.expires_at is not None and beat.expires_at <= db.now():
        raise HeartbeatError(f"{beat.ref} reached expires_at; its run may not continue")
    if run.settlement is not None:
        raise HeartbeatError(
            f"run {run_id} already recorded {run.settlement}; stop after settling a beat"
        )
    return run


def assert_run_active(config: Config, ref: str, run_id: str) -> BeatRun:
    """Recheck current effect authorization without reserving another action or writing events."""
    _enabled(config)
    with _reader(config.paths) as con:
        if con is None:
            raise HeartbeatError(f"no beat {ref}")
        return _owned(con, _load(con, ref), run_id)


def _message(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HeartbeatError("a nonempty message explaining the result or evidence is required")
    return value.strip()[-MAX_OUTPUT:]


def _limit(limit: int, maximum: int = 500) -> None:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise HeartbeatError(f"limit must be an integer from 1 through {maximum}")


def _record(
    con: sqlite3.Connection,
    beat: Beat,
    kind: str,
    message: str,
    *,
    actor: str = "heartbeat",
    run_id: str | None = None,
    payload: dict[str, Any] | None = None,
    requires_handling: bool = False,
    action_key: str | None = None,
    action_status: str | None = None,
    receipt: str | None = None,
) -> BeatEvent:
    cursor = con.execute(
        """INSERT INTO _enso_beat_events
           (beat_id, kind, actor, run_id, message, payload, requires_handling,
            action_key, action_status, receipt, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            beat.id,
            kind,
            actor,
            run_id,
            message[-MAX_OUTPUT:],
            _json(payload or {}),
            requires_handling,
            action_key,
            action_status,
            receipt,
            db.now(),
        ),
    )
    return _event(
        con.execute("SELECT * FROM _enso_beat_events WHERE id = ?", (cursor.lastrowid,)).fetchone()
    )


def _actions(con: sqlite3.Connection, beat: Beat, *, unresolved: bool = False) -> list[BeatEvent]:
    where = "AND e.action_status IN ('pending', 'uncertain')" if unresolved else ""
    rows = con.execute(
        f"""SELECT e.* FROM _enso_beat_events e
            WHERE e.beat_id = ? AND e.action_key IS NOT NULL {where}
              AND NOT EXISTS (
                SELECT 1 FROM _enso_beat_events newer
                WHERE newer.beat_id = e.beat_id AND newer.action_key = e.action_key
                  AND newer.id > e.id)
            ORDER BY e.id""",
        (beat.id,),
    ).fetchall()
    return [_event(row) for row in rows]


def _no_uncertain_actions(con: sqlite3.Connection, beat: Beat) -> None:
    unresolved = _actions(con, beat, unresolved=True)
    if unresolved:
        keys = ", ".join(f"{item.action_key} ({item.action_status})" for item in unresolved)
        raise HeartbeatError(
            f"resolve pending or uncertain actions before settling {beat.ref}: {keys}"
        )


def acquire_lock(paths: Paths, ref: str) -> IO[str] | None:
    """Take a stable per-beat lock kept outside the prunable gate directory."""
    from ..execution import acquire_file_lock

    canonical = f"HB-{parse_ref(ref):03d}"
    directory = paths.heartbeat / ".locks"
    lockfile = directory / f"{canonical}.lock"
    if paths.heartbeat.is_symlink() or directory.is_symlink() or lockfile.is_symlink():
        raise HeartbeatError("heartbeat lock paths must not be symlinks")
    if lockfile.exists() and not lockfile.is_file():
        raise HeartbeatError("heartbeat lock must be a regular file")
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return acquire_file_lock(lockfile)
    except OSError as exc:
        raise HeartbeatError(f"could not lock {canonical}: {exc}") from exc


def _next_existing(beat: Beat, definition: Definition, stamp: str) -> str | None:
    # Editing a title or resuming a paused watch must not repeat a consumed one-shot.
    if beat.at_consumed and definition.at == beat.at:
        definition = replace(definition, at=None)
    return next_check(definition, stamp)


# -- Definitions and read-only views --------------------------------------------


def create(config: Config, data: object, *, actor: str = "user", paused: bool = True) -> Beat:
    """Save a beat paused by default, allowing its gate to be installed before activation."""
    definition = validate_definition(data, config)
    if not paused:
        _enabled(config)
        if definition.gate:
            raise HeartbeatError("create a gated beat paused, write its gate.sh, then resume it")
    stamp = db.now()
    due_at = next_check(definition, stamp)
    db.migrate(config.paths)
    with db.transaction(config.paths) as con:
        cursor = con.execute(
            """INSERT INTO _enso_beats
               (definition, state, next_check_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (_json(definition.as_dict()), "paused" if paused else "active", due_at, stamp, stamp),
        )
        beat = _load(con, f"HB-{cursor.lastrowid}")
        _record(con, beat, "created", "Created paused" if paused else "Created active", actor=actor)
        return beat


def get(paths: Paths, ref: str) -> Beat | None:
    number = parse_ref(ref)
    with _reader(paths) as con:
        if con is None:
            return None
        row = con.execute("SELECT * FROM _enso_beats WHERE id = ?", (number,)).fetchone()
        return _beat(row) if row is not None else None


def list_beats(
    paths: Paths,
    *,
    state: str | None = None,
    workspace: str | None = None,
    include_closed: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[Beat]:
    _limit(limit)
    if type(offset) is not int or offset < 0:
        raise HeartbeatError("offset must be a nonnegative integer")
    if state is not None and state not in STATES:
        raise HeartbeatError(f"state must be one of {', '.join(STATES)}")
    clauses: list[str] = []
    values: list[object] = []
    if state:
        clauses.append("state = ?")
        values.append(state)
    elif not include_closed:
        clauses.append("state IN ('active', 'paused')")
    if workspace:
        clauses.append("json_extract(definition, '$.workspace') = ?")
        values.append(workspace)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _reader(paths) as con:
        if con is None:
            return []
        rows = con.execute(
            f"SELECT * FROM _enso_beats{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        return [_beat(row) for row in rows]


def update(
    config: Config,
    ref: str,
    data: object,
    *,
    actor: str = "user",
    expected_revision: int | None = None,
    run_id: str | None = None,
) -> Beat:
    """Merge a validated definition patch; active gates must be paused before editing."""
    patch = json_object(data, "beat update")
    if run_id is not None:
        raise HeartbeatError("a heartbeat run cannot edit definitions; use note or wait instead")
    current = get(config.paths, ref)
    if current is None:
        raise HeartbeatError(f"no beat {ref}")
    _open(current)
    _revision(current, expected_revision)
    raw = current.definition.as_dict()
    raw.update(patch)
    definition = validate_definition(
        raw, config, at_consumed=current.at_consumed and raw.get("at") == current.at
    )
    changes = {
        key: {"before": current.definition.as_dict()[key], "after": definition.as_dict()[key]}
        for key in DEFINITION_KEYS
        if current.definition.as_dict()[key] != definition.as_dict()[key]
    }
    if not changes:
        return current
    if current.state == "active":
        _enabled(config)
        if definition.gate != current.gate:
            raise HeartbeatError("pause the beat before adding or replacing its gate")
    stamp = db.now()
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        _revision(beat, current.revision)
        con.execute(
            """UPDATE _enso_beats SET definition = ?, revision = revision + 1,
               next_check_at = ?, at_consumed = ?, updated_at = ? WHERE id = ?""",
            (
                _json(definition.as_dict()),
                _next_existing(beat, definition, stamp),
                beat.at_consumed and definition.at == beat.at,
                stamp,
                beat.id,
            ),
        )
        updated = _load(con, ref)
        _record(
            con,
            updated,
            "updated",
            "Updated beat instructions",
            actor=actor,
            payload={"changes": changes},
        )
        return updated


def history(
    paths: Paths,
    ref: str,
    *,
    limit: int = 100,
    before_id: int | None = None,
    after_id: int | None = None,
    kind: str | None = None,
    unhandled: bool = False,
) -> list[BeatEvent]:
    """Page newest first, or oldest first when reading forward or handling pending events."""
    _limit(limit)
    number = parse_ref(ref)
    for name, cursor in (("before_id", before_id), ("after_id", after_id)):
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise HeartbeatError(f"{name} must be a nonnegative integer")
    clauses = ["beat_id = ?"]
    values: list[object] = [number]
    for field, operator, value in (
        ("id", "<", before_id),
        ("id", ">", after_id),
        ("kind", "=", kind),
    ):
        if value is not None:
            clauses.append(f"{field} {operator} ?")
            values.append(value)
    with _reader(paths) as con:
        if con is None:
            return []
        if unhandled:
            beat = _load(con, ref)
            clauses.extend(("requires_handling = 1", "id > ?"))
            values.append(beat.handled_event_id)
        direction = "ASC" if after_id is not None or unhandled else "DESC"
        rows = con.execute(
            f"SELECT * FROM _enso_beat_events WHERE {' AND '.join(clauses)} "
            f"ORDER BY id {direction} LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [_event(row) for row in rows]


def get_run(paths: Paths, run_id: str) -> BeatRun | None:
    with _reader(paths) as con:
        if con is None:
            return None
        row = con.execute("SELECT * FROM _enso_beat_runs WHERE id = ?", (run_id,)).fetchone()
        return _run(row) if row is not None else None


def list_runs(paths: Paths, ref: str, *, limit: int = 100, offset: int = 0) -> list[BeatRun]:
    _limit(limit)
    number = parse_ref(ref)
    if type(offset) is not int or offset < 0:
        raise HeartbeatError("offset must be a nonnegative integer")
    with _reader(paths) as con:
        if con is None:
            return []
        rows = con.execute(
            "SELECT * FROM _enso_beat_runs WHERE beat_id = ? "
            "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (number, limit, offset),
        ).fetchall()
        return [_run(row) for row in rows]


def context(paths: Paths, ref: str, *, run_id: str | None = None) -> dict[str, Any]:
    """A compact current packet; a run can acknowledge only its frozen input cutoff."""
    with _reader(paths) as con:
        if con is None:
            return {}
        beat = _load(con, ref)
        run = _load_run(con, run_id) if run_id is not None else None
        if run is not None and run.beat_id != beat.id:
            raise HeartbeatError(f"run {run.id} does not belong to {beat.ref}")
        count, newest = con.execute(
            "SELECT count(*), COALESCE(max(id), 0) FROM _enso_beat_events WHERE beat_id = ?",
            (beat.id,),
        ).fetchone()
        cutoff = run.input_cutoff if run else newest
        latest = con.execute(
            "SELECT * FROM _enso_beat_events WHERE beat_id = ? AND id <= ? "
            "AND kind != 'notice_delivered' "
            "ORDER BY id DESC LIMIT 1",
            (beat.id, cutoff),
        ).fetchone()
        pending = con.execute(
            """SELECT count(*) FROM _enso_beat_events
               WHERE beat_id = ? AND requires_handling = 1 AND id > ? AND id <= ?""",
            (beat.id, beat.handled_event_id, cutoff),
        ).fetchone()[0]
        return {
            "beat": beat.as_dict(),
            "definition": (run.definition if run else beat.definition).as_dict(),
            "run": run.as_dict() if run else None,
            "input_cutoff": cutoff,
            "latest_event": _event(latest).as_dict() if latest else None,
            "history_count": count,
            "unhandled_count": pending,
            "history_command": f"enso heartbeat history {beat.ref} --json",
            "actions": [item.as_dict() for item in _actions(con, beat, unresolved=True)],
        }


# -- Lifecycle and explicit handling --------------------------------------------


def _transition(
    config: Config,
    ref: str,
    state: str,
    *,
    message: str,
    actor: str,
    run_id: str | None = None,
    expected_revision: int | None = None,
) -> Beat:
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        _revision(beat, expected_revision)
        _open(beat)
        if run_id is not None:
            _owned(con, beat, run_id)
        if beat.state == state:
            return beat
        stamp = db.now()
        closed = state in CLOSED_STATES
        con.execute(
            """UPDATE _enso_beats SET state = ?, revision = revision + 1,
               updated_at = ?, closed_at = ?, next_check_at = ?, attention = ? WHERE id = ?""",
            (
                state,
                stamp,
                stamp if closed else None,
                None if closed else beat.next_check_at,
                beat.attention or state == "expired",
                beat.id,
            ),
        )
        result = _load(con, ref)
        _record(con, result, state, message or state.capitalize(), actor=actor, run_id=run_id)
        return result


def pause(
    config: Config, ref: str, *, message: str = "", actor: str = "user", run_id: str | None = None
) -> Beat:
    return _transition(config, ref, "paused", message=message, actor=actor, run_id=run_id)


def resume(
    config: Config, ref: str, *, message: str = "", actor: str = "user", run_id: str | None = None
) -> Beat:
    """Activate a paused beat only after its current definition and gate are usable."""
    _enabled(config)
    current = get(config.paths, ref)
    if current is None:
        raise HeartbeatError(f"no beat {ref}")
    _open(current)
    validate_definition(current.definition.as_dict(), config, at_consumed=current.at_consumed)
    validate_gate(config.paths, current)
    stamp = db.now()
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        _revision(beat, current.revision)
        if run_id is not None:
            _owned(con, beat, run_id)
        if beat.state == "active":
            return beat
        if beat.claim_run_id is not None:
            raise HeartbeatError(
                f"{ref}'s previous run is still stopping; resume once it has ended"
            )
        con.execute(
            """UPDATE _enso_beats SET state = 'active', revision = revision + 1,
               next_check_at = ?, updated_at = ? WHERE id = ?""",
            (_next_existing(beat, beat.definition, stamp), stamp, beat.id),
        )
        result = _load(con, ref)
        _record(con, result, "resumed", message or "Activated heartbeat checks", actor=actor)
        return result


def cancel(
    config: Config, ref: str, *, message: str = "", actor: str = "user", run_id: str | None = None
) -> Beat:
    return _transition(config, ref, "cancelled", message=message, actor=actor, run_id=run_id)


def expire(
    config: Config,
    ref: str,
    *,
    message: str = "",
    actor: str = "user",
    run_id: str | None = None,
    expected_revision: int | None = None,
) -> Beat:
    return _transition(
        config,
        ref,
        "expired",
        message=message or "Deadline passed without fulfillment",
        actor=actor,
        run_id=run_id,
        expected_revision=expected_revision,
    )


def _settle(
    config: Config,
    ref: str,
    *,
    message: str,
    actor: str,
    run_id: str | None,
    checkpoint: object | None,
    followup_at: str | None,
    complete_beat: bool,
) -> Beat:
    message = _message(message)
    saved_checkpoint = json_object(checkpoint, "checkpoint") if checkpoint is not None else None
    followup = timestamp(followup_at, "followup_at") if followup_at is not None else None
    stamp = db.now()
    if followup is not None and followup <= stamp:
        raise HeartbeatError("followup_at must be in the future")
    if not complete_beat:
        _enabled(config)
        if run_id is None:
            raise HeartbeatError("wait requires the active heartbeat run")
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        _open(beat)
        run = _owned(con, beat, run_id) if run_id is not None else None
        _no_uncertain_actions(con, beat)
        if complete_beat and run is not None:
            newer = con.execute(
                "SELECT 1 FROM _enso_beat_events WHERE beat_id = ? "
                "AND requires_handling = 1 AND id > ? LIMIT 1",
                (beat.id, run.input_cutoff),
            ).fetchone()
            if newer:
                raise HeartbeatError(
                    "new observations arrived after this run started; wait for another assessment "
                    "before completing the beat"
                )
        if followup and beat.expires_at and followup >= beat.expires_at:
            raise HeartbeatError("followup_at must be before expires_at")
        definition = replace(beat.definition, followup_at=followup)
        if not complete_beat and definition.at and followup is None:
            raise HeartbeatError("an unfinished one-shot beat needs followup_at to wait again")
        cutoff = (
            run.input_cutoff
            if run
            else con.execute(
                "SELECT COALESCE(max(id), 0) FROM _enso_beat_events WHERE beat_id = ?",
                (beat.id,),
            ).fetchone()[0]
        )
        next_due = None if complete_beat else next_check(replace(definition, at=None), stamp)
        state = "fulfilled" if complete_beat else beat.state
        con.execute(
            """UPDATE _enso_beats SET definition = ?, state = ?, attention = 0,
               checkpoint = ?, handled_event_id = ?, next_check_at = ?, updated_at = ?,
               closed_at = ?, revision = revision + ? WHERE id = ?""",
            (
                _json(definition.as_dict()),
                state,
                _json(saved_checkpoint if saved_checkpoint is not None else beat.checkpoint),
                cutoff,
                next_due,
                stamp,
                stamp if complete_beat else None,
                int(run is None),
                beat.id,
            ),
        )
        if run:
            con.execute(
                "UPDATE _enso_beat_runs SET settlement = ? WHERE id = ?",
                ("complete" if complete_beat else "wait", run.id),
            )
        result = _load(con, ref)
        _record(
            con,
            result,
            "fulfilled" if complete_beat else "waited",
            message,
            actor=actor,
            run_id=run_id,
            payload={
                "handled_through": cutoff,
                "checkpoint": result.checkpoint,
                "followup_at": followup,
            },
        )
        return result


def complete(
    config: Config,
    ref: str,
    *,
    message: str,
    actor: str = "user",
    run_id: str | None = None,
    checkpoint: object | None = None,
) -> Beat:
    """Record fulfillment with evidence after unfinished actions have been reconciled."""
    return _settle(
        config,
        ref,
        message=message,
        actor=actor,
        run_id=run_id,
        checkpoint=checkpoint,
        followup_at=None,
        complete_beat=True,
    )


def wait(
    config: Config,
    ref: str,
    *,
    message: str,
    actor: str = "user",
    run_id: str | None = None,
    checkpoint: object | None = None,
    followup_at: str | None = None,
) -> Beat:
    """Acknowledge only this run's input, leaving observations received later pending."""
    return _settle(
        config,
        ref,
        message=message,
        actor=actor,
        run_id=run_id,
        checkpoint=checkpoint,
        followup_at=followup_at,
        complete_beat=False,
    )


def note(
    config: Config,
    ref: str,
    message: str,
    *,
    actor: str = "user",
    run_id: str | None = None,
    occurred_at: str | None = None,
) -> BeatEvent:
    payload = (
        {"occurred_at": timestamp(occurred_at, "occurred_at")} if occurred_at is not None else {}
    )
    message = _message(message)
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        if run_id is not None:
            _owned(con, beat, run_id)
        return _record(
            con,
            beat,
            "noted",
            message,
            actor=actor,
            run_id=run_id,
            payload=payload,
            requires_handling=run_id is None and not beat.closed,
        )


# -- Gate observations, checks, and execution claims ----------------------------


def due(config: Config, now: datetime, *, limit: int = 100) -> list[Beat]:
    _limit(limit)
    if not config.heartbeat.enabled:
        return []
    stamp = utc(now)
    with _reader(config.paths) as con:
        if con is None:
            return []
        rows = con.execute(
            """SELECT * FROM _enso_beats b
               WHERE state = 'active' AND claim_run_id IS NULL AND (
                 next_check_at <= ? OR (
                   COALESCE(last_check_status, '') != 'error' AND
                   (json_extract(definition, '$.at') IS NULL OR at_consumed = 1
                     OR json_extract(definition, '$.at') <= ?)
                   AND EXISTS (SELECT 1 FROM _enso_beat_events e
                     WHERE e.beat_id = b.id AND e.requires_handling = 1
                     AND e.id > b.handled_event_id
                     AND e.id > COALESCE((SELECT max(r.input_cutoff)
                       FROM _enso_beat_runs r WHERE r.beat_id = b.id), 0))))
               ORDER BY COALESCE(next_check_at, ?), id LIMIT ?""",
            (stamp, stamp, stamp, limit),
        ).fetchall()
        return [_beat(row) for row in rows]


def due_reason(paths: Paths, beat: Beat, now: datetime) -> str | None:
    """Recheck readiness after taking the beat lock, without replaying already-attempted input."""
    if beat.state != "active" or beat.claim_run_id is not None:
        return None
    stamp = utc(now)
    if beat.expires_at and beat.expires_at <= stamp:
        return "expired"
    if beat.followup_at and beat.followup_at <= stamp:
        return "followup"
    if beat.next_check_at and beat.next_check_at <= stamp:
        return "at" if beat.at and not beat.at_consumed else "schedule"
    if beat.last_check_status == "error":
        # Failed gates retry on their cadence; unclaimed pending input is not a busy retry loop.
        return None
    if beat.at and not beat.at_consumed and beat.at > stamp:
        return None
    with _reader(paths) as con:
        if con is None:
            return None
        found = con.execute(
            """SELECT 1 FROM _enso_beat_events WHERE beat_id = ? AND requires_handling = 1
               AND id > ? AND id > COALESCE((SELECT max(input_cutoff)
                 FROM _enso_beat_runs WHERE beat_id = ?), 0) LIMIT 1""",
            (beat.id, beat.handled_event_id, beat.id),
        ).fetchone()
        return "new_events" if found else None


def _after_check(beat: Beat, stamp: str) -> str | None:
    definition = replace(beat.definition, at=None)
    if definition.followup_at and definition.followup_at <= stamp:
        definition = replace(definition, followup_at=None)
    return next_check(definition, stamp)


def record_check(
    config: Config,
    ref: str,
    status: str,
    *,
    error: str = "",
    checked_at: datetime | None = None,
    expected_revision: int | None = None,
) -> BeatEvent | None:
    """Overwrite check metadata; only entering and leaving failure appends history."""
    _enabled(config)
    if status not in ("ready", "quiet", "error"):
        raise HeartbeatError("check status must be ready, quiet, or error")
    if status == "error":
        error = _message(error)
    stamp = utc(checked_at) if checked_at is not None else db.now()
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        _revision(beat, expected_revision)
        if beat.state != "active":
            raise HeartbeatError(f"{ref} is {beat.state}; no checks may run")
        attention = beat.attention
        if beat.last_check_status == "error" and status != "error":
            last_run = con.execute(
                "SELECT status, settlement FROM _enso_beat_runs "
                "WHERE beat_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
                (beat.id,),
            ).fetchone()
            attention = bool(_actions(con, beat, unresolved=True)) or bool(
                last_run
                and last_run["status"] not in ("ok", "running")
                and last_run["settlement"] is None
            )
        attention = attention or status == "error" or (beat.at is not None and status == "quiet")
        definition = beat.definition
        if definition.followup_at and definition.followup_at <= stamp:
            definition = replace(definition, followup_at=None)
        con.execute(
            """UPDATE _enso_beats SET last_check_at = ?, last_check_status = ?,
               last_check_error = ?, last_success_at = ?, next_check_at = ?, attention = ?,
               definition = ?, at_consumed = ?
               WHERE id = ?""",
            (
                stamp,
                status,
                error if status == "error" else None,
                beat.last_success_at if status == "error" else stamp,
                _after_check(beat, stamp),
                attention,
                _json(definition.as_dict()),
                beat.at is not None,
                beat.id,
            ),
        )
        if status == "error" and beat.last_check_status != "error":
            return _record(con, beat, "check_failed", error)
        if status != "error" and beat.last_check_status == "error":
            message = "Heartbeat checks recovered"
            if beat.at is not None:
                message += "; this one-shot needs an explicit new time or follow-up before retrying"
            return _record(con, beat, "check_recovered", message)
        if beat.at is not None and status == "quiet" and not beat.at_consumed:
            return _record(
                con,
                beat,
                "one_shot_not_ready",
                "The one-shot gate found nothing ready; set a new time or follow-up to retry",
            )
        return None


def persist_observation(
    config: Config,
    ref: str,
    text: str,
    *,
    expected_revision: int | None = None,
) -> BeatEvent | None:
    """Persist gate output before provider work, deduplicating identical pending observations."""
    _enabled(config)
    if not text.strip():
        return None
    text = text.strip()[-MAX_OUTPUT:]
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        _revision(beat, expected_revision)
        if beat.state != "active":
            raise HeartbeatError(f"{ref} is {beat.state}; its gate cannot add observations")
        existing = con.execute(
            """SELECT * FROM _enso_beat_events WHERE beat_id = ? AND kind = 'observed'
               AND id > ? AND message = ? ORDER BY id DESC LIMIT 1""",
            (beat.id, beat.handled_event_id, text),
        ).fetchone()
        if existing is not None:
            return _event(existing)
        return _record(con, beat, "observed", text, requires_handling=True)


def start_run(
    config: Config,
    ref: str,
    *,
    expected_revision: int | None = None,
    trigger: str = "scheduled",
    started_at: datetime | None = None,
    effort: str | None = None,
) -> BeatRun:
    """Atomically claim the beat and freeze the definition and event cutoff for one run."""
    _enabled(config)
    stamp = utc(started_at) if started_at is not None else db.now()
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        _revision(beat, expected_revision)
        if beat.state != "active":
            raise HeartbeatError(f"{ref} is {beat.state}; resume it before running")
        if beat.expires_at is not None and beat.expires_at <= stamp:
            raise HeartbeatError(f"{ref} reached expires_at; it may not start a run")
        if beat.claim_run_id is not None:
            raise HeartbeatError(f"{ref} is already held by run {beat.claim_run_id}")
        cutoff = con.execute(
            "SELECT COALESCE(max(id), 0) FROM _enso_beat_events WHERE beat_id = ?", (beat.id,)
        ).fetchone()[0]
        run_id = uuid.uuid4().hex
        definition = beat.definition
        if effort is not None:
            definition = replace(definition, agent=replace(definition.agent, effort=effort))
        con.execute(
            """INSERT INTO _enso_beat_runs
               (id, beat_id, definition, definition_revision,
                input_cutoff, trigger, started_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'running')""",
            (
                run_id,
                beat.id,
                _json(definition.as_dict()),
                beat.revision,
                cutoff,
                trigger,
                stamp,
            ),
        )
        current_definition = beat.definition
        if current_definition.followup_at and current_definition.followup_at <= stamp:
            current_definition = replace(current_definition, followup_at=None)
        con.execute(
            "UPDATE _enso_beats SET claim_run_id = ?, at_consumed = ?, "
            "next_check_at = ?, definition = ? WHERE id = ?",
            (
                run_id,
                beat.at is not None,
                _after_check(beat, stamp),
                _json(current_definition.as_dict()),
                beat.id,
            ),
        )
        return _load_run(con, run_id)


def _uncertain(con: sqlite3.Connection, beat: Beat, run: BeatRun) -> None:
    for action in _actions(con, beat, unresolved=True):
        if action.action_status == "pending" and action.run_id == run.id:
            _record(
                con,
                beat,
                "action_uncertain",
                "Run ended before the action's outcome was recorded",
                run_id=run.id,
                action_key=action.action_key,
                action_status="uncertain",
            )


def _finish(
    con: sqlite3.Connection,
    run: BeatRun,
    *,
    status: str,
    output: str,
    error: str,
    exit_code: int | None,
    session_id: str | None,
    duration_ms: int | None,
) -> BeatRun:
    if run.status != "running":
        return run
    beat = _load(con, run.ref)
    previous = con.execute(
        "SELECT status, error FROM _enso_beat_runs WHERE beat_id = ? AND id != ? "
        "AND status != 'running' ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (beat.id, run.id),
    ).fetchone()
    if status == "ok" and run.settlement is None:
        status = "error"
        error = "Run ended without recording wait or complete; observations remain unhandled"
    _uncertain(con, beat, run)
    stamp = db.now()
    con.execute(
        """UPDATE _enso_beat_runs SET status = ?, ended_at = ?, output = ?, error = ?,
           exit_code = ?, session_id = ?, duration_ms = ? WHERE id = ?""",
        (
            status,
            stamp,
            output[-MAX_OUTPUT:],
            error[-MAX_OUTPUT:],
            exit_code,
            session_id,
            duration_ms,
            run.id,
        ),
    )
    if beat.claim_run_id == run.id:
        con.execute(
            "UPDATE _enso_beats SET claim_run_id = NULL, attention = ? WHERE id = ?",
            (beat.attention or status != "ok", beat.id),
        )
    if status != "ok":
        notify = previous is None or previous["status"] == "ok" or previous["error"] != error
        _record(
            con,
            beat,
            "run_failed",
            error or f"Run ended with status {status}",
            run_id=run.id,
            payload={"notify": notify},
        )
    elif previous is not None and previous["status"] != "ok":
        _record(
            con,
            beat,
            "run_recovered",
            "Heartbeat assessment recovered and recorded progress",
            run_id=run.id,
        )
    return _load_run(con, run.id)


def finish_run(
    config: Config,
    run_id: str,
    *,
    status: str,
    output: str = "",
    error: str = "",
    exit_code: int | None = None,
    session_id: str | None = None,
    duration_ms: int | None = None,
) -> BeatRun:
    """Close an attempt once, preserving settlement and uncertainty on interrupted actions."""
    if status not in ("ok", "error", "timeout", "cancelled"):
        raise HeartbeatError("run status must be ok, error, timeout, or cancelled")
    with db.transaction(config.paths) as con:
        return _finish(
            con,
            _load_run(con, run_id),
            status=status,
            output=output,
            error=error,
            exit_code=exit_code,
            session_id=session_id,
            duration_ms=duration_ms,
        )


def recover(config: Config) -> list[BeatRun]:
    """Recover only attempts whose per-beat lock is free, never another daemon's live work."""
    with _reader(config.paths) as con:
        if con is None:
            return []
        rows = con.execute(
            "SELECT * FROM _enso_beat_runs WHERE status = 'running' ORDER BY started_at"
        ).fetchall()
    recovered: list[BeatRun] = []
    for row in rows:
        candidate = _run(row)
        lock = acquire_lock(config.paths, candidate.ref)
        if lock is None:
            continue
        with lock, db.transaction(config.paths) as con:
            run = _load_run(con, candidate.id)
            if run.status == "running":
                recovered.append(
                    _finish(
                        con,
                        run,
                        status="error",
                        output=run.output,
                        error="Enso stopped before this heartbeat run finished",
                        exit_code=None,
                        session_id=run.session_id,
                        duration_ms=None,
                    )
                )
    return recovered


# -- Action receipts and retention ----------------------------------------------


def _action(con: sqlite3.Connection, beat: Beat, key: str) -> BeatEvent | None:
    row = con.execute(
        "SELECT * FROM _enso_beat_events WHERE beat_id = ? AND action_key = ? "
        "ORDER BY id DESC LIMIT 1",
        (beat.id, key),
    ).fetchone()
    return _event(row) if row is not None else None


def begin_action(
    config: Config,
    ref: str,
    key: str,
    message: str,
    *,
    actor: str = "user",
    run_id: str | None = None,
) -> BeatEvent:
    """Reserve a stable action key before an external change; retries need a known failure."""
    _enabled(config)
    key, message = _message(key), _message(message)
    if run_id is None:
        raise HeartbeatError("begin_action requires the active heartbeat run")
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        _owned(con, beat, run_id)
        previous = _action(con, beat, key)
        if previous is not None and previous.action_status != "failed":
            raise HeartbeatError(
                f"action {key!r} is {previous.action_status}; do not repeat it. "
                f"Read its receipt: enso heartbeat history {beat.ref} "
                f"--after {previous.id - 1} --limit 1 --json"
            )
        return _record(
            con,
            beat,
            "action_started",
            message,
            actor=actor,
            run_id=run_id,
            action_key=key,
            action_status="pending",
        )


def resolve_action(
    config: Config,
    ref: str,
    key: str,
    status: str,
    message: str,
    *,
    receipt: str | None = None,
    actor: str = "user",
    run_id: str | None = None,
) -> BeatEvent:
    """Record an action's evidence, including manual reconciliation after its run has stopped."""
    if status not in ("succeeded", "failed", "uncertain"):
        raise HeartbeatError("action status must be succeeded, failed, or uncertain")
    message = _message(message)
    with db.transaction(config.paths) as con:
        beat = _load(con, ref)
        previous = _action(con, beat, key)
        if previous is None:
            raise HeartbeatError(f"action {key!r} has not been started")
        if run_id is not None:
            run = _load_run(con, run_id)
            if run.beat_id != beat.id:
                raise HeartbeatError(f"run {run_id} does not belong to {beat.ref}")
            # A receipt reports an already-started effect, even after cancellation or edits.
            # A different run may reconcile uncertainty only while it currently owns the beat.
            if run_id != previous.run_id:
                _owned(con, beat, run_id)
        if previous.action_status not in ("pending", "uncertain"):
            raise HeartbeatError(f"action {key!r} is already {previous.action_status}")
        if previous.action_status == status:
            return previous
        event = _record(
            con,
            beat,
            f"action_{status}",
            message,
            actor=actor,
            run_id=run_id or previous.run_id,
            action_key=key,
            action_status=status,
            receipt=receipt,
        )
        if status == "uncertain":
            con.execute("UPDATE _enso_beats SET attention = 1 WHERE id = ?", (beat.id,))
        return event


def pending_notices(paths: Paths, *, ref: str | None = None, limit: int = 100) -> list[BeatEvent]:
    """Transitions stay pending independently of gate/run state until delivery is recorded."""
    _limit(limit)
    clauses = [
        f"e.kind IN ({','.join('?' for _ in NOTICE_KINDS)})",
        "COALESCE(json_extract(e.payload, '$.notify'), 1) != 0",
    ]
    values: list[object] = list(NOTICE_KINDS)
    if ref is not None:
        clauses.append("e.beat_id = ?")
        values.append(parse_ref(ref))
    clauses.append(
        "NOT EXISTS (SELECT 1 FROM _enso_beat_events delivered "
        "WHERE delivered.beat_id = e.beat_id AND delivered.kind = 'notice_delivered' "
        "AND json_extract(delivered.payload, '$.notice_id') = e.id)"
    )
    with _reader(paths) as con:
        if con is None:
            return []
        rows = con.execute(
            f"SELECT e.* FROM _enso_beat_events e WHERE {' AND '.join(clauses)} "
            "ORDER BY e.id LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [_event(row) for row in rows]


def notice_delivered(config: Config, event: BeatEvent, outbox_id: int) -> None:
    """Acknowledge a successfully recorded outbox send without consuming it from the user's chat."""
    with db.transaction(config.paths) as con:
        existing = con.execute(
            "SELECT 1 FROM _enso_beat_events WHERE beat_id = ? AND kind = 'notice_delivered' "
            "AND json_extract(payload, '$.notice_id') = ?",
            (event.beat_id, event.id),
        ).fetchone()
        if existing is None:
            beat = _load(con, f"HB-{event.beat_id:03d}")
            _record(
                con,
                beat,
                "notice_delivered",
                "Delivered heartbeat notification",
                payload={"notice_id": event.id, "outbox_id": outbox_id},
            )


def notice_outbox(paths: Paths, source: str) -> int | None:
    """Find a known successful notice send when its separate event acknowledgement was lost."""
    with _reader(paths) as con:
        if con is None:
            return None
        row = con.execute(
            "SELECT id FROM messages WHERE source = ? AND status = 'sent' ORDER BY id LIMIT 1",
            (source,),
        ).fetchone()
        return row["id"] if row else None


def prune(config: Config, *, now: datetime | None = None) -> list[str]:
    """Prune closed history under its beat locks; return directory names for safe cleanup."""
    if not config.heartbeat.enabled:
        return []
    stamp = datetime.fromisoformat(utc(now)) - timedelta(days=config.heartbeat.retention_days)
    with _reader(config.paths) as con:
        if con is None:
            return []
        rows = con.execute(
            """SELECT id FROM _enso_beats WHERE state IN ('fulfilled', 'cancelled', 'expired')
               AND closed_at < ? AND claim_run_id IS NULL ORDER BY id""",
            (utc(stamp),),
        ).fetchall()
    removed: list[str] = []
    for row in rows:
        ref = f"HB-{row['id']:03d}"
        lock = acquire_lock(config.paths, ref)
        if lock is None:
            continue
        with lock, db.transaction(config.paths) as con:
            deleted = con.execute(
                "DELETE FROM _enso_beats WHERE id = ? AND claim_run_id IS NULL "
                "AND state IN ('fulfilled', 'cancelled', 'expired') AND closed_at < ?",
                (row["id"], utc(stamp)),
            )
            if deleted.rowcount:
                removed.append(ref)
    return removed
