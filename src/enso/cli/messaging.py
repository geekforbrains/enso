"""``enso message`` (goes wherever the caller belongs) and ``enso telegram``."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from .. import heartbeat, messages
from ..config import Config, Paths
from ..formatting import preview
from ..transports import Transport
from . import slack as slack_cli
from .common import (
    ACTION_KEY,
    JSON_FLAG,
    body,
    columns,
    deliver,
    echo_json,
    fail,
    load,
    report,
    run,
    seconds,
)

if TYPE_CHECKING:
    from ..transports.telegram import TelegramTransport

message_app = typer.Typer(
    no_args_is_help=True,
    help="Send to --to, else the conversation that asked, else the transport's notify target.",
)
telegram_app = typer.Typer(no_args_is_help=True, help="Talk to Telegram from a shell or a job.")

TO = typer.Option(None, "--to", help="slack:C…, telegram:<id>, or a bare id with one transport.")
CHAT = typer.Option(None, "--to", help="Chat id; default: the chat that asked, else notify.")
CAPTION = typer.Argument("", help="Optional caption.")
TEXT = typer.Argument(None, help="Message text; '-' reads stdin.")
FILE = typer.Option(None, "--file", help="Read the text from this file.")
ATTACHMENT = typer.Argument(..., help="The file to send.")


def _telegram(config: Config, *, as_json: bool) -> TelegramTransport:
    if config.telegram is None:
        fail(["transports.telegram is not configured"], as_json=as_json)
    try:
        from ..transports.telegram import TelegramTransport
    except ImportError as exc:
        fail([str(exc)], as_json=as_json)
    return TelegramTransport(config.telegram, config.paths)


def destination(
    config: Config, to: str | None, *, transport: str | None = None
) -> tuple[str, str, str | None]:
    """Explicit target, else the beat's saved target, else the chat origin or configured notify."""
    if to:
        if transport and ":" not in to:
            to = f"{transport}:{to}"
        name, target = config.resolve_target(to)
        if transport and name != transport:
            raise ValueError(f"{to!r} is not a {transport} destination")
        return name, target, None
    if ref := os.environ.get("ENSO_BEAT"):
        beat = heartbeat.get(config.paths, ref)
        if beat is None or beat.notify is None:
            raise ValueError(f"heartbeat {ref} has no saved notification destination")
        name, target = config.resolve_target(beat.notify)
        if transport is not None and name != transport:
            raise ValueError(
                f"heartbeat {ref}'s saved destination is not on {transport}; pass --to"
            )
        return name, target, beat.notify_thread
    origin = messages.origin_from_env(os.environ)
    if origin is not None and (transport is None or origin[0] == transport):
        return origin
    if transport is not None and transport not in config.transports:
        # Config.transports holds only configured entries; match resolve_target's wording.
        raise ValueError(f"transport {transport} is not configured")
    default = (
        (transport, config.transports[transport].notify) if transport else config.default_notify()
    )
    if default is not None and default[1]:
        return default[0], default[1], None
    raise ValueError("no destination: pass --to or set transports.<name>.notify")


def _resolve(
    config: Config, to: str | None, *, transport: str | None, as_json: bool
) -> tuple[Transport, str, str | None]:
    try:
        name, target, thread = destination(config, to, transport=transport)
    except (ValueError, heartbeat.HeartbeatError, OSError, sqlite3.Error) as exc:
        fail([str(exc)], as_json=as_json)
    sender = (
        slack_cli.transport(config, as_json=as_json)
        if name == "slack"
        else _telegram(config, as_json=as_json)
    )
    return sender, target, thread


def _check_file(file: Path, *, as_json: bool) -> None:
    if not file.is_file():
        fail([f"{file} is not a file"], as_json=as_json)


# -- message --


@message_app.command("send")
def message_send(
    text: str | None = TEXT,
    file: Path | None = FILE,
    to: str | None = TO,
    action_key: str | None = ACTION_KEY,
    as_json: bool = JSON_FLAG,
) -> None:
    """Send text; it also reaches the next turn there as background context."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    content = body(text, file, as_json=as_json)
    transport, target, thread = _resolve(config, to, transport=None, as_json=as_json)
    result = run(
        deliver(paths, transport, target, thread, text=content, action_key=action_key),
        as_json=as_json,
    )
    report(result, as_json=as_json)


@message_app.command("attach")
def message_attach(
    file: Path = ATTACHMENT,
    caption: str = CAPTION,
    to: str | None = TO,
    action_key: str | None = ACTION_KEY,
    as_json: bool = JSON_FLAG,
) -> None:
    """Send a file with an optional caption."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _check_file(file, as_json=as_json)
    transport, target, thread = _resolve(config, to, transport=None, as_json=as_json)
    result = run(
        deliver(
            paths, transport, target, thread, file=file, caption=caption, action_key=action_key
        ),
        as_json=as_json,
    )
    report(result, as_json=as_json)


@message_app.command("list")
def message_list(
    limit: int = typer.Option(20, "-n", help="How many, newest first."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Recent out-of-band sends; an unread one reaches the next turn in its conversation."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)
    found = messages.list_messages(paths, limit)
    if as_json:
        echo_json([message.as_dict() for message in found])
        return
    if not found:
        typer.echo("no messages yet")
        return
    rows = [["ID", "CREATED", "STATUS", "TARGET", "SOURCE", "READ", "TEXT"]]
    for message in found:
        target = f"{message.transport}:{message.target}"
        if message.thread:
            target += f":{message.thread}"
        rows.append(
            [
                str(message.id),
                seconds(message.created_at),
                message.status,
                target,
                message.source,
                "yes" if message.consumed_at else "no",
                preview(message.text),
            ]
        )
    typer.echo(columns(rows))


# -- telegram --


@telegram_app.command("send")
def telegram_send(
    text: str | None = TEXT,
    file: Path | None = FILE,
    to: str | None = CHAT,
    action_key: str | None = ACTION_KEY,
    as_json: bool = JSON_FLAG,
) -> None:
    """Send text to a Telegram chat."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    content = body(text, file, as_json=as_json)
    transport, target, _ = _resolve(config, to, transport="telegram", as_json=as_json)
    report(
        run(
            deliver(paths, transport, target, None, text=content, action_key=action_key),
            as_json=as_json,
        ),
        as_json=as_json,
    )


@telegram_app.command("attach")
def telegram_attach(
    file: Path = ATTACHMENT,
    caption: str = CAPTION,
    to: str | None = CHAT,
    action_key: str | None = ACTION_KEY,
    as_json: bool = JSON_FLAG,
) -> None:
    """Send a file to a Telegram chat."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _check_file(file, as_json=as_json)
    transport, target, _ = _resolve(config, to, transport="telegram", as_json=as_json)
    result = run(
        deliver(paths, transport, target, None, file=file, caption=caption, action_key=action_key),
        as_json=as_json,
    )
    report(result, as_json=as_json)
