"""Shared launchd and systemd user-service lifecycle for the agent and viewer."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .config import Config, Paths
from .maintenance import UpdateError, write_bytes

LAUNCHD_LABEL = "com.enso.agent"
SYSTEMD_UNIT = "enso.service"
BOOTOUT_WAIT_SECONDS = 20.0
_STANDARD_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")


@dataclass(frozen=True)
class Definition:
    label: str
    unit: str
    arguments: tuple[str, ...]
    description: str
    cli_group: str

    def name(self, platform: str) -> str:
        return self.label if platform == "launchd" else self.unit


AGENT = Definition(
    LAUNCHD_LABEL, SYSTEMD_UNIT, ("serve",), "Enso - chat with agent CLIs", "service"
)
VIEWER = Definition(
    "com.enso.web",
    "enso-web.service",
    ("web", "start", "--foreground"),
    "Enso - read-only web viewer",
    "web",
)


class ServiceError(Exception):
    """A service-manager command failed; the message is its stderr."""


@dataclass(frozen=True)
class Status:
    platform: str
    unit: Path
    installed: bool
    loaded: bool
    pid: int | None

    @property
    def running(self) -> bool:
        return self.pid is not None


def platform_name(platform: str = sys.platform) -> str:
    """``launchd`` or ``systemd``; anything else cannot run the service."""
    if platform == "darwin":
        return "launchd"
    if platform.startswith("linux"):
        return "systemd"
    raise ServiceError(f"no service manager on {platform}; run `enso serve` yourself")


def unit_path(platform: str | None = None, *, definition: Definition = AGENT) -> Path:
    platform = platform or platform_name()
    if platform == "launchd":
        return Path("~/Library/LaunchAgents").expanduser() / f"{definition.label}.plist"
    return Path("~/.config/systemd/user").expanduser() / definition.unit


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _query(cmd: list[str]) -> str:
    """stdout of a short service-manager query; empty when it fails or is missing."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except OSError, subprocess.SubprocessError:
        return ""


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ServiceError(f"{cmd[0]}: {exc}") from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ServiceError(f"{' '.join(cmd)}: {detail}")
    return result


def restart_command(
    pid: int,
    *,
    query: Callable[[list[str]], str] = _query,
    platform: str = sys.platform,
    definition: Definition = AGENT,
) -> list[str] | None:
    """The launchd/systemd restart for the unit that runs ``pid``; None when nothing does.

    A unit file on disk is not enough: a stale plist, or a unit running a different
    install, would restart the wrong process and leave this one running.
    """
    if platform == "darwin":
        target = f"{_domain()}/{definition.label}"
        match = re.search(r"^\s*pid = (\d+)", query(["launchctl", "print", target]), re.MULTILINE)
        if match and int(match.group(1)) == pid:
            return ["launchctl", "kickstart", "-k", target]
    elif platform.startswith("linux") and shutil.which("systemctl"):
        main_pid = query(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", definition.unit]
        ).strip()
        if main_pid.isdigit() and int(main_pid) == pid:
            return ["systemctl", "--user", "restart", definition.unit]
    return None


# -- Unit files -----------------------------------------------------------------


def enso_binary() -> str:
    """The ``enso`` the service runs: the one on PATH, else this interpreter's."""
    found = shutil.which("enso") or str(Path(sys.prefix) / "bin" / "enso")
    if not Path(found).is_file():
        raise ServiceError("cannot find the enso executable; install it with `uv tool install`")
    return found


def _provider_directory(path: str) -> str | None:
    """The absolute directory to contribute for a provider path, or None when there is none.

    Bare and relative names resolve like config parsing does; an unresolvable name
    contributes nothing. Contributing a relative directory would put ``.``-like
    entries ahead of real tools on the service PATH, letting the daemon's working
    directory shadow them.
    """
    resolved = shutil.which(path) or path
    directory = Path(resolved).parent
    if not directory.is_absolute() or not directory.is_dir():
        return None
    return str(directory)


