"""Enso CLI — the brain behind the bot."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__
from .config import CONFIG_FILE, detect_providers, load_config, resolve_providers, save_config
from .jobs import create_job, load_jobs
from .messages import clear as msg_clear
from .messages import pending as msg_pending
from .messages import send as msg_send
from .transports.telegram import TelegramTransport

log = logging.getLogger(__name__)

app = typer.Typer(help="Enso — AI agents from your phone", no_args_is_help=True)
job_app = typer.Typer(help="Manage background jobs")
message_app = typer.Typer(help="Send messages and files to Telegram")
service_app = typer.Typer(help="Manage the background service")
app.add_typer(job_app, name="job")
app.add_typer(message_app, name="message")
app.add_typer(service_app, name="service")

console = Console()

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

_NOISY_LOGGERS = (
    "httpx", "httpcore", "telegram", "telegram.ext", "telegram.ext._application",
    "telegram.ext._updater", "telegram.ext._base_update_handler", "telegram._bot",
    "hpack", "urllib3", "h11", "h2",
)


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


def _tg_send_file(token: str, chat_id: int, file_path: str, caption: str = "") -> bool:
    """Send a file to Telegram. Auto-selects method based on extension."""
    import mimetypes
    from email.mime.multipart import MIMEMultipart
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

    req = urllib.request.Request(
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception:
        return False


def _tg_send_message(token: str, chat_id: int, text: str) -> bool:
    """Send a message with HTML formatting. Returns True on success."""
    from .formatting import md_to_html

    try:
        html = md_to_html(text)
        result = _tg_call(
            token, "sendMessage",
            chat_id=chat_id, text=html, parse_mode="HTML",
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
_LAUNCHD_PLIST = os.path.expanduser(
    f"~/Library/LaunchAgents/{_LAUNCHD_LABEL}.plist"
)
_SYSTEMD_UNIT = "enso.service"


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
    for cmd in ("claude", "codex", "gemini", "node", "npx"):
        p = shutil.which(cmd)
        if p:
            path_dirs.add(os.path.dirname(p))
    path_dirs.update(["/usr/local/bin", "/usr/bin", "/bin"])
    return ":".join(sorted(path_dirs))


def _systemd_env() -> dict[str, str]:
    """Build env dict with XDG_RUNTIME_DIR and DBUS for systemctl."""
    xdg = os.environ.get(
        "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
    )
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
        path = os.path.expanduser(
            f"~/.config/systemd/user/{_SYSTEMD_UNIT}"
        )
        return os.path.exists(path)
    return False


def _service_is_running() -> bool:
    """Check if the service process is currently running."""
    platform = _service_platform()
    try:
        if platform == "launchd":
            r = subprocess.run(
                ["launchctl", "list", _LAUNCHD_LABEL],
                capture_output=True,
            )
            return r.returncode == 0
        if platform == "systemd":
            r = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet",
                 _SYSTEMD_UNIT],
                env=_systemd_env(), capture_output=True,
            )
            return r.returncode == 0
    except Exception:
        pass
    return False


def _service_install(config: dict) -> bool:
    """Write and load the platform service definition. Returns True on success."""
    enso_bin = _find_enso_bin()
    if not enso_bin:
        console.print("[red]Could not find 'enso' binary.[/]")
        return False

    platform = _service_platform()
    if platform == "launchd":
        return _install_launchd(config, enso_bin)
    if platform == "systemd":
        return _install_systemd(config, enso_bin)

    console.print(
        f"[yellow]Service install not supported on {sys.platform}.[/]"
    )
    return False


def _install_launchd(config: dict, enso_bin: str) -> bool:
    """Write and load a macOS launchd plist."""
    path_str = _build_path_str(enso_bin)
    working_dir = config.get("working_dir", os.getcwd())
    log_path = os.path.expanduser("~/.enso/enso.log")

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
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_str}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
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
            capture_output=True, check=True,
        )
        console.print("[green]\u2713[/] Service installed and started.")
        return True
    except Exception:
        console.print(f"Written to {_LAUNCHD_PLIST}")
        console.print(f"Load with: launchctl load {_LAUNCHD_PLIST}")
        return False


def _install_systemd(config: dict, enso_bin: str) -> bool:
    """Write and enable a systemd user service."""
    path_str = _build_path_str(enso_bin)
    working_dir = config.get("working_dir", os.getcwd())

    unit = f"""\
