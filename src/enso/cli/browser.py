"""``enso browser``: private Chrome profiles and their optional MCP connection."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote

import typer

from .. import browser, service
from ..config import Paths
from ..maintenance import UpdateError
from .common import echo_json

browser_app = typer.Typer(no_args_is_help=True, help="Private Chrome profiles and browser MCP.")


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except (
        browser.BrowserError,
        service.ServiceError,
        UpdateError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        # MCP stdout is exclusively protocol traffic, including on startup failure.
        typer.echo(f"enso browser: {exc}", err=True)
        raise typer.Exit(1) from None


def _profile(name: str) -> browser.Profile:
    profile = browser.Profile(Paths.from_env().home.resolve(), name)
    profile.check_paths()
    return profile


@browser_app.command("create")
def create(profile: str = typer.Argument("default")) -> None:
    """Prepare a private profile without opening Chrome; print JSON."""
    with _errors():
        selected = _profile(profile)
        selected.create()
        with browser.profile_lock(selected):
            echo_json({"profile": selected.name, "path": str(selected.data)})


@browser_app.command("status")
def status(profile: str = typer.Argument("default")) -> None:
    """Print profile/process status as JSON without starting Chrome."""
    with _errors():
        echo_json(browser.status(_profile(profile)))


@browser_app.command("list")
def list_profiles() -> None:
    """Print existing profiles and their status as JSON."""
    with _errors():
        echo_json(browser.profiles(Paths.from_env().home.resolve()))


@browser_app.command("open")
def open_profile(
    profile: str = typer.Argument("default"),
    url: str | None = typer.Option(None, "--url", help="Open a new tab, preserving existing tabs."),
) -> None:
    """Start or reuse Chrome for human handoff; print JSON."""
    with _errors():
        selected = _profile(profile)
        if url is not None:
            browser.validate_url(url)
        selected.create()
        with browser.profile_lock(selected):
            state = browser.start(selected)
            if url is not None:
                browser.request(state.port, f"/json/new?{quote(url, safe='')}", method="PUT")
            echo_json(browser.status(selected))


@browser_app.command("stop")
def stop(profile: str = typer.Argument("default")) -> None:
    """Stop only the verified Chrome process, preserving saved profile data; print JSON."""
    with _errors():
        selected = _profile(profile)
        selected.create()
        with browser.profile_lock(selected):
            echo_json({"profile": selected.name, "status": browser.stop(selected)})


@browser_app.command("mcp")
def mcp(
    profile: str = typer.Argument("default"),
    print_config: bool = typer.Option(
        False, "--print-config", help="Print registration JSON only."
    ),
) -> None:
    """Serve MCP on stdin/stdout; Chrome starts on the first browser tool use."""
    with _errors():
        selected = _profile(profile)
        if print_config:
            echo_json(browser.registration(selected))
            return
        selected.create()
        raise typer.Exit(browser.run_mcp(selected))
