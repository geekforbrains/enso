"""Viewer service lifecycle through fake launchd/systemd, never the user's services."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enso import doctor, maintenance, service, updates, web
from enso.cli import app
from enso.web import service as viewer_service


class Supervisor:
    def __init__(self, paths, platform):
        self.paths = paths
        self.platform = platform
        self.unit = service.unit_path(platform, definition=service.VIEWER)
        self.pid = None
        self.viewer_pid = None
        self.loaded = False
        self.commands = []
        self.events = []
        self.binary = paths.home / "bin/enso"
        self.binary.parent.mkdir()
        self.binary.touch()

    def query(self, args):
        if args[0] == "launchctl":
            return f"state = running\n pid = {self.pid}\n" if self.loaded else ""
        if "is-enabled" in args:
            return "enabled" if self.loaded else "disabled"
        return str(self.pid or 0)

    def run(self, args, *, check=True):
        self.commands.append(args)
        action = args[1] if args[0] == "launchctl" else args[2]
        self.events.append(action)
        if action in {"bootout", "stop", "disable"}:
            if self.viewer_pid == self.pid:
                self.viewer_pid = None
            self.pid = None
            if action != "stop":
                self.loaded = False
        elif action in {"bootstrap", "kickstart", "start", "restart"} or (
            action == "enable" and "--now" in args
        ):
            self.loaded = True
            self.pid = self.viewer_pid = 4242
        return subprocess.CompletedProcess(args, 0, "", "")

    def status(self, paths):
        return web.Status(self.viewer_pid is not None, self.viewer_pid, "127.0.0.1", 8787)

    def stop_viewer(self, paths):
        self.events.append("stop process")
        self.viewer_pid = None
        return "stopped"

    def write_unit(self, home=None):
        self.unit.parent.mkdir(parents=True, exist_ok=True)
        render = service.render_plist if self.platform == "launchd" else service.render_unit
        self.unit.write_text(
            render(
                str(self.binary),
                {"ENSO_HOME": str(home or self.paths.home)},
                self.paths.web_service_log,
                definition=service.VIEWER,
            )
        )


@pytest.fixture(params=["launchd", "systemd"])
def supervisor(enso_home, monkeypatch, request):
    fake = Supervisor(enso_home, request.param)
    monkeypatch.setattr(service, "platform_name", lambda: fake.platform)
    monkeypatch.setattr(service, "_query", fake.query)
    monkeypatch.setattr(service, "_run", fake.run)
    monkeypatch.setattr(service, "enso_binary", lambda: str(fake.binary))
    monkeypatch.setattr(web, "missing_extra", lambda: [])
    monkeypatch.setattr(web, "status", fake.status)
    monkeypatch.setattr(web, "stop", fake.stop_viewer)
    monkeypatch.setattr(web, "_reachable", lambda bind: True)
    return fake


def invoke(*args):
    result = CliRunner().invoke(app, ["web", *args])
    assert result.exit_code == 0, (result.output, result.exception)
    return result.output


def test_install_adopts_standalone_reinstalls_and_uninstalls_only_viewer(supervisor):
    fake = supervisor
    agent = service.unit_path(fake.platform)
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text("agent must stay unchanged")
    fake.paths.web_log.write_text("previous viewer output")
    fake.viewer_pid = 123  # an existing standalone viewer is stopped before adoption

    output = invoke("install")

    assert "listening on http://127.0.0.1:8787" in output
    assert fake.events.index("stop process") < fake.events.index(
        "bootstrap" if fake.platform == "launchd" else "daemon-reload"
    )
    unit = fake.unit.read_bytes()
    state = viewer_service.find(fake.paths)
    assert state is not None and state.pid == fake.viewer_pid == 4242
    assert viewer_service.unit_home(state) == fake.paths.home.resolve()
    if fake.platform == "launchd":
        data = plistlib.loads(unit)
        assert data["ProgramArguments"] == [str(fake.binary), "web", "start", "--foreground"]
        assert data["KeepAlive"] is True and data["RunAtLoad"] is True
        assert data["StandardOutPath"] == str(fake.paths.web_service_log)
    else:
        assert f'ExecStart="{fake.binary}" web start --foreground' in unit.decode()
        assert "Restart=always" in unit.decode() and "WantedBy=default.target" in unit.decode()
        assert f"StandardOutput=append:{fake.paths.web_service_log}" in unit.decode()
    assert doctor._viewer_service(fake.paths).status == "ok"

    invoke("install")  # generated and compatible handwritten definitions are adoptable
    assert fake.unit.read_bytes() == unit
    assert invoke("status").startswith("running pid=4242")
    assert "already running" in invoke("start")
    invoke("stop")
    assert fake.pid is None and fake.viewer_pid is None and fake.unit.exists()
    assert doctor._viewer_service(fake.paths).status == "error"
    invoke("stop")  # stopped services are safe to stop again
    invoke("start")
    assert fake.pid == 4242
    invoke("uninstall")
    assert fake.pid is None and not fake.unit.exists()
    assert "not installed" in invoke("uninstall")
    assert doctor._viewer_service(fake.paths).note == "not installed (optional)"
    assert agent.read_text() == "agent must stay unchanged"
    assert fake.paths.web_log.read_text() == "previous viewer output"
    assert fake.paths.workspace("default").is_dir()
    assert not fake.paths.db.exists()
    assert all(service.LAUNCHD_LABEL not in " ".join(cmd) for cmd in fake.commands)
    assert all(service.SYSTEMD_UNIT not in cmd for cmd in fake.commands)


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_another_home_unit_is_never_replaced_or_stopped(supervisor, action):
    fake = supervisor
    fake.write_unit(fake.paths.home.parent / "another enso")
    before = fake.unit.read_bytes()

    result = CliRunner().invoke(app, ["web", action])

    assert result.exit_code == 1 and "another Enso home" in result.output
    assert fake.unit.read_bytes() == before and fake.events == []
    assert viewer_service.find(fake.paths) is None
    assert doctor._viewer_service(fake.paths).note == "installed for another home"


def test_existing_unit_requires_matching_process_ownership(supervisor):
    fake = supervisor
    fake.write_unit()
    fake.loaded = True
    fake.pid = 4242
    fake.viewer_pid = 9999
    before = fake.unit.read_bytes()
    for action in ("install", "uninstall", "start", "stop"):
        result = CliRunner().invoke(app, ["web", action])
        assert result.exit_code == 1 and "does not own" in result.output
    assert fake.unit.read_bytes() == before and fake.events == []


@pytest.mark.parametrize("kind", ["old dashboard", "symlink", "relative home", "override"])
def test_unverifiable_units_are_left_untouched(supervisor, kind):
    fake = supervisor
    fake.write_unit(Path("relative") if kind == "relative home" else None)
    if kind == "old dashboard":
        fake.unit.write_text("an old dashboard definition")
    elif kind == "symlink":
        target = fake.unit.with_suffix(".original")
        fake.unit.rename(target)
        fake.unit.symlink_to(target)
    elif kind == "override":
        if fake.platform == "launchd":
            data = plistlib.loads(fake.unit.read_bytes())
            data["Program"] = "/another/program"
            fake.unit.write_bytes(plistlib.dumps(data))
        else:
            fake.unit.with_name(fake.unit.name + ".d").mkdir()
    before = fake.unit.read_bytes()
    for action in ("install", "uninstall"):
        result = CliRunner().invoke(app, ["web", action])
        assert result.exit_code == 1 and "cannot verify" in result.output
    assert fake.unit.read_bytes() == before and fake.events == []


def test_managed_install_uses_stable_launcher_and_requires_its_web_extra(supervisor, monkeypatch):
    fake = supervisor
    fake.paths.runtime_dir.mkdir()
    (fake.paths.runtime_dir / "install.json").write_text("{}")
    stable = fake.paths.home / "stable/enso"
    stable.parent.mkdir()
    stable.touch()
    receipt = {"extras": [], "bin_dir": str(stable.parent)}
    monkeypatch.setattr(updates, "installed", lambda paths: receipt)

    def unexpected_binary():
        pytest.fail("a managed installation must not depend on the enso found on PATH")

    monkeypatch.setattr(service, "enso_binary", unexpected_binary)
    result = CliRunner().invoke(app, ["web", "install"])
    assert result.exit_code == 1 and "managed release needs the web extra" in result.output
    assert fake.events == [] and not fake.unit.exists()
    receipt["extras"] = ["web"]
    invoke("install")
    assert str(stable) in fake.unit.read_text() and str(fake.binary) not in fake.unit.read_text()


def test_missing_extra_and_failed_start_are_actionable(supervisor, monkeypatch):
    fake = supervisor
    monkeypatch.setattr(web, "missing_extra", lambda: ["aiohttp"])
    result = CliRunner().invoke(app, ["web", "install"])
    assert result.exit_code == 1 and web.INSTALL_HINT in result.output
    assert fake.events == [] and not fake.unit.exists()
    monkeypatch.setattr(web, "missing_extra", lambda: [])
    monkeypatch.setattr(web, "START_TIMEOUT", 0)
    result = CliRunner().invoke(app, ["web", "install"])
    assert result.exit_code == 1 and "did not become ready" in result.output
    assert str(fake.paths.web_service_log) in result.output
    monkeypatch.setattr(web, "missing_extra", lambda: ["aiohttp"])
    invoke("stop")
    invoke("uninstall")  # cleanup never requires the optional dependencies


def test_supervised_bind_settings_and_foreground_entrypoint(supervisor, monkeypatch):
    fake = supervisor
    fake.write_unit()
    result = CliRunner().invoke(app, ["web", "start", "--port", "9999"])
    assert result.exit_code == 1 and "config.json" in result.output
    assert fake.events == []
    calls = []
    monkeypatch.setattr(web, "start", lambda paths, **kwargs: calls.append(kwargs) or "foreground")
    assert "foreground" in invoke("start", "--foreground", "--port", "9999")
    assert calls == [{"host": None, "port": 9999, "foreground": True}]
    assert fake.events == []


@pytest.mark.parametrize("action", ["install", "uninstall", "start", "stop"])
def test_viewer_lifecycle_waits_for_pending_updates(supervisor, action, monkeypatch):
    fake = supervisor
    fake.paths.runtime_dir.mkdir()
    (fake.paths.runtime_dir / "install.json").write_text("{}")
    maintenance.write_json(fake.paths.update_state, {"id": "op", "status": "staging"})
    result = CliRunner().invoke(app, ["web", action])
    assert result.exit_code == 1 and "Enso is updating" in result.output
    assert fake.events == []
    # The supervisor's foreground entrypoint must not acquire its parent's lock.
    monkeypatch.setattr(web, "start", lambda paths, **kwargs: "foreground")
    with maintenance.lock(fake.paths):
        assert "foreground" in invoke("start", "--foreground")


def test_systemd_owner_parser_preserves_spaces_and_percents(enso_home):
    path = enso_home.home / "enso-web.service"
    home = enso_home.home / '100% space "quote"'
    path.write_text(
        service.render_unit(
            "/opt/enso", {"ENSO_HOME": str(home)}, enso_home.web_log, definition=service.VIEWER
        )
    )
    state = service.Status("systemd", path, True, False, None)
    assert viewer_service.unit_home(state) == home
    path.write_text(
        '[Service]\nExecStart=/opt/enso web start --foreground\nEnvironment="ENSO_HOME=%h/.enso"\n'
    )
    with pytest.raises(service.ServiceError, match="cannot verify"):
        viewer_service.unit_home(state)


def test_failed_systemd_uninstall_preserves_definition(enso_home, monkeypatch):
    unit = service.unit_path("systemd", definition=service.VIEWER)
    unit.parent.mkdir(parents=True)
    unit.write_text("unit")

    def failed(args, **kwargs):
        raise service.ServiceError("permission denied")

    monkeypatch.setattr(service, "_run", failed)
    with pytest.raises(service.ServiceError, match="permission denied"):
        service.uninstall("systemd", definition=service.VIEWER)
    assert unit.read_text() == "unit"


def test_launchd_unload_timeout_preserves_definition(enso_home, monkeypatch):
    unit = service.unit_path("launchd", definition=service.VIEWER)
    unit.parent.mkdir(parents=True)
    unit.write_text("original unit")
    monkeypatch.setattr(service, "_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_query", lambda args: "state = running")
    monkeypatch.setattr(service, "BOOTOUT_WAIT_SECONDS", 0)
    with pytest.raises(service.ServiceError, match="did not unload"):
        service.install(enso_home, None, "launchd", definition=service.VIEWER, binary="/enso")
    assert unit.read_text() == "original unit"
