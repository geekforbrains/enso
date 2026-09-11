"""Updater service retries never reload or kill the normal Enso service."""

import os
import plistlib

import pytest

from enso import service, update_services, web
from enso.maintenance import UpdateError

OPERATION = "a" * 32


@pytest.mark.parametrize("loaded", [None, "state = not running\nlast exit code = 0\n"])
def test_launchd_retry_reuses_an_idle_definition(enso_home, monkeypatch, loaded):
    monkeypatch.setattr(update_services.sys, "platform", "darwin")
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[1] == "print":
            if loaded is None:
                raise UpdateError("service absent")
            return loaded
        return ""

    monkeypatch.setattr(update_services, "run_command", command)
    python = enso_home.runtime_dir / "releases/old/bin/python"
    update_services.launch(enso_home, OPERATION, python)
    target = f"gui/{os.getuid()}/com.enso.update.{OPERATION}"
    if loaded is None:
        unit = enso_home.runtime_dir / "operations" / OPERATION / "updater.plist"
        assert calls[-1] == ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(unit)]
        definition = plistlib.loads(unit.read_bytes())
        assert definition["ProgramArguments"] == [
            str(python),
            "-m",
            "enso.cli",
            "update",
            "_run",
            OPERATION,
        ]
        assert definition["KeepAlive"] == {"SuccessfulExit": False}
    else:
        assert calls[-1] == ["launchctl", "kickstart", target]
        assert not (enso_home.runtime_dir / "operations").exists()
    assert all("com.enso.agent" not in arg for call in calls for arg in call)


def test_launchd_recovery_never_kills_a_live_helper(enso_home, monkeypatch):
    monkeypatch.setattr(update_services.sys, "platform", "darwin")
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return "state = running\n\tpid = 123\n"

    monkeypatch.setattr(update_services, "run_command", command)
    with pytest.raises(UpdateError, match="still running"):
        update_services.launch(enso_home, OPERATION, enso_home.home / "python")
    assert [call[1] for call in calls] == ["print"]


@pytest.mark.parametrize("running", [False, True])
def test_completed_helper_cleanup_is_scoped_and_skips_live_processes(
    enso_home,
    monkeypatch,
    running,
):
    monkeypatch.setattr(update_services.sys, "platform", "darwin")
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return "pid = 123\n" if running else "state = not running\n"

    monkeypatch.setattr(update_services, "run_command", command)
    update_services.cleanup_finished(enso_home, OPERATION)
    assert [call[1] for call in calls] == (["print"] if running else ["print", "bootout"])
    assert all(call[-1] == f"gui/{os.getuid()}/com.enso.update.{OPERATION}" for call in calls)


@pytest.mark.parametrize("action", ["stop", "start"])
def test_service_ownership_is_rechecked_after_staging(enso_home, monkeypatch, action):
    monkeypatch.setattr(update_services, "daemon", lambda paths: {"pid": 123})
    monkeypatch.setattr(update_services, "_unit_pid", lambda paths, name: 456)
    commands = []
    monkeypatch.setattr(update_services.service, "stop", lambda: commands.append("stop"))
    monkeypatch.setattr(update_services.service, "start", lambda: commands.append("start"))
    previous = {"daemon": True, "viewer": False}
    args = (enso_home, previous)
    if action == "start":
        args += (enso_home.home / "bin/enso",)
    with pytest.raises(UpdateError, match="no longer owns"):
        getattr(update_services, action)(*args)
    assert commands == []


