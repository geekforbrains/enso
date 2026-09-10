"""Agent-facing heartbeat commands with bounded input and core-owned lifecycle rules."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from .. import heartbeat, tasks
from ..config import Config, Paths
from .common import JSON_FLAG, columns, echo_json, fail, load

heartbeat_app = typer.Typer(no_args_is_help=True, help="Finite actions and temporary watches.")
INPUT_LIMIT = 256 * 1024
DEFINITION = typer.Option(..., "--file", help="JSON definition or update; - reads stdin.")
MESSAGE = typer.Option(..., "--message", help="What happened and the evidence; - reads stdin.")
CHECKPOINT = typer.Option(None, "--checkpoint", help="Saved gate checkpoint as a JSON object.")


@contextmanager
def _using(*, as_json: bool) -> Iterator[Config]:
    """Keep expected configuration, storage, and lifecycle errors at the CLI boundary."""
    try:
        yield load(Paths.from_env(), as_json=as_json)
    except heartbeat.HeartbeatError as exc:
        fail(exc.problems, as_json=as_json)
    except (OSError, UnicodeError, sqlite3.Error) as exc:
        fail([str(exc)], as_json=as_json)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON objects must not contain duplicate keys")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("JSON must not contain non-finite numbers")


def _object(text: str) -> dict[str, object]:
    """Parse one bounded JSON object without silently replacing repeated fields."""
    if len(text.encode()) > INPUT_LIMIT:
        raise heartbeat.HeartbeatError([f"JSON input exceeds {INPUT_LIMIT} bytes"])
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except json.JSONDecodeError as exc:
        raise heartbeat.HeartbeatError(
            [f"invalid JSON at line {exc.lineno}, column {exc.colno}"]
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise heartbeat.HeartbeatError(
            ["JSON is nested too deeply" if isinstance(exc, RecursionError) else str(exc)]
        ) from exc
    if not isinstance(value, dict):
        raise heartbeat.HeartbeatError(["JSON input must be an object"])
    return value


def _stdin() -> str:
    text = sys.stdin.read(INPUT_LIMIT + 1)
    if len(text.encode()) > INPUT_LIMIT:
        raise heartbeat.HeartbeatError([f"input exceeds {INPUT_LIMIT} bytes"])
    return text


def _definition(file: Path) -> dict[str, object]:
    if str(file) == "-":
        return _object(_stdin())
    with file.open("rb") as source:
        text = source.read(INPUT_LIMIT + 1)
    if len(text) > INPUT_LIMIT:
        raise heartbeat.HeartbeatError([f"JSON input exceeds {INPUT_LIMIT} bytes"])
    return _object(text.decode("utf-8"))


def _message(text: str) -> str:
    return _stdin() if text == "-" else text


def _checkpoint(text: str | None) -> dict[str, object] | None:
    return _object(text) if text is not None else None


def _actor() -> str:
    if beat := os.environ.get("ENSO_BEAT"):
        return f"beat:{beat}"
    return tasks.actor_from_env(os.environ)


def _run_id() -> str | None:
    return os.environ.get("ENSO_BEAT_RUN_ID") or None


def _management_only() -> None:
    """A provider's frozen instructions cannot authorize replacing newer user instructions."""
    if _run_id():
        raise heartbeat.HeartbeatError(
            [
                "create and update require a chat or terminal; "
                "during a heartbeat run use note or wait"
            ]
        )


def _owned(config: Config, ref: str) -> None:
    """A background beat may change itself, never another beat through an altered ref."""
    owner = os.environ.get("ENSO_BEAT")
    if not owner and _run_id():
        raise heartbeat.HeartbeatError(["a beat run must identify ENSO_BEAT"])
    if owner:
        beat = heartbeat.get(config.paths, ref)
        current = heartbeat.get(config.paths, owner)
        if beat is None or current is None or beat.ref != current.ref:
            raise heartbeat.HeartbeatError(["a beat run may only change its own beat"])


def _defaults(data: dict[str, object]) -> dict[str, object]:
    data.setdefault("workspace", os.environ.get("ENSO_WORKSPACE") or "default")
    origin = {
        key: value
        for key in ("transport", "user_id", "user_name", "channel", "channel_name", "thread_ts")
        if (value := os.environ.get(f"ENSO_ORIGIN_{key.upper()}"))
    }
    if origin:
        data.setdefault("origin", origin)
    return data


def _emit(beat: heartbeat.Beat, *, as_json: bool, verb: str) -> None:
    if as_json:
        echo_json(beat.as_dict())
    else:
        typer.echo(f"{verb} {beat.ref}: {beat.state} — {beat.title}")


