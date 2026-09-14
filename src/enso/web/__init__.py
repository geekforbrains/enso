"""The read-only web viewer's process lifecycle: pidfile, ``start``, ``stop``, ``status``.

The viewer is one aiohttp process started as ``python -m enso.web``, separate from
``enso serve`` so it can run while the service is stopped. This module is what the CLI
uses to manage that process and it imports none of the ``web`` extra, so ``enso web
status`` and ``stop`` work without it; ``start`` reports the install command instead.

The pidfile is the coordination point. The viewer opens ``web.pid``, takes an exclusive
``flock`` on it for as long as it lives, and records its pid, host, and port. Anyone else
takes a shared lock: if that succeeds nobody is holding the file, so it is stale; if it
fails the viewer is live, and only then is its pid signalled. A crash leaves a stale file
behind that the next start simply overwrites, so nothing here ever deletes a file it did
not lock.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import importlib.util
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .. import locks
from ..config import Paths, WebConfig, check_config

EXTRA_MODULES = ("aiohttp", "jinja2", "markdown_it")
INSTALL_HINT = "install the web extra: `uv tool install -e './enso[web]'`"
START_TIMEOUT = 10.0  # for the child to bind and claim the pidfile
STOP_TIMEOUT = 15.0  # for SIGTERM to finish an orderly shutdown
KILL_TIMEOUT = 2.0
LOCK_RETRIES = 20  # a status probe holds the shared lock for an instant


class WebError(Exception):
    """A lifecycle step failed; the message says what happened and where to look."""


@dataclass(frozen=True)
class Bind:
    """Where the viewer listens."""

    host: str
    port: int

    @property
    def url(self) -> str:
        host = self.host
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return f"http://{host}:{self.port}"
        if address.is_unspecified:
            host = "127.0.0.1" if address.version == 4 else "[::1]"
        elif address.version == 6:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def loopback(self) -> bool:
        """Only this machine can reach it; anything else deserves a warning."""
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    def connect_address(self) -> tuple[str, int]:
        """Where a readiness probe on this machine connects."""
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError:
            return self.host, self.port
        if address.is_unspecified:
            return ("127.0.0.1" if address.version == 4 else "::1"), self.port
        return self.host, self.port


@dataclass(frozen=True)
class Status:
    """What the pidfile says: live (locked), stale (unlocked), or absent."""

    running: bool
    pid: int | None = None
    host: str | None = None
    port: int | None = None
    stale: bool = False

    @property
    def url(self) -> str | None:
        if self.host is None or self.port is None:
            return None
        return Bind(self.host, self.port).url


def missing_extra() -> list[str]:
    """The ``web`` extra's modules that are not importable."""
    return [name for name in EXTRA_MODULES if importlib.util.find_spec(name) is None]


def resolve_bind(
    paths: Paths, host: str | None = None, port: int | None = None
) -> tuple[Bind, list[str]]:
    """Flags win, then ``config.json``, then the defaults; also returns the config problems.

    The viewer is a diagnostic surface, so a missing or invalid config falls back to the
    safe defaults instead of refusing to start; the Health page reports the problems.
    """
    config, problems, _warnings = check_config(paths)
    base = config.web if config is not None else WebConfig()
    return Bind(host or base.host, port or base.port), problems


# -- The pidfile ----------------------------------------------------------------


class PidFile:
    """The viewer's exclusive claim on ``web.pid``, held from before bind until exit."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> None:
        """Take the exclusive lock, or raise ``WebError`` when another viewer holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(LOCK_RETRIES):
            try:
                fd = locks.acquire(self.path)
            except locks.LockPathError as exc:
                raise WebError(str(exc)) from None
            except BlockingIOError:
                time.sleep(0.05)
                continue
            # An orderly exit unlinks the file before releasing its lock, so the inode we
            # opened may no longer be the path: start over on the current one.
            if _same_file(fd, self.path):
                self.fd = fd
                return
            os.close(fd)
        raise WebError(f"another viewer is already running (it holds {self.path})")

    def write(self, pid: int, bind: Bind) -> None:
        assert self.fd is not None
        record = json.dumps({"pid": pid, "host": bind.host, "port": bind.port}).encode()
        os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, record + b"\n")

    def release(self) -> None:
        """Unlink while still locked, then let go, so no reader mistakes a newcomer's file."""
        if self.fd is None:
            return
        if _same_file(self.fd, self.path):
            with contextlib.suppress(OSError):
                self.path.unlink()
        os.close(self.fd)
        self.fd = None


