"""Ordered home changes shipped with a release and run by its stopped-home updater.

Each step declares every home-relative path it may change, including new destinations.
The updater validates and snapshots those paths before calling ``apply``; it owns locking,
service lifetimes, and rollback. Database steps use SQLite directly so old schemas can be
converted before the new release's normal config and database readers run.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .config import Paths
from .maintenance import UpdateError, read_json, write_json

MARKER = ".migrations.json"


@dataclass(frozen=True)
class Migration:
    """One append-only revision; a successful step is recorded before the next starts."""

    revision: int
    name: str
    paths: Callable[[Paths], tuple[str, ...]]
    apply: Callable[[Paths], None]


def _remove_locks(directory: Path) -> None:
    """Delete a directory's lock files, then the directory once nothing else is in it."""
    if directory.is_symlink() or not directory.is_dir():
        return
    for lock in directory.glob("*.lock"):
        lock.unlink()
    with suppress(OSError):
        directory.rmdir()


def remove_scattered_locks(paths: Paths) -> None:
    """Locks now live in ``runtime/locks/``; the old files were empty, so nothing moves."""
    for name in (".config.lock", ".skills.lock", ".knowledge.lock", ".memory.lock"):
        (paths.home / name).unlink(missing_ok=True)
    for lock in paths.workspaces.glob("*/jobs/*/.run.lock"):
        lock.unlink()
    for directory in (
        paths.home / "heartbeat" / ".locks",
        paths.home / "heartbeat",
        paths.home / ".workflow-locks",
        paths.runtime_dir / ".concurrency",
        paths.runtime_dir / "worktree-locks",
    ):
        _remove_locks(directory)


# 0.2.0 is revision zero. Keep every later step so installations may skip releases.
# Lock files hold nothing to restore, so the first step declares no snapshot paths.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "gather lock files under runtime/locks", lambda paths: (), remove_scattered_locks),
)


def latest_revision() -> int:
    """Validate the complete registry before permitting any home changes."""
    for revision, migration in enumerate(MIGRATIONS, 1):
        if type(migration.revision) is not int or migration.revision != revision:
            raise UpdateError(
                f"migration registry must contain consecutive revisions; missing {revision}"
            )
    return len(MIGRATIONS)


def read_revision(paths: Paths) -> int:
    """Read the last completed revision; an existing 0.2.0 home may have no marker."""
    latest = latest_revision()
    marker = paths.home / MARKER
    try:
        mode = marker.lstat().st_mode
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise UpdateError(f"could not read {MARKER}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise UpdateError(f"{MARKER} must be a regular file")
    try:
        state = read_json(marker)
    except (OSError, RecursionError) as exc:
        raise UpdateError(f"could not read {MARKER}: {exc}") from exc
    revision = state.get("revision")
    if set(state) != {"revision"} or type(revision) is not int or revision < 0:
        raise UpdateError(f"{MARKER} must contain a nonnegative integer revision")
    if revision > latest:
        raise UpdateError(
            f"home migration revision {revision} is newer than this Enso supports ({latest})"
        )
    return revision


def pending(paths: Paths) -> tuple[Migration, ...]:
    """Return all required steps in order without changing the home."""
    return MIGRATIONS[read_revision(paths) :]


def plan(paths: Paths) -> tuple[str, ...]:
    """Declare the extra snapshot paths; the updater validates and stores the plan."""
    return tuple(
        dict.fromkeys(relative for step in pending(paths) for relative in step.paths(paths))
    )


def apply(paths: Paths) -> None:
    """Run required steps after the updater has stopped writers and saved its snapshot."""
    steps = pending(paths)
    for step in steps:
        try:
            step.apply(paths)
        except Exception as exc:
            raise UpdateError(f"migration {step.revision} ({step.name}) failed: {exc}") from exc
        write_json(paths.home / MARKER, {"revision": step.revision})
    if not steps and not (paths.home / MARKER).exists():
        write_json(paths.home / MARKER, {"revision": latest_revision()})
