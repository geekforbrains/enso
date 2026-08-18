"""Enso CLI — the brain behind the bot."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from . import __version__, slack_cache, tables
from . import config as config_module
from .auth import parse_telegram_allowed_users
from .config import (
    CONFIG_FILE,
    DEFAULT_POLICY_NAME,
    DEFAULT_WORKSPACE_NAME,
    ConfigError,
    SetupState,
    config_lock,
    detect_providers,
    load_config,
    managed_workspace_path,
    provider_models,
    resolve_providers,
    save_config,
    setup_state,
    unrestricted_policy_config,
)
from .docs import MAX_DOCS, create_doc, load_docs
from .jobs import create_job, load_jobs, load_jobs_with_errors
from .logging_config import configure_logging
from .messages import clear as msg_clear
from .messages import pending as msg_pending
from .messages import send as msg_send
from .providers import PROVIDER_CLASSES, PROVIDER_NAMES
from .secret_refs import (
    SecretResolutionError,
    resolve_config_secret,
    update_config_secret_reference,
)
from .slack_text import (
    IGNORED_SUBTYPES,
    _flatten_mention_text,
    _message_context_text,
)
from .transports import BaseTransport

log = logging.getLogger(__name__)

app = typer.Typer(help="Enso — AI agents from your phone", no_args_is_help=True)
job_app = typer.Typer(help="Manage background jobs")
doc_app = typer.Typer(help="Manage reference docs")
table_app = typer.Typer(help="Manage registered SQLite data tables")
message_app = typer.Typer(help="Send messages and files via the configured transport")
service_app = typer.Typer(help="Manage the background service")
slack_app = typer.Typer(help="Slack directory lookups and message search")
config_app = typer.Typer(help="Validate routes, workspaces, policies, and jobs")
route_app = typer.Typer(help="Explain Slack routing decisions")
audit_app = typer.Typer(help="Inspect the Slack audit trail")
app.add_typer(job_app, name="job")
app.add_typer(doc_app, name="doc")
app.add_typer(table_app, name="table")
app.add_typer(message_app, name="message")
app.add_typer(service_app, name="service")
app.add_typer(slack_app, name="slack")
app.add_typer(config_app, name="config")
app.add_typer(route_app, name="route")
app.add_typer(audit_app, name="audit")

console = Console()

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _load_config_or_exit(*, allow_missing: bool = False) -> dict:
    """Load strict configuration with a concise CLI diagnostic."""
    try:
        return load_config(allow_missing=True) if allow_missing else load_config()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None


@contextlib.contextmanager
def _config_lock_or_exit():
    """Acquire the config lock with a concise setup diagnostic."""
    try:
        with config_lock():
            yield
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None


def _ensure_repository_or_exit() -> None:
    """Establish the local content-history boundary or stop setup safely."""
    from .repository import EnsoRepository, RepositoryError

    try:
        EnsoRepository().ensure()
    except RepositoryError as exc:
        console.print(f"[red]Could not initialize Enso content history:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None


def _installation_errors(config: dict) -> tuple[str, ...]:
    """Return read-only repository and managed-scaffold diagnostics."""
    from .repository import EnsoRepository, RepositoryError
    from .scaffolding import ScaffoldError, ScaffoldService
    from .teams import load_catalog

    errors: list[str] = []
    try:
        EnsoRepository().validate()
    except RepositoryError as exc:
        errors.append(str(exc))

    catalog = load_catalog(config)
    errors.extend(catalog.errors)
    for problems in catalog.workspace_errors.values():
        errors.extend(problems)

    service = ScaffoldService()
    errors.extend(service.validate_global().errors)
    for name in sorted(catalog.workspaces):
        try:
            errors.extend(service.validate_workspace(name).errors)
        except ScaffoldError as exc:
            errors.append(str(exc))
    return tuple(dict.fromkeys(errors))


def _validate_installation_or_exit(config: dict) -> None:
    """Fail an operational startup without seeding or repairing content."""
    errors = _installation_errors(config)
    if not errors:
        return
    console.print("[red]Enso's managed installation is invalid:[/]")
    for problem in errors:
        console.print(f"  [red]✗[/] {escape(problem)}")
    console.print("[dim]Run `enso setup` to repair structure after migrating legacy paths.[/]")
    raise typer.Exit(1)

# ---------------------------------------------------------------------------
# Telegram API helpers (stdlib only — no extra deps for setup)
# ---------------------------------------------------------------------------


def _tg_call(token: str, method: str, **params: object) -> dict:
    """Call a Telegram Bot API method. Returns the parsed JSON response."""
    url = TELEGRAM_API.format(token=token, method=method)
    if params:
        data = json.dumps(params).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _tg_validate_token(token: str) -> dict | None:
    """Validate a bot token via getMe. Returns bot info or None."""
    try:
        result = _tg_call(token, "getMe")
        if result.get("ok"):
            return result["result"]
    except Exception:
        pass
    return None


def _tg_wait_for_message(token: str, timeout: int = 120) -> dict | None:
    """Poll for the first message. Returns user/chat info or None on timeout."""
    # Clear pending updates
    try:
        result = _tg_call(token, "getUpdates", offset=-1, timeout=0)
        if result.get("ok") and result.get("result"):
            last_id = result["result"][-1]["update_id"]
            _tg_call(token, "getUpdates", offset=last_id + 1, timeout=0)
    except Exception:
        pass

    start = time.time()
    last_update_id = 0
    while time.time() - start < timeout:
        try:
            params: dict[str, int] = {"timeout": 5}
            if last_update_id:
                params["offset"] = last_update_id + 1
            result = _tg_call(token, "getUpdates", **params)
            if result.get("ok"):
                for update in result.get("result", []):
                    last_update_id = update["update_id"]
                    msg = update.get("message")
                    if msg and msg.get("from"):
                        _tg_call(token, "getUpdates", offset=last_update_id + 1, timeout=0)
                        user = msg["from"]
                        return {
                            "user_id": user.get("id"),
                            "username": user.get("username"),
                            "first_name": user.get("first_name"),
                            "chat_id": msg["chat"]["id"],
                        }
        except Exception:
            pass
        time.sleep(1)
    return None


_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_AUDIO_EXTENSIONS = {".mp3", ".ogg", ".wav", ".flac", ".m4a"}
_VOICE_EXTENSIONS = {".oga"}


def _tg_send_file(token: str, chat_id: int | str, file_path: str, caption: str = "") -> bool:
    """Send a file to Telegram. Auto-selects method based on extension."""
    import mimetypes
    from io import BytesIO

    ext = os.path.splitext(file_path)[1].lower()
    if ext in _PHOTO_EXTENSIONS:
        method, field = "sendPhoto", "photo"
    elif ext in _VIDEO_EXTENSIONS:
        method, field = "sendVideo", "video"
    elif ext in _AUDIO_EXTENSIONS:
        method, field = "sendAudio", "audio"
    elif ext in _VOICE_EXTENSIONS:
        method, field = "sendVoice", "voice"
    else:
        method, field = "sendDocument", "document"

    url = TELEGRAM_API.format(token=token, method=method)
    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

    # Build multipart form data (stdlib only)
    boundary = f"----enso{uuid.uuid4().hex}"
    body = BytesIO()

    def add_field(name: str, value: str) -> None:
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(f"{value}\r\n".encode())

    add_field("chat_id", str(chat_id))
    if caption:
        from .formatting import md_to_html

        add_field("caption", md_to_html(caption))
        add_field("parse_mode", "HTML")

    # File part
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
    )
    with open(file_path, "rb") as f:
        body.write(f.read())
    body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())

    data = body.getvalue()
    last_err = "unknown error"
    for attempt in range(1, 4):
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
            if result.get("ok"):
                return True
            last_err = result.get("description", "Telegram returned ok=false")
        except Exception as exc:
            last_err = str(exc)
            # HTTPError carries Telegram's JSON error body — surface it.
            with contextlib.suppress(Exception):
                last_err = exc.read().decode("utf-8", "replace")  # type: ignore[attr-defined]
        log.warning(
            "telegram %s failed (attempt %d/3) file=%s chat=%s: %s",
            method,
            attempt,
            filename,
            chat_id,
            last_err,
        )
        if attempt < 3:
            time.sleep(2 * attempt)
    log.error(
        "telegram %s gave up after 3 attempts file=%s chat=%s: %s",
        method,
        filename,
        chat_id,
        last_err,
    )
    return False


def _tg_send_message(token: str, chat_id: int | str, text: str) -> bool:
    """Send a message with HTML formatting. Returns True on success."""
    from .formatting import md_to_html

    try:
        html = md_to_html(text)
        result = _tg_call(
            token,
            "sendMessage",
            chat_id=chat_id,
            text=html,
            parse_mode="HTML",
        )
        if result.get("ok"):
            return True
    except Exception:
        pass
    # Fallback to plain text
    try:
        result = _tg_call(token, "sendMessage", chat_id=chat_id, text=text)
        return result.get("ok", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Service management
# ---------------------------------------------------------------------------

_LAUNCHD_LABEL = "com.enso.agent"
_LAUNCHD_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{_LAUNCHD_LABEL}.plist")
_SYSTEMD_UNIT = "enso.service"

# Advanced tuning env vars read by the runtime (see core.py). Snapshotted
# into the service definition at install time so exports actually reach
# `enso serve` under launchd/systemd's minimal environment.
_ENSO_TUNING_ENV_KEYS = (
    "ENSO_SESSION_TTL_DAYS",
    "ENSO_JOB_CONCURRENCY",
    "ENSO_PROCESS_TERMINATE_GRACE_SECS",
    "ENSO_JOB_FAILURE_RENOTIFY_SECS",
)


def _find_enso_bin() -> str | None:
    """Locate the enso binary."""
    found = shutil.which("enso")
    if not found:
        venv_bin = os.path.join(sys.prefix, "bin", "enso")
        if os.path.exists(venv_bin):
            found = venv_bin
    return found


def _build_path_str(enso_bin: str) -> str:
    """Build a PATH string from detected CLI locations."""
    path_dirs: set[str] = {os.path.dirname(enso_bin)}
    for cmd in (*PROVIDER_NAMES, "node", "npx"):
        p = shutil.which(cmd)
        if p:
            path_dirs.add(os.path.dirname(p))
    path_dirs.update(["/usr/local/bin", "/usr/bin", "/bin"])
    return ":".join(sorted(path_dirs))


def _systemd_env() -> dict[str, str]:
    """Build env dict with XDG_RUNTIME_DIR and DBUS for systemctl."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return {
        **os.environ,
        "XDG_RUNTIME_DIR": xdg,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xdg}/bus",
    }


def _service_platform() -> str | None:
    """Return 'launchd' or 'systemd' based on platform, or None."""
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "linux":
        return "systemd"
    return None


def _service_is_installed() -> bool:
    """Check if the service definition file exists."""
    platform = _service_platform()
    if platform == "launchd":
        return os.path.exists(_LAUNCHD_PLIST)
    if platform == "systemd":
        path = os.path.expanduser(f"~/.config/systemd/user/{_SYSTEMD_UNIT}")
        return os.path.exists(path)
    return False


def _service_cmd(launchd_argv: list[str], systemd_argv: list[str]) -> bool:
    """Run the current platform's service-control command.

    Returns True when the command exits 0; False on failure, unknown
    platform, or any raised error.
    """
    platform = _service_platform()
    try:
        if platform == "launchd":
            r = subprocess.run(launchd_argv, capture_output=True)
        elif platform == "systemd":
            r = subprocess.run(
                systemd_argv,
                env=_systemd_env(),
                capture_output=True,
            )
        else:
            return False
        return r.returncode == 0
    except Exception:
        return False


