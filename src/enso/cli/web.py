"""``enso web``: start, stop, and inspect the read-only viewer process."""

from __future__ import annotations

import typer

from .. import web
from ..config import Paths, valid_port
from .common import fail

web_app = typer.Typer(
    no_args_is_help=True, help="The read-only web viewer, a separate process from `serve`."
)


@web_app.command("start")
def web_start(
    port: int | None = typer.Option(None, "--port", help="TCP port; overrides web.port."),
    host: str | None = typer.Option(None, "--host", help="Bind address; overrides web.host."),
    foreground: bool = typer.Option(
        False, "--foreground", help="Run in this terminal instead of the background."
    ),
) -> None:
    """Start the viewer, in the background unless told otherwise; a no-op while it is running."""
    paths = Paths.from_env()
    missing = web.missing_extra()
    if missing:
        fail([f"the web viewer needs {', '.join(missing)}; {web.INSTALL_HINT}"])
    if port is not None and not valid_port(port):
        fail(["--port must be an integer from 1 through 65535"])
    if host is not None and not host.strip():
        fail(["--host must not be empty"])
    bind, problems = web.resolve_bind(paths, host, port)
    if problems:
        plural = "s" if len(problems) != 1 else ""
        typer.echo(
            f"warning: config.json is unusable ({len(problems)} problem{plural}); "
            f"using {bind.host}:{bind.port}; see `enso doctor`",
            err=True,
        )
    if not bind.loopback:
        typer.echo(
            f"warning: {bind.host} is reachable from other machines and the viewer has no "
            "authentication; everything it shows is private",
            err=True,
        )
    try:
        line = web.start(paths, host=host, port=port, foreground=foreground)
    except web.WebError as exc:
        fail([str(exc)])
    typer.echo(line)


@web_app.command("stop")
def web_stop() -> None:
    """Stop the viewer if it is running."""
    try:
        typer.echo(web.stop(Paths.from_env()))
    except web.WebError as exc:
        fail([str(exc)])


@web_app.command("status")
def web_status() -> None:
    """Whether the viewer is running, with its pid and URL; exit 1 when it is not."""
    status = web.status(Paths.from_env())
    if status.running:
        typer.echo(f"running pid={status.pid} at {status.url or '?'}")
        return
    typer.echo("not running" + (f" (stale web.pid from pid {status.pid})" if status.stale else ""))
    raise typer.Exit(1)