[Unit]
Description=Enso - Personal AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={enso_bin} serve
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=PATH={path_str}

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
            env=env, capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT],
            env=env, capture_output=True,
        )
        console.print("[green]\u2713[/] Service installed and started.")
        return True
    except Exception:
        console.print(f"Written to {service_path}")
        console.print(
            f"Enable with: systemctl --user enable --now {_SYSTEMD_UNIT}"
        )
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
        path = os.path.expanduser(
            f"~/.config/systemd/user/{_SYSTEMD_UNIT}"
        )
        if os.path.exists(path):
            env = _systemd_env()
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT],
                env=env, capture_output=True,
            )
            os.remove(path)
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                env=env, capture_output=True,
            )
            return True
    return False


def _service_start() -> bool:
    """Start the service. Returns True on success."""
    platform = _service_platform()
    try:
        if platform == "launchd":
            r = subprocess.run(
                ["launchctl", "load", _LAUNCHD_PLIST],
                capture_output=True,
            )
            return r.returncode == 0
        if platform == "systemd":
            r = subprocess.run(
                ["systemctl", "--user", "start", _SYSTEMD_UNIT],
                env=_systemd_env(), capture_output=True,
            )
            return r.returncode == 0
    except Exception:
        pass
    return False


def _service_stop() -> bool:
    """Stop the service. Returns True on success."""
    platform = _service_platform()
    try:
        if platform == "launchd":
            r = subprocess.run(
                ["launchctl", "unload", _LAUNCHD_PLIST],
                capture_output=True,
            )
            return r.returncode == 0
        if platform == "systemd":
            r = subprocess.run(
                ["systemctl", "--user", "stop", _SYSTEMD_UNIT],
                env=_systemd_env(), capture_output=True,
            )
            return r.returncode == 0
    except Exception:
        pass
    return False


def _service_restart() -> bool:
    """Restart the service. Returns True on success."""
    platform = _service_platform()
    try:
        if platform == "launchd":
            uid = str(os.getuid())
            r = subprocess.run(
                ["launchctl", "kickstart", "-k",
                 f"gui/{uid}/{_LAUNCHD_LABEL}"],
                capture_output=True,
            )
            return r.returncode == 0
        if platform == "systemd":
            r = subprocess.run(
                ["systemctl", "--user", "restart", _SYSTEMD_UNIT],
                env=_systemd_env(), capture_output=True,
            )
            return r.returncode == 0
    except Exception:
        pass
    return False


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
        console.print("Install at least one of: claude, codex, gemini")


def _setup_transport(config: dict) -> int | None:
    """Step 2: configure Telegram. Returns chat_id or None."""
    console.rule("[bold]Step 2 \u00b7 Telegram")
    config["transport"] = "telegram"
    return _setup_telegram(config)


