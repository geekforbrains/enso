"""Release installation, quiet checks and durable self-update operations."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import releases, updates
from ..config import Paths
from ..maintenance import UpdateError
from .common import JSON_FLAG, WORKSPACE, echo_json, fail

update_app = typer.Typer(
    no_args_is_help=True, help="Check, install and safely apply Enso releases."
)
BIN_DIR = typer.Option(Path("~/.local/bin"), "--bin-dir")
TOKEN_FILE = typer.Option(None, "--token-file")
PREPARED_RELEASE = typer.Option(None, "--prepared-release", hidden=True)


def _error(exc: Exception, as_json: bool) -> None:
    fail([str(exc)], as_json=as_json)


@update_app.command("install")
def install(
    manifest: str | None = typer.Option(
        None, "--manifest", help="Local or HTTPS release manifest."
    ),
    bin_dir: Path = BIN_DIR,
    extras: str = typer.Option("slack,telegram,web", "--extras"),
    token_file: Path | None = TOKEN_FILE,
    feed: str | None = typer.Option(None, "--feed", help="URL checked for later releases."),
    viewer_service: str = typer.Option(
        "", "--viewer-service", help="User service owning the viewer."
    ),
    prepared_release: Path | None = PREPARED_RELEASE,
    adopt: bool = typer.Option(
        False, "--adopt", help="Preserve and replace an existing enso launcher."
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Install the first managed release, preserving the existing home."""
    try:
        result = updates.install(
            Paths.from_env(),
            manifest,
            bin_dir=bin_dir,
            extras=tuple(filter(None, extras.split(","))),
            token_file=token_file,
            feed=feed,
            viewer_service=viewer_service,
            prepared_release=prepared_release,
            adopt=adopt,
        )
    except (UpdateError, releases.ReleaseError, OSError, ValueError) as exc:
        _error(exc, as_json)
        return
    if as_json:
        echo_json(result)
    else:
        typer.echo(f"Installed Enso {result['installed_version']}: {result['binary']}")


@update_app.command("check")
def check(
    manifest: str | None = typer.Option(None, "--manifest"),
    notify: bool = typer.Option(
        False, "--notify", help="Announce a new release once to your notify target."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress terminal text, including expected network errors."
    ),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Check the selected release feed without installing anything."""
    paths = Paths.from_env()
    try:
        selected = updates.update_workspace(paths, workspace) if notify else None
        result = updates.check(paths, manifest)
        if selected is not None:
            result["notified"] = updates.notify_available(paths, result, workspace=selected)
    except (UpdateError, releases.ReleaseError, OSError, ValueError) as exc:
        if quiet and not as_json:
            return
        _error(exc, as_json)
        return
    if as_json:
        echo_json(result)
    elif not quiet:
        typer.echo(updates.check_message(result))


@update_app.command("apply")
def apply(
    manifest: str | None = typer.Option(None, "--manifest"),
    workspace: str | None = WORKSPACE,
    drain_timeout: float = typer.Option(300, "--drain-timeout", min=1, max=3600),
    startup_timeout: float = typer.Option(60, "--startup-timeout", min=1, max=600),
    as_json: bool = JSON_FLAG,
) -> None:
    """Queue an independent updater; wait for active work and restart safely."""
    try:
        result = updates.request_apply(
            Paths.from_env(),
            manifest,
            workspace=workspace,
            drain_timeout=drain_timeout,
            startup_timeout=startup_timeout,
        )
    except (UpdateError, releases.ReleaseError, OSError, ValueError) as exc:
        _error(exc, as_json)
        return
    if as_json:
        echo_json(result)
    elif result["operation"]:
        operation = result["operation"]
        typer.echo(
            f"Queued update {operation['id']} to {operation['to_version']}; use enso update status."
        )
    else:
        typer.echo(f"Enso {result['current']} is already installed.")


@update_app.command("status")
def status(as_json: bool = JSON_FLAG) -> None:
    """Show installed/running versions and the latest durable update outcome."""
    try:
        result = updates.status(Paths.from_env())
    except (UpdateError, OSError, ValueError) as exc:
        _error(exc, as_json)
        return
    if as_json:
        echo_json(result)
    else:
        typer.echo(
            f"Installed: {result['installed_version'] or 'unmanaged'}; "
            f"running: {result['running_version'] or 'stopped'}"
        )
        if operation := result["operation"]:
            typer.echo(
                f"{operation['id']}: {operation['status']} — {operation.get('error', '')}".rstrip(
                    " —"
                )
            )


@update_app.command("recover")
def recover(as_json: bool = JSON_FLAG) -> None:
    """Resume recovery after an interrupted update, using its original snapshot."""
    try:
        result = updates.recover(Paths.from_env())
    except (UpdateError, OSError, ValueError) as exc:
        _error(exc, as_json)
        return
    if as_json:
        echo_json(result)
    else:
        typer.echo("Recovery queued; use enso update status.")


@update_app.command("_run", hidden=True)
def worker(operation_id: str) -> None:
    updates.run_update(Paths.from_env(), operation_id)


@update_app.command("_prepare-home", hidden=True)
def prepare_home() -> None:
    updates.prepare_home(Paths.from_env())


@update_app.command("_migration-plan", hidden=True)
def migration_plan() -> None:
    echo_json(updates.migration_plan(Paths.from_env()))


@update_app.command("_validate-home", hidden=True)
def validate_home() -> None:
    updates.validate_home(Paths.from_env())
