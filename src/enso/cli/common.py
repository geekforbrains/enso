"""Helpers shared by the ``enso`` command modules."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import typer

from .. import db, heartbeat, messages
from ..config import Config, ConfigError, Paths, load_config
from ..formatting import format_elapsed
from ..outbound import OutboundMessage
from ..transports import Transport

JSON_FLAG = typer.Option(False, "--json", help="Print JSON instead of text.")
ACTION_KEY = typer.Option(
    None, "--action-key", help="Stable purpose key; required for sends inside a heartbeat run."
)
INPUT_LIMIT = 256 * 1024
"""UTF-8 bytes accepted per message, note, task body, or heartbeat input."""
log = logging.getLogger(__name__)
_DURATION_RE = re.compile(r"(\d+)([smhd])")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: str) -> timedelta | None:
    """``30m``, ``24h``, ``7d`` (or ``90s``) as a timedelta; None for anything else."""
    match = _DURATION_RE.fullmatch(value.strip().lower())
    if match is None:
        return None
    return timedelta(**{_DURATION_UNITS[match.group(2)]: int(match.group(1))})


def fail(problems: list[str], *, as_json: bool = False) -> NoReturn:
    """Report problems and exit 1; a JSON caller gets ``{"ok": false, "error"}`` on stdout."""
    if as_json:
        typer.echo(json.dumps({"ok": False, "error": "; ".join(problems)}))
    else:
        for problem in problems:
            typer.echo(f"error: {problem}", err=True)
    raise typer.Exit(1)


def load(paths: Paths, *, as_json: bool = False) -> Config:
    """A validated config with the database ready, or exit 1 listing the problems."""
    try:
        config = load_config(paths)
    except ConfigError as exc:
        fail(exc.problems, as_json=as_json)
    try:
        db.migrate(paths)
    except (db.UnsupportedDatabaseError, OSError, sqlite3.Error) as exc:
        fail([str(exc)], as_json=as_json)
    return config


@dataclass(frozen=True)
class _BeatAction:
    config: Config
    ref: str
    run_id: str
    key: str


def _reserve_beat(paths: Paths, key: str | None, description: str) -> _BeatAction | None:
    """Reserve before connecting, refusing expired authority and ambiguous earlier sends."""
    run_id = os.environ.get("ENSO_BEAT_RUN_ID")
    ref = os.environ.get("ENSO_BEAT")
    if not run_id:
        if ref:
            raise heartbeat.HeartbeatError(
                "heartbeat gates cannot send messages; an active run is required"
            )
        if key is not None:
            raise heartbeat.HeartbeatError("--action-key requires a heartbeat run")
        return None
    if not ref:
        raise heartbeat.HeartbeatError("a heartbeat run must identify ENSO_BEAT")
    if not key or not key.strip():
        raise heartbeat.HeartbeatError("sends during a heartbeat run require --action-key")
    config = load_config(paths)
    event = heartbeat.begin_action(
        config, ref, key, description, actor=f"beat:{ref}", run_id=run_id
    )
    assert event.action_key is not None
    return _BeatAction(config, f"HB-{event.beat_id:03d}", run_id, event.action_key)


def _resolve_beat(action: _BeatAction, status: str, receipt: str, error: str = "") -> None:
    message = {
        "succeeded": "Message sent; platform receipt recorded.",
        "uncertain": "The send outcome is unknown; reconcile before retrying.",
        "failed": "The message send did not start.",
    }[status]
    heartbeat.resolve_action(
        action.config,
        action.ref,
        action.key,
        status,
        message + (f" {error}" if error else ""),
        receipt=receipt,
        actor=f"beat:{action.ref}",
        run_id=action.run_id,
    )


def _check_beat(action: _BeatAction) -> None:
    """Connections may yield; recheck current config and authority at the effect boundary."""
    config = load_config(action.config.paths)
    heartbeat.assert_run_active(config, action.ref, action.run_id)


async def deliver(
    paths: Paths,
    transport: Transport,
    target: str,
    thread: str | None,
    *,
    text: str = "",
    rich: OutboundMessage | None = None,
    file: Path | None = None,
    caption: str = "",
    action_key: str | None = None,
) -> dict:
    """Send and keep the outbox and any heartbeat receipt, even during interruption."""
    source = messages.source_from_env(os.environ)
    if file is not None:
        text = f"[file {file.name}] {caption}".strip()
    elif rich is not None:
        text = rich.fallback_text
    action = await asyncio.to_thread(
        _reserve_beat, paths, action_key, f"Send to {transport.name}:{target}: {text}"
    )
    attempted = False
    sent_id: str | None = None

    async def send() -> str:
        nonlocal attempted, sent_id
        if action is not None:
            await asyncio.to_thread(_check_beat, action)
        attempted = True
        if file is not None:
            sent_id = await transport.send_file(target, str(file), caption=caption, thread=thread)
        elif rich is not None:
            sent_id = await transport.send_rich(target, rich, thread=thread)
        else:
            sent_id = await transport.send(target, text, thread=thread)
        return sent_id

    def receipt() -> str:
        return json.dumps(
            {
                "transport": transport.name,
                "target": target,
                "thread": thread,
                "message_id": sent_id,
                "file": file is not None,
            },
            separators=(",", ":"),
        )

    try:
        async with transport.connect():
            message = await messages.deliver(
                paths,
                send(),
                transport=transport.name,
                target=target,
                thread=thread,
                text=text,
                source=source,
            )
            result = await transport.receipt(
                target, message.message_id, thread, file=file is not None
            )
    except BaseException as exc:
        if action is not None:
            status = "succeeded" if sent_id is not None else "uncertain" if attempted else "failed"
            try:
                await asyncio.to_thread(_resolve_beat, action, status, receipt(), str(exc))
            except Exception:
                log.exception("could not record heartbeat send outcome for %s", action.ref)
        raise
    if action is not None:
        await asyncio.to_thread(_resolve_beat, action, "succeeded", receipt())
    return result


def run[T](work: Coroutine[Any, Any, T], *, as_json: bool = False) -> T:
    """Run one CLI action to completion; any failure becomes the command's error."""
    try:
        return asyncio.run(work)
    except Exception as exc:
        fail([error_text(exc)], as_json=as_json)


