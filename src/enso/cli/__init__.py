"""``enso`` command line: serve, config, workspace, logs, web, jobs, tasks, messaging."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import typer

from .. import (
    __version__,
    audit,
    db,
    doctor,
    initialization,
    maintenance,
    models,
    scheduling,
    service,
    workspaces,
)
from .. import log as logsetup
from ..config import Config, ConfigError, Paths, check_config, load_config
from ..connection_setup import PairingError, service_receiver
from ..heartbeat.runner import HeartbeatRunner
from ..jobs.runner import JobRunner
from ..runtime import Runtime
from ..transports import Transport
from .common import JSON_FLAG, columns, echo_json, fail, human_bytes
from .connect import connect_app
from .heartbeat import heartbeat_app
from .jobs import job_app, runs_app
from .messaging import message_app, telegram_app
from .projects import project_app
from .setup import setup_wizard
from .skills import skill_app
from .slack import slack_app
from .tables import table_app
from .tasks import task_app
from .update import update_app
from .web import web_app

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Chat with agent CLIs.")
config_app = typer.Typer(
    no_args_is_help=True, help="Inspect, validate, apply, and edit config.json."
)
workspace_app = typer.Typer(no_args_is_help=True, help="Workspaces under $ENSO_HOME/workspaces.")
service_app = typer.Typer(
    no_args_is_help=True, help="The launchd/systemd unit running `enso serve`."
)
app.command("setup")(setup_wizard)
app.add_typer(config_app, name="config")
app.add_typer(connect_app, name="connect")
app.add_typer(workspace_app, name="workspace")
app.add_typer(service_app, name="service")
app.add_typer(job_app, name="job")
app.add_typer(heartbeat_app, name="heartbeat")
app.add_typer(runs_app, name="runs")
app.add_typer(task_app, name="task")
app.add_typer(project_app, name="project")
app.add_typer(message_app, name="message")
app.add_typer(slack_app, name="slack")
app.add_typer(telegram_app, name="telegram")
app.add_typer(table_app, name="table")
app.add_typer(web_app, name="web")
app.add_typer(update_app, name="update")
app.add_typer(skill_app, name="skill")
# Named explicitly: after a re-exec this module runs as __main__.
log = logging.getLogger("enso.cli")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"enso {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True),
) -> None:
    """Enso — bridge your chat app to agent CLIs on this machine."""
    paths = Paths.from_env()
    independent = ctx.invoked_subcommand in {"update", "serve", "logs", "web"}
    try:
        state = maintenance.read_json(paths.update_state)
    except (maintenance.UpdateError, OSError) as exc:
        fail([str(exc)], as_json="--json" in ctx.args or "--json" in sys.argv)
    internal = bool(state.get("id")) and os.environ.get("ENSO_UPDATE_INTERNAL") == state["id"]
    if not independent and not internal and (paths.runtime_dir / "install.json").exists():
        try:
            fd = maintenance.acquire_access(paths)
        except maintenance.UpdateError as exc:
            fail([str(exc)], as_json="--json" in ctx.args or "--json" in sys.argv)
        ctx.call_on_close(lambda: os.close(fd))
    if (
        maintenance.paused(paths)
        and not independent
        and state.get("status") != "draining"
        and not internal
    ):
        fail(
            ["Enso is updating; wait for update status before changing its home"],
            as_json="--json" in ctx.args or "--json" in sys.argv,
        )


# -- serve ----------------------------------------------------------------------


def build_transports(config: Config) -> list[Transport]:
    """One transport per configured entry; a missing extra is logged, not fatal."""
    transports: list[Transport] = []
    if config.slack is not None:
        try:
            from ..transports.slack import SlackTransport

            transports.append(SlackTransport(config.slack, config.paths))
        except ImportError as exc:
            log.error("slack transport unavailable: %s", exc)
    if config.telegram is not None:
        try:
            from ..transports.telegram import TelegramTransport

            transports.append(TelegramTransport(config.telegram, config.paths))
        except ImportError as exc:
            log.error("telegram transport unavailable: %s", exc)
    return transports


async def _serve(
    runtime: Runtime,
    transports: list[Transport],
    runner: JobRunner,
    heartbeat_runner: HeartbeatRunner,
) -> None:
    """Run every transport and the scheduler until one fails or the process is told to stop."""
    loop = asyncio.get_running_loop()
    main = asyncio.current_task()
    assert main is not None
    # SIGINT is already turned into a cancellation by asyncio.run; launchd/systemd stop
    # with SIGTERM, which would otherwise kill the process without unwinding anything.
    loop.add_signal_handler(signal.SIGTERM, main.cancel)
    tasks = [
        asyncio.create_task(transport.start(runtime), name=f"transport:{transport.name}")
        for transport in transports
    ]
    scheduler = asyncio.create_task(
        scheduling.minute_loop({"jobs": runner.tick, "heartbeat": heartbeat_runner.tick}),
        name="scheduler",
    )
    watcher = asyncio.create_task(_watch_update(runtime, runner, heartbeat_runner, transports))
    try:
        await asyncio.gather(*tasks)
    finally:
        # Let every transport finish stopping (Telegram's polling stop talks to the
        # API) before asyncio.run tears the loop down and cancels them a second time.
        # Running jobs are stopped here too, so their run rows close and their process
        # trees die before the loop goes; a cancelled run never alerts, so it does not
        # matter that the transports may already be gone.
        for task in (*tasks, scheduler, watcher):
            task.cancel()
        await asyncio.gather(
            *tasks,
            scheduler,
            watcher,
            runner.stop(),
            heartbeat_runner.stop(),
            return_exceptions=True,
        )
        loop.remove_signal_handler(signal.SIGTERM)


async def _watch_update(
    runtime: Runtime,
    runner: JobRunner,
    heartbeat_runner: HeartbeatRunner,
    transports: list[Transport],
) -> None:
    """Publish live readiness/activity; an installed file or PID alone is never health."""
    paths = runtime.paths
    maintenance.prepare(paths)
    try:
        while True:
            gate = maintenance.read_json(paths.maintenance)
            maintenance.write_json(
                paths.daemon_state,
                {
                    "pid": os.getpid(),
                    "version": __version__,
                    "updated_at": time.time(),
                    "ready": all(t.name in runtime.ready_transports for t in transports),
                    "active": runtime.active_count()
                    + len(runner.running())
                    + len(heartbeat_runner.running()),
                    "paused": maintenance.paused(paths),
                    "operation_id": gate.get("operation_id"),
                },
            )
            await asyncio.sleep(0.25)
    finally:
        if maintenance.read_json(paths.daemon_state).get("pid") == os.getpid():
            paths.daemon_state.unlink(missing_ok=True)


def load_secret_env(paths: Paths) -> list[str]:
    """Export ``secrets/*.env`` (``KEY=value`` lines) that the environment lacks; returns the keys.

    launchd starts the service with almost no environment, and provider CLIs and prerun
    scripts read things like keyring passwords from it.
    """
    loaded: list[str] = []
    for env_file in sorted(paths.secrets.glob("*.env")):
        for line in env_file.read_text("utf-8").splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            key = key.strip()
            if not sep or not key or key.startswith("#") or key in os.environ:
                continue
            os.environ[key] = value.strip().strip("'\"")
            loaded.append(key)
    return loaded


@app.command()
def serve(debug: bool = typer.Option(False, "--debug", help="Log prompts and raw events.")) -> None:
    """Run every configured transport."""
    paths = Paths.from_env()
    try:
        with service_receiver(paths):
            _serve_home(paths, debug)
    except PairingError as exc:
        fail([str(exc)])


def _serve_home(paths: Paths, debug: bool) -> None:
    """Load the configuration and run while this process owns the receiver lock."""
    try:
        config = load_config(paths)
    except ConfigError as exc:
        fail(exc.problems)
    logsetup.setup(paths, config.logging, debug=debug)
    for line in audit.startup_warnings(paths, config):
        log.warning(line)  # a malformed workspace is not a reason to stop serving
    loaded = load_secret_env(paths)
    if loaded:
        log.info("loaded %s from %s", ", ".join(loaded), paths.secrets)
    try:
        db.migrate(paths)
        pruned = db.prune_sessions(paths)
    except db.UnsupportedDatabaseError as exc:
        fail([str(exc)])  # nothing has started yet, so nothing has to be unwound
    if pruned:
        log.info("pruned %d idle sessions", pruned)
    transports = build_transports(config)
    if not transports:
        fail(["no transport is configured (or its extra is not installed)"])
    log.info(
        "enso %s serving %s from %s",
        __version__,
        ", ".join(t.name for t in transports),
        paths.home,
    )
    by_name = {transport.name: transport for transport in transports}
    runner = JobRunner(config, by_name)
    heartbeat_runner = HeartbeatRunner(config, by_name)
    closed = runner.recover()
    if closed:
        log.info("closed %d interrupted runs", closed)
    if recovered := heartbeat_runner.recover():
        log.info("closed %d interrupted heartbeat runs", recovered)
    try:
        asyncio.run(_serve(Runtime(config, debug=debug), transports, runner, heartbeat_runner))
    except KeyboardInterrupt, asyncio.CancelledError:
        log.info("stopped")
    except Exception as exc:
        log.exception("serve failed")
        fail([f"serve failed: {exc}"])


# -- logs -----------------------------------------------------------------------


@app.command()
def logs(
    follow: bool = typer.Option(False, "-f", "--follow", help="Keep printing new lines."),
    lines: int = typer.Option(50, "-n", help="How many recent lines."),
    turn: str | None = typer.Option(None, "--turn", help="Only one chat turn (its t: id)."),
    job: str | None = typer.Option(None, "--job", help="Only one job's runs."),
    grep: str | None = typer.Option(None, "--grep", help="Only lines containing this text."),
) -> None:
    """Show the log, rotated backups included, narrowed to a turn, a job, or some text."""
    paths = Paths.from_env()
    if not paths.log.exists():
        fail([f"{paths.log} does not exist yet"])
    needles = [
        needle
        for needle in (f"[t:{turn}]" if turn else "", f"[j:{job}]" if job else "", grep or "")
        if needle
    ]

    def keep(line: str) -> bool:
        return all(needle in line for needle in needles)

    for line in logsetup.tail(paths, lines, keep):
        typer.echo(line)
    if not follow:
        return
    try:
        for line in logsetup.follow(paths):
            if keep(line):
                typer.echo(line)
    except KeyboardInterrupt:
        pass


# -- config ---------------------------------------------------------------------


@app.command("init")
def init_home(as_json: bool = JSON_FLAG) -> None:
    """Prepare a home offline, preserving existing files; leave active configuration absent."""
    result = initialization.initialize_home(Paths.from_env())
    if as_json:
        echo_json(result)
    else:
        for line in result["changes"]:
            typer.echo(line)
        if not result["ok"]:
            fail(result["problems"])
        typer.echo("home prepared; fill the example and use `enso config apply --file FILE`")
    if not result["ok"]:
        raise typer.Exit(1)


@app.command("providers")
def providers_catalog(as_json: bool = JSON_FLAG) -> None:
    """List bundled provider/model/effort choices without network or account checks."""
    result = initialization.provider_catalog()
    if as_json:
        echo_json(result)
        return
    for provider in result["providers"]:
        installed = "installed" if provider["installed"] else "not installed"
        typer.echo(f"{provider['label']} ({provider['id']}, {installed})")
        for model in provider["models"]:
            efforts = (
                ", ".join(model["efforts"]) if model["known"] else "unknown; use `enso models`"
            )
            typer.echo(f"  {model['id']}: {efforts}")


EXPECTED_HASH = typer.Option(
    None, "--expected-hash", help="Require this config SHA256, or missing for a fresh home."
)


def _finish_config_write(result: dict[str, Any], as_json: bool) -> None:
    """Report an apply, set, or unset result in the documented shape, then set the status."""
    sections = result.pop("_restart_sections", ())
    if as_json:
        echo_json(result)
    else:
        for warning in result["warnings"]:
            typer.echo(f"warning: {warning}")
        if not result["ok"]:
            fail(result["problems"])
        typer.echo("configuration applied")
        if any(key in initialization.SERVICE_RESTART_KEYS for key in sections):
            typer.echo("restart the service to apply the transports or logging change")
        if any(key in initialization.VIEWER_RESTART_KEYS for key in sections):
            typer.echo(
                "restart the viewer (enso web stop, then enso web start) to apply the web change"
            )
    if not result["ok"]:
        raise typer.Exit(1)


@config_app.command("apply")
def config_apply(
    file: str = typer.Option(
        ..., "--file", help="Full JSON document, or - for stdin (up to 1 MiB)."
    ),
    expected_hash: str | None = EXPECTED_HASH,
    as_json: bool = JSON_FLAG,
) -> None:
    """Validate and atomically replace config.json; no prompts, message, or service start."""
    try:
        if file == "-":
            text = sys.stdin.read(1024 * 1024 + 1)
        else:
            with Path(file).open(encoding="utf-8") as source:
                text = source.read(1024 * 1024 + 1)
        if len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("configuration input exceeds 1 MiB")
        raw = json.loads(text)
    except OSError, UnicodeError, ValueError, RecursionError:
        result: dict[str, Any] = {
            "version": 1,
            "ok": False,
            "applied": False,
            "config_hash": None,
            "jobs_complete": False,
            "restart_required": False,
            "changes": [],
            "warnings": [],
            "problems": ["could not read a JSON document of at most 1 MiB from --file"],
        }
    else:
        result = initialization.apply_config(Paths.from_env(), raw, expected_hash=expected_hash)
    _finish_config_write(result, as_json)


def _config_value(text: str) -> object:
    """A JSON value, or the text itself when it is not JSON, so bare words need no quoting."""
    try:
        return json.loads(text)
    except ValueError, RecursionError:
        return text


@config_app.command("set")
def config_set(
    path: str = typer.Argument(
        ..., help="Dotted keys, such as defaults.model or workspaces.NAME.restricted."
    ),
    value: str = typer.Argument(..., help="JSON, or plain text when it is not valid JSON."),
    expected_hash: str | None = EXPECTED_HASH,
    as_json: bool = JSON_FLAG,
) -> None:
    """Store one value in config.json, creating missing objects; validated like apply."""
    edit = initialization.ConfigEdit("set", path, _config_value(value))
    result = initialization.patch_config(Paths.from_env(), [edit], expected_hash=expected_hash)
    _finish_config_write(result, as_json)


@config_app.command("unset")
def config_unset(
    path: str = typer.Argument(..., help="Dotted keys of the entry to remove."),
    expected_hash: str | None = EXPECTED_HASH,
    as_json: bool = JSON_FLAG,
) -> None:
    """Remove one key from config.json; validated like apply."""
    edit = initialization.ConfigEdit("unset", path)
    result = initialization.patch_config(Paths.from_env(), [edit], expected_hash=expected_hash)
    _finish_config_write(result, as_json)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.endswith("token") and value[key] else _redact(value[key])
            for key in value
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


@config_app.command("show")
def config_show() -> None:
    """Print config.json with tokens redacted."""
    paths = Paths.from_env()
    config, problems, _ = check_config(paths)
    if config is None:
        fail(problems)
    typer.echo(json.dumps(_redact(config.raw), indent=2))


@config_app.command("check")
def config_check(as_json: bool = JSON_FLAG) -> None:
    """Validate config.json and list every problem."""
    paths = Paths.from_env()
    if as_json:
        result = initialization.check_config_report(paths)
        echo_json(result)
        if not result["ok"]:
            raise typer.Exit(1)
        return
    config, problems, warnings = check_config(paths)
    for warning in warnings:
        typer.echo(f"warning: {warning}")
    if config is None:
        fail(problems)
    typer.echo(f"ok: {paths.config}")


# -- doctor ---------------------------------------------------------------------


def doctor_lines(report: doctor.Report) -> list[str]:
    """One header per section with its context, then its problems and warnings."""
    lines: list[str] = []
    for section in report.sections:
        note = f" ({section.note})" if section.note else ""
        lines.append(f"{section.name}: {section.summary}{note}")
        lines.extend(f"  error: {problem}" for problem in section.problems)
        lines.extend(f"  warning: {warning}" for warning in section.warnings)
    return lines


@app.command("doctor")
def doctor_command(as_json: bool = JSON_FLAG) -> None:
    """Config, home, workspaces, providers, transports, service, and jobs; exit 1 on a problem."""
    paths = Paths.from_env()
    report = doctor.run(paths)
    if as_json:
        echo_json(report.as_dict())
    else:
        for line in doctor_lines(report):
            typer.echo(line)
    if not report.ok:
        raise typer.Exit(1)


# -- models ---------------------------------------------------------------------


def _model_number(value: int | float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}" if isinstance(value, float) else str(value)


@app.command("models")
def models_command(
    show_all: bool = typer.Option(False, "--all", help="Include models without tool calling."),
    as_json: bool = JSON_FLAG,
) -> None:
    """List OpenRouter models that OpenCode can use."""
    try:
        catalog = models.load(Paths.from_env())
    except models.ModelsError as exc:
        fail([str(exc)], as_json=as_json)
    if catalog.warning:
        typer.echo(f"warning: {catalog.warning}", err=True)
    found = [model for model in catalog.models if show_all or model.tool_call is True]
    if as_json:
        echo_json([model.as_dict() for model in found])
        return
    rows = [["MODEL", "TOOLS", "CONTEXT", "INPUT $/M", "OUTPUT $/M", "EFFORTS"]]
    rows.extend(
        [
            model.id,
            "yes" if model.tool_call else "no",
            f"{model.context:,}" if model.context is not None else "-",
            _model_number(model.input_cost),
            _model_number(model.output_cost),
            ",".join(model.efforts) or "-",
        ]
        for model in found
    )
    typer.echo(columns(rows))


# -- service --------------------------------------------------------------------


def _service(action: Any, *args: Any) -> Any:
    try:
        return action(*args)
    except service.ServiceError as exc:
        fail([str(exc)])


@service_app.command("install")
def service_install() -> None:
    """Write the unit for this `enso` and start it (replacing any earlier one)."""
    paths = Paths.from_env()
    try:
        config = load_config(paths)
    except ConfigError as exc:
        fail(exc.problems)
    for line in _service(service.install, paths, config):
        typer.echo(line)


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Stop the service and remove its unit."""
    for line in _service(service.uninstall):
        typer.echo(line)


@service_app.command("start")
def service_start() -> None:
    _service(service.start)


@service_app.command("stop")
def service_stop() -> None:
    _service(service.stop)


@service_app.command("restart")
def service_restart() -> None:
    _service(service.restart)


@service_app.command("status")
def service_status() -> None:
    """Whether the unit is installed, loaded, and running (with its pid)."""
    status = _service(service.status)
    state = (
        f"running pid={status.pid}" if status.running else "loaded" if status.loaded else "stopped"
    )
    if not status.installed:
        state = "not installed"
    typer.echo(f"{status.platform}: {state} ({status.unit})")


# -- workspace ------------------------------------------------------------------


@workspace_app.command("list")
def workspace_list() -> None:
    """List workspaces with their bindings, jobs, and audit state."""
    paths = Paths.from_env()
    names = workspaces.list_workspaces(paths)
    if not names:
        typer.echo("no workspaces yet; run `enso workspace create NAME`")
        return
    config, _, _ = check_config(paths)
    report = audit.audit(paths, names, config=config)
    rows = [["WORKSPACE", "BINDINGS", "JOBS", "AUDIT"]]
    rows.extend(
        [w.name, ", ".join(w.bindings) or "-", ", ".join(w.jobs) or "-", w.summary]
        for w in report.workspaces
    )
    typer.echo(columns(rows))
    if not report.home.ok:
        typer.echo(f"home: {report.home.summary}; run `enso workspace audit`", err=True)


def audit_lines(report: audit.Report, *, fix: bool) -> list[str]:
    """The audit as text: one header per root, then what was fixed and what remains."""
    lines = [f"home {report.home.path}: {report.home.summary}"]
    lines.extend(_root_lines(report.home, fix=fix))
    for workspace in report.workspaces:
        lines.append(f"{workspace.name}: {workspace.summary}")
        if workspace.bindings:
            lines.append(f"  bindings: {', '.join(workspace.bindings)}")
        if workspace.jobs:
            lines.append(f"  jobs: {', '.join(workspace.jobs)}")
        if workspace.uploads_bytes:
            lines.append(f"  uploads: {human_bytes(workspace.uploads_bytes)}")
        lines.extend(_root_lines(workspace, fix=fix))
    return lines


def _root_lines(root: audit.HomeAudit | audit.WorkspaceAudit, *, fix: bool) -> list[str]:
    lines = [f"  fixed: {line.replace(f'{root.path}/', '')}" for line in root.fixed]
    for finding in root.findings:
        hint = " (repairable with --fix)" if finding.fixable and not fix else ""
        lines.append(f"  {finding.severity}: {finding.message}{hint}")
    return lines


@workspace_app.command("audit")
def workspace_audit(
    name: str | None = typer.Argument(None, help="One workspace; default is every workspace."),
    fix: bool = typer.Option(
        False, "--fix", help="Create missing directories and links, repoint wrong links."
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Check the home and the workspaces against the documented layout; exit 1 on errors."""
    paths = Paths.from_env()
    if name is not None and not paths.workspace(name).is_dir():
        fail([f"no workspace named {name}"], as_json=as_json)
    config, _, _ = check_config(paths)
    report = audit.audit(paths, [name] if name is not None else None, fix=fix, config=config)
    if as_json:
        echo_json(report.as_dict())
    else:
        if config is None:
            typer.echo("config.json is unusable, so bindings were not checked", err=True)
        for line in audit_lines(report, fix=fix):
            typer.echo(line)
    if not report.ok:
        raise typer.Exit(1)


@workspace_app.command("create")
def workspace_create(name: str) -> None:
    """Scaffold NAME: AGENTS.md, skills/, knowledge/, drafts/, uploads/, and the links."""
    paths = Paths.from_env()
    try:
        root = workspaces.create_workspace(paths, name)
    except (ValueError, FileExistsError) as exc:
        fail([str(exc)])
    typer.echo(f"created {root}")
    typer.echo(f'bind a conversation to it in {paths.config}: "bindings": {{"slack:C…": "{name}"}}')


def main() -> None:
    app()
