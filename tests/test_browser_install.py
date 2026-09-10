"""Fresh and updated installs run the shipped browser helper using only local fakes."""

from __future__ import annotations

import http.client
import json
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from enso import workspaces
from enso.config import Agent, Paths

FAKE_BROWSER = Path(__file__).parent / "fixtures/fake_browser.py"
BROWSER_FILES = (
    "skills/enso-browser/SKILL.md",
    "skills/enso-browser/scripts/browser.py",
    "skills/enso-browser/references/setup.md",
)


class MCP:
    def __init__(self, process):
        self.process = process
        self.sequence = 0
        self.stderr = ""
        self.remaining_stdout = ""

    def send(self, method, **params):
        self.sequence += 1
        message = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method, **params):
        self.send(method, **params)
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=20), f"MCP timed out on {method}"
        line = self.process.stdout.readline()
        assert line, f"MCP exited on {method}: {self.process.stderr.read()}"
        response = json.loads(line)
        assert response["id"] == self.sequence
        return response["result"]

    def tabs(self):
        result = self.call("tools/call", name="browser_tabs", arguments={"action": "list"})
        return json.loads(result["content"][0]["text"])


class InstalledBrowser:
    def __init__(self, paths, env, events, cwd):
        self.paths = paths
        self.home = paths.home
        self.env = env
        self.event_directory = events
        self.cwd = cwd
        self.helper = self.home / "skills/enso-browser/scripts/browser.py"

    def run(self, *args, check=True):
        return subprocess.run(
            [sys.executable, str(self.helper), *args],
            env=self.env,
            cwd=self.cwd,
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            check=check,
        )

    def result(self, *args):
        return json.loads(self.run(*args).stdout)

    def registration(self, profile="default"):
        servers = self.result("mcp", profile, "--print-config")["mcpServers"]
        return servers[f"enso-browser-{profile}"]

    def install_mcp(self):
        package = self.home / "browser/tooling/node_modules/@playwright/mcp"
        package.mkdir(parents=True)
        (package / "package.json").write_text('{"version": "0.0.80"}')
        (package / "cli.js").write_text("// Executed by the fake node only.\n")

    def events(self, kind):
        return [
            json.loads(path.read_text())
            for path in self.event_directory.glob(f"{kind}-[0-9]*.json")
        ]

    def wait_event(self, kind):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if events := self.events(kind):
                return events[0]
            time.sleep(0.01)
        pytest.fail(f"fake browser did not record {kind}")

    @contextmanager
    def controller(self, registration, *, expected_returncode=0):
        process = subprocess.Popen(
            [registration["command"], *registration["args"]],
            env={**self.env, **registration["env"]},
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            client = MCP(process)
            client.call("initialize")
            yield client
        finally:
            with suppress(BrokenPipeError):
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            client.stderr = process.stderr.read()
            client.remaining_stdout = process.stdout.read()
            process.stdout.close()
            process.stderr.close()
            if sys.exc_info()[0] is None:
                assert process.returncode == expected_returncode, client.stderr

    def cleanup(self):
        # Address only the fake endpoints recorded in this test's private directory.
        # This cleanup also works when a broken helper failed to save Chrome state.
        for event in self.events("chrome"):
            connection = http.client.HTTPConnection("127.0.0.1", event["port"], timeout=1)
            try:
                connection.request("GET", "/__test/stop")
                connection.getresponse().read()
            except OSError, http.client.HTTPException:
                pass
            finally:
                connection.close()


@pytest.fixture
def installed_browser(tmp_path, request, monkeypatch):
    mode = getattr(request, "param", "custom")
    user_directory = tmp_path / "user"
    user_directory.mkdir()
    home = user_directory / ".enso" if mode == "default" else tmp_path / "custom enso"
    assert not home.exists()
    paths = Paths(home)
    with monkeypatch.context() as seed_environment:
        seed_environment.setattr(workspaces.shutil, "which", lambda name: None)
        workspaces.seed_home(paths)
    events = tmp_path / "events"
    events.mkdir()
    executables = tmp_path / "fake-bin"
    executables.mkdir()
    source = f"#!{sys.executable}\n" + FAKE_BROWSER.read_text()
    for name in ("node", "chrome"):
        executable = executables / name
        executable.write_text(source)
        executable.chmod(0o700)
    env = {
        "HOME": str(user_directory),
        "PATH": f"{executables}:/usr/bin:/bin",
        "ENSO_BROWSER_CHROME": str(executables / "chrome"),
        "ENSO_BROWSER_TEST_EVENTS": str(events),
    }
    if mode == "custom":
        env["ENSO_HOME"] = str(home)
        current = home / "runtime/current"
        current.parent.mkdir(parents=True)
        current.symlink_to(sys.prefix, target_is_directory=True)
    installed = InstalledBrowser(paths, env, events, tmp_path)
    try:
        yield installed
    finally:
        installed.cleanup()


def assert_listener_closed(address):
    listener = urlsplit(address)
    with pytest.raises(OSError), socket.create_connection((listener.hostname, listener.port), 1):
        pytest.fail("discovery listener survived MCP exit")


def assert_process_dead(pid):
    process = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        cwd="/",
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    # A detached child can briefly be a zombie until its system parent reaps it.
    assert not process.stdout.strip() or process.stdout.lstrip().startswith("Z")


@pytest.mark.parametrize("installed_browser", ["default", "custom"], indirect=True)
def test_fresh_home_registration_is_lazy_and_reuses_first_browser(installed_browser):
    installed = installed_browser
    for relative in BROWSER_FILES:
        assert (installed.home / relative).read_text() == workspaces._bundled(relative)
    assert not (installed.home / "browser").exists()
    registration = installed.registration()
    assert registration["args"] == [str(installed.helper), "mcp", "default"]
    assert registration["env"]["ENSO_HOME"] == str(installed.home)
    assert not (installed.home / "browser").exists()
    assert installed.result("create")["profile"] == "default"
    installed.install_mcp()
    with installed.controller(registration) as client:
        assert client.call("tools/list")["tools"][0]["name"] == "browser_tabs"
        assert installed.events("chrome") == []
        assert installed.result("status")["running"] is False
        assert client.tabs() == [{"url": "about:blank"}]
        running = installed.result("status")
        assert running["running"] is True
        assert len(installed.events("chrome")) == 1
        child = installed.events("mcp")[0]
        assert child["cwd"] == str(installed.home / "browser/output/default")
    assert installed.result("status")["pid"] == running["pid"]
    assert_listener_closed(child["address"])
    installed.result("open", "--url", "https://example.test/saved-tab")
    with installed.controller(registration) as client:
        assert client.tabs() == [
            {"url": "about:blank"},
            {"url": "https://example.test/saved-tab"},
        ]
        assert installed.result("status")["pid"] == running["pid"]
        assert len(installed.events("chrome")) == 1
    assert installed.result("stop")["status"] == "stopped"


def test_new_profiles_are_independent_and_lock_without_opening_chrome(installed_browser):
    installed = installed_browser
    installed.result("create")
    installed.install_mcp()
    work = installed.registration("work")
    assert not (installed.home / "browser/profiles/work").exists()
    with (
        installed.controller(installed.registration()) as first,
        installed.controller(work) as second,
    ):
        assert first.call("tools/list")["tools"]
        assert second.call("tools/list")["tools"]
        assert installed.events("chrome") == []
        conflicting = installed.run("mcp", "work", check=False)
        assert conflicting.returncode == 1
        assert "another mcp operation" in conflicting.stderr
        assert installed.events("chrome") == []
        second.tabs()
        assert installed.result("status", "work")["running"] is True
        assert installed.result("status")["running"] is False
        first.tabs()
        default, work_status = installed.result("list")
        assert default["pid"] != work_status["pid"]
        assert default["endpoint"] != work_status["endpoint"]
    with installed.controller(work) as second:
        second.tabs()
        assert len(installed.events("chrome")) == 2


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_during_startup_cleans_browser_and_allows_retry(installed_browser, tmp_path, signum):
    installed = installed_browser
    installed.result("create")
    installed.install_mcp()
    ready = tmp_path / "chrome-ready"
    installed.env["ENSO_BROWSER_TEST_READY"] = str(ready)
    registration = installed.registration()
    with installed.controller(registration, expected_returncode=128 + signum) as client:
        client.send("tools/call", name="browser_tabs", arguments={"action": "list"})
        unready = installed.wait_event("chrome-unready")
        child = installed.events("mcp")[0]
        client.process.send_signal(signum)
        assert client.process.wait(timeout=5) == 128 + signum
    assert_process_dead(unready["pid"])
    assert_process_dead(child["pid"])
    assert_listener_closed(child["address"])
    assert "Traceback" not in client.stderr + client.remaining_stdout
    assert all(
        json.loads(line)["jsonrpc"] == "2.0" for line in client.remaining_stdout.splitlines()
    )
    assert installed.result("status")["running"] is False
    ready.touch()
    with installed.controller(registration) as replacement:
        assert replacement.tabs() == [{"url": "about:blank"}]
        assert installed.result("status")["pid"] != unready["pid"]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_signal_after_first_use_preserves_ready_browser(installed_browser, signum):
    installed = installed_browser
    installed.result("create")
    installed.install_mcp()
    registration = installed.registration()
    with installed.controller(registration, expected_returncode=128 + signum) as client:
        client.tabs()
        ready = installed.result("status")
        child = installed.events("mcp")[0]
        client.process.send_signal(signum)
        assert client.process.wait(timeout=5) == 128 + signum
    assert_process_dead(child["pid"])
    assert_listener_closed(child["address"])
    assert "Traceback" not in client.stderr + client.remaining_stdout
    assert all(
        json.loads(line)["jsonrpc"] == "2.0" for line in client.remaining_stdout.splitlines()
    )
    assert installed.result("status")["pid"] == ready["pid"]
    with installed.controller(registration) as replacement:
        assert replacement.tabs() == [{"url": "about:blank"}]
        assert len(installed.events("chrome")) == 1


@pytest.mark.parametrize("customized", [False, True])
def test_browser_bundle_upgrade_preserves_profiles_and_printed_registration(
    installed_browser, tmp_path, monkeypatch, customized
):
    installed = installed_browser
    installed.result("create", "work")
    installed.install_mcp()
    registration = installed.registration("work")
    registration_file = installed.home / "workspaces/work/mcp.json"
    registration_file.parent.mkdir(parents=True)
    registration_file.write_text(json.dumps(registration))
    cookie = installed.home / "browser/profiles/work/Cookies"
    cookie.write_text("saved login")
    with installed.controller(registration) as client:
        client.tabs()
    state = installed.home / "browser/state/work.json"
    original_state = state.read_bytes()
    original_registration = registration_file.read_bytes()
    if customized:
        for relative in BROWSER_FILES:
            target = installed.home / relative
            target.write_text(target.read_text() + "\n# Operator customization.\n")
    original_browser = {
        relative: (installed.home / relative).read_text() for relative in BROWSER_FILES
    }
    package = tmp_path / "next-package"
    shutil.copytree(workspaces.resources.files("enso").joinpath("bundled"), package / "bundled")
    for relative in BROWSER_FILES:
        source = package / "bundled" / relative
        source.write_text(source.read_text() + "\n# Next browser release.\n")
    monkeypatch.setattr(workspaces.resources, "files", lambda name: package)
    changed = workspaces.reconcile_bundles(installed.paths, Agent("claude", "opus", "high"))
    for relative in BROWSER_FILES:
        assert (relative in changed) is not customized
        current = (installed.home / relative).read_text()
        if customized:
            assert current == original_browser[relative]
        else:
            assert current.endswith("# Next browser release.\n")
    assert cookie.read_text() == "saved login"
    assert state.read_bytes() == original_state
    assert registration_file.read_bytes() == original_registration
    assert installed.registration("work") == registration
    with installed.controller(json.loads(registration_file.read_text())) as client:
        client.tabs()
        assert len(installed.events("chrome")) == 1
    assert installed.result("status", "work")["pid"] == json.loads(original_state)["pid"]
