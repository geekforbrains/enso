"""``enso setup``: a linear first-run wizard for a fresh home."""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import typer

from .. import __version__, db, service
from ..config import Config, Paths, load_config
from ..connection_setup import initial_config, pair_in_terminal, safe_error
from ..initialization import apply_config, initialize_home
from ..providers import PROVIDER_CLASSES
from ..transport_registry import TRANSPORTS, TransportSpec
from ..transports.connection import PairedIdentity
from .common import deliver, fail


def detect_providers() -> dict[str, dict[str, Any]]:
    """Every supported CLI on PATH, with its bundled model list and unattended flags."""
    found: dict[str, dict[str, Any]] = {}
    for name, cls in PROVIDER_CLASSES.items():
        path = shutil.which(name)
        if path:
            found[name] = {
                "path": path,
                "models": list(cls.models),
                "args": list(cls.unattended_args),
            }
    return found


# -- Steps ------------------------------------------------------------------------


def _choose(label: str, choices: list[str], default: str) -> str:
    while True:
        answer = str(typer.prompt(f"{label} [{', '.join(choices)}]", default=default)).strip()
        if answer in choices:
            return answer
        typer.echo(f"choose one of {', '.join(choices)}")


def _agent(providers: dict[str, dict[str, Any]]) -> dict[str, str]:
    provider = _choose("Default provider", list(providers), next(iter(providers)))
    models = providers[provider]["models"]
    model = _choose("Default model", models, models[0])
    cls = PROVIDER_CLASSES[provider]
    levels = [level for level in cls.effort_levels if cls.clamp_effort(level, model) == level]
    effort = _choose("Default effort", levels, "high" if "high" in levels else levels[-1])
    return {"provider": provider, "model": model, "effort": effort}


def _credentials(paths: Paths, spec: TransportSpec) -> dict[str, str]:
    values: dict[str, str] = {}
    for credential in spec.credentials:
        typer.echo(credential.hint.format(home=paths.home))
        values[credential.key] = typer.prompt(credential.prompt, hide_input=True)
    return values


def _pair(paths: Paths, spec: TransportSpec, credentials: dict[str, str]) -> PairedIdentity:
    async def ready(details: dict[str, str]) -> None:
        typer.echo(f"Open {details['open_url']}")
        typer.echo(spec.pairing_hint.format(**details))
        typer.echo("Waiting for you (5 minutes; Ctrl-C cancels)…")

    try:
        identity = pair_in_terminal(paths, spec.name, credentials, ready)
    except Exception as exc:
        fail([safe_error(exc)["error"]])
    typer.echo("Your chat is connected.")
    return identity


def _send_test(paths: Paths, config: Config) -> None:
    target = config.default_notify()
    if target is None:
        return
    from . import build_transports  # deferred: the cli package imports this module

    # build_transports logs and skips a transport whose extra will not import, so the config
    # is already written and the wizard should finish rather than die on an empty list.
    transport = next((t for t in build_transports(config) if t.name == target[0]), None)
    if transport is None:
        name = target[0]
        typer.echo(f"no test message: {name} transport unavailable; install enso[{name}]")
        return
    try:
        asyncio.run(
            deliver(
                paths,
                transport,
                target[1],
                None,
                workspace="default",
                text=f"Enso {__version__} is set up.",
            )
        )
        typer.echo(f"sent a test message to {target[0]}:{target[1]}")
    except Exception:
        typer.echo("test message failed; check the bot connection and retry")


def setup_wizard() -> None:
    """Configure a fresh home: providers, one transport, defaults, the default workspace."""
    paths = Paths.from_env()
    if paths.config.exists():
        fail([f"{paths.config} exists; edit it by hand, or move it aside to start over"])
    typer.echo(f"Enso {__version__} — home {paths.home}")
    providers = detect_providers()
    if not providers:
        fail([f"no provider CLI on PATH; install one of {', '.join(PROVIDER_CLASSES)}"])
    for name, entry in providers.items():
        typer.echo(f"found {name} at {entry['path']}")
    defaults = _agent(providers)
    transport = _choose("Transport", list(TRANSPORTS), next(iter(TRANSPORTS)))
    scaffold = initialize_home(paths)
    if not scaffold["ok"]:
        fail(scaffold["problems"])
    for line in scaffold["changes"]:
        typer.echo(line)
    credentials = _credentials(paths, TRANSPORTS[transport])
    owner = _pair(paths, TRANSPORTS[transport], credentials)
    raw = {
        **initial_config(transport, credentials, owner),
        "defaults": defaults,
        "providers": providers,
        "agent": {"timeout": 3600},
        "logging": {"level": "INFO"},
        "runs": {"keep": 500, "max_age_days": 30},
    }
    applied = apply_config(paths, raw, expected_hash="missing")
    for warning in applied["warnings"]:
        typer.echo(f"warning: {warning}")
    if not applied["ok"]:
        fail(applied["problems"])
    typer.echo(f"wrote {paths.config}")
    if applied["changes"]:
        where = (
            "reports problems to your notify target"
            if owner.channel
            else f"has nowhere to report until transports.{transport}.notify is set"
        )
        typer.echo(
            "installed the enso-audit job: a nightly `enso doctor` that stays silent while "
            f"the home is healthy and {where}; `enso job show enso-audit`"
        )
    config = load_config(paths)
    try:
        db.initialize(paths)
    except db.UnsupportedDatabaseError as exc:
        fail([str(exc)])
    _send_test(paths, config)
    if typer.confirm("Install the background service?", default=True):
        try:
            for line in service.install(paths, config):
                typer.echo(line)
        except service.ServiceError as exc:
            typer.echo(f"service install failed: {exc}; run `enso serve` by hand")
    typer.echo("done: `enso service status`, `enso logs -f`, and `enso config check` from here")