def _service_is_running() -> bool:
    """Check if the service process is currently running."""
    return _service_cmd(
        ["launchctl", "list", _LAUNCHD_LABEL],
        ["systemctl", "--user", "is-active", "--quiet", _SYSTEMD_UNIT],
    )


def _service_install() -> bool:
    """Write and load the platform service definition. Returns True on success."""
    enso_bin = _find_enso_bin()
    if not enso_bin:
        console.print("[red]Could not find 'enso' binary.[/]")
        return False

    platform = _service_platform()
    if platform == "launchd":
        return _install_launchd(enso_bin)
    if platform == "systemd":
        return _install_systemd(enso_bin)

    console.print(f"[yellow]Service install not supported on {sys.platform}.[/]")
    return False


def _install_launchd(enso_bin: str) -> bool:
    """Write and load a macOS launchd plist."""
    path_str = _build_path_str(enso_bin)
    log_path = os.path.expanduser("~/.enso/enso.log")

    # Snapshot API keys and essential env vars so provider CLIs work
    # under launchd's minimal environment.
    extra_env = ""
    provider_env_keys = (key for cls in PROVIDER_CLASSES.values() for key in cls.env_keys)
    for key in ("HOME", *provider_env_keys, *_ENSO_TUNING_ENV_KEYS):
        val = os.environ.get(key)
        if val:
            extra_env += f"        <key>{key}</key>\n        <string>{val}</string>\n"

    plist = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{enso_bin}</string>
        <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_str}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
{extra_env}    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""
    os.makedirs(os.path.dirname(_LAUNCHD_PLIST), exist_ok=True)
    # Unload first if already loaded
    if os.path.exists(_LAUNCHD_PLIST):
        subprocess.run(
            ["launchctl", "unload", _LAUNCHD_PLIST],
            capture_output=True,
        )
    with open(_LAUNCHD_PLIST, "w") as f:
        f.write(plist)

    try:
        subprocess.run(
            ["launchctl", "load", _LAUNCHD_PLIST],
            capture_output=True,
            check=True,
        )
        console.print("[green]\u2713[/] Service installed and started.")
        return True
    except Exception:
        console.print(f"Written to {_LAUNCHD_PLIST}")
        console.print(f"Load with: launchctl load {_LAUNCHD_PLIST}")
        return False


def _install_systemd(enso_bin: str) -> bool:
    """Write and enable a systemd user service."""
    path_str = _build_path_str(enso_bin)

    extra_env = ""
    for key in _ENSO_TUNING_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            extra_env += f"Environment={key}={val}\n"

    unit = f"""\
[Unit]
Description=Enso - Personal AI Agent
After=network.target

[Service]
Type=simple
ExecStart={enso_bin} serve
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=PATH={path_str}
{extra_env}
[Install]
WantedBy=default.target
"""
    service_dir = os.path.expanduser("~/.config/systemd/user")
    service_path = os.path.join(service_dir, _SYSTEMD_UNIT)
    os.makedirs(service_dir, exist_ok=True)
    with open(service_path, "w") as f:
        f.write(unit)

    try:
        env = _systemd_env()
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            env=env,
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT],
            env=env,
            capture_output=True,
        )
        console.print("[green]\u2713[/] Service installed and started.")
        return True
    except Exception:
        console.print(f"Written to {service_path}")
        console.print(f"Enable with: systemctl --user enable --now {_SYSTEMD_UNIT}")
        return False


def _service_uninstall() -> bool:
    """Stop and remove the service definition. Returns True on success."""
    platform = _service_platform()
    if platform == "launchd":
        if os.path.exists(_LAUNCHD_PLIST):
            subprocess.run(
                ["launchctl", "unload", _LAUNCHD_PLIST],
                capture_output=True,
            )
            os.remove(_LAUNCHD_PLIST)
            return True
    elif platform == "systemd":
        path = os.path.expanduser(f"~/.config/systemd/user/{_SYSTEMD_UNIT}")
        if os.path.exists(path):
            env = _systemd_env()
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT],
                env=env,
                capture_output=True,
            )
            os.remove(path)
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                env=env,
                capture_output=True,
            )
            return True
    return False


def _service_start() -> bool:
    """Start the service. Returns True on success."""
    return _service_cmd(
        ["launchctl", "load", _LAUNCHD_PLIST],
        ["systemctl", "--user", "start", _SYSTEMD_UNIT],
    )


def _service_stop() -> bool:
    """Stop the service. Returns True on success."""
    return _service_cmd(
        ["launchctl", "unload", _LAUNCHD_PLIST],
        ["systemctl", "--user", "stop", _SYSTEMD_UNIT],
    )


def _service_restart() -> bool:
    """Restart the service. Returns True on success."""
    if _service_platform() is None:
        # Guard before building argv — os.getuid() doesn't exist everywhere.
        return False
    return _service_cmd(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"],
        ["systemctl", "--user", "restart", _SYSTEMD_UNIT],
    )


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------


def _setup_providers(config: dict) -> None:
    """Step 1: detect and display available provider CLIs."""
    console.rule("[bold]Step 1 \u00b7 Provider Detection")
    resolved = resolve_providers()
    config["providers"] = resolved

    available = detect_providers()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("status", width=3)
    table.add_column("name")
    table.add_column("path", style="dim")
    for name, info in resolved.items():
        if available.get(name):
            table.add_row("[green]\u2713[/]", name, info["path"])
        else:
            table.add_row("[red]\u2717[/]", f"[dim]{name}[/]", "")
    console.print(table)

    if not any(available.values()):
        console.print("\n[yellow]No provider CLIs found on PATH.[/]")
        console.print(f"Install at least one of: {', '.join(PROVIDER_NAMES)}")


def _ensure_default_execution_config(config: dict) -> str:
    """Seed the default workspace and its reusable unrestricted admin policy.

    Setup uses the workspace for the first exact Slack DM route and new jobs.
    Existing definitions are retained; a missing workspace policy is filled in.
    """
    workspaces = config.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
        config["workspaces"] = workspaces
    workspace_name = DEFAULT_WORKSPACE_NAME
    configured_workspace = workspaces.get(workspace_name)
    workspace = configured_workspace if isinstance(configured_workspace, dict) else {}
    workspaces[workspace_name] = workspace
    workspace.setdefault("policy", DEFAULT_POLICY_NAME)
    workspace.setdefault("concurrency", 1)

    policies = config.get("policies")
    if not isinstance(policies, dict):
        policies = {}
        config["policies"] = policies
    configured = provider_models(config)
    if workspace["policy"] == DEFAULT_POLICY_NAME:
        policies.setdefault(
            DEFAULT_POLICY_NAME,
            unrestricted_policy_config(list(configured)),
        )
    return workspace_name


def _setup_transport(config: dict) -> int | None:
    """Step 3: configure transport."""
    console.rule("[bold]Step 3 \u00b7 Transport")
    choices = ["telegram", "slack"]
    configured_transport = config.get("transport")
    transport = Prompt.ask(
        "  Transport",
        choices=choices,
        default=configured_transport if configured_transport in choices else ...,
    )
    config["transport"] = transport
    if transport == "telegram":
        return _setup_telegram(config)
    elif transport == "slack":
        _setup_slack(config)
        return None
    return None


def _setup_telegram(config: dict) -> int | None:
    """Configure Telegram bot and capture user. Returns chat_id or None."""
    tg_cfg = config.get("transports", {}).get("telegram", {})
    workspace = tg_cfg.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        workspace = None
    try:
        current_token = resolve_config_secret(tg_cfg, "bot_token")
    except SecretResolutionError as exc:
        console.print(f"[yellow]  Existing Telegram token could not be loaded: {exc}[/]")
        current_token = ""
    current_users = _tg_allowed_users(tg_cfg)
    bot_info = None

    if current_token:
        bot_info = _tg_validate_token(current_token)
        if bot_info:
            console.print(f"  Current bot: [bold]@{bot_info.get('username', '?')}[/]")
            if current_users:
                console.print(f"  Allowed users: {current_users}")
            if not Confirm.ask("\n  Reconfigure Telegram?", default=False):
                default_workspace = _ensure_default_execution_config(config)
                tg_cfg.setdefault("workspace", workspace or default_workspace)
                return None
            current_users = []
        else:
            console.print("[yellow]  Existing token is invalid.[/]")
        current_token = ""

    console.print("  To connect, you need a Telegram bot token.\n")
    console.print("  1. Message @BotFather in Telegram")
    console.print("  2. Send /newbot")
    console.print("  3. Copy the token BotFather gives you\n")

    while True:
        token = Prompt.ask("  Bot token", password=True)
        if not token:
            console.print("[red]  Token is required.[/]")
            continue
        with console.status("Validating..."):
            bot_info = _tg_validate_token(token)
        if bot_info:
            console.print(f"  [green]\u2713[/] Connected to @{bot_info.get('username', '?')}")
            is_reference = _update_referenced_secret_or_exit(
                tg_cfg,
                "bot_token",
                token,
                "Telegram bot token",
            )
            default_workspace = _ensure_default_execution_config(config)
            workspace = workspace or default_workspace
            if is_reference:
                next_cfg = dict(tg_cfg)
                next_cfg.pop("bot_token", None)
                next_cfg.pop("allowed_user_ids", None)
                next_cfg["allowed_users"] = current_users
                next_cfg["workspace"] = workspace
            else:
                next_cfg = {
                    "bot_token": token,
                    "allowed_users": current_users,
                    "notify_channel": tg_cfg.get("notify_channel", ""),
                    "workspace": workspace,
                }
            config.setdefault("transports", {})["telegram"] = next_cfg
            current_token = token
            break
        console.print("[red]  \u2717 Invalid token. Try again.[/]")

    if current_users:
        return None

    console.print(f"\n  Send any message to @{bot_info.get('username', '?')} in Telegram.\n")
    with console.status("Waiting for message..."):
        user_info = _tg_wait_for_message(current_token, timeout=120)

    if not (user_info and user_info.get("user_id")):
        console.print("[yellow]  Timed out. Add your user ID manually in config.json.[/]")
        return None

    user_id = user_info["user_id"]
    name = user_info.get("first_name") or user_info.get("username") or "?"
    console.print(f"  [green]\u2713[/] Got it! {name} (ID: {user_id})")
    config["transports"]["telegram"]["allowed_users"] = [str(user_id)]
    config["transports"]["telegram"]["notify_channel"] = str(user_info.get("chat_id") or user_id)
    return user_info.get("chat_id")


# ---------------------------------------------------------------------------
# Slack API helpers (stdlib only — no extra deps for setup)
# ---------------------------------------------------------------------------


def _slack_validate_token(token: str) -> dict | None:
    """Validate a Slack bot token via auth.test.

    Returns the response dict on success, or None on failure.
    """
    try:
        data = slack_cache.api_get(token, "auth.test")
        return data if data.get("ok") else None
    except Exception:
        return None


def _slack_send_message(
    token: str,
    channel: str,
    text: str,
    thread_ts: str = "",
) -> bool:
    """Send a message to Slack via chat.postMessage.

    When ``thread_ts`` is set the message lands in that thread.
    """
    payload: dict = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        return slack_cache.api_post(token, "chat.postMessage", payload).get("ok", False)
    except Exception:
        return False


