"""Revision 7: preserve workspace overrides as JSON and discard the old Markdown body.

The historical schema is frozen here. Preflight every conversion before writing; the
stopped-home updater snapshots both filenames and owns recovery across workspaces.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

from . import frontmatter, update_snapshot
from .config import Paths, require_workspace, valid_workspace_name
from .maintenance import UpdateError, sync_directory, write_bytes


@dataclass(frozen=True)
class _Conversion:
    source: Path
    target: Path
    data: bytes
    mode: int


def _settings(fields: dict) -> bytes:
    """Validate the old settings shape without interpreting provider-specific values."""
    if set(fields) - {"agent", "providers"}:
        raise ValueError("unrecognized workspace setting")
    if "agent" in fields:
        agent = fields["agent"]
        if (
            not isinstance(agent, dict)
            or set(agent) != {"provider", "model", "effort"}
            or any(not isinstance(value, str) or not value for value in agent.values())
        ):
            raise ValueError("agent must contain nonempty provider, model, and effort strings")
    providers = fields.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("providers must be an object")
    for override in providers.values():
        if (
            not isinstance(override, dict)
            or set(override) != {"args"}
            or not isinstance(override["args"], list)
            or any(not isinstance(arg, str) for arg in override["args"])
        ):
            raise ValueError("each provider override must contain only an args list of strings")
    return (json.dumps(fields, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _prepare(paths: Paths) -> tuple[_Conversion, ...]:
    update_snapshot.plan(paths, ["workspaces"])
    if not paths.workspaces.exists():
        return ()
    if not paths.workspaces.is_dir():
        raise UpdateError(f"{paths.workspaces} must be a directory")
    conversions = []
    for root in sorted(paths.workspaces.iterdir()):
        if not valid_workspace_name(root.name) or not (root.is_dir() or root.is_symlink()):
            continue
        try:
            require_workspace(paths, root.name)
            source, target = root / "WORKSPACE.md", root / "workspace.json"
            if not source.exists() and not source.is_symlink():
                continue
            update_snapshot.plan(
                paths, [path.relative_to(paths.home).as_posix() for path in (source, target)]
            )
            data = _settings(frontmatter.read(source).fields)
            # Only the exact output from an interrupted conversion may already exist.
            if target.exists() and (not target.is_file() or target.read_bytes() != data):
                raise UpdateError(f"{target} conflicts with {source}; resolve the files and retry")
            conversions.append(
                _Conversion(source, target, data, stat.S_IMODE(source.stat().st_mode))
            )
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise UpdateError(
                f"{root / 'WORKSPACE.md'}: cannot migrate workspace settings: {exc}"
            ) from exc
    return tuple(conversions)


def workspace_settings_paths(paths: Paths) -> tuple[str, ...]:
    """Declare old files and new destinations after checking all conversions."""
    return tuple(
        path.relative_to(paths.home).as_posix()
        for conversion in _prepare(paths)
        for path in (conversion.source, conversion.target)
    )


def migrate_workspace_settings(paths: Paths) -> None:
    """Publish JSON before retiring each Markdown file; rerunning is harmless."""
    for conversion in _prepare(paths):
        if not conversion.target.exists():
            write_bytes(conversion.target, conversion.data, mode=conversion.mode)
        conversion.source.unlink()
        sync_directory(conversion.source.parent)
