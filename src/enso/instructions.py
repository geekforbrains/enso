"""Immediate validation of Enso's shared provider-discovery boundary."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import config

if TYPE_CHECKING:
    from .teams import Workspace

# Retained as a module-level path so isolated callers and tests can redirect
# the standalone shared-file validator. The complete launch validator derives
# every managed path from config.CONFIG_DIR instead.
CONFIG_DIR = config.CONFIG_DIR

# Grok and Agy carry this content in one argv element. Linux limits one execve
# argument to 128 KiB, and encoding can expand control bytes substantially.
MAX_SHARED_INSTRUCTION_BYTES = 20 * 1024

_READ_CHUNK_SIZE = 64 * 1024


class InstructionError(RuntimeError):
    """A provider's instruction discovery boundary cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ValidatedInstructions:
    """The freshly validated shared instructions for one imminent launch."""

    source_path: str
    content: str
    revision: str


def _stat_identity(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_uid,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        file_stat.st_nlink,
    )


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _validate_file_stat(
    file_stat: os.stat_result,
    *,
    description: str,
    size_limit: int,
) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise InstructionError(f"{description} must be a regular, non-symlink file")
    if file_stat.st_uid != os.getuid():
        raise InstructionError(f"{description} must be owned by the current user")
    if file_stat.st_size > size_limit:
        raise InstructionError(f"{description} exceeds the {size_limit}-byte limit")


def _read_owned_regular_file(
    path: str,
    *,
    description: str,
    missing_message: str,
    size_limit: int,
) -> tuple[bytes, os.stat_result]:
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError as exc:
        raise InstructionError(missing_message) from exc
    except OSError as exc:
        raise InstructionError(f"could not inspect {description}: {exc}") from exc

    _validate_file_stat(path_stat, description=description, size_limit=size_limit)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise InstructionError(f"{description} changed while it was being read") from exc
    except OSError as exc:
        raise InstructionError(f"could not open {description} safely: {exc}") from exc

    try:
        opened_stat = os.fstat(descriptor)
        _validate_file_stat(opened_stat, description=description, size_limit=size_limit)
        if not _same_file(path_stat, opened_stat):
            raise InstructionError(f"{description} changed while it was being read")

        content = bytearray()
        while len(content) <= size_limit:
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_SIZE, size_limit + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > size_limit:
            raise InstructionError(f"{description} exceeds the {size_limit}-byte limit")

        final_stat = os.fstat(descriptor)
        try:
            final_path_stat = os.lstat(path)
        except OSError as exc:
            raise InstructionError(f"{description} changed while it was being read") from exc
        if (
            _stat_identity(opened_stat) != _stat_identity(final_stat)
            or _stat_identity(final_stat) != _stat_identity(final_path_stat)
            or len(content) != final_stat.st_size
        ):
            raise InstructionError(f"{description} changed while it was being read")
        return bytes(content), final_stat
    finally:
        os.close(descriptor)


def _validate_shared_instructions_at(root: str) -> ValidatedInstructions:
    source_path = os.path.join(root, "AGENTS.md")
    raw, source_stat = _read_owned_regular_file(
        source_path,
        description="shared instruction file",
        missing_message=f"shared instruction file is missing: {source_path}",
        size_limit=MAX_SHARED_INSTRUCTION_BYTES,
    )
    if stat.S_IMODE(source_stat.st_mode) & 0o022:
        raise InstructionError(
            "shared instruction file must not be group- or other-writable"
        )
    if source_stat.st_nlink != 1:
        raise InstructionError("shared instruction file must not have additional hard links")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstructionError("shared instruction file must contain valid UTF-8") from exc
    if "\x00" in content:
        raise InstructionError("shared instruction file must not contain NUL bytes")

    return ValidatedInstructions(
        source_path=source_path,
        content=content,
        revision=hashlib.sha256(raw).hexdigest(),
    )


def validate_shared_instructions(root: str | None = None) -> ValidatedInstructions:
    """Validate and freshly read one root ``AGENTS.md`` file."""
    configured_root = CONFIG_DIR if root is None else root
    absolute_root = os.path.abspath(os.path.expanduser(configured_root))
    return _validate_shared_instructions_at(absolute_root)


def validate_launch_discovery(workspace: Workspace) -> ValidatedInstructions:
    """Validate all native-discovery invariants immediately before launch.

    This check is deliberately read-only. It accepts only the workspace path
    derived from its canonical name, an exact Enso Git worktree, both levels of
    the managed scaffold, and non-overlapping global/workspace skill names.
    The shared instruction bytes are read only after those boundaries pass.
    """
    from .config import ConfigError, managed_workspace_path
    from .repository import EnsoRepository, RepositoryError
    from .scaffolding import ScaffoldError, ScaffoldService

    try:
        expected_workspace_path = managed_workspace_path(workspace.name)
    except ConfigError as exc:
        raise InstructionError(str(exc)) from exc

    if workspace.path != expected_workspace_path:
        raise InstructionError(
            f"workspace {workspace.name!r} must use its exact name-derived path "
            f"{expected_workspace_path}, not {workspace.path}"
        )

    root = os.path.abspath(os.path.expanduser(config.CONFIG_DIR))
    try:
        EnsoRepository(root).validate()
    except RepositoryError as exc:
        raise InstructionError(f"launch discovery repository is invalid: {exc}") from exc

    scaffold = ScaffoldService(root)
    try:
        global_report = scaffold.validate_global()
        workspace_report = scaffold.validate_workspace(workspace.name)
    except ScaffoldError as exc:
        raise InstructionError(f"launch discovery scaffold is invalid: {exc}") from exc

    errors = (*global_report.errors, *workspace_report.errors)
    if errors:
        raise InstructionError("launch discovery scaffold is invalid: " + "; ".join(errors))

    return validate_shared_instructions(root)
