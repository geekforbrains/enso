"""Revision 6: replace the recognized bundled audit's agent with the doctor command.

Historical recognition is frozen here, independent of the current bundled files. The
stopped-home updater snapshots these paths and owns rollback across atomic publications.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from . import frontmatter, update_snapshot
from .config import Paths, valid_workspace_name
from .job_migration import _children, _directory, _document, _regular
from .maintenance import UpdateError, read_json, write_bytes, write_json

_OLD_BODY_HASH = "aa2c21bc7981674454a7990aee0a1ed317a1b0e119d41760a59f33c0f4f1567c"
_OLD_SCRIPT_HASHES = {
    "87c1bb78d9326647eced78b17bde1a457b630353667e01a4f66b7163e00175d4",
    "21e31601f171609098722b56a73cb346f45d1682af13e653deb51a514cf05b20",
}
_COMMAND = "enso doctor --attention --notify --quiet"
_COMMAND_BODY = (
    "Run Enso's health audit and send a concise report to the configured notification\n"
    "target when attention is needed. Healthy checks stay quiet. This job does not use\n"
    "an LLM or repair findings."
)
_DEFAULT_FIELDS = {
    "name": "Enso audit",
    "schedule": "0 3 * * *",
    "enabled": True,
    "catch_up": True,
    "command": _COMMAND,
}
_AGENT_FIELDS = {"provider", "model", "effort", "max_followups"}
_OPERATOR_FIELDS = {
    "name",
    "schedule",
    "enabled",
    "catch_up",
    "misfire_grace_seconds",
    "timeout",
    "notify",
    "secrets",
    "concurrency",
    "concurrency_group",
}


@dataclass(frozen=True)
class _Audit:
    path: Path
    before: bytes | None = None
    after: bytes | None = None
    mode: int = 0o600
    retire_script: bool = False
    pristine_settings: bool = False


def _legacy_executor(fields: dict) -> bool:
    """Recognize both layouts: the complete upgrade plan runs before revision 5."""
    if set(fields) - (
        _OPERATOR_FIELDS | _AGENT_FIELDS | {"agent", "prerun", "prerun_timeout", "gate"}
    ):
        return False
    if "agent" in fields:
        agent = fields["agent"]
        if (
            not isinstance(agent, dict)
            or set(agent) - _AGENT_FIELDS
            or not {"provider", "model", "effort"} <= set(agent)
            or set(fields) & _AGENT_FIELDS
        ):
            return False
    elif not {"provider", "model", "effort"} <= set(fields):
        return False
    if "gate" in fields:
        return (
            "prerun" not in fields
            and "prerun_timeout" not in fields
            and fields["gate"]
            in (
                {"command": "bash prerun.sh"},
                {"command": "bash prerun.sh", "timeout": 120},
            )
        )
    return fields.get("prerun") == "prerun.sh" and fields.get("prerun_timeout", 120) == 120


def _audit(path: Path) -> _Audit:
    if not path.exists() and not path.is_symlink():
        return _Audit(path)
    document, before, _ = _document(path)
    fields = dict(document.fields)
    script = path.parent / "prerun.sh"
    script_known = (script.exists() or script.is_symlink()) and hashlib.sha256(
        _regular(script)
    ).hexdigest() in _OLD_SCRIPT_HASHES
    body = document.body.replace("{{prerun_output}}", "{{gate_output}}")
    legacy = (
        hashlib.sha256(body.encode()).hexdigest() == _OLD_BODY_HASH
        and _legacy_executor(fields)
        and script_known
    )
    converted = (
        fields.get("command") == _COMMAND
        and set(fields) <= _OPERATOR_FIELDS | {"command"}
        and " ".join(document.body.split()) == " ".join(_COMMAND_BODY.split())
    )
    if not legacy and not converted:
        return _Audit(path, before)
    if legacy:
        for name in _AGENT_FIELDS | {"agent", "prerun", "prerun_timeout", "gate"}:
            fields.pop(name, None)
        fields["command"] = _COMMAND
        after = frontmatter.render(fields, _COMMAND_BODY).encode()
    else:
        after = before
    return _Audit(
        path,
        before,
        after,
        stat.S_IMODE(path.stat().st_mode),
        script_known,
        fields == _DEFAULT_FIELDS,
    )


def _prepare(paths: Paths) -> tuple[_Audit, ...]:
    update_snapshot.plan(paths, [".bundles.json", "workspaces"])
    receipts = read_json(paths.home / ".bundles.json").get("files", {})
    if not isinstance(receipts, dict) or any(not isinstance(name, str) for name in receipts):
        raise UpdateError(".bundles.json must contain a mapping of file receipts")
    if not _directory(paths.workspaces):
        return ()
    found = []
    # Revision 5's immutable readers bound each read and reject linked/nonregular paths.
    # Only the named audit in each workspace is inspected, never arbitrary job scripts.
    for workspace in _children(paths.workspaces):
        if not valid_workspace_name(workspace.name):
            continue
        if not _directory(workspace) or not _directory(workspace / "jobs"):
            continue
        directory = workspace / "jobs" / "enso-audit"
        if _directory(directory):
            found.append(_audit(directory / "JOB.md"))
    return tuple(found)


def audit_job_paths(paths: Paths) -> tuple[str, ...]:
    """Preflight historical definitions before any pending migration changes the home."""
    changes = _prepare(paths)
    return (
        ".bundles.json",
        *(change.path.parent.relative_to(paths.home).as_posix() for change in changes),
    )


def migrate_audit_jobs(paths: Paths) -> None:
    """Retain customized pairs and operational settings through later bundle refreshes."""
    changes = _prepare(paths)
    state = read_json(paths.home / ".bundles.json")
    receipts = state.get("files", {})
    updated = dict(receipts)
    for change in changes:
        name = change.path.relative_to(paths.home).as_posix()
        script = change.path.parent / "prerun.sh"
        # Retained agent prompts still need their script. Retirement must never remove
        # it just because that independently unmodified file has a matching receipt.
        updated.pop(script.relative_to(paths.home).as_posix(), None)
        if change.before is None:
            continue  # Keep the JOB.md receipt: its absence is an intentional deletion.
        if change.after is None or not change.pristine_settings:
            # Either file may match its receipt even when its paired file is customized.
            # Detach ownership instead of letting bundle refresh overwrite those choices.
            updated.pop(name, None)
        elif receipts.get(name) == hashlib.sha256(change.before).hexdigest():
            updated[name] = hashlib.sha256(change.after).hexdigest()
    # Publish receipts first. A retry after interrupted file writes recognizes either
    # the historical pair or the completed command without blessing operator changes.
    if updated != receipts:
        write_json(paths.home / ".bundles.json", {**state, "files": updated})
    for change in changes:
        if change.after is not None and change.after != change.before:
            write_bytes(change.path, change.after, mode=change.mode)
        if change.retire_script:
            (change.path.parent / "prerun.sh").unlink(missing_ok=True)