def _resolve_slack_target(
    explicit: str,
    notify_channel: str,
) -> tuple[str, str]:
    """Resolve the Slack channel + thread for ``message send``/``attach``.

    Priority:

    1. Explicit ``--to`` wins and clears thread context (cross-channel reply
       shouldn't pretend to stay in the original thread).
    2. Otherwise fall back to ``ENSO_ORIGIN_CHANNEL`` — the agent's own
       conversation when spawned from a transport — and carry
       ``ENSO_ORIGIN_THREAD_TS`` along.
    3. Finally fall back to the configured ``notify_channel`` (no thread).

    Returns ``(channel, thread_ts)``; an empty channel string means nothing
    was resolved and the caller should report a usage error.
    """
    if explicit:
        return explicit, ""
    origin = os.environ.get("ENSO_ORIGIN_CHANNEL", "")
    if origin:
        return origin, os.environ.get("ENSO_ORIGIN_THREAD_TS", "")
    return notify_channel, ""


def _tg_allowed_users(tg_cfg: dict) -> list[str]:
    """Return exact configured Telegram user IDs; malformed values fail closed."""
    return parse_telegram_allowed_users(tg_cfg)


def _resolve_transport_secret_or_exit(
    transport_cfg: dict,
    key: str,
    transport_name: str,
) -> str:
    """Resolve a transport credential and turn reference errors into CLI output."""
    try:
        return resolve_config_secret(transport_cfg, key)
    except SecretResolutionError as exc:
        console.print(f"[red]✗[/] Could not load {transport_name} credentials: {exc}")
        raise typer.Exit(1) from None


def _update_referenced_secret_or_exit(
    transport_cfg: dict,
    key: str,
    value: str,
    label: str,
) -> bool:
    """Update an existing secret reference or exit without adding plaintext."""
    try:
        return update_config_secret_reference(transport_cfg, key, value)
    except SecretResolutionError as exc:
        console.print(f"[red]✗[/] Could not save {label}: {exc}")
        raise typer.Exit(1) from None


def _update_referenced_secrets_with_rollback_or_exit(
    transport_cfg: dict,
    updates: list[tuple[str, str, str]],
) -> dict[str, bool]:
    """Update a credential set, restoring earlier referenced writes on failure."""
    referenced = [
        (key, label) for key, _value, label in updates if f"{key}_1password" in transport_cfg
    ]
    previous: dict[str, str] = {}
    for key, label in referenced:
        try:
            previous[key] = resolve_config_secret(transport_cfg, key)
        except SecretResolutionError:
            console.print(
                f"[red]✗[/] Could not prepare credential update:"
                f" existing {label} could not be loaded."
            )
            raise typer.Exit(1) from None

    results: dict[str, bool] = {}
    updated: list[tuple[str, str]] = []
    for key, value, label in updates:
        try:
            is_reference = update_config_secret_reference(
                transport_cfg,
                key,
                value,
            )
        except SecretResolutionError:
            rollback_failures: list[str] = []
            for updated_key, updated_label in reversed(updated):
                try:
                    update_config_secret_reference(
                        transport_cfg,
                        updated_key,
                        previous[updated_key],
                    )
                except SecretResolutionError:
                    rollback_failures.append(updated_label)
            console.print(f"[red]✗[/] Could not save {label} through 1Password.")
            if rollback_failures:
                console.print(
                    "[red]✗[/] Rollback also failed for:"
                    f" {', '.join(rollback_failures)}."
                    " Referenced credentials may be inconsistent."
                )
            elif updated:
                console.print("[yellow]Earlier referenced credential updates were restored.[/]")
            raise typer.Exit(1) from None
        results[key] = is_reference
        if is_reference:
            updated.append((key, label))
    return results


def _resolve_send_targets(cfg: dict, to: str) -> tuple[str, str, list[str], str]:
    """Resolve delivery for ``message send``/``attach``.

    Returns ``(transport, token, targets, thread_ts)``. Both transports resolve
    one explicit, originating, or default destination. Authorization never
    doubles as a notification subscription.
    """
    transport = cfg.get("transport", "telegram")

    if transport == "slack":
        slack_cfg = cfg.get("transports", {}).get("slack", {})
        token = _resolve_transport_secret_or_exit(slack_cfg, "bot_token", "Slack")
        if not token:
            console.print("[red]✗[/] Slack not configured. Run [bold]enso setup[/].")
            raise typer.Exit(1)
        target, thread_ts = _resolve_slack_target(
            to,
            slack_cfg.get("notify_channel", ""),
        )
        if not target:
            console.print("[red]✗[/] No destination. Pass --to or set notify_channel in config.")
            raise typer.Exit(1)
        return transport, token, [target], thread_ts

    tg_cfg = cfg.get("transports", {}).get("telegram", {})
    token = _resolve_transport_secret_or_exit(tg_cfg, "bot_token", "Telegram")
    if not token:
        console.print("[red]✗[/] Telegram not configured. Run [bold]enso setup[/].")
        raise typer.Exit(1)
    origin = os.environ.get("ENSO_ORIGIN_CHANNEL", "")
    if to:
        targets = [to]
    elif origin:
        targets = [origin]
    else:
        notify = tg_cfg.get("notify_channel", "")
        targets = [notify] if isinstance(notify, str) and notify else []
    if not targets:
        console.print("[red]✗[/] No destination. Pass --to or set notify_channel in config.")
        raise typer.Exit(1)
    return transport, token, targets, ""


_SLACK_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1 GB