def error_text(exc: Exception) -> str:
    """Slack's own error code when the exception carries one, else the message."""
    response = getattr(exc, "response", None)  # SlackApiError keeps Slack's reply
    if response is not None and hasattr(response, "get"):
        return str(response.get("error") or exc)
    return str(exc)


class InputError(Exception):
    """CLI input that is unreadable, not UTF-8, or over its size limit."""


def read_input(source: str | Path, *, limit: int = INPUT_LIMIT, literal: bool = False) -> str:
    """Read a UTF-8 file or stdin (``-``), or validate literal text, up to ``limit`` bytes.

    ``literal=True`` treats the source as text even when it is ``-`` or names a file.
    Callers report InputError through their command's error output.
    """
    try:
        if literal:
            data = str(source).encode("utf-8")
        elif str(source) == "-":
            data = sys.stdin.buffer.read(limit + 1)
        else:
            with Path(source).open("rb") as stream:
                data = stream.read(limit + 1)
    except UnicodeEncodeError as exc:
        raise InputError("input is not UTF-8") from exc
    except OSError as exc:
        raise InputError(f"could not read {source}: {exc}") from exc
    if len(data) > limit:
        raise InputError(f"input exceeds {limit} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"{'stdin' if str(source) == '-' else source} is not UTF-8") from exc


def body(text: str | None, file: Path | None, *, as_json: bool = False) -> str:
    """Message text from the argument, ``--file``, or stdin (``-``): exactly one of them."""
    if file is not None and text is not None:
        fail(["give TEXT or --file, not both"], as_json=as_json)
    try:
        if file is not None:
            result = read_input(file)
        elif text is not None:
            result = read_input(text, literal=text != "-")
        else:
            fail(["give TEXT, --file FILE, or - to read stdin"], as_json=as_json)
    except InputError as exc:
        fail([str(exc)], as_json=as_json)
    if not result.strip():
        fail(["the message is empty"], as_json=as_json)
    return result


def echo_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2))


def report(result: dict, *, as_json: bool) -> None:
    """A write's outcome: the JSON contract, or its fields on one line."""
    if as_json:
        echo_json(result)
        return
    typer.echo(" ".join(f"{key}={value}" for key, value in result.items() if key != "ok" and value))


def columns(rows: list[list[str]]) -> str:
    """Left-aligned columns, two spaces apart."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def ago(stamp: str) -> str:
    then = datetime.fromisoformat(stamp)
    return format_elapsed(max(0, int((datetime.now(UTC) - then).total_seconds()))) + " ago"


def seconds(stamp: str | None) -> str:
    """A stored microsecond timestamp, trimmed to the second for reading."""
    return datetime.fromisoformat(stamp).isoformat(timespec="seconds") if stamp else "-"


def human_bytes(size: int) -> str:
    """``0 B``, ``12 KB``, ``1.2 MB``: enough precision to decide whether to clean up."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    return f"{int(value)} {unit}" if unit == "B" or value >= 10 else f"{value:.1f} {unit}"