def environment(paths: Paths, config: Config | None, binary: str) -> dict[str, str]:
    """PATH covering enso, every provider CLI, and the installing shell; the home when not default.

    The shell's PATH comes along because prerun scripts and provider tools resolve
    ``python3``, keyring helpers, and the like exactly as they did when the operator
    tested them by hand.
    """
    directories = [str(Path(binary).parent)]
    directories += [
        directory
        for provider in (config.providers.values() if config is not None else ())
        if (directory := _provider_directory(provider.path)) is not None
    ]
    directories += [str(Path(node).parent) for node in (shutil.which("node"),) if node]
    directories += [entry for entry in os.environ.get("PATH", "").split(":") if entry]
    directories += _STANDARD_DIRS
    env = {
        "PATH": ":".join(dict.fromkeys(directories)),
        "HOME": str(Path.home()),
        "PYTHONUNBUFFERED": "1",
    }
    if os.environ.get("ENSO_HOME"):
        env["ENSO_HOME"] = str(paths.home)
    return env


def render_plist(
    binary: str, env: dict[str, str], log: Path, *, definition: Definition = AGENT
) -> str:
    variables = "".join(
        f"        <key>{escape(key)}</key>\n        <string>{escape(value)}</string>\n"
        for key, value in env.items()
    )
    arguments = "".join(f"        <string>{escape(arg)}</string>\n" for arg in definition.arguments)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{escape(definition.label)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{escape(binary)}</string>
{arguments}    </array>
    <key>EnvironmentVariables</key>
    <dict>
{variables}    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{escape(str(log))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(log))}</string>
</dict>
</plist>
"""


def _percent_escaped(value: str) -> str:
    """A value with each literal ``%`` doubled, for any systemd setting.

    systemd expands ``%`` specifiers in paths and environment assignments after it has
    parsed the line, so a literal percent in an operator's path has to arrive doubled.
    """
    return value.replace("%", "%%")


def _quoted(value: str) -> str:
    """A value quoted for a systemd setting that splits its line into words.

    ``ExecStart=`` and ``Environment=`` are parsed with unquoting and C-style
    unescaping, so an unquoted value containing whitespace becomes several arguments or
    several assignments. Settings taken as a whole line, such as ``StandardOutput=``,
    must not go through here: they would keep the quotes as part of the path.
    """
    escaped = _percent_escaped(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_unit(
    binary: str, env: dict[str, str], log: Path, *, definition: Definition = AGENT
) -> str:
    variables = "".join(f"Environment={_quoted(f'{key}={value}')}\n" for key, value in env.items())
    return f"""[Unit]
Description={definition.description}
After=network-online.target