def _same_file(fd: int, path: Path) -> bool:
    try:
        return os.fstat(fd).st_ino == os.stat(path).st_ino
    except FileNotFoundError:
        return False


def _locked(fd: int) -> bool:
    """Whether someone holds the exclusive lock (a shared lock would fail)."""
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return True
        raise
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def _record(fd: int) -> dict | None:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 4096):
        chunks.append(chunk)
    try:
        record = json.loads(b"".join(chunks) or b"null")
    except ValueError:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("pid"), int):
        return None
    return record


def _status_from(fd: int) -> Status:
    live = _locked(fd)
    record = _record(fd)
    if live and record is None:
        # The viewer locks before it binds and writes; give it a moment.
        for _ in range(10):
            time.sleep(0.05)
            record = _record(fd)
            if record is not None:
                break
    if record is None:
        return Status(running=live, stale=not live)
    return Status(
        running=live,
        pid=record["pid"],
        host=record.get("host"),
        port=record.get("port"),
        stale=not live,
    )


def status(paths: Paths) -> Status:
    """Running (the file is locked), stale (it is not), or absent."""
    try:
        fd = locks.open_lock(paths.web_pid, create=False)
    except FileNotFoundError:
        return Status(running=False)
    except locks.LockPathError as exc:
        raise WebError(str(exc)) from None
    try:
        return _status_from(fd)
    finally:
        os.close(fd)


# -- Lifecycle ------------------------------------------------------------------


def child_command(bind: Bind) -> list[str]:
    """The viewer process: this interpreter running ``enso.web``, never the CLI."""
    return [sys.executable, "-m", "enso.web", "--host", bind.host, "--port", str(bind.port)]


def start(
    paths: Paths, *, host: str | None = None, port: int | None = None, foreground: bool = False
) -> str:
    """Start the viewer and say where it listens; idempotent while the same one is live.

    In the foreground this replaces the current process. In the background it spawns the
    child in its own session with ``web.log`` as its output, waits for it to claim the
    pidfile and accept a connection, and raises ``WebError`` if it exits or stalls first.
    """
    missing = missing_extra()
    if missing:
        raise WebError(f"the web viewer needs {', '.join(missing)}; {INSTALL_HINT}")
    bind, _problems = resolve_bind(paths, host, port)
    current = status(paths)
    if current.running:
        return f"already running pid {current.pid} at {current.url or bind.url}"
    command = child_command(bind)
    if foreground:
        os.execv(command[0], command)
    paths.home.mkdir(parents=True, exist_ok=True)
    with open(paths.web_log, "ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise WebError(
                f"the viewer exited with status {code} before it was ready; see {paths.web_log}"
            )
        current = status(paths)
        if current.running and current.pid == process.pid and _reachable(bind):
            return f"listening on {bind.url} (pid {process.pid})"
        time.sleep(0.1)
    process.terminate()
    raise WebError(f"the viewer did not become ready in {START_TIMEOUT:.0f}s; see {paths.web_log}")


def _reachable(bind: Bind) -> bool:
    try:
        with socket.create_connection(bind.connect_address(), timeout=1):
            return True
    except OSError:
        return False


def stop(paths: Paths, timeout: float = STOP_TIMEOUT) -> str:
    """SIGTERM the live viewer and wait for it to let go of the pidfile; idempotent."""
    try:
        fd = locks.open_lock(paths.web_pid, create=False)
    except FileNotFoundError:
        return "not running"
    except locks.LockPathError as exc:
        raise WebError(str(exc)) from None
    try:
        current = _status_from(fd)
        if not current.running:
            return "not running"
        if current.pid is None:
            raise WebError(f"the viewer is running but {paths.web_pid} names no pid")
        # The lock proves the process that wrote this pid is alive right now, so the pid
        # cannot have been recycled by something unrelated.
        _signal(current.pid, signal.SIGTERM)
        if _wait_unlocked(fd, timeout):
            return f"stopped pid {current.pid}"
        _signal(current.pid, signal.SIGKILL)
        if _wait_unlocked(fd, KILL_TIMEOUT):
            return f"killed pid {current.pid} after {timeout:.0f}s"
        raise WebError(f"the viewer (pid {current.pid}) did not stop; see {paths.web_log}")
    finally:
        os.close(fd)


def _signal(pid: int, signum: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signum)


def _wait_unlocked(fd: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _locked(fd):
            return True
        time.sleep(0.1)
    return not _locked(fd)
