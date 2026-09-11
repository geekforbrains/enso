"""``enso web``: the read-only viewer and its optional user service."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import typer

from .. import maintenance, service, web
from ..config import Paths, valid_port
from ..web import service as viewer_service
from .common import fail

web_app = typer.Typer(
    no_args_is_help=True, help="The read-only web viewer, a separate process from `serve`."
)


@contextmanager
def _lifecycle(paths: Paths) -> Iterator[None]:
    """Keep service reconfiguration out of an update's recorded restart plan."""
    from ..updates import TERMINAL

    state = maintenance.read_json(paths.update_state)
    if state.get("id") and os.environ.get("ENSO_UPDATE_INTERNAL") == state["id"]:
        yield
        return
    if not (paths.runtime_dir / "install.json").exists():
        if maintenance.paused(paths):
            raise maintenance.UpdateError("Enso is updating; wait before changing the viewer")
        yield
        return
    with maintenance.lock(paths):
        state = maintenance.read_json(paths.update_state)
        if maintenance.paused(paths) or (state and state.get("status") not in TERMINAL):
            raise maintenance.UpdateError("Enso is updating; wait before changing the viewer")
        yield


def _bind_warnings(paths: Paths, host: str | None = None, port: int | None = None) -> None:
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


@web_app.command("install")
def web_install() -> None:
    """Install and start the viewer's launchd/systemd user service."""
    paths = Paths.from_env()
    try:
        with _lifecycle(paths):
            _bind_warnings(paths)
            lines = viewer_service.install(paths)
    except (service.ServiceError, web.WebError, maintenance.UpdateError, OSError) as exc:
        fail([str(exc)])
    for line in lines:
        typer.echo(line)


@web_app.command("uninstall")
def web_uninstall() -> None:
    """Stop the viewer service and remove its unit, preserving the Enso home."""
    paths = Paths.from_env()
    try:
        with _lifecycle(paths):
            lines = viewer_service.uninstall(paths)
    except (service.ServiceError, web.WebError, maintenance.UpdateError, OSError) as exc:
        fail([str(exc)])
    for line in lines:
        typer.echo(line)


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
    _bind_warnings(paths, host, port)
    try:
        if foreground:
            # The supervisor executes this command; never try to start itself or
            # take the lifecycle lock its installing/updating parent already holds.
            line = web.start(paths, host=host, port=port, foreground=True)
        else:
            with _lifecycle(paths):
                supervised = viewer_service.find(paths)
                if supervised is not None:
                    if host is not None or port is not None:
                        raise service.ServiceError(
                            "the viewer service uses web.host and web.port in config.json; "
                            "change those settings, or use --foreground for a manual start"
                        )
                    line = viewer_service.start(paths, supervised)
                else:
                    line = web.start(paths, host=host, port=port)
    except (service.ServiceError, web.WebError, maintenance.UpdateError, OSError) as exc:
        fail([str(exc)])
    typer.echo(line)


@web_app.command("stop")
def web_stop() -> None:
    """Stop the viewer if it is running."""
    paths = Paths.from_env()
    try:
        with _lifecycle(paths):
            supervised = viewer_service.find(paths)
            line = viewer_service.stop(paths, supervised) if supervised else web.stop(paths)
    except (service.ServiceError, web.WebError, maintenance.UpdateError, OSError) as exc:
        fail([str(exc)])
    typer.echo(line)


@web_app.command("status")
def web_status() -> None:
    """Whether the viewer is running, with its pid and URL; exit 1 when it is not."""
    status = web.status(Paths.from_env())
    if status.running:
        typer.echo(f"running pid={status.pid} at {status.url or '?'}")
        return
    typer.echo("not running" + (f" (stale web.pid from pid {status.pid})" if status.stale else ""))
    raise typer.Exit(1)