[Service]
Type=simple
ExecStart={_quoted(binary)} {" ".join(definition.arguments)}
Restart=always
RestartSec=5
StandardOutput=append:{_percent_escaped(str(log))}
StandardError=append:{_percent_escaped(str(log))}
{variables}
[Install]
WantedBy=default.target
"""


# -- Lifecycle ------------------------------------------------------------------


def install(
    paths: Paths,
    config: Config | None,
    platform: str | None = None,
    *,
    definition: Definition = AGENT,
    log: Path | None = None,
    binary: str | None = None,
) -> list[str]:
    """Write the unit for the current ``enso`` and (re)load it; returns what was done."""
    platform = platform or platform_name()
    binary = binary or enso_binary()
    env = environment(paths, config, binary)
    if definition == VIEWER:
        env["ENSO_HOME"] = str(paths.home.resolve())
    log = log or paths.launchd_log
    unit = unit_path(platform, definition=definition)
    unit.parent.mkdir(parents=True, exist_ok=True)
    if platform == "launchd":
        _bootout(definition=definition)
        _write_unit(unit, render_plist(binary, env, log, definition=definition))
        _bootstrap(unit, definition=definition)
    else:
        _write_unit(unit, render_unit(binary, env, log, definition=definition))
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", definition.unit])
        _run(["systemctl", "--user", "restart", definition.unit])
    return [
        f"wrote {unit} running {binary} {' '.join(definition.arguments)}",
        f"started {definition.name(platform)}",
    ]


def _write_unit(unit: Path, text: str) -> None:
    try:
        write_bytes(unit, text.encode(), mode=0o644)
    except (OSError, UpdateError) as exc:
        raise ServiceError(f"could not write {unit}: {exc}") from exc


def uninstall(platform: str | None = None, *, definition: Definition = AGENT) -> list[str]:
    platform = platform or platform_name()
    unit = unit_path(platform, definition=definition)
    if not unit.exists():
        return ["service is not installed"]
    if platform == "launchd":
        _bootout(definition=definition)
    else:
        _run(["systemctl", "--user", "disable", "--now", definition.unit])
    unit.unlink()
    if platform == "systemd":
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    return [f"stopped and removed {unit}"]


def _bootout(*, definition: Definition = AGENT) -> None:
    """Unload the agent and wait for launchd to let go of it.

    bootout returns while the old process is still shutting down (Enso closes its
    transports first), and a bootstrap in that window fails with "Input/output error".
    """
    target = f"{_domain()}/{definition.label}"
    _run(["launchctl", "bootout", target], check=False)
    deadline = time.monotonic() + BOOTOUT_WAIT_SECONDS
    while _query(["launchctl", "print", target]).strip():
        if time.monotonic() >= deadline:
            raise ServiceError(f"{definition.label} did not unload; its unit was left unchanged")
        time.sleep(0.2)


def _bootstrap(unit: Path, *, definition: Definition = AGENT) -> None:
    # A label left disabled by `launchctl unload -w` makes bootstrap fail with "Input/output
    # error"; enabling it first is harmless otherwise.
    _run(["launchctl", "enable", f"{_domain()}/{definition.label}"], check=False)
    _run(["launchctl", "bootstrap", _domain(), str(unit)])


def start(platform: str | None = None, *, definition: Definition = AGENT) -> None:
    platform = platform or platform_name()
    unit = unit_path(platform, definition=definition)
    if not unit.exists():
        raise ServiceError(f"service is not installed; run `enso {definition.cli_group} install`")
    if platform == "launchd":
        if status(platform, definition=definition).loaded:
            _run(["launchctl", "kickstart", f"{_domain()}/{definition.label}"])
        else:
            _bootstrap(unit, definition=definition)
    else:
        _run(["systemctl", "--user", "start", definition.unit])


def stop(platform: str | None = None, *, definition: Definition = AGENT) -> None:
    platform = platform or platform_name()
    # KeepAlive would restart a stopped launchd job, so stopping means unloading it.
    if platform == "launchd":
        _bootout(definition=definition)
    else:
        _run(["systemctl", "--user", "stop", definition.unit])


def restart(platform: str | None = None, *, definition: Definition = AGENT) -> None:
    platform = platform or platform_name()
    if platform == "launchd" and status(platform, definition=definition).loaded:
        _run(["launchctl", "kickstart", "-k", f"{_domain()}/{definition.label}"])
    elif platform == "launchd":
        start(platform, definition=definition)
    else:
        _run(["systemctl", "--user", "restart", definition.unit])


def status(platform: str | None = None, *, definition: Definition = AGENT) -> Status:
    platform = platform or platform_name()
    unit = unit_path(platform, definition=definition)
    if platform == "launchd":
        output = _query(["launchctl", "print", f"{_domain()}/{definition.label}"])
        match = re.search(r"^\s*pid = (\d+)", output, re.MULTILINE)
        loaded = bool(output.strip())
    else:
        output = _query(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", definition.unit]
        )
        match = re.search(r"^([1-9]\d*)$", output.strip())
        loaded = _query(["systemctl", "--user", "is-enabled", definition.unit]).strip() == "enabled"
    return Status(
        platform=platform,
        unit=unit,
        installed=unit.exists(),
        loaded=loaded,
        pid=int(match.group(1)) if match else None,
    )
