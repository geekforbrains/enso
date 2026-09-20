"""Ordered home changes shipped with a release and run by its stopped-home updater.

Each step declares every home-relative path it may change, including new destinations.
The updater validates and snapshots those paths before calling ``apply``; it owns locking,
service lifetimes, and rollback. Database steps use SQLite directly so old schemas can be
converted before the new release's normal config and database readers run.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path

from . import frontmatter, update_snapshot
from .config import Paths, valid_workspace_name
from .knowledge.catalog import Note, scan_roots
from .knowledge.links import extract_links
from .maintenance import UpdateError, read_json, write_json
from .note_storage import discover_roots, publish, read_bytes, split_document

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


def _general_linked(paths: Paths) -> tuple[Note, ...]:
    """Knowledge and memory notes with a link target in the old ``general`` scope."""
    roots = (
        *discover_roots(paths, "knowledge", shared=True)[0],
        *discover_roots(paths, "memory")[0],
    )
    notes = scan_roots(roots)[0]
    return tuple(n for n in notes if any(_general(link.target) for link in n.links))


def _general(target: str) -> bool:
    # The catalog strips a target before reading its scope, as in ``< general:X.md>``.
    return target.lstrip().startswith("general:")


def _refuse_shared_knowledge_conflicts(paths: Paths) -> None:
    """Refuse an unfinished move, a ``shared`` that is not a directory, or two note roots.

    An empty ``shared/knowledge/``, such as a fixing audit creates, is not a second root.
    """
    if any(paths.home.glob(".knowledge-move-*")):
        raise UpdateError(
            "a .knowledge-move-* recovery directory is in the home; finish or reverse that "
            "interrupted move, remove the directory, and retry"
        )
    if paths.shared.is_symlink() or (paths.shared.exists() and not paths.shared.is_dir()):
        raise UpdateError("shared is not a directory; move it aside and retry")
    old, new = paths.home / "knowledge", paths.knowledge
    if (old.exists() or old.is_symlink()) and (
        new.is_symlink() or (new.exists() and (not new.is_dir() or any(new.iterdir())))
    ):
        raise UpdateError(
            "both knowledge/ and shared/knowledge/ exist; merge them into "
            "shared/knowledge/, remove knowledge/, and retry"
        )


def shared_knowledge_paths(paths: Paths) -> tuple[str, ...]:
    """The old and new roots, and each workspace root holding links to rename.

    Whole roots keep the declaration bounded by workspaces rather than by notes.
    """
    _refuse_shared_knowledge_conflicts(paths)
    home = paths.home.resolve()
    roots = (n.root.path.relative_to(home).as_posix() for n in _general_linked(paths))
    return tuple(dict.fromkeys(("knowledge", "shared", *roots)))


def move_shared_knowledge(paths: Paths) -> None:
    """Move ``knowledge/`` to ``shared/knowledge/`` and rename ``general:`` links to ``shared:``.

    Only parsed link targets change: prose, code, metadata, ``updated``, and the file's
    modification time stay, since a format migration is not a content update. A retry
    recognizes the completed move; a conflict stops the step before anything changes.
    """
    _refuse_shared_knowledge_conflicts(paths)
    old = paths.home / "knowledge"
    if old.exists() or old.is_symlink():
        with suppress(FileNotFoundError):
            paths.knowledge.rmdir()
        paths.shared.mkdir(exist_ok=True)
        old.rename(paths.knowledge)
    else:
        paths.knowledge.mkdir(parents=True, exist_ok=True)
    for note in _general_linked(paths):
        text = read_bytes(note.root, note.path).decode("utf-8")
        # Split as the catalog reads it: a leading block that is not a mapping is body.
        head, body = split_document(text) if frontmatter.parse(text)[0] else ("", text)
        for link in reversed(extract_links(body)):
            if _general(link.target):
                at = link.start + len(link.target) - len(link.target.lstrip())
                body = body[:at] + "shared" + body[at + len("general") :]
        path = note.root.path / note.path
        before = path.stat()
        publish(note.root, note.path, head + body, expected_hash=note.sha256)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _retired_jobs(paths: Paths) -> tuple[str, ...]:
    workspaces = {"default"}
    if paths.workspaces.is_dir() and not paths.workspaces.is_symlink():
        workspaces.update(
            entry.name for entry in paths.workspaces.iterdir() if valid_workspace_name(entry.name)
        )
    return (
        *(f"workspaces/{name}/jobs/enso-memory" for name in sorted(workspaces)),
        "workspaces/default/jobs/memory",
    )


def retire_history_paths(paths: Paths) -> tuple[str, ...]:
    """Snapshot obsolete processing state, leaving all retained note roots alone."""
    names = ("enso.db", ".bundles.json", "skills/enso-memory", *_retired_jobs(paths))
    update_snapshot.plan(paths, list(names))  # reject escaping/linked targets before any writes
    _retired_locks(paths)
    return names


def _retired_locks(paths: Paths) -> tuple[Path, ...]:
    root = paths.runtime_dir / "locks"
    locks = (
        root / "memory.lock",
        *(
            root / "jobs" / name.split("/")[1] / (name.split("/")[-1] + ".lock")
            for name in _retired_jobs(paths)
        ),
    )
    for lock in locks:
        if any(
            path.is_symlink() for path in (lock, *lock.parents) if path.is_relative_to(paths.home)
        ):
            raise UpdateError(f"{lock} must not be a symbolic link during retirement")
        if lock.exists() and (not lock.is_file() or lock.stat().st_size):
            raise UpdateError(f"{lock} must be an empty lock file during retirement")
    return locks


def _retire_history_database(paths: Paths) -> None:
    if not paths.db.exists():
        return
    with closing(sqlite3.connect(paths.db)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        revision = connection.execute("PRAGMA user_version").fetchone()[0]
        if application != 0x454E534F or revision not in (1, 2):
            raise UpdateError("expected an Enso database at schema 1 or 2")
        if revision == 1:
            connection.execute(
                "CREATE TABLE _enso_received_messages ("
                "transport TEXT NOT NULL, channel TEXT NOT NULL, message_id TEXT NOT NULL, "
                "PRIMARY KEY (transport, channel, message_id))"
            )
            connection.execute(
                "INSERT INTO _enso_received_messages SELECT transport, channel, message_id "
                "FROM _enso_captures WHERE kind != 'reply'"
            )
            for table in (
                "_enso_memory_inputs",
                "_enso_memory_receipts",
                "_enso_memory_progress",
                "_enso_memory_batches",
                "_enso_captures",
            ):
                connection.execute(f'DROP TABLE "{table}"')
            connection.execute("PRAGMA user_version = 2")
        retired = "job = 'enso-memory' OR (workspace = 'default' AND job = 'memory')"
        connection.execute(
            f"DELETE FROM _enso_run_attempts WHERE run_id IN (SELECT id FROM runs WHERE {retired})"
        )
        connection.execute(f"DELETE FROM runs WHERE {retired}")
        connection.execute(f"DELETE FROM job_state WHERE {retired}")
        connection.execute(
            "DELETE FROM messages WHERE source LIKE 'job:%:enso-memory' "
            "OR source = 'job:default:memory'"
        )


def retire_history(paths: Paths) -> None:
    """Retire the old transcript pipeline and its jobs without deleting Markdown archives."""
    retire_history_paths(paths)
    state = read_json(paths.home / ".bundles.json")
    files = state.get("files", {})
    if not isinstance(files, dict) or any(not isinstance(name, str) for name in files):
        raise UpdateError(".bundles.json must contain a mapping of file receipts")
    _retire_history_database(paths)
    retired = ("skills/enso-memory", *_retired_jobs(paths))
    for name in retired:
        target = paths.home / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    # Empty locks carry no rollback data; the updater has already stopped every writer.
    for lock in _retired_locks(paths):
        lock.unlink(missing_ok=True)
        if lock.parent != paths.runtime_dir / "locks":
            with suppress(OSError):
                lock.parent.rmdir()
    if state:
        state["files"] = {
            name: value
            for name, value in files.items()
            if not any(name == root or name.startswith(root + "/") for root in retired)
        }
        write_json(paths.home / ".bundles.json", state)


# 0.2.0 is revision zero. Keep every later step so installations may skip releases.
# Lock files hold nothing to restore, so the first step declares no snapshot paths.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "gather lock files under runtime/locks", lambda paths: (), remove_scattered_locks),
    Migration(
        2,
        "move knowledge to shared/knowledge and rename general links",
        shared_knowledge_paths,
        move_shared_knowledge,
    ),
    Migration(3, "retire conversation processing state", retire_history_paths, retire_history),
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