@pytest.mark.parametrize("changed", [False, True])
def test_verified_service_can_be_recovered_before_candidate_writes_heartbeat(
    enso_home, monkeypatch, tmp_path, changed
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(update_services.sys, "platform", "linux")
    directory = tmp_path / ".config/systemd/user"
    directory.mkdir(parents=True)
    definition = directory / "enso.service"
    definition.write_text("[Service]\nExecStart=/test/enso serve\n")
    previous = {
        "daemon": True,
        "viewer": False,
        "daemon_definition": update_services._definition_digest("enso.service"),
    }
    monkeypatch.setattr(update_services, "daemon", lambda paths: {})
    monkeypatch.setattr(update_services, "_unit_pid", lambda paths, name: 456)
    if changed:
        definition.write_text("[Service]\nExecStart=/another/home/enso serve\n")
        with pytest.raises(UpdateError, match="definition changed"):
            update_services._check_ownership(enso_home, previous)
    else:
        update_services._check_ownership(enso_home, previous)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("custom", [False, True])
def test_viewer_supervisor_is_discovered_stopped_and_restored(
    enso_home, monkeypatch, platform, custom
):
    monkeypatch.setattr(update_services.sys, "platform", platform)
    manager = "launchd" if platform == "darwin" else "systemd"
    name = (
        ("cloud-viewer.service" if custom else service.VIEWER.unit)
        if manager == "systemd"
        else ("com.cloud.viewer" if custom else service.VIEWER.label)
    )
    unit = service.unit_path(manager, definition=service.VIEWER)
    unit = unit.with_name(name if manager == "systemd" else f"{name}.plist")
    unit.parent.mkdir(parents=True)
    unit.write_text("unchanged viewer service definition")
    state = {"pid": 4242}
    commands = []

    def command(args, **kwargs):
        commands.append(args)
        if "show" in args or "print" in args:
            return f"pid = {state['pid']}" if manager == "launchd" else str(state["pid"] or 0)
        if "stop" in args or "bootout" in args:
            state["pid"] = None
        elif "start" in args or "bootstrap" in args:
            state["pid"] = 4242
        return ""

    monkeypatch.setattr(update_services, "run_command", command)
    monkeypatch.setattr(update_services, "daemon", lambda paths: {})
    monkeypatch.setattr(update_services, "receiver_active", lambda paths: False)
    monkeypatch.setattr(
        web,
        "status",
        lambda paths: web.Status(state["pid"] is not None, state["pid"], "127.0.0.1", 9000),
    )
    previous = update_services.discover(enso_home, name if custom else "")
    assert previous["viewer_service"] == name and previous["viewer"] is True
    assert previous["viewer_definition"] == update_services._definition_digest(name)
    update_services.stop(enso_home, previous)
    assert state["pid"] is None
    update_services.start(enso_home, previous, enso_home.home / "bin/enso")
    assert state["pid"] == 4242
    assert commands[-1] == (
        ["systemctl", "--user", "start", name]
        if manager == "systemd"
        else ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(unit)]
    )
    # Recovery after a failed candidate uses the same verified supervisor even when
    # its process has exited; a changed definition must never be restarted.
    state["pid"] = None
    update_services.start(enso_home, previous, enso_home.home / "old/bin/enso")
    assert state["pid"] == 4242
    unit.write_text("changed while update pending")
    with pytest.raises(UpdateError, match="definition changed"):
        update_services.start(enso_home, previous, enso_home.home / "old/bin/enso")


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_unproven_standard_viewer_is_not_treated_as_unsupervised(enso_home, monkeypatch, platform):
    monkeypatch.setattr(update_services.sys, "platform", platform)
    manager = "launchd" if platform == "darwin" else "systemd"
    unit = service.unit_path(manager, definition=service.VIEWER)
    unit.parent.mkdir(parents=True)
    render = service.render_plist if manager == "launchd" else service.render_unit
    unit.write_text(
        render(
            "/enso",
            {"ENSO_HOME": str(enso_home.home)},
            enso_home.web_log,
            definition=service.VIEWER,
        )
    )
    monkeypatch.setattr(update_services, "daemon", lambda paths: {})
    monkeypatch.setattr(update_services, "receiver_active", lambda paths: False)
    monkeypatch.setattr(web, "status", lambda paths: web.Status(True, 123, "127.0.0.1", 8787))

    def unavailable(*args, **kwargs):
        raise UpdateError("service query failed")

    monkeypatch.setattr(update_services, "run_command", unavailable)
    with pytest.raises(UpdateError, match="cannot confirm"):
        update_services.discover(enso_home)
    # Without a supervisor definition, the existing standalone path remains valid.
    unit.unlink()
    previous = update_services.discover(enso_home)
    assert previous["viewer"] and previous["viewer_service"] == ""
    with pytest.raises(UpdateError, match="does not own"):
        update_services.discover(enso_home, service.VIEWER.name(manager))


def test_update_leaves_stopped_viewer_stopped(enso_home, monkeypatch):
    monkeypatch.setattr(update_services, "daemon", lambda paths: {})
    monkeypatch.setattr(update_services, "receiver_active", lambda paths: False)
    monkeypatch.setattr(web, "status", lambda paths: web.Status(False))

    def unexpected(*args, **kwargs):
        pytest.fail("a stopped viewer must not query, stop, or start its supervisor")

    monkeypatch.setattr(update_services, "run_command", unexpected)
    previous = update_services.discover(enso_home)
    update_services.stop(enso_home, previous)
    update_services.start(enso_home, previous, enso_home.home / "bin/enso")
