"""Bounded subprocesses and service-manager handoffs for managed Enso updates.

An updater must be in a separate service, not merely a detached child in the
daemon's process group or systemd cgroup. Only services proven to own this home's
processes may be stopped, and the independent viewer is restored separately.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import plistlib
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import service, web
from .config import Paths
from .connection_setup import receiver_active
from .maintenance import UpdateError, daemon, read_json, write_bytes

OUTPUT_LIMIT = 65536


def run_command(
    args: list[str], *, cwd: Path, timeout: float = 60, env: dict[str, str] | None = None
) -> str:
    """Run a fixed argument array with a bounded output tail and process-tree cleanup."""
    output = bytearray()
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise UpdateError(f"could not start {Path(args[0]).name}: {exc.strerror}") from exc
    assert process.stdout is not None

    def collect() -> None:
        assert process.stdout is not None
        with process.stdout:
            while chunk := process.stdout.read(8192):
                output.extend(chunk)
                del output[:-OUTPUT_LIMIT]

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        raise UpdateError(f"{Path(args[0]).name} timed out after {timeout:g}s") from None
    finally:
        # A successful parent can leave a child holding the pipe or continuing a
        # write. Bound that child too; service-managed daemons live in other groups.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if process.poll() is None:
            process.wait(timeout=10)
        reader.join(timeout=5)
    if process.returncode:
        # Provider/configuration output can carry credentials. Keep failures concise;
        # callers can inspect their release's private logs instead of echoing secrets.
        raise UpdateError(f"{Path(args[0]).name} failed (exit {process.returncode})")
    return output.decode("utf-8", errors="replace").strip()


def launch(paths: Paths, operation_id: str, python: Path) -> None:
    """Ask the OS to run the updater independently of the requesting chat process."""
    command = [str(python), "-m", "enso.cli", "update", "_run", operation_id]
    env = {"ENSO_HOME": str(paths.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if sys.platform.startswith("linux"):
        run_command(
            [
                "systemd-run",
                "--user",
                f"--unit=enso-update-{operation_id}",
                "--collect",
                "--property=Restart=on-failure",
                "--property=RestartSec=2",
                *[f"--setenv={key}={value}" for key, value in env.items()],
                *command,
            ],
            cwd=paths.home,
        )
    elif sys.platform == "darwin":
        label = _helper_label(operation_id)
        target = f"gui/{os.getuid()}/{label}"
        loaded = _launchd_definition(paths, target)
        if loaded is not None:
            if re.search(r"^\s*pid = [1-9]\d*", loaded, re.MULTILINE):
                raise UpdateError(
                    "the updater helper is still running; retry recovery after it exits"
                )
            # SuccessfulExit=False stops retrying after an orderly failure report,
            # but launchd keeps its definition loaded. Bootstrapping it again fails.
            run_command(["launchctl", "kickstart", target], cwd=paths.home)
            return
        unit = paths.runtime_dir / "operations" / operation_id / "updater.plist"
        write_bytes(
            unit,
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": command,
                    "EnvironmentVariables": env,
                    "WorkingDirectory": str(paths.home),
                    "RunAtLoad": True,
                    "KeepAlive": {"SuccessfulExit": False},
                    "ThrottleInterval": 2,
                    "StandardOutPath": str(unit.parent / "worker.log"),
                    "StandardErrorPath": str(unit.parent / "worker.log"),
                }
            ),
        )
        run_command(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(unit)], cwd=paths.home)
    else:
        raise UpdateError("self-updates require macOS launchd or Linux user systemd")


def _helper_label(operation_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise UpdateError("invalid update helper id")
    return f"com.enso.update.{operation_id}"


def _launchd_definition(paths: Paths, target: str) -> str | None:
    try:
        return run_command(["launchctl", "print", target], cwd=paths.home)
    except UpdateError:
        return None


def cleanup_finished(paths: Paths, operation_id: str) -> None:
    """Forget one completed idle launchd helper; caller must hold the worker lock.

    Linux transient units already use --collect. Keep the operation's private
    plist and log as recovery evidence, and never stop a helper that is running.
    """
    if sys.platform != "darwin":
        return
    target = f"gui/{os.getuid()}/{_helper_label(operation_id)}"
    loaded = _launchd_definition(paths, target)
    if loaded is None or re.search(r"^\s*pid = [1-9]\d*", loaded, re.MULTILINE):
        return
    with contextlib.suppress(UpdateError):
        run_command(["launchctl", "bootout", target], cwd=paths.home)


def _unit_pid(paths: Paths, name: str) -> int | None:
    if sys.platform.startswith("linux"):
        value = run_command(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", name], cwd=paths.home
        )
        return int(value) if value.isdigit() and int(value) > 0 else None
    try:
        value = run_command(["launchctl", "print", f"gui/{os.getuid()}/{name}"], cwd=paths.home)
    except UpdateError:
        return None
    match = re.search(r"^\s*pid = (\d+)", value, re.MULTILINE)
    return int(match[1]) if match else None


def discover(paths: Paths, viewer_service: str = "") -> dict[str, Any]:
    state = daemon(paths)
    if receiver_active(paths) and not state:
        raise UpdateError("a legacy or unresponsive Enso is running; stop it before migrating")
    if state and service.restart_command(state["pid"]) is None:
        raise UpdateError(
            "running Enso is not managed by its user service; install the service first"
        )
    viewer = web.status(paths)
    if viewer_service and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", viewer_service):
        raise UpdateError("viewer service must be a user service name")
    if viewer.running and viewer_service and _unit_pid(paths, viewer_service) != viewer.pid:
        raise UpdateError("the configured viewer service does not own this home's viewer")
    return {
        "daemon": bool(state),
        "daemon_pid": state.get("pid"),
        "viewer": viewer.running,
        "viewer_service": viewer_service,
        "viewer_host": viewer.host,
        "viewer_port": viewer.port,
        "daemon_definition": _definition_digest(
            service.SYSTEMD_UNIT if sys.platform.startswith("linux") else service.LAUNCHD_LABEL
        )
        if state
        else None,
        "viewer_definition": _definition_digest(viewer_service)
        if viewer.running and viewer_service
        else None,
    }


def _definition_digest(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
        raise UpdateError("invalid user service name")
    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config/systemd/user" / name
    else:
        unit = Path.home() / "Library/LaunchAgents" / f"{name}.plist"
    try:
        with unit.open("rb") as file:
            definition = file.read(65537)
        if not definition or len(definition) > 65536:
            raise UpdateError("the user service definition is empty or too large")
        return hashlib.sha256(definition).hexdigest()
    except OSError as exc:
        raise UpdateError(
            "cannot verify the user service definition; reinstall its service"
        ) from exc


def _unchanged_definition(previous: dict[str, Any], key: str, name: str) -> bool:
    expected = previous.get(key)
    if not expected:
        return False
    if (
        not isinstance(expected, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
        or _definition_digest(name) != expected
    ):
        raise UpdateError("the user service definition changed during this update")
    return True


def _check_ownership(paths: Paths, previous: dict[str, Any]) -> None:
    """Recheck the verified generated unit before stopping or starting its processes."""
    if previous["daemon"]:
        name = service.SYSTEMD_UNIT if sys.platform.startswith("linux") else service.LAUNCHD_LABEL
        # A candidate may fail or be interrupted before its first heartbeat. The
        # original verified unit still owns that launch, including its recovery.
        if not _unchanged_definition(previous, "daemon_definition", name):
            pid = _unit_pid(paths, name)
            if pid is not None and pid != daemon(paths).get("pid"):
                raise UpdateError("the Enso service no longer owns this home's daemon")
    viewer_name = previous.get("viewer_service")
    if (
        previous["viewer"]
        and viewer_name
        and not _unchanged_definition(previous, "viewer_definition", viewer_name)
    ):
        pid = _unit_pid(paths, viewer_name)
        viewer = web.status(paths)
        if pid is not None and (not viewer.running or pid != viewer.pid):
            raise UpdateError("the viewer service no longer owns this home's viewer")


def stop(paths: Paths, previous: dict[str, Any]) -> None:
    _check_ownership(paths, previous)
    if previous["daemon"]:
        service.stop()
    if previous["viewer"]:
        name = previous.get("viewer_service")
        if name and sys.platform.startswith("linux"):
            run_command(["systemctl", "--user", "stop", name], cwd=paths.home)
        elif name:
            run_command(["launchctl", "bootout", f"gui/{os.getuid()}/{name}"], cwd=paths.home)
        else:
            web.stop(paths)
    deadline = time.monotonic() + 30
    while receiver_active(paths) or web.status(paths).running:
        if time.monotonic() >= deadline:
            raise UpdateError("Enso processes did not stop; update will not change the home")
        time.sleep(0.1)


def start(paths: Paths, previous: dict[str, Any], binary: Path) -> None:
    _check_ownership(paths, previous)
    if previous["daemon"]:
        service.start()
    if not previous["viewer"]:
        return
    name = previous.get("viewer_service")
    if name and sys.platform.startswith("linux"):
        run_command(["systemctl", "--user", "start", name], cwd=paths.home)
    elif name:
        unit = Path.home() / "Library" / "LaunchAgents" / f"{name}.plist"
        run_command(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(unit)], cwd=paths.home)
    else:
        args = [str(binary), "web", "start"]
        if previous.get("viewer_host"):
            args.extend(["--host", str(previous["viewer_host"])])
        if previous.get("viewer_port"):
            args.extend(["--port", str(previous["viewer_port"])])
        run_command(
            args,
            cwd=paths.home,
            env={
                **os.environ,
                "ENSO_HOME": str(paths.home),
                "ENSO_UPDATE_INTERNAL": read_json(paths.update_state).get("id", ""),
            },
        )


def healthy(paths: Paths, previous: dict[str, Any], version: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        state = daemon(paths)
        ready = not previous["daemon"] or (
            state.get("version") == version and state.get("ready") and state.get("paused")
        )
        if previous["viewer"]:
            viewer = web.status(paths)
            ready = (
                ready
                and viewer.running
                and web._reachable(web.Bind(viewer.host or "127.0.0.1", viewer.port or 8787))
            )
        if ready:
            return
        if time.monotonic() >= deadline:
            raise UpdateError(f"Enso {version} did not become ready before the startup deadline")
        time.sleep(0.25)
