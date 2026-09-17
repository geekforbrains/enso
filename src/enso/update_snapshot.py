"""Durable snapshots of a release's declared home paths, including absent destinations."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from .config import Paths
from .maintenance import UpdateError, read_json, sync_directory, write_json


def _names(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 4096:
        raise UpdateError("the migration snapshot paths are invalid")
    for name in value:
        if (
            not isinstance(name, str)
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\x00" in name
            or "\\" in name
            or name.split("/")[0] in {"runtime", ".git", "cache", "heartbeat", "web.pid"}
            or any(part.endswith(".lock") for part in name.split("/"))
        ):
            raise UpdateError("the migration snapshot paths are invalid")
    return value


def _parents(root: Path, name: str) -> None:
    if root.is_symlink():
        raise UpdateError(f"{root} must not be a symbolic link during a managed update")
    if root.exists() and not root.is_dir():
        raise UpdateError(f"{root} must be a directory during a managed update")
    current = root
    for part in name.split("/")[:-1]:
        current /= part
        if current.is_symlink():
            raise UpdateError(f"{current} must not be a symbolic link during a managed update")
        if current.exists() and not current.is_dir():
            raise UpdateError(f"{current} must be a directory during a managed update")


def plan(paths: Paths, names: object) -> list[str]:
    """Validate declarations, collapse overlaps, and cover newly created parent directories."""
    result: set[str] = set()
    for name in _names(names):
        _parents(paths.home, name)
        current = paths.home
        for part in name.split("/"):
            current /= part
            if current.is_symlink():
                raise UpdateError(f"{current} must not be a symbolic link during a managed update")
            if not current.exists():
                break
            if not current.is_file() and not current.is_dir():
                raise UpdateError(
                    f"{current} must be a regular file or directory during a managed update"
                )
        result.add(current.relative_to(paths.home).as_posix())
    return [
        name
        for name in sorted(result)
        if not any(parent.as_posix() in result for parent in Path(name).parents)
    ]


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def _sync(root: Path) -> None:
    if root.is_file():
        with root.open("rb") as file:
            os.fsync(file.fileno())
        return
    for directory, _children, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                with path.open("rb") as file:
                    os.fsync(file.fileno())
        sync_directory(Path(directory))


def capture(paths: Paths, directory: Path, names: list[str]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise UpdateError("the snapshot operation must be a real directory")
    if plan(paths, names) != names:
        raise UpdateError("migration paths changed before the snapshot")
    backup = directory / "backup"
    backup.mkdir(mode=0o700)
    present = []
    for name in names:
        source = paths.home / name
        if source.exists():
            _copy(source, backup / name)
            present.append(name)
    _sync(backup)
    write_json(directory / "snapshot.json", {"paths": names, "present": present})


def restore(paths: Paths, directory: Path) -> None:
    backup = directory / "backup"
    try:
        manifest = directory / "snapshot.json"
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or backup.is_symlink()
            or not backup.is_dir()
            or manifest.is_symlink()
            or not manifest.is_file()
        ):
            raise ValueError
        snapshot = read_json(manifest)
        names = _names(snapshot.get("paths"))
        present = snapshot["present"]
        if (
            names != sorted(set(names))
            or any(parent.as_posix() in names for name in names for parent in Path(name).parents)
            or not isinstance(present, list)
            or any(name not in names for name in present)
            or present != [name for name in names if name in present]
        ):
            raise ValueError
        for name in names:
            _parents(paths.home, name)
            _parents(backup, name)
            source = backup / name
            if source.is_symlink() or ((name in present) != source.exists()):
                raise ValueError
            if name in present and not source.is_file() and not source.is_dir():
                raise ValueError
    except (UpdateError, KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
        raise UpdateError(
            "the pre-update snapshot is incomplete; recovery requires inspection"
        ) from exc
    # Preserve failed state until the old services have passed their health checks.
    failed = directory / f"failed-state-{uuid.uuid4().hex[:8]}"
    failed.mkdir(mode=0o700)
    for name in names:
        current = paths.home / name
        if current.exists() or current.is_symlink():
            destination = failed / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(current, destination)
        if name in present:
            _copy(backup / name, current)
            _sync(current)
    _sync(failed)
    sync_directory(paths.home)