def _setup_telegram(config: dict) -> int | None:
    """Configure Telegram bot and capture user. Returns chat_id or None."""
    tg_cfg = config.get("transports", {}).get("telegram", {})
    current_token = tg_cfg.get("bot_token", "")
    current_users = tg_cfg.get("allowed_user_ids", [])
    bot_info = None

    if current_token:
        bot_info = _tg_validate_token(current_token)
        if bot_info:
            console.print(f"  Current bot: [bold]@{bot_info.get('username', '?')}[/]")
            if current_users:
                console.print(f"  Allowed users: {current_users}")
            if not Confirm.ask("\n  Reconfigure Telegram?", default=False):
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
        token = Prompt.ask("  Bot token")
        if not token:
            console.print("[red]  Token is required.[/]")
            continue
        with console.status("Validating..."):
            bot_info = _tg_validate_token(token)
        if bot_info:
            console.print(f"  [green]\u2713[/] Connected to @{bot_info.get('username', '?')}")
            config.setdefault("transports", {})["telegram"] = {
                "bot_token": token,
                "allowed_user_ids": current_users,
            }
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
    config["transports"]["telegram"]["allowed_user_ids"] = [user_id]
    return user_info.get("chat_id")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def setup() -> None:
    """Interactive setup wizard."""
    console.print(Panel("Enso Setup", subtitle=f"v{__version__}", expand=False))
    config = load_config()

    _setup_providers(config)
    captured_chat_id = _setup_transport(config)

    # Step 3: Working directory
    console.rule("[bold]Step 3 \u00b7 Working Directory")
    console.print("  Where agents run commands and create files.\n")
    default_dir = os.path.join(os.path.expanduser("~/.enso"), "workspace")
    current_dir = config.get("working_dir", default_dir)
    config["working_dir"] = os.path.abspath(Prompt.ask("  Working directory", default=current_dir))
    os.makedirs(config["working_dir"], exist_ok=True)

    from .core import Runtime

    Runtime(config).install_system_prompts()

    with console.status("Saving config..."):
        save_config(config)
    console.print(f"[green]\u2713[/] Config saved to {CONFIG_FILE}")

    # Send test message if telegram
    if config.get("transport") == "telegram":
        tg = config.get("transports", {}).get("telegram", {})
        token = tg.get("bot_token", "")
        users = tg.get("allowed_user_ids", [])
        chat_id = captured_chat_id or (users[0] if users else None)
        if token and chat_id:
            with console.status("Sending test message..."):
                sent = _tg_send_message(token, chat_id, f"Enso v{__version__} ready.")
            if sent:
                console.print("[green]\u2713[/] Test message sent!")
            else:
                console.print("[yellow]Failed to send test message.[/]")

    # Step 4: Background service
    console.rule("[bold]Step 4 \u00b7 Background Service (optional)")
    installed = False
    if _service_platform():
        if _service_is_installed():
            console.print("  Service already installed.")
            if Confirm.ask("  Reinstall?", default=False):
                _service_install(config)
                installed = True
            else:
                installed = True
        elif Confirm.ask("  Install background service?", default=True):
            _service_install(config)
            installed = True
    else:
        console.print(
            f"[yellow]  Auto service not supported on {sys.platform}.[/]"
        )

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
    console.print(Panel(
        summary, title="Setup Complete", border_style="green", expand=False,
    ))


@app.command()
def serve(
    working_dir: Annotated[
        str | None, typer.Option("--working-dir", help="Override working directory")
    ] = None,
) -> None:
    """Start the bot and job scheduler."""
    from .core import Runtime

    config = load_config()
    if working_dir:
        config["working_dir"] = working_dir

    wd = config.get("working_dir", os.getcwd())
    if not os.path.isdir(wd):
        console.print(f"[red]Error: Working directory does not exist: {wd}[/]")
        raise typer.Exit(1)
    os.chdir(wd)

    tg_cfg = config.get("transports", {}).get("telegram", {})
    if not tg_cfg.get("bot_token"):
        console.print("[red]Telegram not configured. Run 'enso setup' first.[/]")
        raise typer.Exit(1)

    runtime = Runtime(config)
    runtime.install_system_prompts()
    runtime.load_state()

    log.info("Starting Enso v%s", __version__)
    log.info("  working_dir=%s", wd)

    transport = TelegramTransport(runtime)
    runtime.transport = transport
    transport.start()


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
    table.add_column("Enabled")
    for job in jobs:
        enabled = "[green]\u2713[/]" if job.enabled else "[red]\u2717[/]"
        table.add_row(job.dir_name, job.schedule, job.provider, job.model, enabled)
    console.print(table)


