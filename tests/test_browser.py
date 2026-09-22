"""The browser CLI uses isolated profiles and never trusts a stale PID or port."""

from __future__ import annotations

import json
import signal
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection

import pytest
from typer.testing import CliRunner

from enso import browser as browser_module
from enso.cli import app
from enso.maintenance import write_json


def invoke(*args):
    return CliRunner().invoke(app, ["browser", *args])


@pytest.fixture
def browser():
    return browser_module


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


def test_create_defaults_to_private_profile_without_external_tools(browser, tmp_path, monkeypatch):
    home = tmp_path / "new-home"
    monkeypatch.setenv("ENSO_HOME", str(home))
    monkeypatch.setattr(
        browser.subprocess, "Popen", lambda *a, **k: pytest.fail("launched a process")
    )
    result = invoke("create")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["profile"] == "default"
    profile = browser.Profile(home)
    (profile.data / "user-content").write_text("keep")
    assert invoke("create").exit_code == 0
    assert (profile.data / "user-content").read_text() == "keep"
    for path in (profile.root, profile.data, profile.output, profile.state.parent):
        assert path.stat().st_mode & 0o777 == 0o700
    lock = profile.state.with_suffix(".lifecycle.lock")
    assert lock.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name", ["../outside", "Work", "a" * 49])
def test_rejects_invalid_profile_names_before_creating_files(browser, tmp_path, name):
    with pytest.raises(browser.BrowserError, match="profile must"):
        browser.Profile(tmp_path / "enso", name)
    assert not (tmp_path / "enso").exists()


def test_rejects_symlinked_browser_directories(browser, tmp_path):
    home = tmp_path / "enso"
    outside = tmp_path / "outside"
    outside.mkdir()
    link = home / "browser/profiles/default"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(browser.BrowserError, match="symbolic link"):
        browser.Profile(home).create()
    assert not list(outside.iterdir())


def test_read_only_commands_do_not_initialize_home(browser, tmp_path, monkeypatch):
    home = tmp_path / "new-home"
    monkeypatch.setenv("ENSO_HOME", str(home))
    for command in (["status"], ["list"], ["mcp", "work", "--print-config"]):
        result = invoke(*command)
        assert result.exit_code == 0, result.output
        json.loads(result.stdout)
    assert not home.exists()


def test_registration_carries_exact_home_profile_and_stable_launcher(
    browser, profile, tmp_path, monkeypatch
):
    from enso import updates

    launcher = tmp_path / "bin/enso"
    launcher.parent.mkdir()
    launcher.touch()
    monkeypatch.setattr(updates, "installed", lambda paths: {"bin_dir": str(launcher.parent)})
    monkeypatch.setenv("PATH", "/an/unrelated/environment/bin")
    registration = browser.registration(profile)["mcpServers"]["enso-browser-default"]
    assert registration["command"] == str(launcher)
    assert registration["env"] == {"ENSO_HOME": str(profile.home)}
    assert registration["args"] == ["browser", "mcp", "default"]


def test_registration_keeps_development_launcher_over_activated_venv(
    browser, profile, tmp_path, monkeypatch
):
    launcher = tmp_path / "stable/enso"
    launcher.parent.mkdir()
    launcher.touch()
    operation = "a" * 32
    runtime = profile.home / "runtime"
    write_json(runtime / "development.json", {"id": operation, "phase": "ready"})
    write_json(
        runtime / "development" / operation / "operation.json",
        {
            "id": operation,
            "phase": "ready",
            "services": {"daemon": False, "viewer": False},
            "previous_install": {},
            "launcher": str(launcher),
        },
    )
    monkeypatch.setenv("PATH", "/an/unrelated/environment/bin")
    registration = browser.registration(profile)["mcpServers"]["enso-browser-default"]
    assert registration["command"] == str(launcher)


def test_commands_need_no_config_and_do_not_hold_update_access(enso_home):
    from enso import maintenance

    write_json(enso_home.runtime_dir / "development.json", {})
    with maintenance.exclusive_access(enso_home, 0):
        result = invoke("create", "smoke")
    assert result.exit_code == 0, result.output
    assert not enso_home.config.exists() and not enso_home.db.exists()


