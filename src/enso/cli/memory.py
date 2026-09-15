"""Recall episodic memory and settle bounded refinement batches through the core API."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import typer

from .. import db, memory
from ..config import Config, ConfigError, Paths, load_config
from ..formatting import preview
from .common import JSON_FLAG, InputError, echo_json, read_input

memory_app = typer.Typer(no_args_is_help=True, help="Recall dated Enso conversations.")
WORKSPACE = typer.Option(None, "--workspace", help="Default: ENSO_WORKSPACE, otherwise all.")
ALL_WORKSPACES = typer.Option(False, "--all-workspaces", help="Ignore ENSO_WORKSPACE.")
TRANSPORT = typer.Option(None, "--transport", help="Exact transport name.")
CHANNEL = typer.Option(None, "--channel", help="Exact channel ID.")
SINCE = typer.Option(None, "--since", help="today, week, month, Nh, Nd, or ISO date/timestamp.")
UNTIL = typer.Option(None, "--until", help="Exclusive date boundary; same values as --since.")
LIMIT = typer.Option(20, "--limit", min=1, max=100)
OFFSET = typer.Option(0, "--offset", min=0)
BATCH = typer.Option(..., "--batch", help="Stable batch ID, normally ENSO_RUN_ID.")


@contextmanager
def _using(*, as_json: bool, error_code: int = 1, write: bool = False) -> Iterator[Config]:
    try:
        config = load_config(Paths.from_env())
        if write:
            db.migrate(config.paths)
        yield config
    except (
        ConfigError,
        db.UnreadableDatabaseError,
        db.UnsupportedDatabaseError,
        InputError,
        OSError,
        sqlite3.Error,
        ValueError,
    ) as exc:
        if as_json:
            echo_json({"ok": False, "error": str(exc)})
        else:
            typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(error_code) from exc


def _workspace(workspace: str | None, all_workspaces: bool) -> str | None:
    if workspace is not None and all_workspaces:
        raise memory.MemoryError("give --workspace or --all-workspaces, not both")
    return None if all_workspaces else workspace or os.environ.get("ENSO_WORKSPACE") or None


def _when(stamp: str, timezone: str) -> str:
    return (
        datetime.fromisoformat(stamp)
        .astimezone(memory.timezone_info(timezone))
        .strftime("%Y-%m-%d %H:%M %Z")
    )


def _listing(
    query: str,
    workspace: str | None,
    all_workspaces: bool,
    transport: str | None,
    channel: str | None,
    since: str | None,
    until: str | None,
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    with _using(as_json=as_json) as config:
        result = memory.list_entries(
            config.paths,
            workspace=_workspace(workspace, all_workspaces),
            transport=transport,
            channel=channel,
            since=memory.parse_since(since, config.memory.timezone) if since else None,
            until=memory.parse_since(until, config.memory.timezone) if until else None,
            query=query,
            limit=limit,
            offset=offset,
        )
        if as_json:
            echo_json(
                {
                    "entries": [entry.as_dict() for entry in result.entries],
                    "total": result.total,
                    "limit": limit,
                    "offset": offset,
                }
            )
        else:
            for entry in result.entries:
                data = entry.as_dict()
                location = data.get("channel_name") or data.get("channel") or ""
                origin = f"{data['transport']}:{location}" if location else data["transport"]
                typer.echo(
                    f"{data['ref']}  {_when(data['occurred_at'], config.memory.timezone)}  "
                    f"{data['workspace']}  {origin}\n  {preview(data['summary'], 240)}"
                )
            typer.echo(f"{len(result.entries)} of {result.total} memories")


@memory_app.command("list")
def list_memories(
    workspace: str | None = WORKSPACE,
    all_workspaces: bool = ALL_WORKSPACES,
    transport: str | None = TRANSPORT,
    channel: str | None = CHANNEL,
    since: str | None = SINCE,
    until: str | None = UNTIL,
    limit: int = LIMIT,
    offset: int = OFFSET,
    as_json: bool = JSON_FLAG,
) -> None:
    """List recent memories, newest event first, within optional context and date filters."""
    _listing(
        "", workspace, all_workspaces, transport, channel, since, until, limit, offset, as_json
    )


@memory_app.command("search")
def search(
    query: str,
    workspace: str | None = WORKSPACE,
    all_workspaces: bool = ALL_WORKSPACES,
    transport: str | None = TRANSPORT,
    channel: str | None = CHANNEL,
    since: str | None = SINCE,
    until: str | None = UNTIL,
    limit: int = LIMIT,
    offset: int = OFFSET,
    as_json: bool = JSON_FLAG,
) -> None:
    """Search memory summaries with the same filters and ordering as list."""
    _listing(
        query, workspace, all_workspaces, transport, channel, since, until, limit, offset, as_json
    )


@memory_app.command("show")
def show(
    ref: str,
    with_sources: bool = typer.Option(False, "--sources", help="Include original clean exchanges."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Read a memory by ID, optionally including its underlying conversation exchanges."""
    with _using(as_json=as_json) as config:
        entry = memory.get_entry(config.paths, ref)
        if entry is None:
            raise memory.MemoryError(f"no memory named {ref}")
        result = entry.as_dict()
        if with_sources:
            result["sources"] = [source.as_dict() for source in memory.sources(config.paths, ref)]
        if as_json:
            echo_json(result)
        else:
            typer.echo(
                f"{result['ref']} · {result['workspace']} · {result['transport']}\n"
                f"Event: {_when(result['occurred_at'], config.memory.timezone)}\n"
                f"Recorded: {_when(result['created_at'], config.memory.timezone)}\n\n"
                f"{result['summary']}"
            )
            for source in result.get("sources", []):
                sender = source["user_id"] or "unknown"
                if source["user_name"]:
                    sender = f"{source['user_name']} ({sender})"
                channel = source["channel"] or "unknown"
                if source["channel_name"]:
                    channel = f"{source['channel_name']} ({channel})"
                completed = (
                    _when(source["completed_at"], config.memory.timezone)
                    if source["completed_at"]
                    else "not completed"
                )
                typer.echo(
                    f"\nExchange {source['id']} · {source['status']}\n"
                    f"Received: {_when(source['received_at'], config.memory.timezone)}\n"
                    f"Completed: {completed}\n"
                    f"Origin: {source['workspace']} · {source['transport']} · {channel}\n"
                    f"Sender: {sender}"
                )
                if source["thread"]:
                    typer.echo(f"Thread: {source['thread']}")
                for field in ("request", "response"):
                    if source[f"{field}_truncated"]:
                        typer.echo(f"{field.capitalize()} truncated in storage.")
                if source["error"]:
                    typer.echo(f"Error: {source['error']}")
                typer.echo(f"User: {source['request']}\nAgent: {source['response']}")


