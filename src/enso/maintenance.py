"""Private update coordination shared by the CLI, daemon and independent updater.

The admission gate survives a crash. A newer daemon can establish readiness behind
it without accepting work until its updater commits the release or restores state.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Paths


class UpdateError(Exception):
    """An installation or update could not safely complete."""


def prepare(paths: Paths) -> None:
    for directory in (paths.home, paths.runtime_dir):
        if directory.is_symlink():
            raise UpdateError(f"update directory must not be a symbolic link: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.chmod(0o700)
    for name in ("operations", "releases", "python", "cache", "tools"):
        if (paths.runtime_dir / name).is_symlink():
            raise UpdateError(f"runtime/{name} must not be a symbolic link")


def read_json(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    try:
        with os.fdopen(fd, encoding="utf-8") as file:
            text = file.read(262145)
        if len(text) > 262144:
            raise ValueError("state is too large")
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, ValueError) as exc:
        raise UpdateError(f"could not read {path.name}: {exc}") from exc


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Replace one complete private document and sync its containing directory."""
    write_bytes(path, (json.dumps(value, indent=2) + "\n").encode())


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    if path.parent.is_symlink():
        raise UpdateError(f"update directory must not be a symbolic link: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".update-", delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), mode)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def lock(paths: Paths, name: str = "control") -> Iterator[None]:
    prepare(paths)
    fd = os.open(paths.runtime_dir / f"{name}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("another update operation is in progress") from None
        yield
    finally:
        os.close(fd)


def paused(paths: Paths) -> bool:
    """An unreadable gate fails closed just like an intact gate."""
    return paths.maintenance.exists() or paths.maintenance.is_symlink()


def acquire_access(paths: Paths, *, exclusive: bool = False, timeout: float = 0) -> int:
    """Hold home access through blocking input/execution, or wait to begin an update."""
    prepare(paths)
    fd = os.open(paths.runtime_dir / "access.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise UpdateError("another command is using this home; update deferred") from None
            time.sleep(0.1)


@contextmanager
def exclusive_access(paths: Paths, timeout: float) -> Iterator[None]:
    fd = acquire_access(paths, exclusive=True, timeout=timeout)
    try:
        yield
    finally:
        os.close(fd)


def daemon(paths: Paths) -> dict[str, Any]:
    state = read_json(paths.daemon_state)
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return {}
    stamp = state.get("updated_at", 0)
    if not isinstance(stamp, int | float) or time.time() - stamp > 5:
        return {}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {}
    return state
