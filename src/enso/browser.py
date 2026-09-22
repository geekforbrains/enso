"""Keep one private Chrome per Enso profile and attach a locally installed MCP server.

The browser outlives agent turns. Its process identity and Chrome-assigned debugging
endpoint are recorded together so stale state never authorizes stopping another process.
"""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import FrameType
from urllib.parse import urlsplit

from . import service
from .config import Paths

MCP_VERSION = "0.0.80"
START_TIMEOUT = 15.0
STOP_TIMEOUT = 5.0
MAX_RESPONSE = 65_536
NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
WEBSOCKET_PATH = re.compile(r"/devtools/browser/[a-zA-Z0-9-]{1,100}\Z")


class BrowserError(Exception):
    """An expected dependency, ownership, or browser lifecycle problem."""


@dataclass(frozen=True)
class Profile:
    home: Path
    name: str = "default"

    def __post_init__(self) -> None:
        if len(self.name) > 48 or not NAME.fullmatch(self.name):
            raise BrowserError("profile must be 1-48 lowercase letters/digits in kebab-case")

    @property
    def root(self) -> Path:
        return self.home / "browser"

    @property
    def data(self) -> Path:
        return self.root / "profiles" / self.name

    @property
    def output(self) -> Path:
        return self.root / "output" / self.name

    @property
    def state(self) -> Path:
        return self.root / "state" / f"{self.name}.json"

    @property
    def tooling(self) -> Path:
        return self.root / "tooling"

    def check_paths(self) -> None:
        for target in (self.data, self.output, self.state.parent, self.tooling):
            _check_path(self.home, target)

    def create(self) -> None:
        self.check_paths()
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        for target in (self.data, self.output, self.state.parent, self.tooling):
            current = self.home
            for part in target.relative_to(self.home).parts:
                current /= part
                current.mkdir(mode=0o700, exist_ok=True)
                current.chmod(0o700)


def _check_path(home: Path, target: Path) -> None:
    current = home
    for part in target.relative_to(home).parts:
        current /= part
        if current.is_symlink():
            raise BrowserError(f"browser path must not be a symbolic link: {current}")
        if current.exists() and current != target and not current.is_dir():
            raise BrowserError(f"browser directory is not a directory: {current}")


@contextmanager
def profile_lock(profile: Profile, kind: str = "lifecycle") -> Iterator[int]:
    """Fail promptly on concurrent operations; never unlink a lock another writer holds."""
    target = profile.state.with_suffix(f".{kind}.lock")
    fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BrowserError(f"profile '{profile.name}' has another {kind} operation") from None
        yield fd
    finally:
        os.close(fd)


@dataclass(frozen=True)
class State:
    pid: int
    started: str
    command: str
    owner: str
    port: int = 0
    websocket: str = ""


def _read_text(path: Path) -> str:
    if path.is_symlink():
        raise BrowserError(f"browser state must not be a symbolic link: {path}")
    with path.open("rb") as handle:
        raw = handle.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise BrowserError(f"browser state is too large: {path}")
    return raw.decode("utf-8")


