"""Revision 5's stopped-home conversion from implicit agents to explicit job executors.

This reads the historical fields directly, never through the current job/config parser.
File writes are atomic and repeatable; the updater owns the enclosing home snapshot.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import frontmatter, update_snapshot
from .config import Paths, valid_workspace_name
from .maintenance import UpdateError, read_json, write_bytes, write_json

_MAX_FILES = 10_000
_MAX_DOCUMENT = 1024 * 1024
_AGENT_FIELDS = ("provider", "model", "effort", "max_followups")
_UPDATE_SCRIPT = (
    b"#!/usr/bin/env bash\n"
    b"# Sending is handled by the CLI, which records only successfully delivered notices.\n"
    b"# An offline check remains quiet and retries at the next nightly slot.\n"
    b"enso update check --notify --quiet\nexit 1\n"
)
_UPDATE_BODY = (
    "The prerun performs a deterministic release check and delivers a notification only\n"
    "when a newer release has not already been announced. It never opens the agent gate.\n"
    "Do not install updates from this job. The operator requests an upgrade in chat or\n"
    "with `enso update apply` when ready."
)
_COMMAND_BODY = (
    "Check for a newer release and notify the operator once per version. This job never\n"
    "installs updates; the operator requests an upgrade in chat or with `enso update apply`\n"
    "when ready."
)
_UPDATE_COMMAND = "enso update check --notify --quiet"


@dataclass(frozen=True)
class _Rewrite:
    path: Path
    before: bytes
    after: bytes
    mode: int
    retired_script: Path | None = None
    preserve_bundle: bool = False


def _regular(path: Path) -> bytes:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise UpdateError(f"{path} must be a regular file for job migration")
            data = file.read(_MAX_DOCUMENT + 1)
        if len(data) > _MAX_DOCUMENT:
            raise UpdateError(f"{path} exceeds the 1 MiB job migration limit")
        return data
    except OSError as exc:
        raise UpdateError(f"cannot read {path} for job migration: {exc}") from exc


def _document(path: Path) -> tuple[frontmatter.Document, bytes, str]:
    data = _regular(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError(f"{path} must be UTF-8 for job migration") from exc
    document, problem = frontmatter.parse(text)
    if document is None:
        raise UpdateError(f"{path} {problem}")
    # Preserve the prompt's original whitespace and line endings, including a trailing newline.
    lines = text.splitlines(keepends=True)
    end = next(i for i in range(1, len(lines)) if lines[i].rstrip() == "---")
    return document, data, "".join(lines[end + 1 :])


def _directory(path: Path) -> bool:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise UpdateError(f"{path} must be a real directory for job migration")
    return path.exists()


def _children(path: Path) -> tuple[Path, ...]:
    entries: list[Path] = []
    with os.scandir(path) as scan:
        for entry in scan:
            if len(entries) >= _MAX_FILES:
                raise UpdateError(f"{path}: too many entries for job migration (limit 10000)")
            entries.append(Path(entry.path))
    return tuple(sorted(entries))


def _job_paths(paths: Paths) -> tuple[Path, ...]:
    if not _directory(paths.home) or not _directory(paths.workspaces):
        return ()
    found = []
    count = 0
    prior_revision = read_json(paths.home / ".migrations.json").get("revision", 0)
    # Traverse only the two declared levels; scripts and user data under jobs are not read.
    for workspace in _children(paths.workspaces):
        count += 1
        if count > _MAX_FILES:
            raise UpdateError("too many workspace/job entries for job migration (limit 10000)")
        if not valid_workspace_name(workspace.name):
            continue
        if not _directory(workspace) or not _directory(workspace / "jobs"):
            continue
        for job in _children(workspace / "jobs"):
            count += 1
            if count > _MAX_FILES:
                raise UpdateError("too many workspace/job entries for job migration (limit 10000)")
            if job.name.startswith("."):
                continue
            # A multi-release preview sees these before revision 3 removes them.
            if prior_revision < 3 and (
                job.name == "enso-memory" or (workspace.name == "default" and job.name == "memory")
            ):
                continue
            if not job.is_symlink() and not job.is_dir():
                continue
            if not _directory(job):
                continue
            path = job / "JOB.md"
            if path.exists() or path.is_symlink():
                found.append(path)
    return tuple(found)


def _project_executor(paths: Paths, path: Path, fields: dict) -> bool:
    project, stage = fields.get("project"), fields.get("stage")
    if project is None and stage is None:
        return False
    if not isinstance(project, str) or not isinstance(stage, str):
        raise UpdateError(f"{path}: project and stage must both be text")
    if Path(project).name != project or project in {".", ".."}:
        raise UpdateError(f"{path}: project must be a directory name")
    definition = path.parents[2] / "projects" / project / "PROJECT.md"
    update_snapshot.plan(paths, [definition.relative_to(paths.home).as_posix()])
    document, _, _ = _document(definition)
    stages = document.fields.get("stages")
    if not isinstance(stages, list):
        raise UpdateError(f"{definition}: stages must be a list")
    for entry in stages:
        if isinstance(entry, dict) and entry.get("name") == stage:
            return entry.get("command") is not None or entry.get("integrate") is True
        if isinstance(entry, str) and entry == stage:
            return False
    raise UpdateError(f"{path}: stage is not present in {definition}")


def _convert(paths: Paths, path: Path) -> _Rewrite:
    document, before, body = _document(path)
    fields = dict(document.fields)
    old_agent = {key: fields.pop(key) for key in _AGENT_FIELDS if key in fields}
    project_executor = _project_executor(paths, path, fields)
    if old_agent:
        if "agent" in fields or "command" in fields:
            raise UpdateError(f"{path}: legacy agent fields conflict with agent/command")
        if not project_executor:
            if any(key not in old_agent for key in _AGENT_FIELDS[:3]):
                raise UpdateError(f"{path}: legacy agent needs provider, model, and effort")
            fields["agent"] = old_agent
    elif not project_executor and "agent" not in fields and "command" not in fields:
        raise UpdateError(f"{path}: missing agent or command executor")
    for old, new in (("prerun", "gate"), ("postrun", "postrun")):
        value = fields.get(old)
        timeout = fields.pop(f"{old}_timeout", 120)
        if value is None:
            continue
        if isinstance(value, dict) and old == new:
            continue
        if not isinstance(value, str) or not value.strip():
            raise UpdateError(f"{path}: legacy {old} must name a script")
        if old != new and new in fields:
            raise UpdateError(f"{path}: {old} conflicts with {new}")
        fields.pop(old)
        fields[new] = {"command": f"bash {shlex.quote(value)}", "timeout": timeout}
    if "concurrency_group" in fields:
        if "concurrency" in fields:
            raise UpdateError(f"{path}: concurrency_group conflicts with concurrency")
        fields["concurrency"] = {"group": fields.pop("concurrency_group"), "on_busy": "skip"}
    body = body.replace("{{prerun_output}}", "{{gate_output}}")
    script = path.parent / "prerun.sh"
    retired = None
    # Only this shipped deterministic script has a known no-agent meaning. Custom gates
    # that exit 1 remain gates; interpreting their shell programs would lose user intent.
    old_update = (
        path.parent.name == "enso-update"
        and document.body == _UPDATE_BODY
        and document.fields.get("prerun") == "prerun.sh"
        and set(document.fields)
        <= {"name", "schedule", "provider", "model", "effort", "enabled", "prerun", "catch_up"}
    )
    converted_update = (
        path.parent.name == "enso-update"
        and fields.get("command") == _UPDATE_COMMAND
        and document.body == _COMMAND_BODY
    )
    if (
        (old_update or converted_update)
        and (script.exists() or script.is_symlink())
        and _regular(script) == _UPDATE_SCRIPT
    ):
        fields.pop("agent", None)
        fields.pop("gate", None)
        fields["command"] = _UPDATE_COMMAND
        body = f"\n{_COMMAND_BODY}\n"
        retired = script
    # Normalize the YAML only. Prompt whitespace is not a formatting preference to infer.
    head = frontmatter.render(fields, "").split("\n---\n", 1)[0] + "\n---\n"
    return _Rewrite(
        path,
        before,
        (head + body).encode(),
        stat.S_IMODE(path.stat().st_mode),
        retired,
        path.parent.name == "enso-update" and "agent" in fields,
    )


def _database_version(paths: Paths) -> int | None:
    if not paths.db.exists():
        return None
    update_snapshot.plan(paths, ["enso.db"])
    with closing(sqlite3.connect(f"{paths.db.as_uri()}?mode=ro", uri=True)) as connection:
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if application != 0x454E534F or version not in (1, 2, 3, 4):
            raise UpdateError("expected an Enso database at schema 1 through 4")
        return version


def _prepare(paths: Paths) -> tuple[_Rewrite, ...]:
    update_snapshot.plan(paths, ["enso.db", ".bundles.json", "workspaces"])
    _database_version(paths)
    receipts = read_json(paths.home / ".bundles.json").get("files", {})
    if not isinstance(receipts, dict) or any(not isinstance(name, str) for name in receipts):
        raise UpdateError(".bundles.json must contain a mapping of file receipts")
    return tuple(_convert(paths, path) for path in _job_paths(paths))


def job_executor_paths(paths: Paths) -> tuple[str, ...]:
    """Preview every conversion and conflict before the updater takes its snapshot."""
    changes = _prepare(paths)
    roots = (change.path.parent.parent.relative_to(paths.home).as_posix() for change in changes)
    return tuple(dict.fromkeys(("enso.db", ".bundles.json", *roots)))


def _database(paths: Paths) -> None:
    version = _database_version(paths)
    if version is None or version == 4:
        return
    if version != 3:
        raise UpdateError(f"job migration requires database schema 3, found {version}")
    with closing(sqlite3.connect(paths.db)) as connection, connection:
        connection.execute("PRAGMA legacy_alter_table = ON")
        connection.execute("BEGIN IMMEDIATE")
        # Keep user-defined indexes and triggers on the public history table too.
        dependent = connection.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = 'runs' AND sql IS NOT NULL "
            "AND type IN ('index', 'trigger') AND name != '_enso_runs_delete_attempts'"
        ).fetchall()
        connection.execute(
            "CREATE TABLE _enso_runs_v4 ("
            "id TEXT PRIMARY KEY, job TEXT NOT NULL, workspace TEXT NOT NULL, "
            "kind TEXT NOT NULL CHECK (kind IN ('agent', 'command', 'integration')), "
            "provider TEXT, model TEXT, effort TEXT, trigger TEXT NOT NULL, "
            "started_at TEXT NOT NULL, ended_at TEXT, duration_ms INTEGER, status TEXT NOT NULL, "
            "exit_code INTEGER, output TEXT, error TEXT, session_id TEXT, postrun_error TEXT)"
        )
        connection.execute(
            "INSERT INTO _enso_runs_v4 SELECT id, job, workspace, "
            "CASE WHEN provider = 'command' THEN 'command' ELSE 'agent' END, "
            "CASE WHEN provider != 'command' THEN provider END, "
            "CASE WHEN provider != 'command' THEN model END, "
            "CASE WHEN provider != 'command' THEN effort END, trigger, started_at, ended_at, "
            "duration_ms, CASE WHEN status = 'prerun_error' THEN 'gate_error' ELSE status END, "
            "exit_code, output, error, session_id, postrun_error FROM runs ORDER BY rowid"
        )
        connection.execute("DROP TABLE runs")
        connection.execute("ALTER TABLE _enso_runs_v4 RENAME TO runs")
        for (statement,) in dependent:
            connection.execute(statement)
        connection.execute(
            "CREATE TABLE _enso_job_waiters (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT NOT NULL UNIQUE, workspace TEXT NOT NULL, job TEXT NOT NULL, "
            "group_name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX _enso_job_waiters_group ON _enso_job_waiters (group_name, sequence)"
        )
        connection.execute(
            "CREATE TRIGGER _enso_runs_delete_attempts AFTER DELETE ON runs BEGIN "
            "DELETE FROM _enso_run_attempts WHERE run_id = OLD.id; "
            "DELETE FROM _enso_job_waiters WHERE run_id = OLD.id; END"
        )
        connection.execute(
            "UPDATE _enso_run_attempts SET status = 'gate_error' WHERE status = 'prerun_error'"
        )
        connection.execute("PRAGMA user_version = 4")


def migrate_job_executors(paths: Paths) -> None:
    """Convert all workspace jobs and history, retaining custom data and execution semantics."""
    changes = _prepare(paths)
    state = read_json(paths.home / ".bundles.json")
    receipts = state.get("files", {})
    updated = dict(receipts)
    for change in changes:
        name = change.path.relative_to(paths.home).as_posix()
        if change.preserve_bundle:
            # A custom/deleted release-check script or customized prompt keeps this job
            # as an agent with a gate. Do not authorize the new command-only bundle to
            # overwrite it, or let bundle retirement remove the gate it still invokes.
            # The old JOB.md receipt continues to record bundle ownership/deletions.
            script = change.path.parent / "prerun.sh"
            updated.pop(script.relative_to(paths.home).as_posix(), None)
        elif receipts.get(name) == hashlib.sha256(change.before).hexdigest():
            updated[name] = hashlib.sha256(change.after).hexdigest()
        if change.retired_script is not None:
            updated.pop(change.retired_script.relative_to(paths.home).as_posix(), None)
    _database(paths)
    # Record expected migrated hashes before publishing files. A retry can then finish
    # any interrupted publications without misclassifying them as operator edits.
    if updated != receipts:
        write_json(paths.home / ".bundles.json", {**state, "files": updated})
    for change in changes:
        if change.before != change.after:
            write_bytes(change.path, change.after, mode=change.mode)
        if change.retired_script is not None:
            change.retired_script.unlink(missing_ok=True)