@heartbeat_app.command("status")
def heartbeat_status(as_json: bool = JSON_FLAG) -> None:
    """Show whether heartbeat is enabled and how long closed beats are retained."""
    with _using(as_json=as_json) as config:
        value = {
            "enabled": config.heartbeat.enabled,
            "retention_days": config.heartbeat.retention_days,
        }
        if as_json:
            echo_json(value)
        else:
            typer.echo(f"heartbeat: {'enabled' if value['enabled'] else 'disabled'}")
            typer.echo(f"closed beat retention: {value['retention_days']} days")


@heartbeat_app.command("create")
def heartbeat_create(file: Path = DEFINITION, as_json: bool = JSON_FLAG) -> None:
    """Save a paused beat; create its returned directory when adding a gate, then resume."""
    with _using(as_json=as_json) as config:
        _management_only()
        beat = heartbeat.create(config, _defaults(_definition(file)), actor=_actor())
        directory = str(config.paths.heartbeat / beat.ref)
        if as_json:
            echo_json({**beat.as_dict(), "directory": directory})
        else:
            _emit(beat, as_json=False, verb="created")
            typer.echo(f"directory: {directory}")


@heartbeat_app.command("update")
def heartbeat_update(
    ref: str,
    file: Path = DEFINITION,
    if_revision: int | None = typer.Option(None, "--if-revision", help="Refuse a stale update."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Merge changed definition fields; omitted fields keep their current values."""
    with _using(as_json=as_json) as config:
        _management_only()
        _owned(config, ref)
        beat = heartbeat.update(
            config, ref, _definition(file), actor=_actor(), expected_revision=if_revision
        )
        _emit(beat, as_json=as_json, verb="updated")


@heartbeat_app.command("list")
def heartbeat_list(
    state: str | None = typer.Option(None, "--state"),
    workspace: str | None = typer.Option(None, "--workspace"),
    show_all: bool = typer.Option(False, "--all", help="Include closed beats."),
    limit: int = typer.Option(100, "--limit"),
    offset: int = typer.Option(0, "--offset"),
    as_json: bool = JSON_FLAG,
) -> None:
    """List current beats, or include recent closed beats with --all."""
    with _using(as_json=as_json) as config:
        found = heartbeat.list_beats(
            config.paths,
            state=state,
            workspace=workspace,
            include_closed=show_all,
            limit=limit,
            offset=offset,
        )
        if as_json:
            echo_json([beat.as_dict() for beat in found])
        elif not found:
            typer.echo("no beats match")
        else:
            rows = [["REF", "STATE", "WORKSPACE", "TITLE"]]
            rows.extend([beat.ref, beat.state, beat.workspace, beat.title] for beat in found)
            typer.echo(columns(rows))


@heartbeat_app.command("show")
def heartbeat_show(ref: str, as_json: bool = JSON_FLAG) -> None:
    """Show current instructions and history pointers without loading the whole history."""
    with _using(as_json=as_json) as config:
        value = heartbeat.context(config.paths, ref, run_id=_run_id())
        if as_json:
            echo_json(value)
        else:
            for key, item in value.items():
                text = (
                    json.dumps(item, ensure_ascii=False) if isinstance(item, dict | list) else item
                )
                typer.echo(f"{key}: {text if text is not None else '-'}")


@heartbeat_app.command("history")
def heartbeat_history(
    ref: str,
    limit: int = typer.Option(100, "--limit"),
    before: int | None = typer.Option(None, "--before", help="Events before this event ID."),
    after: int | None = typer.Option(None, "--after", help="Events after this event ID."),
    unhandled: bool = typer.Option(False, "--unhandled", help="Pending observations only."),
    kind: str | None = typer.Option(None, "--kind"),
    as_json: bool = JSON_FLAG,
) -> None:
    """Read timestamped history, with optional pagination and pending-event filters."""
    with _using(as_json=as_json) as config:
        found = heartbeat.history(
            config.paths,
            ref,
            limit=limit,
            before_id=before,
            after_id=after,
            kind=kind,
            unhandled=unhandled,
        )
        if as_json:
            echo_json([event.as_dict() for event in found])
        elif not found:
            typer.echo("no events match")
        else:
            for event in found:
                happened = event.payload.get("occurred_at")
                source_time = f" (happened {happened})" if happened else ""
                typer.echo(
                    f"{event.id} {event.created_at} {event.kind}{source_time} "
                    f"by {event.actor}: {event.message}"
                )


def _transition(ref: str, name: str, message: str, *, as_json: bool) -> None:
    with _using(as_json=as_json) as config:
        _owned(config, ref)
        operation = {
            "pause": heartbeat.pause,
            "resume": heartbeat.resume,
            "cancel": heartbeat.cancel,
            "expire": heartbeat.expire,
        }[name]
        beat = operation(config, ref, message=_message(message), actor=_actor(), run_id=_run_id())
        _emit(beat, as_json=as_json, verb=name)


@heartbeat_app.command("pause")
def heartbeat_pause(
    ref: str, message: str = typer.Option("", "--message"), as_json: bool = JSON_FLAG
) -> None:
    """Suspend a beat while preserving its instructions and pending work."""
    _transition(ref, "pause", message, as_json=as_json)


@heartbeat_app.command("resume")
def heartbeat_resume(
    ref: str, message: str = typer.Option("", "--message"), as_json: bool = JSON_FLAG
) -> None:
    """Validate the saved beat and gate, then enable future checks."""
    _transition(ref, "resume", message, as_json=as_json)


@heartbeat_app.command("cancel")
def heartbeat_cancel(ref: str, message: str = MESSAGE, as_json: bool = JSON_FLAG) -> None:
    """Close a beat because its objective is no longer wanted."""
    _transition(ref, "cancel", message, as_json=as_json)


@heartbeat_app.command("expire")
def heartbeat_expire(ref: str, message: str = MESSAGE, as_json: bool = JSON_FLAG) -> None:
    """Close a beat because the opportunity or deadline has passed."""
    _transition(ref, "expire", message, as_json=as_json)


@heartbeat_app.command("complete")
def heartbeat_complete(
    ref: str,
    message: str = MESSAGE,
    checkpoint: str | None = CHECKPOINT,
    as_json: bool = JSON_FLAG,
) -> None:
    """Fulfill a beat with the outcome and supporting evidence."""
    with _using(as_json=as_json) as config:
        _owned(config, ref)
        beat = heartbeat.complete(
            config,
            ref,
            message=_message(message),
            actor=_actor(),
            run_id=_run_id(),
            checkpoint=_checkpoint(checkpoint),
        )
        _emit(beat, as_json=as_json, verb="completed")


@heartbeat_app.command("wait")
def heartbeat_wait(
    ref: str,
    message: str = MESSAGE,
    checkpoint: str | None = CHECKPOINT,
    followup_at: str | None = typer.Option(
        None, "--followup-at", help="Next needed attention time."
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Finish this run's assessment while keeping the beat open for future developments."""
    with _using(as_json=as_json) as config:
        _owned(config, ref)
        beat = heartbeat.wait(
            config,
            ref,
            message=_message(message),
            actor=_actor(),
            run_id=_run_id(),
            checkpoint=_checkpoint(checkpoint),
            followup_at=followup_at,
        )
        _emit(beat, as_json=as_json, verb="waiting")


@heartbeat_app.command("note")
def heartbeat_note(
    ref: str,
    message: str,
    occurred_at: str | None = typer.Option(
        None, "--occurred-at", help="When the event happened, as a timestamp with timezone."
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Append useful context without changing the beat's current instructions."""
    with _using(as_json=as_json) as config:
        _owned(config, ref)
        event = heartbeat.note(
            config,
            ref,
            _message(message),
            actor=_actor(),
            run_id=_run_id(),
            occurred_at=occurred_at,
        )
        if as_json:
            echo_json(event.as_dict())
        else:
            typer.echo(f"recorded event {event.id}")


@heartbeat_app.command("action")
def heartbeat_action(ref: str, key: str, message: str = MESSAGE, as_json: bool = JSON_FLAG) -> None:
    """Record an intended external action before performing it; keys identify retries."""
    with _using(as_json=as_json) as config:
        _owned(config, ref)
        event = heartbeat.begin_action(
            config, ref, key, _message(message), actor=_actor(), run_id=_run_id()
        )
        if as_json:
            echo_json(event.as_dict())
        else:
            typer.echo(f"recorded action {key}: event {event.id}")


@heartbeat_app.command("action-result")
def heartbeat_action_result(
    ref: str,
    key: str,
    status: str = typer.Option(..., "--status", help="succeeded, failed, or uncertain."),
    message: str = MESSAGE,
    receipt: str | None = typer.Option(None, "--receipt", help="Delivery receipt or evidence."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Record a confirmed or uncertain outcome before deciding whether an action may retry."""
    with _using(as_json=as_json) as config:
        _owned(config, ref)
        event = heartbeat.resolve_action(
            config,
            ref,
            key,
            status,
            _message(message),
            receipt=receipt,
            actor=_actor(),
            run_id=_run_id(),
        )
        if as_json:
            echo_json(event.as_dict())
        else:
            typer.echo(f"recorded action {key}: {status} (event {event.id})")
