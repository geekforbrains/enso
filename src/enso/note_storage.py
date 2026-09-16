"""Safe Markdown paths and bounded reads; no symlinks or hidden files are served."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from . import locks
from .config import Paths

MAX_NOTE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
EXCLUDED_DIRS = {"node_modules", "__pycache__"}


class NoteError(Exception):
    """A note reference or operation cannot safely be used."""


@dataclass(frozen=True)
class Root:
    """One independently named note directory."""

    scope: str
    label: str
    path: Path


def relative_parts(relative: str) -> tuple[str, ...]:
    """Refuse paths that address hidden/system files or leave the selected root."""
    path = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or "\x00" in relative
        or path.is_absolute()
        or any(p.startswith(".") or p in EXCLUDED_DIRS for p in path.parts)
        or any(ord(c) < 32 for c in relative)
    ):
        raise NoteError("note paths must be visible relative paths inside their scope")
    if not path.parts:
        raise NoteError("a note or asset path is required")
    return path.parts


def safe_path(root: Root, relative: str) -> Path:
    """Validate a path for display or writing; use read_bytes for race-safe file reads."""
    parts = relative_parts(relative)
    if any(path.is_symlink() for path in (root.path, *root.path.parents)):
        raise NoteError("note root ancestry must not contain symbolic links")
    current = root.path
    for part in parts:
        current /= part
        if current.is_symlink():
            raise NoteError("note paths must not contain symbolic links")
    if not current.resolve().is_relative_to(root.path.resolve()):
        raise NoteError("note path leaves its scope")
    return current


def read_bytes(root: Root, relative: str, *, limit: int = MAX_FILE_BYTES) -> bytes:
    """Read one regular file through directory descriptors without following symlinks."""
    directory, name = open_parent(root, relative)
    try:
        return read_at(directory, name, limit=limit)
    finally:
        os.close(directory)


def open_directory(path: Path, *, create: bool = False) -> int:
    """Open an absolute directory by walking every ancestor without following links."""
    absolute = path.absolute()
    directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=directory)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory
    except BaseException:
        os.close(directory)
        raise


def open_parent(root: Root, relative: str, *, create: bool = False) -> tuple[int, str]:
    """Pin a note's parent directory; the caller closes the returned descriptor."""
    parts = relative_parts(relative)
    directory = open_directory(root.path, create=create)
    try:
        for part in parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=directory)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return directory, parts[-1]
    except BaseException:
        os.close(directory)
        raise


def read_at(directory: int, name: str, *, limit: int = MAX_FILE_BYTES) -> bytes:
    """Read a regular file relative to an already pinned directory."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise NoteError("note entries must be regular files")
        if info.st_size > limit:
            raise NoteError(f"note files must be at most {limit} bytes")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise NoteError("note file grew beyond the read limit")
        return data


def split_document(text: str) -> tuple[str, str]:
    """Keep raw note metadata/body whitespace; shared parsing owns only YAML syntax."""
    match = re.match(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not match:
        return "", text
    return match[0], text[match.end() :]


def valid_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def valid_timestamp(value: Any) -> str | None:
    """Require a real instant, including timezone; file mtimes are not creation dates."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    except OverflowError:
        return None


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def writer(paths: Paths, kind: str) -> Iterator[None]:
    paths.home.mkdir(parents=True, exist_ok=True)
    try:
        fd = locks.acquire(paths.home / f".{kind}.lock")
    except BlockingIOError:
        raise NoteError(f"another {kind} write is in progress; retry") from None
    try:
        yield
    finally:
        os.close(fd)


def publish(root: Root, relative: str, text: str, *, expected_hash: str | None) -> None:
    """Atomically publish one file, refusing a stale revision or occupied new destination."""
    if len(text.encode("utf-8")) > MAX_NOTE_BYTES:
        raise NoteError(f"notes must be at most {MAX_NOTE_BYTES} bytes including metadata")
    path = safe_path(root, relative)
    if path.suffix.lower() != ".md":
        raise NoteError("notes must use the .md extension")
    directory, name = open_parent(root, relative, create=True)
    temporary = f".enso-note-{uuid4()}"
    try:
        if hash_at(directory, name) != expected_hash:
            raise NoteError("note changed or destination already exists; read it again")
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            if expected_hash is not None:
                mode = os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode & 0o777
                os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if hash_at(directory, name) != expected_hash:
            raise NoteError("note changed during write; read it again")
        if expected_hash is None:
            # A hard link is an atomic create-only publication, unlike replace().
            os.link(
                temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
            )
        else:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)


def hash_at(directory: int, name: str) -> str | None:
    try:
        return hashlib.sha256(read_at(directory, name)).hexdigest()
    except FileNotFoundError:
        return None


def body_only(body: str) -> None:
    lines = body.lstrip("\r\n").splitlines()
    if lines and lines[0].rstrip() == "---":
        raise NoteError("provide the Markdown body without frontmatter")