@memory_app.command("status")
def status(as_json: bool = JSON_FLAG) -> None:
    """Show capture settings, saved history, and the refinement backlog."""
    with _using(as_json=as_json) as config:
        result = {
            "enabled": config.memory.enabled,
            "timezone": config.memory.timezone,
            **memory.status(config.paths),
        }
        if as_json:
            echo_json(result)
        else:
            typer.echo(
                f"Memory {'enabled' if result['enabled'] else 'disabled'}; "
                f"timezone: {result['timezone']}\n"
                f"{result['entries']} memories, {result['turns']} exchanges, "
                f"{result['pending_turns']} awaiting refinement"
            )


@memory_app.command("forget")
def forget(
    ref: str,
    yes: bool = typer.Option(False, "--yes", help="Confirm permanent removal of this history."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Permanently remove a memory, its sources, and any memories sharing those sources."""
    with _using(as_json=as_json, write=True) as config:
        if not yes:
            raise memory.MemoryError(
                "forget permanently removes the memory, its source exchanges, and memories "
                "sharing those sources; repeat with --yes"
            )
        if not memory.forget(config.paths, ref):
            raise memory.MemoryError(f"no memory named {ref}")
        if as_json:
            echo_json({"ok": True, "ref": ref})
        else:
            typer.echo(f"Forgot {ref} and its source history")


@memory_app.command("prepare")
def prepare(
    batch: str = BATCH,
    limit: int = typer.Option(40, "--limit", min=1, max=40),
    as_json: bool = JSON_FLAG,
) -> None:
    """Prepare a refinement batch; exit 1 for no work or disabled Memory, 2 for errors."""
    with _using(as_json=as_json, error_code=2, write=True) as config:
        result = (
            memory.prepare_batch(config.paths, batch_id=batch, limit=limit)
            if config.memory.enabled
            else None
        )
        if result is None:
            if as_json:
                echo_json({"batch": None, "enabled": config.memory.enabled})
            else:
                typer.echo("no work" if config.memory.enabled else "Memory is disabled")
            raise typer.Exit(1)
        echo_json(result.as_dict())


@memory_app.command("record")
def record(
    batch: str = BATCH,
    file: str = typer.Option(..., "--file", help="JSON array of memories; - reads stdin."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Atomically record summaries and settle every exchange in a prepared batch."""
    with _using(as_json=as_json, write=True) as config:
        if not config.memory.enabled:
            raise memory.MemoryError("Memory is disabled")
        data = json.loads(read_input(file))
        if not isinstance(data, list) or any(not isinstance(entry, dict) for entry in data):
            raise memory.MemoryError("input must be a JSON array of memory objects")
        entries = memory.record_batch(config.paths, batch, data)
        result = {"ok": True, "batch": batch, "entries": [entry.as_dict() for entry in entries]}
        if as_json:
            echo_json(result)
        else:
            typer.echo(f"Recorded {len(entries)} memories; batch {batch} settled")


@memory_app.command("check")
def check(batch: str = BATCH, as_json: bool = JSON_FLAG) -> None:
    """Check a batch: exit 0 if settled, 10 for a pending batch, and 2 for errors."""
    with _using(as_json=as_json, error_code=2) as config:
        result = memory.batch_status(config.paths, batch)
        if as_json:
            echo_json(result)
        elif result["status"] != "recorded":
            typer.echo(
                "The memory batch is still pending. Record your summaries with "
                '`enso memory record --batch "$ENSO_RUN_ID" --file FILE --json`; '
                "record [] when there is nothing useful. Use only the supplied batch and "
                "stop when it is settled."
            )
        if result["status"] != "recorded":
            raise typer.Exit(10)