def _slack_upload_file(
    token: str,
    channel: str,
    file_path: str,
    caption: str = "",
    thread_ts: str = "",
) -> tuple[bool, str]:
    """Upload a file to Slack using the external upload flow.

    Returns (ok, error_message). error_message is empty on success.
    When ``thread_ts`` is set the file is shared into that thread.
    """
    from urllib.parse import urlencode

    filename = os.path.basename(file_path)
    filesize = os.path.getsize(file_path)
    if filesize == 0:
        return (False, "File is empty.")
    if filesize > _SLACK_MAX_UPLOAD_BYTES:
        mb = filesize / (1024 * 1024)
        return (False, f"File is {mb:.1f} MB; Slack limit is 1024 MB.")

    # Step 1: request an upload URL
    body = urlencode({"filename": filename, "length": filesize}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/files.getUploadURLExternal",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return (False, f"getUploadURLExternal request failed: {e}")
    if not result.get("ok"):
        return (False, f"getUploadURLExternal: {result.get('error', 'unknown')}")
    upload_url = result.get("upload_url")
    file_id = result.get("file_id")
    if not upload_url or not file_id:
        return (False, "getUploadURLExternal: missing upload_url or file_id")

    # Step 2: POST file bytes to the returned URL
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        req = urllib.request.Request(
            upload_url,
            data=file_bytes,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            if resp.status not in (200, 201, 204):
                return (False, f"Upload POST returned HTTP {resp.status}")
    except Exception as e:
        return (False, f"File upload failed: {e}")

    # Step 3: complete the upload (shares file to channel with optional comment)
    complete_payload: dict = {
        "files": [{"id": file_id, "title": filename}],
        "channel_id": channel,
    }
    if caption:
        complete_payload["initial_comment"] = caption
    if thread_ts:
        complete_payload["thread_ts"] = thread_ts
    req = urllib.request.Request(
        "https://slack.com/api/files.completeUploadExternal",
        data=json.dumps(complete_payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        return (False, f"completeUploadExternal request failed: {e}")
    if not result.get("ok"):
        return (False, f"completeUploadExternal: {result.get('error', 'unknown')}")
    return (True, "")


def _write_slack_manifest_copy() -> str:
    """Copy the bundled Slack app manifest into ``~/.enso/`` and return path."""
    import importlib.resources as resources

    source = resources.files("enso").joinpath("slack_manifest.yaml")
    dest = os.path.join(os.path.expanduser("~/.enso"), "slack-app-manifest.yaml")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        content = source.read_text(encoding="utf-8")
        with open(dest, "w") as f:
            f.write(content)
    except OSError:
        log.warning("Could not write Slack manifest to %s", dest, exc_info=True)
    return dest


# A linear interactive wizard: every branch is one prompt in a fixed sequence,
# so splitting it would only scatter the script the user is walked through.
def _setup_slack(config: dict) -> None:  # noqa: C901
    """Configure Slack credentials and one exact routed owner DM."""
    if "routes" in config:
        console.print(
            "[red]  Legacy top-level routes are no longer supported; move"
            " routes.slack fields into transports.slack, remove routes, and"
            " rerun setup.[/]"
        )
        raise typer.Exit(1)
    slack_cfg = config.get("transports", {}).get("slack", {})
    manifest_path = _write_slack_manifest_copy()
    existing_routes = (
        slack_cfg
        if isinstance(slack_cfg.get("account_id"), str)
        and slack_cfg.get("account_id")
        and ("dms" not in slack_cfg or isinstance(slack_cfg["dms"], dict))
        and ("channels" not in slack_cfg or isinstance(slack_cfg["channels"], dict))
        else None
    )
    needs_allowlist_migration = "allowed_users" in slack_cfg
    try:
        current_bot = resolve_config_secret(slack_cfg, "bot_token")
    except SecretResolutionError as exc:
        console.print(f"[yellow]  Existing Slack token could not be loaded: {exc}[/]")
        current_bot = ""

    if current_bot:
        auth = _slack_validate_token(current_bot)
        if auth:
            console.print(f"  Current bot: [bold]{auth.get('user', '?')}[/]")
            if needs_allowlist_migration:
                console.print(
                    "[yellow]  This Slack configuration uses the removed"
                    " allowed_users mode and must be migrated to exact routes.[/]"
                )
            if not Confirm.ask(
                "\n  Migrate Slack now?" if needs_allowlist_migration else "\n  Reconfigure Slack?",
                default=needs_allowlist_migration,
            ):
                console.print(
                    "  Slack credentials unchanged. Apply the current app manifest"
                    " before restarting: "
                    f"[bold]{manifest_path}[/]"
                )
                return
        else:
            console.print("[yellow]  Existing token is invalid.[/]")

    # Offer a one-paste app manifest to short-circuit the Slack app wizard.
    console.print("  To create the Slack app, paste the bundled manifest:\n")
    console.print("   1. Open [bold]https://api.slack.com/apps?new_app=1[/]")
    console.print("   2. Choose [bold]From an app manifest[/]")
    console.print("   3. Pick your workspace")
    console.print(f"   4. Paste the contents of [bold]{manifest_path}[/]")
    console.print(
        "      (scopes, events, App Home, interactivity, and Socket Mode"
        " are pre-configured)"
    )
    console.print("   5. [bold]Install to workspace[/] — this gives you the Bot Token")
    console.print(
        "   6. Basic Information \u2192 [bold]App-Level Tokens[/]"
        " \u2192 Generate, with scope [bold]connections:write[/]"
    )
    console.print("   7. Copy both tokens; paste them when prompted below.\n")
    console.print(
        "  [dim]For an existing app, apply this manifest and reinstall if Slack"
        " requests new scope consent before reusing its tokens.[/]\n"
    )

    while True:
        bot_token = Prompt.ask("  Bot Token (xoxb-...)", password=True)
        if not bot_token:
            console.print("[red]  Token is required.[/]")
            continue
        with console.status("Validating bot token..."):
            auth = _slack_validate_token(bot_token)
        if auth:
            bot_name = auth.get("user", "?")
            bot_user_id = auth.get("user_id", "")
            team_id = auth.get("team_id", "")
            if not isinstance(team_id, str) or not team_id:
                console.print("[red]  \u2717 Slack did not return a workspace ID.[/]")
                continue
            console.print(f"  [green]\u2713[/] Authenticated as {bot_name}")
            break
        console.print("[red]  \u2717 Invalid token. Try again.[/]")

    # App Token (for Socket Mode — required, but there is no validation API)
    while True:
        app_token = Prompt.ask("  App Token (xapp-...)", password=True)
        if app_token:
            break
        console.print("[red]  Token is required.[/]")

    reset_routes = existing_routes is None
    if existing_routes is not None and existing_routes.get("account_id") != team_id:
        if not Confirm.ask(
            "\n  The token belongs to a different Slack workspace. Replace the"
            " existing Slack routes?",
            default=False,
        ):
            console.print("[yellow]  Slack configuration was not changed.[/]")
            return
        reset_routes = True

    owner_id = ""
    if reset_routes:
        console.print(
            "\n  Add the first administrator DM route. Channel routes can be"
            " added later in config.json.\n"
        )
        while not owner_id:
            candidate = Prompt.ask("  Owner Slack user ID").strip()
            if candidate and candidate != "*" and not any(ch.isspace() for ch in candidate):
                owner_id = candidate
            else:
                console.print("[red]  Enter one exact Slack user ID (for example U012ABC).[/]")

    # Notify channel — where `enso message send` (no --to) and scheduled-job
    # alerts deliver. Without one they fail with "no destination" because
    # Slack never auto-broadcasts.
    console.print()
    console.print(
        "  [bold]Default notify channel[/] \u2014 channel/DM ID where"
        " background alerts go\n  (scheduled-job failures and"
        " `enso message send` with no --to).\n"
    )
    notify = Prompt.ask(
        "  Notify channel (leave blank to configure later)",
        default="",
    )
    if not notify:
        console.print(
            "  [yellow]\u26a0[/] Without a notify channel, background"
            " Slack messages (job alerts and `enso message send`)"
            " will be dropped.\n      Set it later by editing"
            " ~/.enso/config.json or re-running `enso setup`."
        )

    reference_updates = _update_referenced_secrets_with_rollback_or_exit(
        slack_cfg,
        [
            ("bot_token", bot_token, "Slack bot token"),
            ("app_token", app_token, "Slack app token"),
        ],
    )
    bot_is_reference = reference_updates["bot_token"]
    app_is_reference = reference_updates["app_token"]
    next_cfg = dict(slack_cfg)
    next_cfg.pop("allowed_users", None)
    next_cfg.setdefault("dms", {})
    next_cfg.setdefault("channels", {})
    if bot_is_reference:
        next_cfg.pop("bot_token", None)
    else:
        next_cfg["bot_token"] = bot_token
    if app_is_reference:
        next_cfg.pop("app_token", None)
    else:
        next_cfg["app_token"] = app_token
    next_cfg.update(
        {
            "bot_user_id": bot_user_id,
            "notify_channel": notify,
        }
    )
    if reset_routes:
        workspace = _ensure_default_execution_config(config)
        next_cfg.pop("channel_defaults", None)
        next_cfg.update(
            {
                "account_id": team_id,
                "dms": {
                    owner_id: {
                        "workspace": workspace,
                    },
                },
                "channels": {},
            },
        )
    config.setdefault("transports", {})["slack"] = next_cfg


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _reject_legacy_setup_config(config: dict) -> None:
    """Stop setup before it partially rewrites a manual workspace migration."""
    if "working_dir" in config:
        console.print(
            "[red]working_dir is no longer supported. Move that directory into a"
            " named workspaces entry (normally default), bind Telegram to the"
            " workspace if configured, remove working_dir, and rerun setup.[/]"
        )
        raise typer.Exit(1)
    workspaces = config.get("workspaces")
    if not isinstance(workspaces, dict):
        return
    for name, workspace in workspaces.items():
        if isinstance(workspace, dict) and "path" in workspace:
            console.print(
                f"[red]workspaces.{escape(str(name))}.path is no longer supported. "
                "Move its content to the name-derived directory, remove the path key, "
                "and follow docs/migrations/v1.3-managed-workspaces.md before rerunning "
                "setup.[/]"
            )
            raise typer.Exit(1)


def _setup_default_workspace(config: dict) -> str:
    """Configure setup's canonical default workspace without touching disk."""
    console.rule("[bold]Step 2 \u00b7 Workspace")
    name = _ensure_default_execution_config(config)
    console.print(f"  Default workspace: [bold]{managed_workspace_path(name)}[/]\n")
    return name


def _print_scaffold_warnings(warnings: tuple[str, ...]) -> None:
    for warning in warnings:
        console.print(f"  [yellow]![/] {escape(warning)}")


def _scaffold_setup_or_exit(
    config: dict,
    *,
    seed_fresh: bool | None = None,
) -> None:
    """Seed a fresh install once, or conservatively repair existing structure."""
    from .scaffolding import ScaffoldError, ScaffoldService

    service = ScaffoldService()
    raw_workspaces = config.get("workspaces", {})
    if not isinstance(raw_workspaces, dict):
        console.print("[red]Could not scaffold workspaces:[/] workspaces must be an object")
        raise typer.Exit(1)
    try:
        state = setup_state(config)
        should_seed = state is SetupState.INCOMPLETE if seed_fresh is None else seed_fresh
        global_report = (
            service.seed_fresh_global()
            if should_seed
            else service.repair_global()
        )
        _print_scaffold_warnings(global_report.warnings)
        if should_seed:
            starter_report = service.seed_fresh_starter_docs()
            _print_scaffold_warnings(starter_report.warnings)

        for name in sorted(raw_workspaces):
            workspace_path = service.workspace_path(name)
            if should_seed:
                if os.path.lexists(workspace_path):
                    report = service.validate_workspace(name)
                    if report.errors:
                        raise ScaffoldError("; ".join(report.errors))
                else:
                    workspace_report = service.create_workspace(name)
                    _print_scaffold_warnings(workspace_report.warnings)
            else:
                workspace_report = service.repair_workspace(name)
                _print_scaffold_warnings(workspace_report.warnings)

        errors = [*service.validate_global().errors]
        for name in sorted(raw_workspaces):
            errors.extend(service.validate_workspace(name).errors)
        if errors:
            raise ScaffoldError("; ".join(errors))
    except (ConfigError, ScaffoldError, OSError) as exc:
        console.print(f"[red]Could not establish managed workspaces:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None


_INITIAL_SETUP_SNAPSHOT_SUBJECT = "Initialize Enso content"
_INITIAL_SETUP_GLOBAL_SNAPSHOT_PATHS = (
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    ".agents/skills",
    ".claude/skills",
    "docs",
    "skills",
)
_INITIAL_SETUP_GLOBAL_REQUIRED_PATHS = (
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    ".agents/skills",
    ".claude/skills",
    "skills/docs/SKILL.md",
    "skills/jobs/SKILL.md",
    "skills/slack/SKILL.md",
    "skills/tables/SKILL.md",
    "skills/workspace/SKILL.md",
    "docs/enso/content_model.md",
    "docs/enso/layout.md",
    "docs/operator.md",
)


def _initial_setup_snapshot_paths(
    config: dict,
    repository_root: str,
) -> tuple[str, ...]:
    """Return broad versionable scopes captured by the one fresh snapshot."""
    paths = list(_INITIAL_SETUP_GLOBAL_SNAPSHOT_PATHS)
    raw_workspaces = config.get("workspaces", {})
    if not isinstance(raw_workspaces, dict):
        raise ConfigError("workspaces must be an object")
    for name in sorted(raw_workspaces):
        base = f"workspaces/{name}"
        paths.extend(
            (
                f"{base}/AGENTS.md",
                f"{base}/CLAUDE.md",
                f"{base}/.agents/skills",
                f"{base}/.claude/skills",
                f"{base}/knowledge",
            )
        )
        if os.listdir(os.path.join(repository_root, "workspaces", name, "skills")):
            paths.append(f"{base}/skills")
    return tuple(paths)


def _initial_setup_required_paths(config: dict) -> tuple[str, ...]:
    """Return every exact baseline entry required in the initial commit tree."""
    paths = list(_INITIAL_SETUP_GLOBAL_REQUIRED_PATHS)
    raw_workspaces = config.get("workspaces", {})
    if not isinstance(raw_workspaces, dict):
        raise ConfigError("workspaces must be an object")
    for name in sorted(raw_workspaces):
        base = f"workspaces/{name}"
        paths.extend(
            (
                f"{base}/AGENTS.md",
                f"{base}/CLAUDE.md",
                f"{base}/.agents/skills",
                f"{base}/.claude/skills",
                f"{base}/knowledge/README.md",
            )
        )
    return tuple(paths)


def _missing_initial_setup_paths(
    repository_root: str,
    required_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """Return required exact paths absent from the fresh scaffold."""
    return tuple(
        path
        for path in required_paths
        if not os.path.lexists(os.path.join(repository_root, *path.split("/")))
    )


def _required_paths_absent_from(
    required_paths: tuple[str, ...],
    observed_paths: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Return required paths missing from one index or historical commit tree."""
    observed = frozenset(observed_paths or ())
    return tuple(path for path in required_paths if path not in observed)


def _initial_snapshot_problem(message: str, paths: tuple[str, ...] = ()) -> NoReturn:
    """Report an actionable initial-snapshot failure and stop setup."""
    suffix = f" {', '.join(escape(path) for path in paths)}" if paths else ""
    console.print(
        "[red]Could not create the initial Enso content snapshot:[/] "
        f"{escape(message)}{suffix}"
    )
    console.print(
        "[dim]Setup remains incomplete; repair the listed content-history problem and "
        "rerun `enso setup`.[/]"
    )
    raise typer.Exit(1)


def _save_setup_config_or_exit(config: dict, *, action: str) -> None:
    """Persist setup state with an actionable CLI diagnostic."""
    try:
        save_config(config)
    except (OSError, TypeError, ValueError) as exc:
        console.print(f"[red]{action}:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None


def _finalize_setup_or_exit(config: dict) -> None:
    """Persist, scaffold, snapshot, and complete one setup transaction."""
    from .repository import EnsoRepository, RepositoryError

    try:
        state = setup_state(config)
    except ConfigError as exc:
        console.print(f"[red]Could not finalize setup:[/] {escape(str(exc))}")
        raise typer.Exit(1) from None

    if state is not SetupState.INCOMPLETE:
        _scaffold_setup_or_exit(config)
        _save_setup_config_or_exit(
            config,
            action="Could not save the conservatively repaired configuration",
        )
        return

    # The on-disk null marker makes every later content mutation recoverable.
    config["setup"]["completed_at"] = None
    _save_setup_config_or_exit(
        config,
        action="Could not record incomplete setup before seeding content",
    )

    try:
        repository = EnsoRepository()
        repository.ensure()
        required_paths = _initial_setup_required_paths(config)
        marker_paths = repository.commit_subject_paths(_INITIAL_SETUP_SNAPSHOT_SUBJECT)

        if marker_paths is not None:
            missing_from_marker = _required_paths_absent_from(
                required_paths,
                marker_paths,
            )
            if missing_from_marker:
                _initial_snapshot_problem(
                    "the historical initial marker is missing required baseline paths:",
                    missing_from_marker,
                )
            _scaffold_setup_or_exit(config, seed_fresh=False)
        else:
            if repository.has_head():
                _initial_snapshot_problem(
                    "repository history exists but the exact initial marker is absent; "
                    "refusing to create a second baseline commit. Review the local Git "
                    "history and restore the initial marker before retrying."
                )

            _scaffold_setup_or_exit(config)
            missing_from_disk = _missing_initial_setup_paths(
                repository.root,
                required_paths,
            )
            if missing_from_disk:
                _initial_snapshot_problem(
                    "required fresh-setup paths are missing after scaffolding:",
                    missing_from_disk,
                )

            ignored = repository.ignored_paths(required_paths)
            if ignored:
                _initial_snapshot_problem(
                    "required fresh-setup paths are ignored; remove the matching "
                    ".gitignore rule before retrying:",
                    ignored,
                )

            repository.snapshot(
                _initial_setup_snapshot_paths(config, repository.root),
                _INITIAL_SETUP_SNAPSHOT_SUBJECT,
                recover_interrupted=True,
            )
            missing_from_index = _required_paths_absent_from(
                required_paths,
                repository.tracked_paths(),
            )
            if missing_from_index:
                _initial_snapshot_problem(
                    "the completed snapshot did not track required baseline paths:",
                    missing_from_index,
                )
            missing_from_marker = _required_paths_absent_from(
                required_paths,
                repository.commit_subject_paths(_INITIAL_SETUP_SNAPSHOT_SUBJECT),
            )
            if missing_from_marker:
                _initial_snapshot_problem(
                    "the initial marker commit is missing required baseline paths:",
                    missing_from_marker,
                )
    except typer.Exit:
        raise
    except (ConfigError, RepositoryError, OSError) as exc:
        _initial_snapshot_problem(str(exc))

    config["setup"]["completed_at"] = datetime.now(timezone.utc).isoformat()
    try:
        save_config(config)
    except (OSError, TypeError, ValueError) as exc:
        # The first save left null on disk. Restore the in-memory marker too,
        # and retry that null write in case a wrapper failed after persisting.
        config["setup"]["completed_at"] = None
        rollback_error: Exception | None = None
        try:
            save_config(config)
        except (OSError, TypeError, ValueError) as rollback_exc:
            rollback_error = rollback_exc
        console.print(f"[red]Could not mark setup complete:[/] {escape(str(exc))}")
        if rollback_error is not None:
            console.print(
                "[yellow]Could not re-confirm the incomplete setup marker:[/] "
                f"{escape(str(rollback_error))}"
            )
        console.print(
            "[dim]Rerun `enso setup`; existing seeded content and the clean initial "
            "snapshot will be reused.[/]"
        )
        raise typer.Exit(1) from None


@app.command()
def setup() -> None:
    """Interactive setup wizard."""
    console.print(Panel("Enso Setup", subtitle=f"v{__version__}", expand=False))
    # Strict preflight must happen before even the config lock is created.
    _load_config_or_exit(allow_missing=True)
    with _config_lock_or_exit():
        # Re-read under the lock so another Enso process cannot win a race
        # between validation and the setup read-modify-write transaction.
        config = _load_config_or_exit(allow_missing=True)
        _reject_legacy_setup_config(config)
        _ensure_repository_or_exit()

        _setup_providers(config)

        # Step 2: managed default workspace and shared execution catalog
        _setup_default_workspace(config)

        captured_chat_id = _setup_transport(config)
        with console.status("Saving config..."):
            _finalize_setup_or_exit(config)
    console.print(f"[green]\u2713[/] Config saved to {CONFIG_FILE}")

    # Send test message
    if config.get("transport") == "telegram":
        tg = config.get("transports", {}).get("telegram", {})
        try:
            token = resolve_config_secret(tg, "bot_token")
        except SecretResolutionError as exc:
            console.print(f"[yellow]Skipping test message: {exc}[/]")
            token = ""
        chat_id = captured_chat_id or tg.get("notify_channel")
        if token and chat_id:
            with console.status("Sending test message..."):
                sent = _tg_send_message(
                    token,
                    chat_id,
                    f"Enso v{__version__} ready.",
                )
            if sent:
                console.print("[green]\u2713[/] Test message sent!")
            else:
                console.print("[yellow]Failed to send test message.[/]")
    elif config.get("transport") == "slack":
        slack_cfg = config.get("transports", {}).get("slack", {})
        try:
            token = resolve_config_secret(slack_cfg, "bot_token")
        except SecretResolutionError as exc:
            console.print(f"[yellow]Skipping test message: {exc}[/]")
            token = ""
        target = slack_cfg.get("notify_channel", "")
        if not target:
            console.print("[yellow]Skipping test message \u2014 no notify_channel set.[/]")
        elif token:
            with console.status("Sending test message..."):
                sent = _slack_send_message(
                    token,
                    target,
                    f"Enso v{__version__} ready.",
                )
            if sent:
                console.print(f"[green]\u2713[/] Test message sent to {target}.")
            else:
                console.print(
                    f"[yellow]Failed to send test message to {target}."
                    " If the bot isn't a member of that channel yet,"
                    " invite it and try `enso message send 'hi'`.[/]"
                )

    # Step 4: Background service
    console.rule("[bold]Step 4 \u00b7 Background Service (optional)")
    installed = False
    if _service_platform():
        if _service_is_installed():
            console.print("  Service already installed.")
            if Confirm.ask("  Reinstall?", default=False):
                _service_install()
                installed = True
            else:
                installed = True
        elif Confirm.ask("  Install background service?", default=True):
            _service_install()
            installed = True
    else:
        console.print(f"[yellow]  Auto service not supported on {sys.platform}.[/]")

    # Summary
    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column("key", style="bold")
    summary.add_column("value")
    summary.add_row("Config", str(CONFIG_FILE))
    if installed:
        summary.add_row("Status", "enso service status")
        summary.add_row("Restart", "enso service restart")
        summary.add_row("Logs", "tail -f ~/.enso/enso.log")
    else:
        summary.add_row("Run", "enso serve")
    console.print(
        Panel(
            summary,
            title="Setup Complete",
            border_style="green",
            expand=False,
        )
    )


def _load_transport(name: str, runtime) -> BaseTransport:
    """Lazily import and instantiate a transport by name."""
    if name == "telegram":
        from .transports.telegram import TelegramTransport

        return TelegramTransport(runtime)
    if name == "slack":
        from .transports.slack import SlackTransport

        return SlackTransport(runtime)
    console.print(f"[red]Unknown transport: {name}[/]")
    raise typer.Exit(1)


SECRETS_DIR = os.path.expanduser("~/.enso/secrets")


def _read_secret_env() -> dict[str, str]:
    """Parse ~/.enso/secrets/*.env without touching os.environ.

    The first occurrence of a key wins, matching how _load_secret_env has
    always applied the files.
    """
    values: dict[str, str] = {}
    if not os.path.isdir(SECRETS_DIR):
        return values
    for name in sorted(os.listdir(SECRETS_DIR)):
        if not name.endswith(".env"):
            continue
        path = os.path.join(SECRETS_DIR, name)
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            log.warning("Could not read secret env file: %s", path)
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.removeprefix("export ").strip()
            key, sep, val = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            val = val.strip().strip("'\"")
            if key:
                values.setdefault(key, val)
    return values


def _load_secret_env() -> list[str]:
    """Load ~/.enso/secrets/*.env into os.environ.

    launchd gives the daemon a minimal environment, and jobs (both prerun
    scripts and the provider process) inherit from it. Secrets that CLIs read
    from the environment — GOG_KEYRING_PASSWORD, OP_SERVICE_ACCOUNT_TOKEN —
    have to be injected here or unattended runs fail. Existing values win, so
    an explicit export still overrides the file.
    """
    loaded: list[str] = []
    for key, val in _read_secret_env().items():
        if key in os.environ:
            continue
        os.environ[key] = val
        loaded.append(key)
    return loaded


@app.command()
def serve(
    transport: Annotated[
        str | None, typer.Option("--transport", help="Override transport (telegram, slack)")
    ] = None,
) -> None:
    """Start the bot and job scheduler."""
    from .core import Runtime

    config = _load_config_or_exit()
    _validate_installation_or_exit(config)
    logging_state = configure_logging(config, force=True)
    log.debug("Logging configured: %s", logging_state)
    secret_keys = _load_secret_env()
    if secret_keys:
        log.info("Loaded secret env keys: %s", ", ".join(secret_keys))

    transport_name = transport or config.get("transport", "")
    if not transport_name:
        console.print("[red]No transport configured. Run 'enso setup' first.[/]")
        raise typer.Exit(1)

    runtime = Runtime(config)
    runtime.load_state()

    log.info("Starting Enso v%s", __version__)
    log.info("  transport=%s", transport_name)

    try:
        tp = _load_transport(transport_name, runtime)
    except SecretResolutionError as exc:
        console.print(f"[red]✗[/] Could not load transport credentials: {exc}")
        raise typer.Exit(1) from None
    runtime.transport = tp
    tp.start()


@app.command()
def web(
    host: Annotated[
        str | None, typer.Option("--host", help="Bind host (default from web.host)")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Bind port (default from web.port)")
    ] = None,
) -> None:
    """Serve the Enso web dashboard (jobs and run history)."""
    from .core import Runtime

    config = _load_config_or_exit()
    _validate_installation_or_exit(config)
    web_cfg = config.get("web", {})
    if not isinstance(web_cfg, dict):
        web_cfg = {}
    bind_host = host or web_cfg.get("host", "127.0.0.1")
    bind_port = int(port if port is not None else web_cfg.get("port", 1337))
    # Keep request-host validation aligned with a one-off CLI bind override.
    config["web"] = {**web_cfg, "host": bind_host, "port": bind_port}

    runtime = Runtime(config)
    # Read-only snapshot: the dashboard must never write back pruned state
    # over the serve process's live state.json.
    runtime.load_state(persist=False)

    # Lazy import so missing optional web deps never break other commands.
    try:
        import uvicorn

        from .web.app import create_app
    except Exception as exc:
        console.print(f"[red]✗[/] Web dependencies missing: {exc}")
        console.print("Install them with: [bold]pip install 'enso[web]'[/]")
        raise typer.Exit(1) from exc
    try:
        app_ = create_app(runtime)
    except SecretResolutionError as exc:
        console.print(f"[red]✗[/] Could not load web credentials: {exc}")
        raise typer.Exit(1) from None

    url = f"http://{bind_host}:{bind_port}"
    console.print(f"[green]✓[/] Enso web serving at [bold]{url}[/]")
    uvicorn.run(app_, host=bind_host, port=bind_port)


# ---------------------------------------------------------------------------
# Job subcommands
# ---------------------------------------------------------------------------


@job_app.command("list")
def job_list() -> None:
    """List all configured jobs."""
    jobs = load_jobs()
    if not jobs:
        console.print("No jobs found. Create one with: enso job create")
        return
    table = Table(box=None, padding=(0, 2))
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Workspace")
    table.add_column("Enabled")
    for job in jobs:
        enabled = "[green]\u2713[/]" if job.enabled else "[red]\u2717[/]"
        table.add_row(
            job.dir_name,
            job.schedule,
            job.provider,
            job.model,
            job.workspace,
            enabled,
        )
    console.print(table)


@job_app.command("create")
def job_create(
    name: Annotated[str, typer.Option("--name", help="Display name for the job")],
    provider: Annotated[str, typer.Option("--provider", help=" or ".join(PROVIDER_NAMES))],
    model: Annotated[
        str, typer.Option("--model", help="Model name (e.g. sonnet, sol, terra, luna)")
    ],
    schedule: Annotated[str, typer.Option("--schedule", help="Cron expression (e.g. '0 9 * * *')")],
    workspace: Annotated[
        str, typer.Option("--workspace", help="Named workspace where the provider runs")
    ],
) -> None:
    """Create a new background job. Edit the JOB.md to add the prompt and optional prerun."""
    dir_name = re.sub(r"[^\w]+", "-", name.casefold()).strip("-_")
    if not dir_name:
        console.print("[red]Job name must contain at least one letter or number.[/]")
        raise typer.Exit(1)
    try:
        job = create_job(
            dir_name,
            name,
            provider,
            model,
            schedule,
            workspace=workspace,
        )
    except (FileExistsError, ValueError) as exc:
        console.print(f"[red]Could not create job:[/] {exc}")
        raise typer.Exit(1) from None
    console.print(f"[green]\u2713[/] Job created: {job.path}")
    console.print("  Edit the JOB.md to add your prompt and optional prerun script.")


@job_app.command("run")
def job_run(
    name: Annotated[str, typer.Argument(help="Job directory name")],
) -> None:
    """Manually run a job (output goes to stdout, no notifications)."""
    import asyncio

    from .core import Runtime

    config = _load_config_or_exit()
    runtime = Runtime(config)
    try:
        result = asyncio.run(runtime.jobs.run_now(name))
    except ValueError:
        console.print(f"[red]Job '{name}' not found.[/]")
        raise typer.Exit(1) from None

    if result.status == "no_work":
        console.print("[yellow]No work (prerun exit 1); provider was not run.[/]")
        return
    if result.status in {"prerun_error", "prerun_timeout"}:
        console.print(result.output, style="red", markup=False)
        raise typer.Exit(1)

    if result.output:
        console.print(result.output, markup=False)
    if result.status in {"error", "timeout"}:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Doc subcommands
# ---------------------------------------------------------------------------


@doc_app.command("list")
def doc_list() -> None:
    """List reference docs under ~/.enso/docs/."""
    listing = load_docs()
    if not listing.docs:
        console.print("No docs found. Create one with: enso doc create <path>.md")
        return
    table = Table(box=None, padding=(0, 2))
    table.add_column("Path")
    table.add_column("Name")
    table.add_column("Description")
    for doc in listing.docs:
        # Frontmatter is operator text: render it literally so square brackets
        # cannot be read as Rich markup and drop the whole listing.
        table.add_row(
            Text(doc.rel_path),
            Text(doc.name),
            Text(doc.description) if doc.has_frontmatter else "[yellow]needs frontmatter[/]",
        )
    console.print(table)
    if listing.truncated:
        console.print(f"[yellow]Listing truncated at {MAX_DOCS} docs; some docs are not shown.[/]")


@doc_app.command("create")
def doc_create(
    path: Annotated[
        str, typer.Argument(help="Relative path under ~/.enso/docs (e.g. stuff/sub_stuff.md)")
    ],
    name: Annotated[
        str, typer.Option("--name", help="Display name (defaults to the filename)")
    ] = "",
) -> None:
    """Create a reference doc with scaffolded frontmatter."""
    try:
        doc = create_doc(path, name)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Could not create doc:[/] {exc}")
        raise typer.Exit(1) from None
    console.print(f"[green]✓[/] Doc created: {doc.path}")
    console.print("  Fill in the description and body.")


# ---------------------------------------------------------------------------
# Table subcommands
# ---------------------------------------------------------------------------


@table_app.command("list")
def table_list() -> None:
    """List registered user data tables in ~/.enso/enso.db."""
    try:
        listing = tables.list_tables()
    except (OSError, sqlite3.Error) as exc:
        console.print("[red]Could not list tables:[/] ", end="")
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from None
    if not listing.tables:
        console.print(
            "No registered tables found. Register one with: "
            "enso table register <table> --description <description>"
        )
        return

    output = Table(box=None, padding=(0, 2))
    output.add_column("Table")
    output.add_column("Name")
    output.add_column("Columns", justify="right")
    output.add_column("Description")
    for item in listing.tables:
        output.add_row(
            Text(item.table_name),
            Text(item.name),
            str(item.column_count) if item.available else "[yellow]missing[/]",
            Text(item.description),
        )
    console.print(output)
    if listing.truncated:
        console.print(
            f"[yellow]Listing truncated at {tables.MAX_TABLES} tables; "
            "some tables are not shown.[/]"
        )


@table_app.command("register")
def table_register(
    table_name: Annotated[str, typer.Argument(help="Existing SQLite table name")],
    description: Annotated[
        str, typer.Option("--description", "-d", help="What the table contains and when to use it")
    ],
    name: Annotated[
        str, typer.Option("--name", help="Display name (defaults to the table name)")
    ] = "",
) -> None:
    """Register an existing SQLite table for Enso discovery."""
    try:
        item = tables.register_table(table_name, name=name, description=description)
    except (OSError, sqlite3.Error, ValueError, tables.TableNotFoundError) as exc:
        console.print("[red]Could not register table:[/] ", end="")
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from None
    console.print(f"[green]✓[/] Table registered: {item.table_name}")


@table_app.command("schema")
def table_schema(
    table_name: Annotated[str, typer.Argument(help="Registered SQLite table name")],
) -> None:
    """Show the columns, indexes, and CREATE SQL for a registered table."""
    try:
        item = tables.get_table(table_name)
    except tables.TableNotFoundError:
        console.print(f"[red]Table not found:[/] {table_name}")
        raise typer.Exit(1) from None
    except (OSError, sqlite3.Error, ValueError) as exc:
        console.print("[red]Could not read table schema:[/] ", end="")
        console.print(str(exc), markup=False)
        raise typer.Exit(1) from None

    console.print(Text(f"{item.name} ({item.table_name})"), style="bold")
    console.print(Text(item.description))
    output = Table(box=None, padding=(0, 2))
    output.add_column("Column")
    output.add_column("Type")
    output.add_column("Constraints")
    for column in item.columns:
        constraints: list[str] = []
        if column.primary_key:
            constraints.append(
                "PRIMARY KEY" if column.primary_key == 1 else f"PRIMARY KEY {column.primary_key}"
            )
        if column.not_null:
            constraints.append("NOT NULL")
        if column.default_value is not None:
            constraints.append(f"DEFAULT {column.default_value}")
        output.add_row(
            Text(column.name),
            Text(column.declared_type or "untyped"),
            Text(", ".join(constraints)),
        )
    console.print(output)
    console.print("\nCREATE SQL", style="bold")
    console.print(Text(item.sql))
    if item.indexes:
        console.print("\nIndexes", style="bold")
        for index in item.indexes:
            console.print(Text(index.sql))


# ---------------------------------------------------------------------------
# Message subcommands
# ---------------------------------------------------------------------------


@message_app.command("send")
def message_send(
    text: Annotated[str, typer.Argument(help="Message text to send")],
    to: Annotated[
        str,
        typer.Option(
            "--to",
            "-t",
            help=(
                "Destination. Slack: channel/DM/user ID (required if no"
                " notify_channel). Telegram: chat ID; omit to use the current"
                " chat or notify_channel."
            ),
        ),
    ] = "",
) -> None:
    """Send a text-only message via the configured transport."""
    cfg = _load_config_or_exit()
    transport, token, targets, thread_ts = _resolve_send_targets(cfg, to)

    if transport == "slack":
        if not _slack_send_message(token, targets[0], text[:40000], thread_ts):
            console.print(f"[red]\u2717[/] Failed to send to {targets[0]}.")
            raise typer.Exit(1)
    else:
        for uid in targets:
            if not _tg_send_message(token, uid, text[:4096]):
                console.print(f"[red]\u2717[/] Failed to send to user {uid}.")
                raise typer.Exit(1)

    msg_send(text, source="notify")
    console.print("[green]\u2713[/] Message sent.")


@message_app.command("list")
def message_list() -> None:
    """Show pending background messages."""
    msgs = msg_pending()
    if not msgs:
        console.print("No pending messages.")
        return
    for msg in msgs:
        ts = msg.get("timestamp", "?")
        source = msg.get("source", "?")
        text = msg.get("text", "")
        console.print(f"[dim]{ts}[/] [bold]({source})[/]")
        console.print(f"  {text[:200]}{'...' if len(text) > 200 else ''}\n")


@message_app.command("attach")
def message_attach(
    file: Annotated[str, typer.Argument(help="Path to file to send")],
    caption: Annotated[str, typer.Argument(help="Optional caption")] = "",
    to: Annotated[
        str,
        typer.Option(
            "--to",
            "-t",
            help=(
                "Destination. Slack: channel/DM/user ID (required if no"
                " notify_channel). Telegram: chat ID; omit to use the current"
                " chat or notify_channel."
            ),
        ),
    ] = "",
) -> None:
    """Send a file via the configured transport."""
    if not os.path.isfile(file):
        console.print(f"[red]\u2717[/] File not found: {file}")
        raise typer.Exit(1)
    cfg = _load_config_or_exit()
    filename = os.path.basename(file)
    transport, token, targets, thread_ts = _resolve_send_targets(cfg, to)

    if transport == "slack":
        target = targets[0]
        with console.status(f"Uploading {filename} to {target}..."):
            ok, err = _slack_upload_file(token, target, file, caption, thread_ts)
        if not ok:
            console.print(f"[red]\u2717[/] {err}")
            raise typer.Exit(1)
        note = f"Sent attachment: {filename} \u2192 {target}"
        if caption:
            note += f" \u2014 {caption}"
        msg_send(note, source="attach")
        console.print("[green]\u2713[/] File sent.")
        return

    failures = [uid for uid in targets if not _tg_send_file(token, uid, file, caption)]
    if failures:
        # File delivery failed (after retries). Fall back to a lightweight
        # text alert on the same channel \u2014 a small sendMessage usually gets
        # through even when a large upload times out \u2014 so a dropped
        # attachment is never silent. See ~/.enso/enso.log for the cause.
        alert = f"\u26a0\ufe0f Couldn't deliver attachment: {filename}"
        if caption:
            alert += f" ({caption})"
        alert += ". The error is in ~/.enso/enso.log."
        for uid in failures:
            _tg_send_message(token, uid, alert)
        log.error("attach delivery failed file=%s users=%s", filename, failures)
        console.print(
            "[red]\u2717[/] Failed to send to user(s): "
            + ", ".join(str(u) for u in failures)
            + " (text alert sent)."
        )
        raise typer.Exit(1)
    note = f"Sent attachment: {filename}"
    if caption:
        note += f" \u2014 {caption}"
    msg_send(note, source="attach")
    console.print("[green]\u2713[/] File sent.")


@message_app.command("clear")
def message_clear() -> None:
    """Clear all pending background messages."""
    msg_clear()
    console.print("[green]\u2713[/] Messages cleared.")


# ---------------------------------------------------------------------------
# Service subcommands
# ---------------------------------------------------------------------------


@service_app.command("status")
def service_status() -> None:
    """Show whether the background service is installed and running."""
    if not _service_platform():
        console.print(f"[yellow]Not supported on {sys.platform}.[/]")
        return
    installed = _service_is_installed()
    running = _service_is_running() if installed else False
    console.print(f"Installed: {'yes' if installed else 'no'}")
    console.print(f"Running:   {'yes' if running else 'no'}")


@service_app.command("install")
def service_install_cmd() -> None:
    """Install the background service (launchd on macOS, systemd on Linux)."""
    if _service_install():
        return
    raise typer.Exit(1)


@service_app.command("uninstall")
def service_uninstall_cmd() -> None:
    """Stop and remove the background service."""
    if _service_uninstall():
        console.print("[green]\u2713[/] Service uninstalled.")
    else:
        console.print("[yellow]No service found to uninstall.[/]")


@service_app.command("start")
def service_start_cmd() -> None:
    """Start the background service."""
    if not _service_is_installed():
        console.print("[red]Service not installed. Run: enso service install[/]")
        raise typer.Exit(1)
    if _service_start():
        console.print("[green]\u2713[/] Service started.")
    else:
        console.print("[red]Failed to start service.[/]")


@service_app.command("stop")
def service_stop_cmd() -> None:
    """Stop the background service."""
    if _service_stop():
        console.print("[green]\u2713[/] Service stopped.")
    else:
        console.print("[yellow]Service not running or not found.[/]")


@service_app.command("restart")
def service_restart_cmd() -> None:
    """Restart the background service."""
    if not _service_is_installed():
        console.print("[red]Service not installed. Run: enso service install[/]")
        raise typer.Exit(1)
    if _service_restart():
        console.print("[green]\u2713[/] Service restarted.")
    else:
        console.print("[red]Failed to restart service.[/]")


@service_app.command("logs")
def service_logs_cmd(
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow log output")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Number of lines")] = 25,
) -> None:
    """Show service logs."""
    log_path = os.path.expanduser("~/.enso/enso.log")
    if not os.path.exists(log_path):
        console.print("No log file found.")
        return
    if follow:
        os.execlp("tail", "tail", "-f", "-n", str(lines), log_path)
    else:
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode(errors="replace")
            for line in tail.splitlines()[-lines:]:
                console.print(line)
        except Exception as exc:
            console.print(f"[red]Error reading logs: {exc}[/]")


# ---------------------------------------------------------------------------
# Slack subcommands (directory cache + message search)
# ---------------------------------------------------------------------------


def _slack_token_or_exit() -> str:
    """Load the Slack bot token or exit with a clear error."""
    cfg = _load_config_or_exit()
    slack_cfg = cfg.get("transports", {}).get("slack", {})
    token = _resolve_transport_secret_or_exit(slack_cfg, "bot_token", "Slack")
    if not token:
        console.print("[red]\u2717[/] Slack not configured. Run [bold]enso setup[/].")
        raise typer.Exit(1)
    return token


def _fmt_user(u: dict) -> str:
    parts = [u["id"]]
    real = u.get("real_name") or u.get("display_name") or u.get("name") or "?"
    parts.append(real)
    if u.get("name") and u["name"] != real:
        parts.append(f"(@{u['name']})")
    if u.get("email"):
        parts.append(u["email"])
    tags = []
    if u.get("is_bot"):
        tags.append("bot")
    if u.get("deleted"):
        tags.append("deleted")
    if tags:
        parts.append(f"[{','.join(tags)}]")
    return "  ".join(parts)


def _fmt_channel(c: dict) -> str:
    parts = [c["id"], f"#{c['name']}"]
    if c.get("is_private"):
        parts.append("[private]")
    if not c.get("is_member"):
        parts.append("[not-a-member]")
    if c.get("num_members"):
        parts.append(f"({c['num_members']} members)")
    if c.get("topic"):
        parts.append(f"\u2014 {c['topic']}")
    return "  ".join(parts)


@slack_app.command("lookup-user")
def slack_lookup_user(
    query: Annotated[str, typer.Argument(help="Name, display name, email, or U-ID")],
) -> None:
    """Find a Slack user. Searches the cache; refreshes on miss."""
    token = _slack_token_or_exit()
    matches = slack_cache.lookup_user(query, token=token)
    if not matches:
        console.print(f"No user found matching '{query}'.")
        raise typer.Exit(1)
    for u in matches:
        console.print(_fmt_user(u))


@slack_app.command("lookup-channel")
def slack_lookup_channel(
    query: Annotated[str, typer.Argument(help="Channel name (with or without #) or C-ID")],
) -> None:
    """Find a Slack channel. Searches the cache; refreshes on miss."""
    token = _slack_token_or_exit()
    matches = slack_cache.lookup_channel(query, token=token)
    if not matches:
        console.print(f"No channel found matching '{query}'.")
        raise typer.Exit(1)
    for c in matches:
        console.print(_fmt_channel(c))


@slack_app.command("whois")
def slack_whois(
    user_id: Annotated[str, typer.Argument(help="Slack user ID (e.g. U0123456789)")],
) -> None:
    """Resolve a U-ID to a user record. Calls users.info on cache miss."""
    token = _slack_token_or_exit()
    entry = slack_cache.whois(user_id, token=token)
    if not entry:
        console.print(f"No user with ID {user_id}.")
        raise typer.Exit(1)
    console.print(_fmt_user(entry))


@slack_app.command("open-dm")
def slack_open_dm(
    user: Annotated[str, typer.Argument(help="User ID (U…) or a name to look up")],
) -> None:
    """Open a DM channel with a user and print the resulting channel ID."""
    token = _slack_token_or_exit()
    user_id = user
    if not user.startswith(("U", "W")):
        matches = slack_cache.lookup_user(user, token=token)
        if not matches:
            console.print(f"No user found matching '{user}'.")
            raise typer.Exit(1)
        if len(matches) > 1:
            console.print(
                f"Ambiguous '{user}' \u2014 {len(matches)} matches. Use --user with a specific ID:"
            )
            for u in matches:
                console.print(_fmt_user(u))
            raise typer.Exit(1)
        user_id = matches[0]["id"]
    try:
        channel_id = slack_cache.open_dm(user_id, token)
    except Exception as exc:
        console.print(f"[red]\u2717[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(channel_id)


@slack_app.command("list")
def slack_list(
    what: Annotated[str, typer.Argument(help="users | channels")] = "users",
) -> None:
    """Dump the cached directory. Auto-refreshes if the cache is empty."""
    token = _slack_token_or_exit()
    cache = slack_cache.load()
    if what == "users":
        if not cache["users"]["items"]:
            cache = slack_cache.refresh_users(token, cache)
        for u in sorted(
            cache["users"]["items"].values(),
            key=lambda x: (x.get("real_name") or x.get("name") or "").lower(),
        ):
            console.print(_fmt_user(u))
    elif what == "channels":
        if not cache["channels"]["items"]:
            cache = slack_cache.refresh_channels(token, cache)
        for c in sorted(
            cache["channels"]["items"].values(),
            key=lambda x: x.get("name", "").lower(),
        ):
            console.print(_fmt_channel(c))
    else:
        console.print(f"[red]\u2717[/] Unknown target '{what}'. Use 'users' or 'channels'.")
        raise typer.Exit(1)


@slack_app.command("refresh")
def slack_refresh(
    users: Annotated[bool, typer.Option("--users", help="Refresh users only")] = False,
    channels: Annotated[bool, typer.Option("--channels", help="Refresh channels only")] = False,
) -> None:
    """Force-refresh the Slack directory cache. Default is both."""
    token = _slack_token_or_exit()
    do_users = users or not channels
    do_channels = channels or not users
    if do_users:
        slack_cache.refresh_users(token)
        console.print("[green]\u2713[/] Users refreshed.")
    if do_channels:
        slack_cache.refresh_channels(token)
        console.print("[green]\u2713[/] Channels refreshed.")


@slack_app.command("search")
def slack_search(
    query: Annotated[str, typer.Argument(help="Search query (Slack search syntax allowed)")],
    count: Annotated[int, typer.Option("--count", "-n", help="Max results")] = 10,
) -> None:
    """Search Slack messages across accessible channels."""
    token = _slack_token_or_exit()
    data = slack_cache.api_post(
        token,
        "search.messages",
        {
            "query": query,
            "count": count,
            "sort": "timestamp",
            "sort_dir": "desc",
        },
    )
    if not data.get("ok"):
        console.print(f"[red]\u2717[/] search.messages: {data.get('error', '?')}")
        raise typer.Exit(1)
    matches = data.get("messages", {}).get("matches", [])
    if not matches:
        console.print("No results.")
        return
    for msg in matches:
        channel = msg.get("channel", {}).get("name", "?")
        user = msg.get("username", msg.get("user", "?"))
        ts = msg.get("ts", "?")
        text = msg.get("text", "")[:200]
        console.print(f"#{channel}  {user}  {ts}")
        console.print(f"  {text}")
        permalink = msg.get("permalink", "")
        if permalink:
            console.print(f"  {permalink}")
        console.print()


_SINCE_RE = re.compile(r"^(\d+)([smhd])$")
_SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_since(value: str) -> float:
    """Turn a ``30m``/``24h``/``7d`` window into an absolute epoch floor."""
    match = _SINCE_RE.match(value.strip().lower())
    if not match:
        console.print(
            f"[red]\u2717[/] Could not read --since {escape(repr(value))}."
            " Use a count and a unit, e.g. 30m, 24h, 7d."
        )
        raise typer.Exit(1)
    return time.time() - int(match.group(1)) * _SINCE_UNITS[match.group(2)]


def _message_author(msg: dict, cache: dict) -> str:
    """Name the author of a fetched message, falling back to raw IDs."""
    user_id = msg.get("user", "")
    user = cache.get("users", {}).get("items", {}).get(user_id, {})
    name = user.get("display_name") or user.get("real_name") or user.get("name") or ""
    if not name:
        name = (msg.get("bot_profile") or {}).get("name", "")
    if name and user_id:
        return f"{name} ({user_id})"
    return name or user_id or "unknown"


def _print_messages(messages: list[dict], *, show_all: bool = False) -> None:
    """Render fetched Slack messages for an agent (or a human) to read.

    Matches what the transport used to inject into prompts: resolved names,
    inert mention text, forwarded bodies, and no channel lifecycle noise.
    Timestamps print in both forms because the readable one is what gets
    reasoned about and the raw one is what ``enso slack thread`` takes.
    """
    cache = slack_cache.load()

    def _name(user_id: str) -> str:
        entry = cache.get("users", {}).get("items", {}).get(user_id, {})
        return entry.get("display_name") or entry.get("real_name") or entry.get("name") or ""

    for msg in messages:
        if not show_all and msg.get("subtype") in IGNORED_SUBTYPES:
            continue
        body = _flatten_mention_text(
            _message_context_text(msg),
            bot_user_id="",
            bot_label="",
            lookup=_name,
            strip_addressing=False,
        )
        if not body:
            continue
        ts = msg.get("ts", "?")
        try:
            when = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            when = "?"
        header = f"{when}  {_message_author(msg, cache)}  ts={ts}"
        replies = msg.get("reply_count")
        if replies:
            header += f"  [{replies} {'reply' if replies == 1 else 'replies'}]"
        # markup=False: message bodies carry bracketed text of their own.
        console.print(header, markup=False)
        console.print(f"  {body}", markup=False)
        console.print()


@slack_app.command("history")
def slack_history(
    channel: Annotated[str, typer.Argument(help="Channel ID (C\u2026, G\u2026, D\u2026)")],
    count: Annotated[int, typer.Option("--count", "-n", help="Max messages")] = 10,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only messages newer than e.g. 30m, 24h, 7d"),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Include joins, pins and other lifecycle noise"),
    ] = False,
) -> None:
    """Fetch recent top-level messages from a channel.

    Thread replies are not included \u2014 Slack keeps them out of channel
    history. Read one with ``enso slack thread`` and the parent's ts.
    """
    token = _slack_token_or_exit()
    params = {"channel": channel, "limit": str(count)}
    if since is not None:
        params["oldest"] = f"{_parse_since(since):.6f}"
    data = slack_cache.api_get(token, "conversations.history", params)
    if not data.get("ok"):
        console.print(f"[red]\u2717[/] conversations.history: {data.get('error', '?')}")
        raise typer.Exit(1)
    _print_messages(list(reversed(data.get("messages", []))), show_all=show_all)


@slack_app.command("thread")
def slack_thread(
    channel: Annotated[str, typer.Argument(help="Channel ID")],
    thread_ts: Annotated[str, typer.Argument(help="Thread timestamp (parent ts)")],
    count: Annotated[
        int,
        typer.Option("--count", "-n", help="Keep the root plus this many recent messages"),
    ] = 100,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Include joins, pins and other lifecycle noise"),
    ] = False,
) -> None:
    """Fetch every message in a thread, oldest first.

    A long thread can be more text than a caller wants at once, so ``-n``
    keeps the most recent messages. The root always survives the trim \u2014 it
    is what the thread is about \u2014 and anything dropped is reported rather
    than silently cut, so a truncated read is never mistaken for the whole
    thread.
    """
    token = _slack_token_or_exit()
    data = slack_cache.api_get(
        token,
        "conversations.replies",
        {"channel": channel, "ts": thread_ts, "limit": "100"},
    )
    if not data.get("ok"):
        console.print(f"[red]\u2717[/] conversations.replies: {data.get('error', '?')}")
        raise typer.Exit(1)
    messages = data.get("messages", [])
    hidden = 0
    if count > 0 and len(messages) > count:
        hidden = len(messages) - count
        messages = messages[:1] + messages[-(count - 1) :] if count > 1 else messages[:1]
    _print_messages(messages, show_all=show_all)
    if hidden:
        noun = "reply" if hidden == 1 else "replies"
        console.print(f"[dim]\u2026 {hidden} earlier {noun} not shown (raise -n to see them)[/]")


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# -- Routed configuration / route / audit --


# A linear diagnostic report: each branch prints one more finding, and the
# order of the printed sections is the feature.
@config_app.command("check")
def config_check() -> None:  # noqa: C901
    """Validate execution bindings and native-policy launch plumbing."""
    from .instructions import InstructionError, validate_shared_instructions
    from .policy import check_provider, verify_grok_rules
    from .teams import load_catalog, load_teams, load_telegram

    if not os.path.lexists(config_module.CONFIG_FILE):
        console.print(
            f"[red]✗[/] {config_module.CONFIG_FILE} is missing; run `enso setup` first"
        )
        raise typer.Exit(1)
    config = _load_config_or_exit()
    catalog = load_catalog(config)

    failed = False
    reported = set(catalog.errors)
    for error in catalog.errors:
        failed = True
        console.print(f"[red]✗[/] {error}")
    for problems in catalog.workspace_errors.values():
        reported.update(problems)
    for error in _installation_errors(config):
        if error in reported:
            continue
        failed = True
        console.print(f"[red]✗[/] {escape(error)}")

    from .repository import EnsoRepository, RepositoryError

    try:
        protected = EnsoRepository().tracked_protected_paths()
    except RepositoryError:
        protected = ()
    if protected:
        failed = True
        console.print(
            "[red]✗[/] protected paths are already tracked and block snapshots: "
            + ", ".join(escape(path) for path in protected)
        )

    try:
        shared_instructions = validate_shared_instructions()
    except InstructionError as exc:
        failed = True
        console.print(f"[red]✗[/] {escape(str(exc))}")
    else:
        console.print(
            "[green]✓[/] Shared instructions — "
            f"{escape(shared_instructions.source_path)} "
            f"({shared_instructions.revision[:12]})"
        )

    for name, workspace in sorted(catalog.workspaces.items()):
        console.print(f"\n[bold]Workspace {name}[/] — {workspace.path}")
        for problem in catalog.workspace_errors.get(name, ()):
            failed = True
            console.print(f"  [red]✗[/] {problem}")
        expanded = os.path.expanduser(workspace.path)
        if not os.path.isdir(expanded):
            failed = True
            console.print("  [red]✗[/] workspace path does not exist")

    secret_env = _read_secret_env()
    for name, execution_policy in sorted(catalog.policies.items()):
        mode = "unrestricted" if execution_policy.unrestricted else "policy-controlled"
        console.print(f"\n[bold]Policy {name}[/] ({mode})")
        for problem in catalog.policy_errors.get(name, ()):
            failed = True
            console.print(f"  [red]✗[/] {escape(problem)}")
        if not execution_policy.unrestricted and execution_policy.env_passthrough:
            console.print("  env_passthrough:")
            for env_name in execution_policy.env_passthrough:
                if env_name in os.environ or env_name in secret_env:
                    console.print(f"    [green]✓[/] {escape(env_name)}")
                else:
                    console.print(f"    [yellow]![/] {escape(env_name)} not set")
            console.print(
                "  [dim]checked against this shell and ~/.enso/secrets/*.env; "
                "the service environment may differ[/]"
            )

    bindings: dict[str, set[str]] = {}
    transports_cfg = config.get("transports", {})
    has_slack_config = isinstance(transports_cfg, dict) and "slack" in transports_cfg
    if config.get("transport") == "slack" or has_slack_config:
        teams = load_teams(config)
        for error in teams.errors:
            if error not in catalog.errors:
                failed = True
                console.print(f"[red]✗[/] {error}")
        if teams.errors:
            console.print("[red]Slack dispatch is disabled until this is fixed.[/]")
        routes = (*teams.dm_routes.values(), *teams.channel_routes.values())
        for route in sorted(routes, key=lambda item: item.route_id):
            if teams.route_usable(route):
                execution_policy = teams.catalog.policy_for(route.workspace)
                bindings.setdefault(route.workspace, set()).update(execution_policy.providers)
        for route_id, problems in sorted(teams.route_errors.items()):
            failed = True
            for problem in problems:
                console.print(f"[red]✗[/] {route_id}: {problem}")

    has_telegram_config = isinstance(transports_cfg, dict) and "telegram" in transports_cfg
    if config.get("transport") == "telegram" or has_telegram_config:
        telegram = load_telegram(config)
        for error in telegram.errors:
            if error not in catalog.errors:
                failed = True
                console.print(f"[red]✗[/] {error}")
        if telegram.errors:
            console.print("[red]Telegram dispatch is disabled until this is fixed.[/]")
        if telegram.usable:
            assert telegram.workspace is not None and telegram.policy is not None
            bindings.setdefault(telegram.workspace.name, set()).update(
                telegram.policy.providers
            )

    jobs, job_errors = load_jobs_with_errors(config)
    for name, problems in sorted(job_errors.items()):
        failed = True
        for problem in problems:
            console.print(f"[red]✗[/] jobs.{name}: {problem}")
    for job in jobs:
        if job.dir_name in job_errors:
            continue
        if catalog.usable(job.workspace):
            bindings.setdefault(job.workspace, set()).add(job.provider)

    for workspace_name, providers in sorted(bindings.items()):
        workspace = catalog.workspaces[workspace_name]
        execution_policy = catalog.policy_for(workspace)
        console.print(
            f"\n[bold]{workspace_name} → {execution_policy.name}[/] native launch"
        )
        for provider in sorted(providers):
            check = check_provider(workspace, execution_policy, provider)
            provider_problems = list(check.problems)
            if check.ok and provider == "grok":
                # A wrong-shaped grok permission config loads zero rules with
                # no error, so back the static checks with the rule count the
                # CLI actually loads from the staged home.
                grok_path = config.get("providers", {}).get("grok", {}).get("path", "grok")
                provider_problems = verify_grok_rules(workspace, execution_policy, grok_path)
            if check.ok and not provider_problems:
                revision = (check.policy_revision or "")[:12]
                servers = f" mcp: {', '.join(check.mcp_servers)}" if check.mcp_servers else ""
                console.print(f"  [green]✓[/] {provider} ({revision}){escape(servers)}")
                for warning in check.warnings:
                    console.print(f"    [yellow]![/] {escape(warning)}")
            else:
                failed = True
                for problem in provider_problems:
                    console.print(f"  [red]✗[/] {provider}: {escape(problem)}")

    if failed:
        raise typer.Exit(1)
    console.print("\n[green]All checks passed.[/]")
    console.print(
        "[dim]Plumbing only: this confirms Enso can select each native policy, not that "
        "the policy is safe. Before trusting a restricted policy, test it with the "
        "installed CLI — a forbidden read, a forbidden write, and command execution.[/]"
    )


@route_app.command("explain")
def route_explain(
    transport: Annotated[str, typer.Argument(help="Transport (only 'slack')")],
    user_id: Annotated[str, typer.Argument(help="Slack user ID, e.g. U012ABC")],
    channel_id: Annotated[str | None, typer.Argument(help="Channel ID; omit for a DM")] = None,
) -> None:
    """Explain how a Slack sender/location pair would resolve."""
    from .teams import load_teams, resolve

    if transport != "slack":
        console.print("[red]✗[/] Only 'slack' has teams routing.")
        raise typer.Exit(1)
    teams = load_teams(_load_config_or_exit())

    console.print(f"Account: {teams.account_id}")
    decision = resolve(teams, user_id=user_id, channel_id=channel_id)
    console.print(f"Decision: [bold]{decision.status}[/] ({decision.reason})")
    if decision.route is not None:
        route = decision.route
        console.print(f"Route: {route.route_id}")
        console.print(f"Workspace: {route.workspace}")
        workspace = teams.workspaces.get(route.workspace)
        console.print(f"Policy: {workspace.policy if workspace is not None else 'unresolved'}")
        console.print(f"Audit: {'on' if route.audit else 'off'}")
        if route.kind == "channel":
            console.print(f"Mention required: {'yes' if route.mention_required else 'no'}")
            console.print(
                "Thread mention required: "
                f"{'yes' if route.thread_mention_required else 'no'}"
            )
    if not teams.dispatchable:
        console.print(
            "[red]Teams dispatch is disabled by config errors (see 'enso config check').[/]"
        )


@audit_app.command("tail")
def audit_tail(
    count: Annotated[int, typer.Option("--count", "-n", help="Rows to show")] = 20,
    route: Annotated[str | None, typer.Option("--route", help="Filter by route id")] = None,
    user: Annotated[str | None, typer.Option("--user", help="Filter by user id")] = None,
) -> None:
    """Show the most recent audit turns."""
    from . import audit as audit_store

    turns = audit_store.list_turns(route_id=route, user_id=user, limit=count)
    if not turns:
        console.print("No audit turns recorded.")
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("When", "Route", "User", "Decision", "Outcome", "Request", "Response"):
        table.add_column(column)
    for turn in reversed(turns):
        table.add_row(
            turn["received_at"][:19],
            turn["route_id"],
            turn["user_name"] or turn["user_id"],
            turn["decision"],
            turn["outcome"],
            (turn["request_text"] or "")[:60],
            (turn["response_text"] or "")[:60],
        )
    console.print(table)


@audit_app.command("export")
def audit_export(
    route: Annotated[str | None, typer.Option("--route", help="Filter by route id")] = None,
    user: Annotated[str | None, typer.Option("--user", help="Filter by user id")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum rows")] = 1000,
) -> None:
    """Export audit turns as JSON lines (newest first)."""
    from . import audit as audit_store

    for turn in audit_store.list_turns(route_id=route, user_id=user, limit=limit):
        print(json.dumps(turn))


def _load_startup_config_for_logging() -> dict | None:
    """Read config for early logging setup without creating config files."""
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"enso {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True)
    ] = False,
) -> None:
    """Enso — Personal AI Agent."""
    configure_logging(_load_startup_config_for_logging())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
