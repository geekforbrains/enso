"""Validate, install, and resume a project's workflow preset without losing user files."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db, frontmatter, jobs, locks, maintenance, tasks, workflows, worktrees
from .config import Config, Paths, config_lock, load_config, parse_config, read_raw_config
from .jobs.runner import acquire_lock


@dataclass(frozen=True)
class Change:
    path: Path
    before: bytes | None
    after: bytes


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _safe_path(paths: Paths, relative: str) -> Path:
    path = paths.home / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("workflow operation contains an invalid path")
    current = path
    while current != paths.home:
        if current.is_symlink():
            raise ValueError(f"workflow migration will not replace a symbolic link: {current}")
        current = current.parent
    return path


def _change(paths: Paths, path: Path, after: bytes) -> Change:
    _safe_path(paths, str(path.relative_to(paths.home)))
    return Change(path, path.read_bytes() if path.exists() else None, after)


def _job_text(path: Path, fields: dict[str, Any], prompt: str, config: Config) -> bytes:
    complete = {
        "schedule": None,
        "provider": "command",
        "model": "command",
        "effort": "none",
        **fields,
    }
    job = jobs.Job(dir_name=path.parent.name, path=path, prompt=prompt, **complete)
    problems = jobs.validate(job, config)
    if problems:
        raise ValueError("; ".join(problems))
    return jobs.render(fields, prompt).encode()


def _idle(paths: Paths, key: str) -> list[tasks.Task]:
    existing = tasks.list_tasks(paths, project=key, all=True)
    summaries = workflows.current_summaries(paths, [t.ref for t in existing])
    if any(t.claim_run_id for t in existing) or any(
        t["status"] in workflows.ACTIVE for t in summaries.values()
    ):
        raise ValueError("stop active project runs before changing the workflow")
    if any(
        event["status"] in workflows.PENDING_EVENTS
        for task in existing
        for event in workflows.event_history(paths, task.ref)
    ):
        raise ValueError("complete or retry pending project lifecycle hooks before migration")
    return existing


def _plan(
    paths: Paths,
    config: Config,
    key: str,
    stages: list[str | dict],
    prompts: dict[str, str],
    *,
    development: bool,
    migrate: bool,
    base: str | None,
    worktree_root: str | None,
) -> tuple[list[Change], dict[str, Any], list[jobs.Job]]:
    project = config.projects[key]
    if development and project.repo is None:
        raise ValueError("the development preset requires a repository")
    existing = _idle(paths, key)
    mapping = {"triage": "plan", "todo": "implement"} if migrate and development else {}
    names = [stage if isinstance(stage, str) else stage["name"] for stage in stages]
    invalid = [
        t.ref
        for t in existing
        if (t.stage not in (*names, *tasks.BUILTIN_STAGES) and mapping.get(t.stage) not in names)
        or (t.previous_stage and mapping.get(t.previous_stage, t.previous_stage) not in names)
    ]
    if invalid:
        raise ValueError("existing tasks need explicit stage migration: " + ", ".join(invalid))
    raw = copy.deepcopy(read_raw_config(paths))
    entry = raw["projects"][key]
    entry.update(stages=stages, max_concurrency=3 if development else 1)
    if base is not None:
        entry["base"] = base
    if worktree_root is not None:
        entry["worktree_root"] = worktree_root
    changed, problems, _ = parse_config(raw, paths)
    if changed is None:
        raise ValueError("; ".join(problems))
    all_jobs, faults = jobs.load_jobs(paths, config)
    old_jobs = [job for job in all_jobs if job.project == key]
    if old_jobs and not migrate:
        raise ValueError("project has stage jobs; use --migrate to preserve and retire them")
    if any(job.dir_name in faults for job in old_jobs):
        raise ValueError("repair invalid project jobs before migration")
    changes: dict[Path, Change] = {}
    for job in old_jobs:
        original = job.path.read_bytes()
        backup = job.path.with_name("JOB.md.pre-workflow")
        if backup.exists() and backup.read_bytes() != original:
            raise ValueError(f"preserving an existing migration backup: {backup}")
        changes[backup] = _change(paths, backup, original)
        document, error = frontmatter.parse(original.decode())
        if document is None:
            raise ValueError(f"cannot archive {job.path}: {error}")
        fields = dict(document.fields)
        fields.update(enabled=False)
        fields.pop("project", None)
        fields.pop("stage", None)
        fields.setdefault("schedule", "0 0 1 1 *")
        # Archived command-stage jobs become inert, valid periodic records.
        if job.provider == "command":
            fields.update(
                provider=config.defaults.provider,
                model=config.defaults.model,
                effort=config.defaults.effort,
            )
        changes[job.path] = _change(
            paths,
            job.path,
            _job_text(job.path, fields, document.body or "Archived workflow stage.", changed),
        )
    targets = []
    for name in names:
        path = paths.jobs / f"{key.lower()}-{name}" / "JOB.md"
        if path.exists() and path not in {job.path for job in old_jobs}:
            raise ValueError(
                f"preserving existing {path}; choose another job name or edit it directly"
            )
        fields = {
            "name": f"{project.name}: {name}",
            "workspace": project.workspace,
            "project": key,
            "stage": name,
            "enabled": False,
            "timeout": 1800,
        }
        if name != "integrate":
            workspace = config.workspaces.get(project.workspace)
            agent = workspace.agent if workspace and workspace.agent else config.defaults
            fields.update(provider=agent.provider, model=agent.model, effort=agent.effort)
        changes[path] = _change(paths, path, _job_text(path, fields, prompts[name], changed))
        targets.append(str(path))
    changes[paths.config] = _change(
        paths, paths.config, (json.dumps(raw, indent=2) + "\n").encode()
    )
    return (
        list(changes.values()),
        {
            "project": key,
            "stages": names,
            "jobs": targets,
            "enabled": False,
            "mapping": mapping,
        },
        old_jobs,
    )


def _record(paths: Paths, changes: list[Change], result: dict[str, Any]) -> tuple[Path, dict]:
    operation = uuid.uuid4().hex
    directory = paths.runtime_dir / "workflow-migrations" / operation
    directory.mkdir(parents=True, mode=0o700)
    entries = []
    for number, change in enumerate(changes):
        name = str(number)
        maintenance.write_bytes(directory / f"{name}.after", change.after)
        if change.before is not None:
            maintenance.write_bytes(directory / f"{name}.before", change.before)
        entries.append(
            {
                "path": str(change.path.relative_to(paths.home)),
                "snapshot": name,
                "before": _digest(change.before),
                "after": _digest(change.after),
            }
        )
    manifest = {
        "operation_id": operation,
        "kind": "workflow-init",
        "status": "prepared",
        "result": result,
        "files": entries,
    }
    maintenance.write_json(directory / "manifest.json", manifest)
    return directory, manifest


def _resume(paths: Paths, key: str) -> tuple[Path, dict] | None:
    if not maintenance.paused(paths):
        return None
    gate = maintenance.read_json(paths.maintenance)
    if gate.get("kind") != "workflow-init" or gate.get("project") != key:
        raise ValueError("another maintenance operation is in progress")
    operation = gate.get("operation_id", "")
    if (
        not isinstance(operation, str)
        or len(operation) != 32
        or not all(c in "0123456789abcdef" for c in operation)
    ):
        raise ValueError("invalid workflow migration operation")
    directory = paths.runtime_dir / "workflow-migrations" / operation
    manifest = maintenance.read_json(directory / "manifest.json")
    if (
        manifest.get("operation_id") != operation
        or manifest.get("result", {}).get("project") != key
    ):
        raise ValueError("workflow migration manifest does not match its admission gate")
    return directory, manifest


def _apply(paths: Paths, directory: Path, manifest: dict) -> dict[str, Any]:
    result = manifest["result"]
    # Refuse to overwrite edits made while an interrupted migration was being inspected.
    for entry in manifest["files"]:
        path = _safe_path(paths, entry["path"])
        current = path.read_bytes() if path.exists() else None
        desired = (directory / f"{entry['snapshot']}.after").read_bytes()
        if _digest(desired) != entry["after"]:
            raise ValueError("workflow migration snapshot checksum mismatch")
        if _digest(current) not in (entry["before"], entry["after"]):
            raise ValueError(
                f"{path} changed since migration began; "
                "restore its recorded snapshot before retrying"
            )
    manifest["status"] = "applying"
    maintenance.write_json(directory / "manifest.json", manifest)
    for entry in manifest["files"]:
        path = _safe_path(paths, entry["path"])
        desired = (directory / f"{entry['snapshot']}.after").read_bytes()
        if not path.exists() or _digest(path.read_bytes()) != entry["after"]:
            maintenance.write_bytes(path, desired)
    # This operation is idempotent and atomic, including its task audit events.
    tasks.migrate_stages(paths, result["project"], result["mapping"])
    manifest["status"] = "complete"
    maintenance.write_json(directory / "manifest.json", manifest)
    paths.maintenance.unlink()
    maintenance.sync_directory(paths.maintenance.parent)
    return {key: value for key, value in result.items() if key != "mapping"} | {
        "backup": str(directory),
        "resumed": manifest.pop("resumed", False),
    }


def initialize(
    paths: Paths,
    key: str,
    stages: list[str | dict],
    prompts: dict[str, str],
    *,
    development: bool,
    migrate: bool,
    base: str | None,
    worktree_root: str | None,
) -> dict[str, Any]:
    """Install one prevalidated plan; an interrupted plan resumes behind its admission gate."""
    workflows._operator()
    with maintenance.lock(paths), config_lock(paths), contextlib.ExitStack() as held:
        db.migrate(paths)
        pending = _resume(paths, key)
        if pending is not None:
            directory, manifest = pending
            manifest["resumed"] = True
            return _apply(paths, directory, manifest)
        config = load_config(paths)
        if key not in config.projects:
            raise ValueError(f"unknown project {key}; create it with enso project add first")
        changes, result, old_jobs = _plan(
            paths,
            config,
            key,
            stages,
            prompts,
            development=development,
            migrate=migrate,
            base=base,
            worktree_root=worktree_root,
        )
        for job in sorted(old_jobs, key=lambda job: job.dir_name):
            lock = acquire_lock(job.job_dir)
            if lock is None:
                raise ValueError(f"stop running project job {job.dir_name} before migration")
            held.callback(lock.close)
        for task in _idle(paths, key):
            held.enter_context(worktrees.execution_context(paths, task.ref))
        event_dir = paths.home / ".workflow-locks"
        event_dir.mkdir(parents=True, exist_ok=True)
        try:
            event_lock = locks.acquire(event_dir / "events.lock")
        except BlockingIOError:
            raise ValueError(
                "lifecycle delivery is active; retry migration after it finishes"
            ) from None
        held.callback(os.close, event_lock)
        directory, manifest = _record(paths, changes, result)
        maintenance.write_json(
            paths.maintenance,
            {"kind": "workflow-init", "project": key, "operation_id": manifest["operation_id"]},
        )
        try:
            return _apply(paths, directory, manifest)
        except (OSError, ValueError, tasks.TaskError, maintenance.UpdateError) as exc:
            raise ValueError(
                f"workflow migration paused safely: {exc}; rerun enso workflow init {key} "
                f"to resume the recorded operation. Backups: {directory}"
            ) from exc
