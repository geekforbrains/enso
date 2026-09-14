"""Updater service handoffs go through service.py and never touch the agent's unit."""

import hashlib
import os
import plistlib
import subprocess
import sys

import pytest

from enso import service, update_services, web
from enso.maintenance import UpdateError

OPERATION = "a" * 32
LABEL = f"com.enso.update.{OPERATION}"
HELPER = f"gui/{os.getuid()}/{LABEL}"


class Manager:
    """A fake launchd or systemd holding one unit: loaded or not, running a pid or not."""

    def __init__(self, platform, *, loaded=False, pid=None):
        self.platform = platform
        self.loaded = loaded
        self.pid = pid
        self.queries = []
        self.commands = []

    def query(self, args):
        self.queries.append(args)
        if "is-enabled" in args:
            return "enabled" if self.loaded else "disabled"
        if args[0] == "systemctl":
            return f"{self.pid or 0}\n"
        if not self.loaded:
            return ""
        return f"state = running\n\tpid = {self.pid}\n" if self.pid else "state = not running\n"

    def run(self, args, *, check=True):
        self.commands.append(args)
        action = args[1] if args[0] == "launchctl" else args[2]
        if action in {"bootout", "stop"}:
            self.pid = None
            self.loaded = action == "stop"  # systemd keeps a stopped unit enabled
        elif action in {"bootstrap", "kickstart", "start"}:
            self.loaded, self.pid = True, 4242
        return subprocess.CompletedProcess(args, 0, "", "")

    def command(self, args, **kwargs):
        self.run(args)
        return ""


def fake_manager(monkeypatch, platform, **state):
    fake = Manager(platform, **state)
    monkeypatch.setattr(sys, "platform", {"launchd": "darwin", "systemd": "linux"}[platform])
    monkeypatch.setattr(service, "_query", fake.query)
    monkeypatch.setattr(service, "_run", fake.run)
    monkeypatch.setattr(update_services, "run_command", fake.command)
    return fake


@pytest.fixture(params=["launchd", "systemd"])
def manager(monkeypatch, request):
    return fake_manager(monkeypatch, request.param)


def no_daemon(monkeypatch):
    """A home whose agent is stopped, so only the viewer's supervisor is in play."""
    monkeypatch.setattr(update_services, "daemon", lambda paths: {})
    monkeypatch.setattr(update_services, "receiver_active", lambda paths: False)


@pytest.mark.parametrize("loaded", [False, True])
def test_launchd_retry_reuses_an_idle_definition(enso_home, monkeypatch, loaded):
    fake = fake_manager(monkeypatch, "launchd", loaded=loaded)
    python = enso_home.runtime_dir / "releases/old/bin/python"
    update_services.launch(enso_home, OPERATION, python)
    unit = enso_home.runtime_dir / "operations" / OPERATION / "updater.plist"
    if loaded:
        assert fake.commands == [["launchctl", "kickstart", HELPER]]
        assert not unit.exists()
    else:
        assert fake.commands == [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(unit)]]
        definition = plistlib.loads(unit.read_bytes())
        assert definition["Label"] == LABEL
        assert definition["ProgramArguments"] == [
            str(python),
            "-m",
            "enso.cli",
            "update",
            "_run",
            OPERATION,
        ]
        assert definition["KeepAlive"] == {"SuccessfulExit": False}
    assert all(service.LAUNCHD_LABEL not in arg for call in fake.commands for arg in call)


def test_launch_never_kickstarts_a_live_helper(enso_home, monkeypatch):
    fake = fake_manager(monkeypatch, "launchd", loaded=True, pid=123)
    with pytest.raises(UpdateError, match="still running"):
        update_services.launch(enso_home, OPERATION, enso_home.home / "python")
    assert fake.commands == []


@pytest.mark.parametrize("pid", [None, 123])
def test_completed_helper_cleanup_skips_live_processes(enso_home, monkeypatch, pid):
    fake = fake_manager(monkeypatch, "launchd", loaded=True, pid=pid)
    update_services.cleanup_finished(enso_home, OPERATION)
    assert fake.commands == ([] if pid else [["launchctl", "bootout", HELPER]])


