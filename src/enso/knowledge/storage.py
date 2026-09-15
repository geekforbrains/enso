"""Safe knowledge paths and bounded reads; no symlinks or hidden files are served."""

from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 32 * 1024 * 1024
EXCLUDED_DIRS = {"node_modules", "__pycache__"}


class KnowledgeError(Exception):
    """A knowledge reference or operation cannot safely be used."""


@dataclass(frozen=True)
class Root:
    """One independently named knowledge directory."""

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
        raise KnowledgeError("knowledge paths must be visible relative paths inside their scope")
    if not path.parts:
        raise KnowledgeError("a note or asset path is required")
    return path.parts


def safe_path(root: Root, relative: str) -> Path:
    """Validate a path for display or writing; use read_bytes for race-safe file reads."""
    parts = relative_parts(relative)
    if any(path.is_symlink() for path in (root.path, *root.path.parents)):
        raise KnowledgeError("knowledge root ancestry must not contain symbolic links")
    current = root.path
    for part in parts:
        current /= part
        if current.is_symlink():
            raise KnowledgeError("knowledge paths must not contain symbolic links")
    if not current.resolve().is_relative_to(root.path.resolve()):
        raise KnowledgeError("knowledge path leaves its scope")
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
            raise KnowledgeError("knowledge entries must be regular files")
        if info.st_size > limit:
            raise KnowledgeError(f"knowledge files must be at most {limit} bytes")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise KnowledgeError("knowledge file grew beyond the read limit")
        return data


def split_document(text: str) -> tuple[str, str]:
    """Keep raw knowledge metadata/body whitespace; shared parsing owns only YAML syntax."""
    match = re.match(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not match:
        return "", text
    return match[0], text[match.end() :]
