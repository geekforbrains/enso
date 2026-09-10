"""The optional browser helper uses isolated profiles and never trusts a stale PID or port."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import types
from pathlib import Path

import pytest

from enso import skills

SKILL = Path(__file__).parents[1] / "src/enso/bundled/skills/enso-browser"


@pytest.fixture
def browser(monkeypatch):
    module = types.ModuleType("enso_browser_test")
    module.__file__ = str(SKILL / "scripts/browser.py")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(Path(module.__file__).read_text(), module.__file__, "exec"), module.__dict__)
    return module


@pytest.fixture
def profile(browser, tmp_path):
    value = browser.Profile(tmp_path / "enso")
    value.create()
    return value


def running_state(browser, profile, *, pid=42):
    owner = "a" * 32
    command = f"/chrome --user-data-dir={profile.data} --enso-browser-owner={owner} about:blank"
    return browser.State(
        pid, "Mon Sep 7 12:00:00 2026", command, owner, 34567, "/devtools/browser/id"
    )


def install_fake_mcp(browser, profile):
    package = profile.root / "tooling/node_modules/@playwright/mcp"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": browser.MCP_VERSION}))
    (package / "cli.js").write_text("// fake server")
    return package


def test_skill_frontmatter_is_valid_and_support_files_are_present():
    skill = skills._read(SKILL, "enso")
    assert skill.ok and skill.name == "enso-browser"
    assert (SKILL / "references/setup.md").is_file()
    assert (SKILL / "scripts/browser.py").is_file()


def test_create_defaults_to_private_profile_without_external_tools(
    browser, tmp_path, monkeypatch, capsys
):
    home = tmp_path / "new-home"
    monkeypatch.setenv("ENSO_HOME", str(home))
    monkeypatch.setattr(
        browser.subprocess, "Popen", lambda *a, **k: pytest.fail("launched a process")
    )
    assert browser.main(["create"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == "default"
    profile = browser.Profile(home)
    (profile.data / "user-content").write_text("keep")
    assert browser.main(["create"]) == 0
    assert (profile.data / "user-content").read_text() == "keep"
    for path in (profile.root, profile.data, profile.output, profile.state.parent):
        assert path.stat().st_mode & 0o777 == 0o700
    lock = profile.state.with_suffix(".lifecycle.lock")
    assert lock.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "name", ["../outside", "", "Work", "-work", "work--two", "work/next", "a" * 49]
)
def test_rejects_invalid_profile_names_before_creating_files(browser, tmp_path, name):
    with pytest.raises(browser.BrowserError, match="profile must"):
        browser.Profile(tmp_path / "enso", name)
    assert not (tmp_path / "enso").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "browser",
        "browser/profiles",
        "browser/profiles/default",
        "browser/output/default",
        "browser/state",
        "browser/tooling",
    ],
)
def test_rejects_symlinked_browser_directories(browser, tmp_path, relative):
    home = tmp_path / "enso"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / relative
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(browser.BrowserError, match="symbolic link"):
        browser.Profile(home).create()
    assert not list(outside.iterdir())


def test_read_only_commands_do_not_initialize_home(browser, tmp_path, monkeypatch, capsys):
    home = tmp_path / "new-home"
    monkeypatch.setenv("ENSO_HOME", str(home))
    for command in (["status"], ["list"], ["mcp", "work", "--print-config"]):
        assert browser.main(command) == 0
        json.loads(capsys.readouterr().out)
    assert not home.exists()


def test_registration_carries_exact_home_profile_and_stable_managed_python(browser, profile):
    python = profile.home / "runtime/current/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    registration = browser.registration(profile)["mcpServers"]["enso-browser-default"]
    assert registration["command"] == str(python)
    assert registration["env"] == {"ENSO_HOME": str(profile.home)}
    assert registration["args"] == [str(SKILL / "scripts/browser.py"), "mcp", "default"]


def test_registration_preserves_chrome_override(browser, profile, monkeypatch):
    monkeypatch.setenv("ENSO_BROWSER_CHROME", "/custom/Google Chrome")
    registration = browser.registration(profile)["mcpServers"]["enso-browser-default"]
    assert registration["env"]["ENSO_BROWSER_CHROME"] == "/custom/Google Chrome"


def test_create_reports_permission_denied_without_traceback(browser, profile, monkeypatch, capsys):
    monkeypatch.setenv("ENSO_HOME", str(profile.home))

    def deny(self):
        raise PermissionError("profile is not writable")

    monkeypatch.setattr(browser.Profile, "create", deny)
    assert browser.main(["create"]) == 1
    captured = capsys.readouterr()
    assert not captured.out and captured.err == "enso-browser: profile is not writable\n"


@pytest.mark.parametrize(
    "url",
    [
        "--no-sandbox",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@example.com",
        "https://:password@example.com",
        "https://@example.com",
        "https://example.com:notaport",
        "https://example.com:99999",
        "https://example.com\n--flag",
    ],
)
def test_rejects_unsafe_url_without_launch_or_traceback(
    browser, tmp_path, monkeypatch, capsys, url
):
    home = tmp_path / "enso"
    monkeypatch.setenv("ENSO_HOME", str(home))
    assert browser.main(["open", f"--url={url}"]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "URL must be" in captured.err and "Traceback" not in captured.err
    assert not home.exists()


def test_state_is_private_atomic_and_symlink_protected(browser, profile, tmp_path):
    state = running_state(browser, profile)
    browser.write_state(profile, state)
    assert browser.read_state(profile) == state
    assert profile.state.stat().st_mode & 0o777 == 0o600
    profile.state.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    profile.state.symlink_to(outside)
    with pytest.raises(browser.BrowserError, match="symbolic link"):
        browser.write_state(profile, state)
    with pytest.raises(browser.BrowserError, match="symbolic link"):
        browser.read_state(profile)
    assert outside.read_text() == "keep"


def test_malformed_or_oversized_state_is_not_accepted(browser, profile):
    for value in ('{"pid": 42}', "x" * (browser.MAX_RESPONSE + 1)):
        profile.state.write_text(value)
        with pytest.raises(browser.BrowserError):
            browser.read_state(profile)


def test_process_identity_is_checked_with_bounded_command(browser, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "Mon Sep  7 12:00:00 2026 /chrome --flag\n")

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    assert browser.process_identity(42) == ("Mon Sep 7 12:00:00 2026", "/chrome --flag")
    assert calls[0][0] == ["ps", "-ww", "-p", "42", "-o", "lstart=", "-o", "args="]
    assert calls[0][1]["timeout"] == 2 and calls[0][1]["cwd"] == "/"


@pytest.mark.parametrize("mismatch", ["started", "command", "profile", "owner"])
def test_stop_never_signals_a_recycled_pid_or_different_profile(
    browser, profile, monkeypatch, mismatch
):
    state = running_state(browser, profile)
    identity = (state.started, state.command)
    if mismatch == "started":
        identity = ("different start", state.command)
    elif mismatch == "command":
        identity = (state.started, "/some/other/app")
    elif mismatch == "profile":
        state = browser.replace(
            state, command=state.command.replace(str(profile.data), str(profile.data) + "-other")
        )
        identity = (state.started, state.command)
    else:
        state = browser.replace(state, command=state.command.replace(state.owner, "b" * 32))
        identity = (state.started, state.command)
    browser.write_state(profile, state)
    monkeypatch.setattr(browser, "process_identity", lambda pid: identity)
    monkeypatch.setattr(
        browser.os, "kill", lambda *args: pytest.fail("signaled an unowned process")
    )
    with pytest.raises(browser.BrowserError, match="identity no longer matches"):
        browser.stop(profile)
    assert profile.state.exists()


def test_stop_signals_only_owned_pid_and_preserves_profile_data(browser, profile, monkeypatch):
    state = running_state(browser, profile)
    browser.write_state(profile, state)
    (profile.data / "Cookies").write_text("saved login")
    live = {state.pid: (state.started, state.command)}
    calls = []
    monkeypatch.setattr(browser, "process_identity", lambda pid: live.get(pid))

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        live.pop(pid)

    monkeypatch.setattr(browser.os, "kill", fake_kill)
    assert browser.stop(profile) == "stopped"
    assert calls == [(state.pid, signal.SIGTERM)]
    assert not profile.state.exists()
    assert (profile.data / "Cookies").read_text() == "saved login"


def test_lifecycle_and_mcp_locks_fail_promptly_and_release(browser, profile):
    for kind in ("lifecycle", "mcp"):
        with (
            browser.profile_lock(profile, kind),
            pytest.raises(browser.BrowserError, match="another"),
            browser.profile_lock(profile, kind),
        ):
            pytest.fail("concurrent profile lock succeeded")
        with browser.profile_lock(profile, kind):
            pass


def test_endpoint_must_match_process_port_file_and_websocket(browser, profile, monkeypatch):
    state = running_state(browser, profile)
    monkeypatch.setattr(browser, "process_identity", lambda pid: (state.started, state.command))
    port_file = profile.data / "DevToolsActivePort"
    port_file.write_text(f"{state.port}\n{state.websocket}\n")
    monkeypatch.setattr(
        browser,
        "request",
        lambda *a, **k: {"webSocketDebuggerUrl": f"ws://127.0.0.1:{state.port}{state.websocket}"},
    )
    assert browser.endpoint(profile, state) == state
    monkeypatch.setattr(
        browser,
        "request",
        lambda *a, **k: {
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{state.port}/devtools/browser/other"
        },
    )
    with pytest.raises(browser.BrowserError, match="does not match"):
        browser.endpoint(profile, state)
    port_file.write_text("45678\n/devtools/browser/id\n")
    with pytest.raises(browser.BrowserError, match="identity changed"):
        browser.endpoint(profile, state)


@pytest.mark.parametrize(
    "response_status,body",
    [(200, b'{"Browser": "Chrome"}'), (302, b"redirect"), (200, b"x" * 65537)],
)
def test_http_boundary_is_loopback_bounded_and_does_not_follow_redirects(
    browser, monkeypatch, response_status, body
):
    calls = []

    class Response:
        status = response_status

        def read(self, size):
            calls.append(("read", size))
            return body[:size]

    class Connection:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def request(self, method, path):
            calls.append(("request", method, path))

        def getresponse(self):
            return Response()

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(browser.http.client, "HTTPConnection", Connection)
    if response_status == 200 and len(body) <= browser.MAX_RESPONSE:
        assert browser.request(34567, "/json/version") == {"Browser": "Chrome"}
    else:
        with pytest.raises(browser.BrowserError, match="invalid debugging response"):
            browser.request(34567, "/json/version")
    assert calls == [
        ("connect", "127.0.0.1", 34567, 1),
        ("request", "GET", "/json/version"),
        ("read", browser.MAX_RESPONSE + 1),
        ("close",),
    ]


def test_start_reuses_owned_browser_without_launching_or_closing_tabs(
    browser, profile, monkeypatch
):
    state = running_state(browser, profile)
    browser.write_state(profile, state)
    monkeypatch.setattr(browser, "process_identity", lambda pid: (state.started, state.command))
    monkeypatch.setattr(browser, "endpoint", lambda p, s: state)
    monkeypatch.setattr(
        browser.subprocess, "Popen", lambda *a, **k: pytest.fail("launched a second Chrome")
    )
    assert browser.start(profile) == state


def test_start_refuses_an_unmanaged_profile_lock(browser, profile, monkeypatch):
    (profile.data / "SingletonLock").symlink_to("host-123")
    monkeypatch.setattr(
        browser.subprocess, "Popen", lambda *a, **k: pytest.fail("launched another Chrome")
    )
    with pytest.raises(browser.BrowserError, match="locked by Chrome"):
        browser.start(profile)


def test_start_is_detached_uses_ephemeral_port_and_keeps_identity(browser, profile, monkeypatch):
    calls = []
    live = {}

    class FakeProcess:
        pid = 123

        def __init__(self, args, **kwargs):
            calls.append((args, kwargs))
            live[self.pid] = ("Mon Sep 7 12:00:00 2026", " ".join(args))

        def poll(self):
            return None

    monkeypatch.setattr(browser, "chrome_binary", lambda: "/fake/chrome")
    monkeypatch.setattr(browser.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(browser, "process_identity", lambda pid: live.get(pid))
    monkeypatch.setattr(
        browser,
        "endpoint",
        lambda p, s: browser.replace(s, port=37893, websocket="/devtools/browser/new"),
    )
    state = browser.start(profile)
    args, kwargs = calls[0]
    assert "--remote-debugging-port=0" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert f"--user-data-dir={profile.data}" in args
    assert not any(arg.startswith("--remote-allow-origins") for arg in args)
    assert kwargs["start_new_session"] and kwargs["cwd"] == profile.home
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert state.port == 37893 and browser.read_state(profile) == state


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
def test_failed_or_cancelled_start_cleans_up_only_its_child(browser, profile, monkeypatch, failure):
    calls = []

    class FakeProcess:
        pid = 123

        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append(("wait", timeout))

    monkeypatch.setattr(browser, "chrome_binary", lambda: "/fake/chrome")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: FakeProcess())
    if failure == "timeout":
        clock = iter((0, browser.START_TIMEOUT + 1))
        monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))
        error = browser.BrowserError
    else:

        def interrupt(pid):
            raise KeyboardInterrupt

        monkeypatch.setattr(browser, "process_identity", interrupt)
        error = KeyboardInterrupt
    with pytest.raises(error):
        browser.start(profile)
    assert calls == ["terminate", ("wait", browser.STOP_TIMEOUT)]


def test_mcp_refuses_missing_or_wrong_version_without_download_or_browser(
    browser, profile, monkeypatch, capsys
):
    monkeypatch.setenv("ENSO_HOME", str(profile.home))
    monkeypatch.setattr(browser, "start", lambda *a: pytest.fail("opened Chrome without MCP"))
    monkeypatch.setattr(
        browser.subprocess, "Popen", lambda *a, **k: pytest.fail("downloaded dependencies")
    )
    assert browser.main(["mcp"]) == 1
    assert f"@playwright/mcp@{browser.MCP_VERSION}" in capsys.readouterr().err
    package = install_fake_mcp(browser, profile)
    (package / "package.json").write_text('{"version": "0.0.1"}')
    assert browser.main(["mcp"]) == 1


def test_mcp_exec_uses_local_dependency_loopback_output_and_holds_exclusive_lock(
    browser, profile, monkeypatch, capsys
):
    install_fake_mcp(browser, profile)
    state = running_state(browser, profile)
    monkeypatch.setenv("ENSO_HOME", str(profile.home))
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(browser, "start", lambda p: state)
    monkeypatch.chdir(profile.home)
    calls = []

    class ExecReplacedError(Exception):
        pass

    def fake_exec(executable, args):
        calls.append((executable, args, Path.cwd()))
        with (
            pytest.raises(browser.BrowserError, match="another mcp"),
            browser.profile_lock(profile, "mcp"),
        ):
            pytest.fail("MCP lock lost")
        raise ExecReplacedError

    monkeypatch.setattr(browser.os, "execv", fake_exec)
    with pytest.raises(ExecReplacedError):
        browser.main(["mcp"])
    executable, args, cwd = calls[0]
    assert executable == "/usr/bin/node"
    assert args[1] == str(profile.root / "tooling/node_modules/@playwright/mcp/cli.js")
    assert args[args.index("--cdp-endpoint") + 1] == f"http://127.0.0.1:{state.port}"
    assert args[args.index("--output-dir") + 1] == str(profile.output)
    assert cwd == profile.output and not capsys.readouterr().out


def test_open_preserves_tabs_and_encodes_url_in_single_loopback_request(
    browser, profile, monkeypatch, capsys
):
    state = running_state(browser, profile)
    monkeypatch.setenv("ENSO_HOME", str(profile.home))
    monkeypatch.setattr(browser, "start", lambda p: state)
    monkeypatch.setattr(browser, "status", lambda p: {"profile": p.name, "running": True})
    calls = []
    monkeypatch.setattr(browser, "request", lambda *a, **k: calls.append((a, k)))
    assert browser.main(["open", "--url", "https://example.com/?one=1&two=2"]) == 0
    assert calls == [
        (
            (state.port, "/json/new?https%3A%2F%2Fexample.com%2F%3Fone%3D1%26two%3D2"),
            {"method": "PUT"},
        )
    ]
    assert json.loads(capsys.readouterr().out)["running"]
    assert browser.main(["open"]) == 0
    assert len(calls) == 1
