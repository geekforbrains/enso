"""Short JSON commands over the guest-owned, expiring chat-pairing process."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from .. import connection_setup
from ..config import Paths
from .common import JSON_FLAG, echo_json

connect_app = typer.Typer(no_args_is_help=True, help="Pair a new Slack or Telegram account.")


def _input(file: str) -> Any:
    if file == "-":
        text = sys.stdin.read(connection_setup.MAX_INPUT + 1)
    else:
        with Path(file).open(encoding="utf-8") as stream:
            text = stream.read(connection_setup.MAX_INPUT + 1)
    if len(text) > connection_setup.MAX_INPUT:
        raise connection_setup.PairingError("invalid_input", "Setup input is too large.")
    try:
        return json.loads(text)
    except ValueError:
        raise connection_setup.PairingError("invalid_input", "Supply one JSON object.") from None


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        echo_json(result)
    elif not result["ok"]:
        typer.echo(result["error"])
    elif connection := result.get("connection"):
        typer.echo(f"{connection['attempt_id']}: {connection['state']}")
        for key in ("open_url", "instruction", "error"):
            if connection.get(key):
                typer.echo(connection[key])
    else:
        typer.echo("No connection attempt yet.")
    if not result["ok"]:
        raise typer.Exit(1)


@connect_app.command()
def start(
    transport: str = typer.Option(..., "--transport", help="slack or telegram"),
    file: str = typer.Option(..., "--file", help="Credential JSON file, or - for stdin."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Start a five-minute pairing attempt; tokens are read only from the input document."""
    try:
        result = connection_setup.start(Paths.from_env(), transport, _input(file))
    except Exception as exc:
        result = connection_setup.safe_error(exc)
    _emit(result, as_json)


@connect_app.command()
def status(attempt_id: str | None = typer.Argument(None), as_json: bool = JSON_FLAG) -> None:
    """Show the latest attempt, or require a matching attempt ID; never prints bot tokens."""
    try:
        result = connection_setup.snapshot(Paths.from_env(), attempt_id)
    except Exception as exc:
        result = connection_setup.safe_error(exc)
    _emit(result, as_json)


@connect_app.command()
def cancel(attempt_id: str | None = typer.Argument(None), as_json: bool = JSON_FLAG) -> None:
    """Stop the attempt and invalidate its code without changing an active configuration."""
    try:
        result = connection_setup.cancel(Paths.from_env(), attempt_id)
    except Exception as exc:
        result = connection_setup.safe_error(exc)
    _emit(result, as_json)


@connect_app.command()
def finish(
    file: str = typer.Option(..., "--file", help="JSON with attempt_id, defaults, expected_hash."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Apply the paired owner and AI defaults; provider login and service start remain separate."""
    try:
        result = connection_setup.finish(Paths.from_env(), _input(file))
    except Exception as exc:
        result = connection_setup.safe_error(exc)
    _emit(result, as_json)
