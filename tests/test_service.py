"""Unit rendering, installation, status parsing, and the restart target rule."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from enso import service
from enso.config import Config, Paths, parse_config


@pytest.mark.parametrize(
    ("platform", "output", "expected"),
    [
        ("darwin", "\tpid = 4242\n", ["launchctl", "kickstart", "-k", "gui/UID/com.enso.agent"]),
        ("darwin", "", None),  # unit not loaded, even though a stale plist may exist
        ("darwin", "\tpid = 1\n", None),  # loaded, but it runs some other process
        ("linux", "4242\n", ["systemctl", "--user", "restart", "enso.service"]),
        ("linux", "0\n", None),
    ],
)
def test_restart_command_targets_only_the_unit_running_this_process(
    platform: str, output: str, expected: list[str] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service.shutil, "which", lambda name: "/bin/systemctl")
    if expected is not None:
        expected = [part.replace("UID", str(os.getuid())) for part in expected]
    assert service.restart_command(4242, query=lambda cmd: output, platform=platform) == expected


def test_units_carry_the_binary_provider_paths_and_home(
    enso_home: Paths, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    env = service.environment(enso_home, config, "/opt/enso/bin/enso")
    path = env["PATH"].split(":")
    assert path[0] == "/opt/enso/bin" and path.count("/usr/bin") == 1
    assert os.path.dirname(config.providers["claude"].path) in path
    assert path.index("/opt/homebrew/bin") < path.index(
        "/usr/local/bin"
    )  # the shell's PATH rides along
    assert env["ENSO_HOME"] == str(enso_home.home)  # the fixture exported a non-default home
    plist = service.render_plist("/opt/enso/bin/enso", env, enso_home.launchd_log)
    assert "<string>/opt/enso/bin/enso</string>" in plist
    assert f"<string>{enso_home.launchd_log}</string>" in plist
    unit = service.render_unit("/opt/enso/bin/enso", env, enso_home.launchd_log)
    assert 'ExecStart="/opt/enso/bin/enso" serve' in unit
    assert f'Environment="ENSO_HOME={enso_home.home}"' in unit


def _systemd_words(value: str) -> list[str]:
    """Read a systemd setting back the way it does: unquote, unescape, expand ``%%``."""
    return [word.replace("%%", "%") for word in shlex.split(value)]


def _plist_value(node: ElementTree.Element) -> object:
    """A plist element as Python: dicts and arrays recurse, everything else is its text."""
    if node.tag == "dict":
        keys, values = list(node)[::2], list(node)[1::2]
        return {key.text: _plist_value(value) for key, value in zip(keys, values, strict=True)}
    if node.tag == "array":
        return [_plist_value(child) for child in node]
    return node.text


def test_units_keep_awkward_paths_and_environment_values_whole() -> None:
    binary = '/opt/enso 100% tools/bin/dan"dy'
    log = Path("/var/log/enso logs/100% enso.log")
    env = {
        "HOME": "/Users/some one",
        "PATH": '/opt/a b/bin:/opt/c=d/bin:/opt/100%/bin:/opt/q"t/bin',
    }

    unit = service.render_unit(binary, env, log)
    settings = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    assert _systemd_words(settings["ExecStart"]) == [binary, "serve"]
    assert settings["StandardOutput"] == settings["StandardError"]
    assert settings["StandardOutput"].removeprefix("append:").replace("%%", "%") == str(log)
    assignments = [
        line.removeprefix("Environment=")
        for line in unit.splitlines()
        if line.startswith("Environment=")
    ]
    # One word per line is the whole point: an unquoted space would start a second one.
    assert [_systemd_words(line) for line in assignments] == [
        [f"{key}={value}"] for key, value in env.items()
    ]
    # Doubling is asserted on the raw text: reading a setting back undoes it, so a
    # round-trip alone holds just as well when the percent was never doubled at all.
    assert "100%% tools" in settings["ExecStart"]
    assert "100%% enso.log" in settings["StandardOutput"]
    assert "/opt/100%%/bin" in next(line for line in assignments if "PATH=" in line)

    rendered = ElementTree.fromstring(service.render_plist(binary, env, log))
    plist = _plist_value(rendered.find("dict"))
    assert isinstance(plist, dict)
    assert plist["ProgramArguments"] == [binary, "serve"]
    assert plist["StandardOutPath"] == str(log) and plist["StandardErrorPath"] == str(log)
    assert plist["EnvironmentVariables"] == env


def test_unresolvable_bare_provider_names_never_put_dot_on_the_service_path(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_config["providers"]["grok"]["path"] = "no-such-cli-anywhere"
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    env = service.environment(enso_home, config, "/opt/enso/bin/enso")
    path = env["PATH"].split(":")
    assert "." not in path and "" not in path
    assert path[0] == "/opt/enso/bin"
    # resolvable providers still ride along; the unresolvable one contributes nothing
    assert str(Path(config.providers["claude"].path).parent) in path
    assert path.index("/opt/homebrew/bin") < path.index("/usr/bin")


@pytest.mark.parametrize(
    ("platform", "output", "loaded", "pid"),
    [
        ("launchd", "state = running\n\tpid = 77\n", True, 77),
        ("launchd", "state = waiting\n", True, None),
        ("launchd", "", False, None),
        ("systemd", "0\n", False, None),
        ("systemd", "88\n", False, 88),
    ],
)
def test_status_parses_the_service_manager(
    platform: str, output: str, loaded: bool, pid: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_query", lambda cmd: output if "is-enabled" not in cmd else "")
    status = service.status(platform)
    assert (status.loaded, status.pid, status.running) == (loaded, pid, pid is not None)


def _scratch_install(monkeypatch: pytest.MonkeyPatch, unit: Path) -> list[list[str]]:
    """Point `install` at a scratch unit and capture the service-manager commands it runs."""
    commands: list[list[str]] = []

    def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(service, "unit_path", lambda platform=None, **kwargs: unit)
    monkeypatch.setattr(service, "enso_binary", lambda: "/opt/enso/bin/enso")
    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "_query", lambda cmd: "")  # bootout stops waiting at once
    return commands


def test_install_writes_the_agent_and_touches_no_other_label(
    enso_home: Paths, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = tmp_path / "LaunchAgents" / "com.enso.agent.plist"
    unit.parent.mkdir(parents=True)
    stale = unit.parent / "com.enso.web.plist"  # v2's dashboard agent, if one was ever installed
    stale.write_text("v2")
    commands = _scratch_install(monkeypatch, unit)

    done = service.install(enso_home, config, "launchd")

    assert "<string>/opt/enso/bin/enso</string>" in unit.read_text()
    assert [cmd[1] for cmd in commands] == ["bootout", "enable", "bootstrap"]
    # pre-1.0 has no migration path, so install owns its own label and nothing else
    assert not any("com.enso.web" in part for cmd in commands for part in cmd)
    assert stale.read_text() == "v2"
    assert done == [
        f"wrote {unit} running /opt/enso/bin/enso serve",
        "started com.enso.agent",
    ]


def test_install_reloads_and_restarts_the_systemd_unit(
    enso_home: Paths, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = tmp_path / "systemd" / "enso.service"  # install creates the directory
    commands = _scratch_install(monkeypatch, unit)

    done = service.install(enso_home, config, "systemd")

    assert 'ExecStart="/opt/enso/bin/enso" serve' in unit.read_text()
    assert [cmd[2:] for cmd in commands] == [
        ["daemon-reload"],
        ["enable", "--now", "enso.service"],
        ["restart", "enso.service"],
    ]
    assert done[-1] == "started enso.service"
