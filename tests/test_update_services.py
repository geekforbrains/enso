"""Updater service retries never reload or kill the normal Enso service."""

import os
import plistlib

import pytest

from enso import update_services
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