def read_state(profile: Profile) -> State | None:
    try:
        raw = json.loads(_read_text(profile.state))
    except FileNotFoundError:
        return None
    except ValueError, UnicodeError:
        raise BrowserError(
            "invalid browser state; inspect it before restarting this profile"
        ) from None
    fields = {"pid", "started", "command", "owner", "port", "websocket"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise BrowserError("invalid browser state fields")
    if (
        type(raw["pid"]) is not int
        or raw["pid"] < 2
        or type(raw["port"]) is not int
        or not 0 <= raw["port"] <= 65535
        or any(
            not isinstance(raw[key], str) for key in ("started", "command", "owner", "websocket")
        )
        or not re.fullmatch(r"[0-9a-f]{32}", raw["owner"])
    ):
        raise BrowserError("invalid browser process identity")
    return State(**raw)


def write_state(profile: Profile, state: State) -> None:
    _check_path(profile.home, profile.state)
    with tempfile.NamedTemporaryFile(mode="w", dir=profile.state.parent, delete=False) as temporary:
        name = Path(temporary.name)
        try:
            json.dump(asdict(state), temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(name, profile.state)
        finally:
            name.unlink(missing_ok=True)


def process_identity(pid: int) -> tuple[str, str] | None:
    """Read one process, including its start time to reject a recycled PID."""
    result = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "args="],
        cwd="/",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=2,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode:
        return None
    parts = result.stdout.strip().split(maxsplit=5)
    if len(parts) != 6 or len(result.stdout) > MAX_RESPONSE:
        return None
    return " ".join(parts[:5]), parts[5]


def owned(profile: Profile, state: State) -> bool:
    identity = process_identity(state.pid)
    return (
        identity == (state.started, state.command)
        and f"--enso-browser-owner={state.owner}" in state.command.split()
        and f" --user-data-dir={profile.data} " in f" {state.command} "
    )


def request(port: int, path: str, *, method: str = "GET") -> object:
    """Use loopback directly, without proxy environment variables or HTTP redirects."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if response.status != 200 or len(raw) > MAX_RESPONSE:
            raise BrowserError("Chrome returned an invalid debugging response")
        return json.loads(raw)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise BrowserError("Chrome debugging endpoint is not responding") from exc
    finally:
        connection.close()


def endpoint(profile: Profile, state: State) -> State:
    try:
        lines = _read_text(profile.data / "DevToolsActivePort").splitlines()
    except FileNotFoundError:
        raise BrowserError("Chrome has not published its debugging endpoint") from None
    if len(lines) != 2 or not lines[0].isdigit() or not WEBSOCKET_PATH.fullmatch(lines[1]):
        raise BrowserError("invalid Chrome debugging endpoint file")
    port = int(lines[0])
    if not 1 <= port <= 65535:
        raise BrowserError("invalid Chrome debugging port")
    if state.port and (state.port, state.websocket) != (port, lines[1]):
        raise BrowserError("Chrome debugging endpoint identity changed")
    data = request(port, "/json/version")
    if not isinstance(data, dict):
        raise BrowserError("invalid Chrome debugging response")
    address = data.get("webSocketDebuggerUrl")
    if address != f"ws://127.0.0.1:{port}{lines[1]}":
        raise BrowserError("Chrome debugging response does not match this profile")
    if not owned(profile, state):
        raise BrowserError("Chrome process identity changed")
    return replace(state, port=port, websocket=lines[1])


def chrome_binary() -> str:
    override = os.environ.get("ENSO_BROWSER_CHROME")
    candidates = [override] if override else []
    if not override and sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif not override and sys.platform.startswith("linux"):
        candidates = [shutil.which("google-chrome"), shutil.which("google-chrome-stable")]
    for candidate in candidates:
        if (
            candidate
            and Path(candidate).is_absolute()
            and Path(candidate).is_file()
            and os.access(candidate, os.X_OK)
        ):
            return str(Path(candidate).resolve())
    raise BrowserError(
        "install Google Chrome, or set ENSO_BROWSER_CHROME to its absolute executable"
    )


def validate_url(url: str) -> str:
    if url == "about:blank":
        return url
    try:
        parsed = urlsplit(url)
        _ = parsed.port  # Validate malformed or out-of-range port numbers too.
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        valid = False
    if not valid or any(ord(char) < 32 or ord(char) == 127 for char in url) or len(url) > 8192:
        raise BrowserError("URL must be http://, https://, or about:blank, without credentials")
    return url


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise BrowserError("browser startup cancelled")


def start(profile: Profile, *, cancelled: Callable[[], bool] | None = None) -> State:
    """Reuse verified Chrome or start it detached, preserving all existing tabs."""
    _check_cancelled(cancelled)
    state = read_state(profile)
    if state and owned(profile, state):
        return endpoint(profile, state)
    if (profile.data / "SingletonLock").exists() or (profile.data / "SingletonLock").is_symlink():
        raise BrowserError("profile is locked by Chrome; quit its window before retrying")
    binary = chrome_binary()
    active_port = profile.data / "DevToolsActivePort"
    _check_path(profile.home, active_port)
    active_port.unlink(missing_ok=True)
    owner = uuid.uuid4().hex
    args = [
        binary,
        f"--user-data-dir={profile.data}",
        f"--enso-browser-owner={owner}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    args.append("--use-mock-keychain" if sys.platform == "darwin" else "--password-store=basic")
    args.append("about:blank")
    _check_cancelled(cancelled)
    process = subprocess.Popen(
        args,
        cwd=profile.home,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + START_TIMEOUT
    state = None
    try:
        while time.monotonic() < deadline:
            _check_cancelled(cancelled)
            if process.poll() is not None:
                raise BrowserError(
                    "Chrome exited; check profile locks and that a desktop is available"
                )
            identity = process_identity(process.pid)
            if identity and f"--enso-browser-owner={owner}" in identity[1].split():
                candidate = State(process.pid, *identity, owner)
                if owned(profile, candidate):
                    state = candidate
                    write_state(profile, state)
                    try:
                        state = endpoint(profile, state)
                    except BrowserError:
                        pass
                    else:
                        _check_cancelled(cancelled)
                        write_state(profile, state)
                        return state
            time.sleep(0.1)
        raise BrowserError("Chrome did not become ready within 15 seconds")
    except BaseException:
        # This Popen owns only the child we just launched, never a name-matched process.
        if process.poll() is None:
            process.terminate()
            # Keep recorded state on timeout; never force-kill a profile with recent cookies.
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=STOP_TIMEOUT)
        raise


def stop(profile: Profile) -> str:
    state = read_state(profile)
    if not state:
        return "not running"
    if not owned(profile, state):
        if process_identity(state.pid) is not None:
            raise BrowserError("saved process identity no longer matches; no process was stopped")
        profile.state.unlink()
        return "not running"
    os.kill(state.pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not owned(profile, state):
            profile.state.unlink()
            return "stopped"
        time.sleep(0.1)
    raise BrowserError("Chrome did not exit; quit this profile's window manually and retry")


def status(profile: Profile) -> dict[str, object]:
    profile.check_paths()
    state = read_state(profile)
    result: dict[str, object] = {
        "profile": profile.name,
        "path": str(profile.data),
        "running": False,
    }
    if state and owned(profile, state):
        result.update(running=True, pid=state.pid)
        try:
            active = endpoint(profile, state)
            result["endpoint"] = f"http://127.0.0.1:{active.port}"
        except BrowserError as exc:
            result["problem"] = str(exc)
    return result


def mcp_command(profile: Profile, address: str) -> list[str]:
    package = profile.tooling / "node_modules/@playwright/mcp"
    _check_path(profile.home, package / "cli.js")
    try:
        installed = json.loads(_read_text(package / "package.json"))
    except FileNotFoundError, ValueError, UnicodeError:
        installed = {}
    if not isinstance(installed, dict) or installed.get("version") != MCP_VERSION:
        raise BrowserError(f"install @playwright/mcp@{MCP_VERSION}; read references/setup.md")
    node = shutil.which("node")
    if not node or not (package / "cli.js").is_file():
        raise BrowserError("Node.js and the local Playwright MCP installation must be available")
    return [
        node,
        str(package / "cli.js"),
        "--cdp-endpoint",
        address,
        "--caps",
        "vision,pdf",
        "--output-dir",
        str(profile.output),
        "--output-max-size",
        "52428800",
    ]


class _DiscoveryServer(HTTPServer):
    """Publish verified CDP metadata on demand; browser traffic goes directly to Chrome."""

    def __init__(self, profile: Profile, cancelled: Callable[[], bool]) -> None:
        self.profile = profile
        self.cancelled = cancelled
        self.token = uuid.uuid4().hex
        super().__init__(("127.0.0.1", 0), _DiscoveryHandler)
        # Poll child exit and signals even when no browser tools are requested.
        self.timeout = 0.1
        self.host = f"127.0.0.1:{self.server_port}"
        self.address = f"http://{self.host}/{self.token}"
        self.route = f"/{self.token}/json/version/"


class _DiscoveryHandler(BaseHTTPRequestHandler):
    server: _DiscoveryServer

    def setup(self) -> None:
        # A partial local request must not prevent MCP shutdown indefinitely.
        self.request.settimeout(1)
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _respond(self, code: int, data: dict[str, str]) -> None:
        body = json.dumps(data).encode("utf-8")
        with suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self) -> None:
        server = self.server
        if self.path != server.route or self.headers.get_all("Host", []) != [server.host]:
            self._respond(404, {"error": "not found"})
            return
        try:
            with profile_lock(server.profile):
                state = start(server.profile, cancelled=server.cancelled)
        except (BrowserError, OSError, subprocess.SubprocessError) as exc:
            print(f"enso-browser: {exc}", file=sys.stderr)
            self._respond(503, {"error": str(exc)})
            return
        self._respond(
            200, {"webSocketDebuggerUrl": f"ws://127.0.0.1:{state.port}{state.websocket}"}
        )


def _stop_mcp(process: subprocess.Popen[bytes]) -> None:
    """Reap the controller without signaling the detached persistent Chrome."""
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            process.kill()
        process.wait(timeout=STOP_TIMEOUT)


def run_mcp(profile: Profile) -> int:
    """Keep stdio with Playwright while starting Chrome only for endpoint discovery."""
    # Check optional dependencies before opening a listener or taking the controller lock.
    mcp_command(profile, "http://127.0.0.1")
    process: subprocess.Popen[bytes] | None = None
    stopped = 0

    def on_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal stopped
        stopped = signum

    def cancelled() -> bool:
        return bool(stopped) or (process is not None and process.poll() is not None)

    with profile_lock(profile, "mcp") as fd, ExitStack() as stack:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(signum, on_signal)
            stack.callback(signal.signal, signum, previous)
        server = stack.enter_context(_DiscoveryServer(profile, cancelled))
        process = subprocess.Popen(
            mcp_command(profile, server.address),
            cwd=profile.output,
            pass_fds=(fd,),
            start_new_session=True,
        )
        try:
            while not cancelled():
                server.handle_request()
        finally:
            _stop_mcp(process)
    if stopped:
        return 128 + stopped
    assert process.returncode is not None
    return process.returncode if process.returncode >= 0 else 128 - process.returncode


def registration(profile: Profile) -> dict[str, object]:
    """Persist the public launcher so a release switch keeps the registration usable."""
    env = {"ENSO_HOME": str(profile.home)}
    if chrome := os.environ.get("ENSO_BROWSER_CHROME"):
        env["ENSO_BROWSER_CHROME"] = chrome
    return {
        "mcpServers": {
            f"enso-browser-{profile.name}": {
                "command": service.enso_binary(Paths(profile.home)),
                "args": ["browser", "mcp", profile.name],
                "env": env,
            }
        }
    }


def profiles(home: Path) -> list[dict[str, object]]:
    """Inspect existing profiles without initializing a home or opening Chrome."""
    directory = home / "browser/profiles"
    _check_path(home, directory)
    entries = [] if not directory.exists() else sorted(directory.iterdir())
    return [status(Profile(home, entry.name)) for entry in entries if entry.is_dir()]
