"""One-time, user-owned knowledge starter for fresh homes, outside bundle reconciliation."""

from __future__ import annotations

import os
import stat
import tempfile
from importlib import resources
from pathlib import Path

from . import knowledge
from .config import Paths
from .maintenance import UpdateError, read_json, write_json
from .note_storage import open_directory, writer

RECEIPT = "knowledge-starter.json"
FOLDERS = ("!Inbox", "Meta", "Memory", "People", "Projects", "Areas", "Topics", "Ideas", "Archive")
NOTES = ("Meta/Guide.md", "Meta/Index.md", "Meta/Templates/Person.md", "Meta/Templates/Project.md")


def begin(paths: Paths) -> None:
    """Record fresh-home intent before any scaffold makes the home look initialized."""
    receipt = paths.runtime_dir / RECEIPT
    if not receipt.exists() and not receipt.is_symlink():
        write_json(receipt, {"version": 1, "state": "pending"})


def _collection_exists(directory: int) -> bool:
    try:
        mode = os.stat("knowledge", dir_fd=directory, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(mode):
        raise UpdateError("shared/knowledge must be a real directory; existing path was preserved")
    return True


def seed(paths: Paths) -> list[str]:
    """Finish a recorded fresh bootstrap, never filling or replacing an existing collection.

    Consume the one-time receipt before publishing all notes together. An interruption
    in that narrow window can omit the optional starter, but a later retry can never
    recreate content the user removed after publication. Existing homes have no receipt,
    so preparing or updating them never seeds notes.
    """
    # External parent aliases such as macOS /tmp are valid; the home itself and its
    # managed descendants still pass through the no-follow directory traversal below.
    paths = Paths(paths.home.parent.resolve() / paths.home.name)
    receipt = paths.runtime_dir / RECEIPT
    state = read_json(receipt)
    if not state and not receipt.exists():
        return []
    if state.get("version") != 1 or state.get("state") not in ("pending", "complete"):
        raise UpdateError(f"{RECEIPT} has an unsupported state")
    if state["state"] == "complete":
        return []
    changes: list[str] = []
    with writer(paths):
        # Pin shared/ without following any ancestor links, including changes after preflight.
        directory = open_directory(paths.knowledge.parent, create=True)
        try:
            if _collection_exists(directory):
                write_json(receipt, {"version": 1, "state": "complete"})
                return changes
            # A scratch home lets the normal writer generate and validate IDs and dates.
            with tempfile.TemporaryDirectory(
                dir=paths.runtime_dir, prefix=".knowledge-starter-"
            ) as temporary:
                staged = Paths(Path(temporary))
                for folder in FOLDERS:
                    (staged.knowledge / folder).mkdir(parents=True, exist_ok=True)
                bundled = resources.files("enso").joinpath("bundled", "shared", "knowledge")
                for relative in NOTES:
                    knowledge.create_note(
                        staged, "shared", relative, bundled.joinpath(relative).read_text("utf-8")
                    )
                write_json(receipt, {"version": 1, "state": "complete"})
                if not _collection_exists(directory):
                    os.rename(staged.knowledge, "knowledge", dst_dir_fd=directory)
                    os.fsync(directory)
                    changes.append(f"created knowledge starter in {paths.knowledge}")
        finally:
            os.close(directory)
    return changes