def test_cli_help_and_invalid_syntax(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSO_HOME", str(tmp_path / "absent"))
    assert "mcp" in invoke("--help").stdout
    assert invoke("open", "--unknown").exit_code == 2
    assert invoke("create", "../escape").exit_code == 1
    assert not (tmp_path / "absent").exists()


def test_registration_preserves_chrome_override(browser, profile, monkeypatch):
    monkeypatch.setenv("ENSO_BROWSER_CHROME", "/custom/Google Chrome")
    registration = browser.registration(profile)["mcpServers"]["enso-browser-default"]
    assert registration["env"]["ENSO_BROWSER_CHROME"] == "/custom/Google Chrome"


@pytest.mark.parametrize(
    "url",
    [
        "--no-sandbox",
        "file:///etc/passwd",
        "https://user:pass@example.com",
        "https://example.com:99999",
        "https://example.com\n--flag",
    ],
)
def test_rejects_unsafe_url_without_launch_or_traceback(browser, tmp_path, monkeypatch, url):
    home = tmp_path / "enso"
    monkeypatch.setenv("ENSO_HOME", str(home))
    result = invoke("open", f"--url={url}")
    assert result.exit_code == 1
    assert not result.stdout and "URL must be" in result.stderr and "Traceback" not in result.stderr
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


@pytest.mark.parametrize("mismatch", ["started", "profile", "owner"])
def test_stop_never_signals_a_recycled_pid_or_different_profile(
    browser, profile, monkeypatch, mismatch
):
    state = running_state(browser, profile)
    identity = (state.started, state.command)
    if mismatch == "started":
        identity = ("different start", state.command)
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
    assert not any(arg.startswith("--remote-allow-origins") for arg in args)
    assert kwargs["start_new_session"]
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
    browser, profile, monkeypatch
):
    monkeypatch.setenv("ENSO_HOME", str(profile.home))
    monkeypatch.setattr(browser, "start", lambda *a: pytest.fail("opened Chrome without MCP"))
    monkeypatch.setattr(
        browser.subprocess, "Popen", lambda *a, **k: pytest.fail("downloaded dependencies")
    )
    result = invoke("mcp")
    assert result.exit_code == 1 and not result.stdout
    assert f"@playwright/mcp@{browser.MCP_VERSION}" in result.stderr
    package = install_fake_mcp(browser, profile)
    (package / "package.json").write_text('{"version": "0.0.1"}')
    assert invoke("mcp").exit_code == 1


def discovery_request(server, *, path=None, host=None):
    def request():
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", path or server.route, headers={"Host": host or server.host})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader("Cache-Control")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(request)
        while not future.done():
            server.handle_request()
        return future.result()


def test_discovery_starts_under_lifecycle_lock_and_returns_verified_websocket(
    browser, profile, monkeypatch
):
    state = running_state(browser, profile)
    calls = []

    def start(selected, *, cancelled):
        assert selected == profile and not cancelled()
        with (
            pytest.raises(browser.BrowserError, match="another lifecycle"),
            browser.profile_lock(profile),
        ):
            pytest.fail("lifecycle lock missing")
        calls.append("start")
        return state

    monkeypatch.setattr(browser, "start", start)
    with browser._DiscoveryServer(profile, lambda: False) as server:
        code, body, cache = discovery_request(server)
        assert code == 200 and cache == "no-store"
        assert body == {"webSocketDebuggerUrl": f"ws://127.0.0.1:{state.port}{state.websocket}"}
    assert calls == ["start"]


@pytest.mark.parametrize("overrides", [{"path": "/json/version/"}, {"host": "foreign.invalid"}])
def test_discovery_rejects_unexpected_path_or_host_without_start(
    browser, profile, monkeypatch, overrides
):
    monkeypatch.setattr(browser, "start", lambda *a, **k: pytest.fail("unexpected browser start"))
    with browser._DiscoveryServer(profile, lambda: False) as server:
        code, body, _ = discovery_request(server, **overrides)
        assert code == 404 and body == {"error": "not found"}


def test_discovery_failure_is_retryable_and_never_publishes_an_unverified_endpoint(
    browser, profile, monkeypatch, capsys
):
    state = running_state(browser, profile)
    attempts = []

    def start(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise browser.BrowserError("Chrome process identity changed")
        return state

    monkeypatch.setattr(browser, "start", start)
    with browser._DiscoveryServer(profile, lambda: False) as server:
        code, body, _ = discovery_request(server)
        assert code == 503 and body == {"error": "Chrome process identity changed"}
        assert discovery_request(server)[0] == 200
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == "enso-browser: Chrome process identity changed\n"


def test_open_preserves_tabs_and_encodes_url_in_single_loopback_request(
    browser, profile, monkeypatch
):
    state = running_state(browser, profile)
    monkeypatch.setenv("ENSO_HOME", str(profile.home))
    monkeypatch.setattr(browser, "start", lambda p: state)
    monkeypatch.setattr(browser, "status", lambda p: {"profile": p.name, "running": True})
    calls = []
    monkeypatch.setattr(browser, "request", lambda *a, **k: calls.append((a, k)))
    result = invoke("open", "--url", "https://example.com/?one=1&two=2")
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            (state.port, "/json/new?https%3A%2F%2Fexample.com%2F%3Fone%3D1%26two%3D2"),
            {"method": "PUT"},
        )
    ]
    assert json.loads(result.stdout)["running"]
    assert invoke("open").exit_code == 0
    assert len(calls) == 1
