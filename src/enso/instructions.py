"""Home and workspace ``AGENTS.md``: bounded reads and revision-checked atomic saves.

The instructions are the operator's file, and agents edit them too, so a save names the
revision it replaces and is refused when the file has changed since. Directories are
opened without following links, a linked or special ``AGENTS.md`` is never read or
written through, and the new text replaces the old in one rename. ``CLAUDE.md`` links to
the name, so it keeps pointing at the saved file.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .config import Paths, require_workspace
from .note_storage import open_directory

FILENAME = "AGENTS.md"
MAX_BYTES = 128 * 1024
NEW_FILE_MODE = 0o644


class InstructionsError(Exception):
    """The instructions cannot be read or saved as asked; the message says why."""


class StaleRevisionError(InstructionsError):
    """The file changed after the editor loaded it."""


@dataclass(frozen=True)
class Instructions:
    path: Path
    text: str
    revision: str | None  # sha256 of the bytes on disk; None while the file is missing


def directory(paths: Paths, workspace: str | None) -> Path:
    """The home for ``None``, else the named workspace; ``ValueError`` if it is not one."""
    home = paths.home.resolve()
    if workspace is None:
        if paths.home.is_symlink() or not home.is_dir():
            raise ValueError(f"home directory {paths.home} must be a real directory")
        return home
    return require_workspace(Paths(home), workspace)


def read(paths: Paths, workspace: str | None) -> Instructions:
    folder = directory(paths, workspace)
    pinned = _open(folder)
    try:
        data = _read_at(pinned)
    finally:
        os.close(pinned)
    return Instructions(folder / FILENAME, _decode(data), revision(data))


def save(paths: Paths, workspace: str | None, text: str, *, expected: str | None) -> None:
    """Replace the file if it is still at ``expected``; ``None`` means create it."""
    data = text.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise InstructionsError(f"{FILENAME} must be at most {MAX_BYTES // 1024} KiB")
    pinned = _open(directory(paths, workspace))
    temporary = f".{FILENAME}.{uuid4().hex}.tmp"
    try:
        current = _read_at(pinned)
        _require(current, expected)
        mode = (
            NEW_FILE_MODE
            if current is None
            else os.stat(FILENAME, dir_fd=pinned, follow_symlinks=False).st_mode & 0o777
        )
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=pinned
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Checked again just before publishing, which narrows the window to the rename.
        _require(_read_at(pinned), expected)
        if expected is None:
            try:  # A hard link publishes atomically without replacing a file created since.
                os.link(
                    temporary, FILENAME, src_dir_fd=pinned, dst_dir_fd=pinned, follow_symlinks=False
                )
            except FileExistsError:
                raise StaleRevisionError(f"{FILENAME} was created since this page loaded") from None
        else:
            os.replace(temporary, FILENAME, src_dir_fd=pinned, dst_dir_fd=pinned)
        os.fsync(pinned)
    finally:
        try:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=pinned)
        finally:
            os.close(pinned)


def revision(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _open(folder: Path) -> int:
    try:
        return open_directory(folder)
    except OSError as exc:
        raise InstructionsError(f"{folder} cannot be opened safely ({exc.strerror})") from exc


def _read_at(pinned: int) -> bytes | None:
    """The current bytes, or None when absent; refuses links, special files, and oversize."""
    try:
        fd = os.open(FILENAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=pinned)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise InstructionsError(f"{FILENAME} is a symbolic link; edit its target") from exc
        raise InstructionsError(f"{FILENAME} cannot be read ({exc.strerror})") from exc
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InstructionsError(f"{FILENAME} is not a regular file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InstructionsError(
            f"{FILENAME} is larger than {MAX_BYTES // 1024} KiB; edit it in a text editor"
        )
    return data


def _decode(data: bytes | None) -> str:
    if data is None:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InstructionsError(f"{FILENAME} is not UTF-8 text; edit it in a text editor") from None


def _require(current: bytes | None, expected: str | None) -> None:
    if revision(current) != expected:
        raise StaleRevisionError(f"{FILENAME} changed since this page loaded")