@job_app.command("create")
def job_create(
    name: Annotated[str, typer.Option("--name", help="Display name for the job")],
    provider: Annotated[str, typer.Option("--provider", help="claude, codex, or gemini")],
    model: Annotated[str, typer.Option("--model", help="Model name")],
    schedule: Annotated[str, typer.Option("--schedule", help="Cron expression (e.g. '0 9 * * *')")],
) -> None:
    """Create a new background job. Edit the JOB.md to add the prompt and optional prerun."""
    dir_name = name.lower().replace(" ", "-")
    job = create_job(dir_name, name, provider, model, schedule)
    console.print(f"[green]\u2713[/] Job created: {job.path}")
    console.print("  Edit the JOB.md to add your prompt and optional prerun script.")


@job_app.command("run")
def job_run(
    name: Annotated[str, typer.Argument(help="Job directory name")],
) -> None:
    """Manually run a job (output goes to stdout, no notifications)."""
    import asyncio

    from .core import Runtime

    jobs = load_jobs()
    job = next((j for j in jobs if j.dir_name == name), None)
    if not job:
        console.print(f"[red]Job '{name}' not found.[/]")
        raise typer.Exit(1)

    config = load_config()
    runtime = Runtime(config)

    async def _run() -> None:
        # Prerun
        prerun_output = ""
        if job.prerun:
            script = os.path.join(job.job_dir, job.prerun)
            if os.path.isfile(script):
                proc = await asyncio.create_subprocess_exec(
                    "bash", script,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=job.job_dir,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode != 0:
                    console.print("[yellow]Prerun check failed, skipping.[/]")
                    return
                prerun_output = stdout.decode(errors="replace").strip()

        prompt = job.prompt
        if prerun_output:
            prompt = prompt.replace("{{prerun_output}}", prerun_output)

        provider = runtime.make_provider(job.provider)
        cmd = provider.build_batch_command(prompt, job.model)
        console.print(f"[dim]Running {job.provider}/{job.model}...[/]")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=runtime.working_dir,
        )
        stdout, _ = await proc.communicate()
        console.print(stdout.decode(errors="replace"))

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Message subcommands
# ---------------------------------------------------------------------------

@message_app.command("send")
def message_send(
    text: Annotated[str, typer.Argument(help="Message text to send")],
) -> None:
    """Send a message to Telegram and queue as background context."""
    cfg = load_config()
    tg_cfg = cfg.get("transports", {}).get("telegram", {})
    token = tg_cfg.get("bot_token", "")
    user_ids = tg_cfg.get("allowed_user_ids", [])
    if not token or not user_ids:
        console.print("[red]\u2717[/] Telegram not configured. Run [bold]enso setup[/].")
        raise typer.Exit(1)
    for uid in user_ids:
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
) -> None:
    """Send a file (image, video, audio, document) to Telegram."""
    if not os.path.isfile(file):
        console.print(f"[red]✗[/] File not found: {file}")
        raise typer.Exit(1)
    cfg = load_config()
    tg_cfg = cfg.get("transports", {}).get("telegram", {})
    token = tg_cfg.get("bot_token", "")
    user_ids = tg_cfg.get("allowed_user_ids", [])
    if not token or not user_ids:
        console.print("[red]✗[/] Telegram not configured. Run [bold]enso setup[/].")
        raise typer.Exit(1)
    for uid in user_ids:
        if not _tg_send_file(token, uid, file, caption):
            console.print(f"[red]✗[/] Failed to send to user {uid}.")
            raise typer.Exit(1)
    filename = os.path.basename(file)
    note = f"Sent attachment: {filename}"
    if caption:
        note += f" — {caption}"
    msg_send(note, source="attach")
    console.print("[green]✓[/] File sent.")


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
    config = load_config()
    if _service_install(config):
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
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Follow log output")
    ] = False,
    lines: Annotated[
        int, typer.Option("--lines", "-n", help="Number of lines")
    ] = 25,
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
# App setup
# ---------------------------------------------------------------------------

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
    logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