def test_unsupported_platform_is_reported_before_any_service_work(enso_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    for attempt in (
        lambda: update_services.discover(enso_home),
        lambda: update_services.launch(enso_home, OPERATION, enso_home.home / "python"),
    ):
        with pytest.raises(UpdateError, match="no service manager on win32"):
            attempt()


@pytest.mark.parametrize("action", ["stop", "start"])
def test_service_ownership_is_rechecked_after_staging(enso_home, monkeypatch, manager, action):
    manager.loaded, manager.pid = True, 456
    monkeypatch.setattr(update_services, "daemon", lambda paths: {"pid": 123})
    previous = {"daemon": True, "viewer": False}
    args = (enso_home, previous) + ((enso_home.home / "bin/enso",) if action == "start" else ())
    with pytest.raises(UpdateError, match="no longer owns"):
        getattr(update_services, action)(*args)
    assert manager.commands == []


@pytest.mark.parametrize("changed", [False, True])
def test_verified_service_can_be_recovered_before_candidate_writes_heartbeat(
    enso_home, monkeypatch, manager, changed
):
    manager.loaded, manager.pid = True, 456
    unit = service.unit_path(manager.platform)
    unit.parent.mkdir(parents=True)
    unit.write_text("verified agent definition")
    previous = {
        "daemon": True,
        "viewer": False,
        "daemon_definition": hashlib.sha256(unit.read_bytes()).hexdigest(),
    }
    no_daemon(monkeypatch)
    if changed:
        unit.write_text("changed while update pending")
        with pytest.raises(UpdateError, match="definition changed"):
            update_services.stop(enso_home, previous)
        assert manager.commands == []
    else:
        update_services.stop(enso_home, previous)
        assert manager.pid is None and len(manager.commands) == 1


@pytest.mark.parametrize("custom", [False, True])
def test_viewer_supervisor_is_discovered_stopped_and_restored(
    enso_home, monkeypatch, manager, custom
):
    platform = manager.platform
    name = (
        {"launchd": "com.cloud.viewer", "systemd": "cloud-viewer.service"}[platform]
        if custom
        else service.VIEWER.name(platform)
    )
    unit = service.unit_path(platform, definition=service.VIEWER.named(name))
    unit.parent.mkdir(parents=True)
    unit.write_text("unchanged viewer service definition")
    manager.loaded, manager.pid = True, 4242
    no_daemon(monkeypatch)
    monkeypatch.setattr(
        web,
        "status",
        lambda paths: web.Status(manager.pid is not None, manager.pid, "127.0.0.1", 9000),
    )

    previous = update_services.discover(enso_home, name if custom else "")
    assert previous["viewer_service"] == name and previous["viewer"] is True
    assert previous["viewer_definition"] == hashlib.sha256(unit.read_bytes()).hexdigest()
    update_services.stop(enso_home, previous)
    assert manager.pid is None
    update_services.start(enso_home, previous, enso_home.home / "bin/enso")
    assert manager.pid == 4242
    assert manager.commands[-1] == (
        ["systemctl", "--user", "start", name]
        if platform == "systemd"
        else ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(unit)]
    )
    # Recovery after a failed candidate uses the same verified supervisor even when
    # its process has exited; a changed definition must never be restarted.
    manager.pid = None
    update_services.start(enso_home, previous, enso_home.home / "old/bin/enso")
    assert manager.pid == 4242
    unit.write_text("changed while update pending")
    with pytest.raises(UpdateError, match="definition changed"):
        update_services.start(enso_home, previous, enso_home.home / "old/bin/enso")


def test_unproven_standard_viewer_is_not_treated_as_unsupervised(enso_home, monkeypatch, manager):
    unit = service.unit_path(manager.platform, definition=service.VIEWER)
    unit.parent.mkdir(parents=True)
    render = service.render_plist if manager.platform == "launchd" else service.render_unit
    unit.write_text(
        render(
            "/enso",
            {"ENSO_HOME": str(enso_home.home)},
            enso_home.web_log,
            definition=service.VIEWER,
        )
    )
    no_daemon(monkeypatch)
    monkeypatch.setattr(web, "status", lambda paths: web.Status(True, 123, "127.0.0.1", 8787))
    with pytest.raises(UpdateError, match="cannot confirm"):
        update_services.discover(enso_home)
    # Without a supervisor definition, the existing standalone path remains valid.
    unit.unlink()
    previous = update_services.discover(enso_home)
    assert previous["viewer"] and previous["viewer_service"] == ""
    with pytest.raises(UpdateError, match="does not own"):
        update_services.discover(enso_home, service.VIEWER.name(manager.platform))


def test_update_leaves_stopped_viewer_stopped(enso_home, monkeypatch, manager):
    no_daemon(monkeypatch)
    monkeypatch.setattr(web, "status", lambda paths: web.Status(False))
    previous = update_services.discover(enso_home)
    update_services.stop(enso_home, previous)
    update_services.start(enso_home, previous, enso_home.home / "bin/enso")
    assert manager.queries == [] and manager.commands == []
