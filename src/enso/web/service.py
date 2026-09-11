"""The optional viewer supervisor, kept separate from the pidfile/process primitives."""

from __future__ import annotations

import plistlib
import shlex
import time
from pathlib import Path
from xml.parsers.expat import ExpatError

from .. import service, web
from ..config import Paths, check_config


def _systemd_values(content: str) -> tuple[list[str] | None, dict[str, str]]:
    arguments = None
    env: dict[str, str] = {}
    section = ""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        if section != "[Service]" or "=" not in line or line.startswith(("#", ";")):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"EnvironmentFile", "UnsetEnvironment"}:
            raise ValueError("indirect environment")
        if key in {"ExecStart", "Environment"} and "%" in value.replace("%%", ""):
            raise ValueError("expanded service specifier")
        if key == "ExecStart":
            if arguments and value.strip():
                raise ValueError("multiple start commands")
            arguments = shlex.split(value.replace("%%", "%"))
        elif key == "Environment":
            if not value.strip():
                env.clear()
            for assignment in shlex.split(value.replace("%%", "%")):
                name, setting = assignment.split("=", 1)
                env[name] = setting
    return arguments, env


def unit_home(state: service.Status) -> Path:
    """Read the home of a generated or compatible handwritten viewer unit.

    A fixed user-service name is shared by every ENSO_HOME. Its name alone is never
    permission to replace a definition or stop a process belonging to another home.
    """
    try:
        if state.unit.is_symlink():
            raise ValueError("symbolic link")
        with state.unit.open("rb") as file:
            content = file.read(65537)
        if len(content) > 65536:
            raise ValueError("oversized unit")
        if state.platform == "launchd":
            data = plistlib.loads(content)
            if data.get("Label") != service.VIEWER.label:
                raise ValueError("unexpected label")
            arguments = data.get("ProgramArguments")
            if data.get("Program") is not None:
                raise ValueError("overridden program")
            env = data.get("EnvironmentVariables", {})
        else:
            if state.unit.with_name(state.unit.name + ".d").exists():
                raise ValueError("unit overrides")
            arguments, env = _systemd_values(content.decode())
        if (
            not isinstance(arguments, list)
            or not all(isinstance(arg, str) for arg in arguments)
            or len(arguments) != len(service.VIEWER.arguments) + 1
            or arguments[1:] != list(service.VIEWER.arguments)
            or not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        ):
            raise ValueError("unexpected command or environment")
        home = env.get("ENSO_HOME") or str(Path(env.get("HOME", str(Path.home()))) / ".enso")
        path = Path(home)
        if not path.is_absolute():
            raise ValueError("relative home")
        return path.resolve()
    except (
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        plistlib.InvalidFileException,
        ExpatError,
    ) as exc:
        raise service.ServiceError(
            f"cannot verify the viewer unit {state.unit}; it must directly run "
            "`enso web start --foreground` with an absolute home"
        ) from exc


def find(paths: Paths) -> service.Status | None:
    """This home's standard viewer service, or None for a standalone viewer."""
    try:
        platform = service.platform_name()
    except service.ServiceError:
        return None
    unit = service.unit_path(platform, definition=service.VIEWER)
    if not unit.exists() and not unit.is_symlink():
        return None
    state = service.status(platform, definition=service.VIEWER)
    return state if unit_home(state) == paths.home.resolve() else None


def _check_owner(paths: Paths, state: service.Status) -> None:
    if unit_home(state) != paths.home.resolve():
        raise service.ServiceError(
            f"{state.unit} belongs to another Enso home; it was left unchanged"
        )
    current = web.status(paths)
    if state.running and (not current.running or state.pid != current.pid):
        raise service.ServiceError(
            "the viewer service does not own this home's viewer; retry after checking its status"
        )


def _ready(paths: Paths, platform: str) -> str:
    deadline = time.monotonic() + web.START_TIMEOUT
    while time.monotonic() < deadline:
        current = web.status(paths)
        supervisor = service.status(platform, definition=service.VIEWER)
        if (
            current.running
            and current.pid is not None
            and current.pid == supervisor.pid
            and current.host is not None
            and current.port is not None
            and web._reachable(web.Bind(current.host, current.port))
        ):
            return (
                f"listening on {current.url} (pid {current.pid}, {service.VIEWER.name(platform)})"
            )
        time.sleep(0.1)
    raise service.ServiceError(
        f"the viewer service did not become ready; see {paths.web_service_log} and `enso doctor`"
    )


def install(paths: Paths) -> list[str]:
    """Adopt this home's compatible unit, or its standalone process, then supervise it."""
    missing = web.missing_extra()
    if missing:
        raise service.ServiceError(f"the web viewer needs {', '.join(missing)}; {web.INSTALL_HINT}")
    state = service.status(definition=service.VIEWER)
    if state.installed or state.loaded or state.running:
        _check_owner(paths, state)
    if (paths.runtime_dir / "install.json").exists():
        from ..updates import installed

        receipt = installed(paths)
        if "web" not in receipt["extras"]:
            raise service.ServiceError(
                "the managed release needs the web extra before installing its viewer service"
            )
        binary = str(Path(receipt["bin_dir"]) / "enso")
        if not Path(binary).is_file():
            raise service.ServiceError("the managed installation's stable enso launcher is missing")
    else:
        binary = service.enso_binary()
    if state.loaded or state.running:
        service.stop(state.platform, definition=service.VIEWER)
    web.stop(paths)
    paths.home.mkdir(parents=True, exist_ok=True)
    config, _problems, _warnings = check_config(paths)
    lines = service.install(
        paths,
        config,
        state.platform,
        definition=service.VIEWER,
        log=paths.web_service_log.resolve(),
        binary=binary,
    )
    lines.append(_ready(paths, state.platform))
    return lines


def uninstall(paths: Paths) -> list[str]:
    state = service.status(definition=service.VIEWER)
    if not state.installed and not state.loaded and not state.running:
        return ["viewer service is not installed"]
    _check_owner(paths, state)
    lines = service.uninstall(state.platform, definition=service.VIEWER)
    web.stop(paths)
    return lines


def start(paths: Paths, state: service.Status) -> str:
    _check_owner(paths, state)
    current = web.status(paths)
    if current.running:
        if current.pid != state.pid:
            raise service.ServiceError(
                "a standalone viewer is running; stop it before starting the viewer service"
            )
        return f"already running pid {current.pid} at {current.url}"
    service.start(state.platform, definition=service.VIEWER)
    return _ready(paths, state.platform)


def stop(paths: Paths, state: service.Status) -> str:
    _check_owner(paths, state)
    service.stop(state.platform, definition=service.VIEWER)
    web.stop(paths)
    return "stopped viewer service"
