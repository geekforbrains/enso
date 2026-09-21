"""Secret management and explicit command injection, independent of the chat service."""

from __future__ import annotations

import getpass
import os
import sys
from typing import Annotated

import typer

from .. import secrets
from ..config import Paths
from .common import fail

secret_app = typer.Typer(no_args_is_help=True, help="Store secrets and supply them to commands.")


@secret_app.command("list")
def list_secrets() -> None:
    """Print saved names only."""
    try:
        for name in secrets.names(Paths.from_env()):
            typer.echo(name)
    except secrets.SecretError as exc:
        fail([str(exc)])


@secret_app.command()
def add(
    name: str,
    stdin: bool = typer.Option(False, "--stdin", help="Read exact UTF-8 text from stdin."),
) -> None:
    """Create a secret; an existing name must be deleted before replacement."""
    try:
        secrets.validate_name(name)
        if stdin:
            value = sys.stdin.buffer.read(secrets.MAX_VALUE_BYTES + 1).decode("utf-8")
        elif sys.stdin.isatty():
            value = getpass.getpass("Value: ")
        else:
            raise secrets.SecretError("use --stdin to read a piped value")
        secrets.add(Paths.from_env(), name, value)
    except (secrets.SecretError, EOFError) as exc:
        fail([str(exc) or "no value supplied"])
    except UnicodeError:
        fail(["secret values must be UTF-8 text"])
    typer.echo(f"Created {name}.")


@secret_app.command()
def delete(name: str) -> None:
    """Delete one secret."""
    try:
        secrets.delete(Paths.from_env(), name)
    except secrets.SecretError as exc:
        fail([str(exc)])
    typer.echo(f"Deleted {name}.")


@secret_app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", help="Delete without the confirmation prompt."),
) -> None:
    """Permanently delete every secret and the key binding; the way past a lost master key."""
    if not yes:
        typer.echo(
            "This permanently deletes every saved secret. Values cannot be recovered, and jobs "
            "or commands that need them fail until they are added again. The master key file "
            "is left untouched.",
            err=True,
        )
        if not sys.stdin.isatty():
            fail(["pass --yes to reset without a prompt"])
        typer.confirm("Delete all secrets?", abort=True)
    try:
        count = secrets.reset(Paths.from_env())
    except secrets.SecretError as exc:
        fail([str(exc)])
    typer.echo(f"Deleted {count} secret{'' if count == 1 else 's'}; the store is uninitialized.")


@secret_app.command()
def get(name: str) -> None:
    """Write the exact value to stdout, without adding a newline."""
    try:
        value = secrets.resolve(Paths.from_env(), [name])[name]
    except secrets.SecretError as exc:
        fail([str(exc)])
    sys.stdout.buffer.write(value.encode("utf-8"))
    sys.stdout.buffer.flush()


@secret_app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    secret: Annotated[
        list[str], typer.Option("--secret", help="Name to inject; repeat for multiple secrets.")
    ],
) -> None:
    """Replace this process with COMMAND after --, preserving its exit and signal behavior."""
    if not ctx.args:
        fail(["supply a command after --"])
    try:
        env = {**os.environ, **secrets.resolve(Paths.from_env(), secret)}
        os.execvpe(ctx.args[0], ctx.args, env)
    except secrets.SecretError as exc:
        fail([str(exc)])
    except OSError as exc:
        typer.echo(f"error: could not start command: {exc.strerror}", err=True)
        raise typer.Exit(127 if isinstance(exc, FileNotFoundError) else 126) from None
