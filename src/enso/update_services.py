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
import threading
import time
from collections.abc import Iterator
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


@contextlib.contextmanager
def _service_errors() -> Iterator[None]:
    """Report a service-manager failure the way every other update step fails."""
    try:
        yield
    except service.ServiceError as exc:
        raise UpdateError(str(exc)) from exc


def _platform() -> str:
    with _service_errors():
        return service.platform_name()


def _helper(operation_id: str) -> service.Definition:
    """The per-operation updater unit: a launchd job, or a transient systemd unit."""
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise UpdateError("invalid update helper id")
    return service.Definition(
        f"com.enso.update.{operation_id}",
        f"enso-update-{operation_id}",
        ("-m", "enso.cli", "update", "_run", operation_id),
        "Enso updater",
        "update",
    )


def launch(paths: Paths, operation_id: str, python: Path) -> None:
    """Ask the OS to run the updater independently of the requesting chat process."""
    helper = _helper(operation_id)
    platform = _platform()
    command = [str(python), *helper.arguments]
    env = {"ENSO_HOME": str(paths.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if platform == "systemd":
        run_command(
            [
                "systemd-run",
                "--user",
                f"--unit={helper.unit}",
                "--collect",
                "--property=Restart=on-failure",
                "--property=RestartSec=2",
                *[f"--setenv={key}={value}" for key, value in env.items()],
                *command,
            ],
            cwd=paths.home,
        )
        return
    if service.unit_pid(platform, definition=helper):
        raise UpdateError("the updater helper is still running; retry recovery after it exits")
    if service.unit_loaded(platform, definition=helper):
        # SuccessfulExit=False stops retrying after an orderly failure report,
        # but launchd keeps its definition loaded. Bootstrapping it again fails.
        target = f"{service.domain()}/{helper.label}"
        run_command(["launchctl", "kickstart", target], cwd=paths.home)
        return
    unit = paths.runtime_dir / "operations" / operation_id / "updater.plist"
    write_bytes(
        unit,
        plistlib.dumps(
            {
                "Label": helper.label,
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
    run_command(["launchctl", "bootstrap", service.domain(), str(unit)], cwd=paths.home)


def _helper_state(platform: str, helper: service.Definition) -> str:
    """Distinguish confirmed absence from failed queries before deleting a helper's files."""
    if platform == "launchd":
        result = service._run(
            ["launchctl", "print", f"{service.domain()}/{helper.label}"], check=False
        )
        if result.returncode == 113 and f'Could not find service "{helper.label}"' in result.stderr:
            return "absent"
        if result.returncode or re.search(r"^\s*pid = [1-9]\d*", result.stdout, re.MULTILINE):
            return "busy"
        return (
            "idle"
            if re.search(r"^\s*state = not running$", result.stdout, re.MULTILINE)
            else "busy"
        )
    result = service._run(
        [
            "systemctl",
            "--user",
            "show",
            "--property=LoadState,ActiveState,MainPID",
            helper.unit,
        ],
        check=False,
    )
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or state.get("MainPID") != "0":
        return "busy"
    if state.get("LoadState") == "not-found" and state.get("ActiveState") == "inactive":
        return "absent"
    return "idle" if state.get("ActiveState") in {"inactive", "failed"} else "busy"


def cleanup_finished(paths: Paths, operation_id: str) -> bool:
    """Unload an idle completed helper; return whether its files may now be pruned.

    The caller holds the worker lock. Running or restarting helpers and uncertain
    service state keep their files; systemd's transient units use --collect.
    """
    platform = _platform()
    helper = _helper(operation_id)
    try:
        state = _helper_state(platform, helper)
        if state == "absent":
            return True
        if state != "idle":
            return False
        service.stop(platform, definition=helper)
        return _helper_state(platform, helper) == "absent"
    except service.ServiceError:
        return False


def _viewer(name: str) -> service.Definition:
    """The viewer unit an installer named, validated before it names any path."""
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
        raise UpdateError("viewer service must be a user service name")
    return service.VIEWER.named(name)


def _standard_viewer(paths: Paths, platform: str, viewer: web.Status) -> service.Definition | None:
    """The built-in viewer unit when it provably runs this home's viewer, else None.

    A matching PID proves ownership, including for older handwritten units, so the
    built-in service needs no saved installer override.
    """
    if (
        viewer.pid is not None
        and service.unit_pid(platform, definition=service.VIEWER) == viewer.pid
    ):
        return service.VIEWER
    unit = service.unit_path(platform, definition=service.VIEWER)
    if unit.exists() or unit.is_symlink():
        from .web.service import unit_home

        with _service_errors():
            home = unit_home(service.Status(platform, unit, True, False, None))
        if home == paths.home.resolve():
            raise UpdateError(
                "cannot confirm the standard viewer service owns this home's viewer; "
                "check its service state before updating"
            )
    return None


def discover(paths: Paths, viewer_service: str = "") -> dict[str, Any]:
    platform = _platform()
    state = daemon(paths)
    if receiver_active(paths) and not state:
        raise UpdateError("a legacy or unresponsive Enso is running; stop it before migrating")
    if state and service.restart_command(state["pid"], platform=platform) is None:
        raise UpdateError(
            "running Enso is not managed by its user service; install the service first"
        )
    viewer = web.status(paths)
    viewer_unit = _viewer(viewer_service) if viewer_service else None
    if viewer.running:
        if viewer_unit is None:
            viewer_unit = _standard_viewer(paths, platform, viewer)
        elif service.unit_pid(platform, definition=viewer_unit) != viewer.pid:
            raise UpdateError("the configured viewer service does not own this home's viewer")
    return {
        "daemon": bool(state),
        "daemon_pid": state.get("pid"),
        "viewer": viewer.running,
        "viewer_service": viewer_unit.name(platform) if viewer_unit else "",
        "viewer_host": viewer.host,
        "viewer_port": viewer.port,
        "daemon_definition": _definition_digest(platform, service.AGENT) if state else None,
        "viewer_definition": _definition_digest(platform, viewer_unit)
        if viewer.running and viewer_unit
        else None,
    }


def _definition_digest(platform: str, definition: service.Definition) -> str:
    unit = service.unit_path(platform, definition=definition)
    try:
        with unit.open("rb") as file:
            content = file.read(65537)
        if not content or len(content) > 65536:
            raise UpdateError("the user service definition is empty or too large")
        return hashlib.sha256(content).hexdigest()
    except OSError as exc:
        raise UpdateError(
            "cannot verify the user service definition; reinstall its service"
        ) from exc


def _unchanged_definition(
    previous: dict[str, Any], key: str, platform: str, definition: service.Definition
) -> bool:
    expected = previous.get(key)
    if not expected:
        return False
    if (
        not isinstance(expected, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected)
        or _definition_digest(platform, definition) != expected
    ):
        raise UpdateError("the user service definition changed during this update")
    return True


def _check_ownership(platform: str, paths: Paths, previous: dict[str, Any]) -> None:
    """Recheck the verified generated unit before stopping or starting its processes."""
    if previous["daemon"] and not _unchanged_definition(
        previous, "daemon_definition", platform, service.AGENT
    ):
        # A candidate may fail or be interrupted before its first heartbeat. The
        # original verified unit still owns that launch, including its recovery.
        pid = service.unit_pid(platform)
        if pid is not None and pid != daemon(paths).get("pid"):
            raise UpdateError("the Enso service no longer owns this home's daemon")
    name = previous.get("viewer_service")
    if previous["viewer"] and name:
        viewer_unit = _viewer(name)
        if not _unchanged_definition(previous, "viewer_definition", platform, viewer_unit):
            pid = service.unit_pid(platform, definition=viewer_unit)
            viewer = web.status(paths)
            if pid is not None and (not viewer.running or pid != viewer.pid):
                raise UpdateError("the viewer service no longer owns this home's viewer")


def stop(paths: Paths, previous: dict[str, Any]) -> None:
    platform = _platform()
    _check_ownership(platform, paths, previous)
    name = previous.get("viewer_service")
    with _service_errors():
        if previous["daemon"]:
            service.stop(platform)
        if previous["viewer"] and name:
            service.stop(platform, definition=_viewer(name))
    if previous["viewer"] and not name:
        web.stop(paths)
    deadline = time.monotonic() + 30
    while receiver_active(paths) or web.status(paths).running:
        if time.monotonic() >= deadline:
            raise UpdateError("Enso processes did not stop; update will not change the home")
        time.sleep(0.1)


def start(paths: Paths, previous: dict[str, Any], binary: Path) -> None:
    platform = _platform()
    _check_ownership(platform, paths, previous)
    name = previous.get("viewer_service")
    with _service_errors():
        if previous["daemon"]:
            service.start(platform)
        if previous["viewer"] and name:
            service.start(platform, definition=_viewer(name))
    if not previous["viewer"] or name:
        return
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
