"""Read-only task board and detail models: grouping, timelines, and worktree inspection.

Task persistence and search belong to ``enso.tasks``; this module owns their presentation.
The server builds these models in a worker thread, including the bounded Git calls.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Container, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from .. import db, slack_cache, tasks, workflows, worktrees
from ..config import BUILTIN_STAGES, Config, Paths, ProjectConfig
from ..jobs import Job, load_jobs
from . import common, files, timeline
from .timeline import operator_verification as _operator_verification
from .timeline import run_kind as _run_kind

# The board reads top to bottom: what needs a person first, then what the agents hold,
# then what is waiting, then what is finished. Each entry is a group's key, its heading,
# and the qualifier the heading adds after the count.
BOARD_GROUPS = (
    ("blocked", "Blocked", "needs you"),
    ("active", "Active", ""),
    ("ready", "Ready", ""),
    ("backlog", "Backlog", ""),
    ("done", "Done", ""),
)
# Which group lists a task, by the state ``_task_state`` gives it. Every state maps to
# exactly one group, so a task is on the board once and never in two places.
TASK_GROUP_OF = {
    "blocked": "blocked",
    "needs-you": "blocked",
    "active": "active",
    "ready": "ready",
    "backlog": "backlog",
    "done": "done",
    "cancelled": "done",
}
DONE_WINDOW = timedelta(days=7)  # the count line says this week's finishes; the list shows more
DONE_LIMIT = 200
GIT_TIMEOUT = 5.0
_STAGE_NAME = re.compile(r"[a-z][a-z0-9-]{0,23}")


@dataclass(frozen=True)
class TaskRow:
    """One task as listed: its state for the dot, and the project's name for the detail line."""

    task: tasks.Task
    state: str
    project_name: str
    phase: str = ""
    operator_verification: bool = False


@dataclass(frozen=True)
class TaskGroup:
    label: str
    rows: list[TaskRow]
    note: str = ""  # a qualifier the heading adds after the count


def _project_of(config: Config | None, task: tasks.Task) -> ProjectConfig | None:
    project = config.projects.get(task.project) if config else None
    return project if project and project.workspace == task.workspace else None


def _task_state(task: tasks.Task, project: ProjectConfig | None) -> str:
    if task.finished:
        return task.stage
    if task.stage == "blocked":
        return "blocked"
    stage = project.stage(task.stage) if project else None
    if (stage and stage.human) or task.attention:
        return "needs-you"
    if task.claim_run_id:
        return "active"
    if task.stage == "backlog":
        return "backlog"
    return "ready" if stage else "needs-you"


def _task_row(
    config: Config | None, task: tasks.Task, transaction: dict[str, Any] | None = None
) -> TaskRow:
    project = _project_of(config, task)
    phase = ""
    if transaction and transaction.get("stage") == task.stage:
        status = transaction.get("status", "")
        if status in ("working", "submitted", "checking", "repairing", "interrupted", "blocked"):
            phase = _WORKFLOW_LABELS.get(status, status)
    return TaskRow(
        task,
        _task_state(task, project),
        project.name if project else task.project,
        phase,
        _operator_verification(task.claim_run_id),
    )


def _ready_order(config: Config | None, row: TaskRow) -> tuple[str, int, str]:
    """Ready reads down the pipeline: by project, then the project's stage order."""
    project = _project_of(config, row.task)
    index = project.index(row.task.stage) if project and project.stage(row.task.stage) else 999
    return (row.task.project, index, row.task.stage)


def _board_groups(
    config: Config | None, live: list[TaskRow], finished: list[TaskRow]
) -> list[TaskGroup]:
    """The five groups in reading order, each sorted its own way; an empty group is dropped.

    Every task lands in one group, chosen by its state, so the board never lists the same
    task twice. The finished rows arrive newest first from the query and keep that order.
    """
    held: dict[str, list[TaskRow]] = {key: [] for key, _label, _note in BOARD_GROUPS}
    for row in live:
        held[TASK_GROUP_OF[row.state]].append(row)
    held["blocked"].sort(key=lambda row: row.task.entered_stage_at)  # longest wait on top
    held["active"].sort(key=lambda row: row.task.claim_at or "", reverse=True)
    held["ready"].sort(key=partial(_ready_order, config))
    held["backlog"].sort(key=lambda row: row.task.entered_stage_at)
    held["done"] = finished
    return [TaskGroup(label, held[key], note) for key, label, note in BOARD_GROUPS if held[key]]


def tasks_model(paths: Paths, query: Mapping[str, str]) -> dict[str, Any]:
    """Project navigation and one board under workspace, project, stage, and search filters.

    Every matching task is on the page, in five groups the filters narrow together. The
    whole history is counted in SQL; at most ``DONE_LIMIT`` finished tasks are listed.
    """
    config, problems = common.read_config(paths)
    workspace = tasks.clean_text(query.get("workspace", ""), single_line=True)[:64] or None
    projects, projects_error = common.project_summaries(paths, config, workspace=workspace)
    project = query.get("project", "").strip().upper() or None
    selected_project = next((entry for entry in projects if entry.key == project), None)
    stage_jobs: dict[str, list[Job]] = {}
    jobs_error = None
    if selected_project and selected_project.stages:
        loaded, jobs_error = common.attempt(partial(load_jobs, paths, config))
        for job in loaded[0] if loaded else []:
            if (
                job.project == selected_project.key
                and job.workspace == selected_project.workspace
                and job.stage
            ):
                stage_jobs.setdefault(job.stage, []).append(job)
    stage = query.get("stage", "").strip().lower() or None
    if stage is not None and not _STAGE_NAME.fullmatch(stage):
        stage = None
    q = tasks.clean_text(query.get("q", ""), single_line=True)[:200]
    # The four live groups share one read of the unfinished tasks, which is the working set;
    # the finished history is counted and capped separately so it never sets the page's cost.
    live: list[tasks.Task] = []
    error: str | None = None
    if stage not in tasks.FINISHED:
        listed, error = common.attempt(
            partial(
                tasks.list_tasks,
                paths,
                project=project,
                stage=stage,
                query=q or None,
                config=config,
                workspace=workspace,
            )
        )
        live = listed or []
    finished, finished_error = common.attempt(
        partial(
            tasks.finished_tasks,
            paths,
            since=datetime.now(UTC) - DONE_WINDOW,
            limit=DONE_LIMIT,
            project=project,
            stage=stage,
            query=q or None,
            workspace=workspace,
        )
    )
    history = finished or tasks.FinishedTasks(rows=[], total=0, done_count=0)
    done_tasks = history.rows
    error = error or finished_error or projects_error
    current, workflow_error = common.attempt(
        partial(workflows.current_summaries, paths, [task.ref for task in live])
    )
    error = error or workflow_error
    groups = _board_groups(
        config,
        [_task_row(config, task, (current or {}).get(task.ref)) for task in live],
        [_task_row(config, task) for task in done_tasks],
    )
    stages = list(BUILTIN_STAGES)
    for entry in projects:
        if project in (None, entry.key):
            stages.extend(step.name for step in entry.stages if step.name not in stages)
    if stage and stage not in stages:
        stages.append(stage)
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "groups": groups,
        "listed": sum(len(group.rows) for group in groups),
        "total": len(live) + history.total,
        "done_count": history.done_count,
        "done_days": DONE_WINDOW.days,
        "done_limit": DONE_LIMIT,
        "project": project,
        "selected_project": selected_project,
        "stage_jobs": stage_jobs,
        "jobs_error": jobs_error,
        "workspace": workspace,
        "workspaces": sorted(
            {
                *(config.workspaces if config else ()),
                *(entry.workspace for entry in projects),
                *([workspace] if workspace else []),
            }
        ),
        "stage": stage,
        "q": q,
        "projects": projects,
        "stages": stages,
        "empty": _tasks_empty(project, stage, q, workspace),
        "error": error,
    }


def _tasks_empty(project: str | None, stage: str | None, q: str, workspace: str | None) -> str:
    """Say which filter emptied the board, so a blank page is not mistaken for a quiet one."""
    narrowed = [
        text
        for text, value in (
            (f"in project {project}", project),
            (f"in workspace {workspace}", workspace),
            (f"in stage {stage}", stage),
            (f"matching “{q}”", q),
        )
        if value
    ]
    return f"No tasks {' '.join(narrowed)}." if narrowed else "No tasks yet."


def _existing_runs(paths: Paths, ids: list[str]) -> dict[str, dict[str, Any]]:
    """The executions of these run ids that still have a row; task events outlive pruned runs."""
    if not ids:
        return {}
    with db.reader(paths) as con:
        rows = con.execute(
            f"SELECT id, {', '.join(tasks.RUN_EXECUTION)} FROM runs "
            f"WHERE id IN ({', '.join('?' for _ in ids)})",
            ids,
        ).fetchall()
    return {row["id"]: {key: row[key] for key in tasks.RUN_EXECUTION} for row in rows}


def _actor_names(paths: Paths, history: list[tasks.TaskEvent]) -> dict[str, str]:
    """Slack names for chat actors whose events predate recording the sender's name."""
    wanted = {
        event.actor
        for event in history
        if event.actor.startswith("slack:") and not event.payload.get("actor_name")
    }
    if not wanted:
        return {}
    users = (slack_cache.load(paths).get("users") or {}).get("items") or {}
    names = {}
    for actor in wanted:
        name = slack_cache.display_name(users.get(actor.partition(":")[2]))
        if name:
            names[actor] = name
    return names


_WORKFLOW_LABELS = {
    "working": "Working",
    "submitted": "Handoff submitted",
    "checking": "Running required checks",
    "repairing": "Repairing failed checks",
    "accepted": "Handoff accepted",
    "blocked": "Workflow blocked",
    "interrupted": "Workflow interrupted",
    "overridden": "Operator override",
}
_WORKFLOW_TONES = {
    "working": "running",
    "submitted": "running",
    "checking": "running",
    "repairing": "running",
    "accepted": "ok",
    "blocked": "warning",
    "interrupted": "warning",
    "overridden": "warning",
}


def workflow_rows(history: list[dict[str, Any]], live: Container[str]) -> list[dict[str, Any]]:
    """Decorate engine records without deriving acceptance from provider output or checks."""
    rows = []
    for transaction in history:
        status = transaction.get("status", "")
        checks = transaction.get("checks") or []
        configured = (transaction.get("stage_definition") or {}).get("checks")
        checked = {check["name"] for check in checks}
        run_id = transaction.get("run_id")
        run_kind = _run_kind(run_id, live)
        rows.append(
            {
                **transaction,
                "label": _WORKFLOW_LABELS.get(status, status.replace("_", " ")),
                "tone": _WORKFLOW_TONES.get(status, "muted"),
                "checks": checks,
                "configured_checks": configured,
                "pending_checks": [
                    check["name"] for check in configured or [] if check["name"] not in checked
                ],
                "hooks": transaction.get("hooks") or [],
                "run_kind": run_kind,
                "run_link": f"/runs/{run_id}" if run_kind == "live" else None,
            }
        )
    return rows


def _workflow_notice(task: tasks.Task, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    latest = rows[0]
    status = latest.get("status")
    stage = latest.get("stage")
    why = latest.get("error") or ""
    if not why:
        if status == "accepted":
            why = f"{stage} → {latest.get('to_stage')}. The recorded handoff was accepted by Enso."
        elif status in ("submitted", "checking", "repairing"):
            why = f"The task remains in {task.stage} until Enso accepts this handoff."
        elif status == "working":
            why = "The stage has not submitted a handoff yet."
        elif status == "overridden":
            why = "An operator bypassed the configured checks; this is not a passing check result."
    return {**latest, "why": why}


def _git(cwd: Any, *args: str) -> str | None:
    """One read-only Git query; None on any failure, because the page must always render."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError, subprocess.SubprocessError, ValueError:
        return None
    return done.stdout.strip()[:200] if done.returncode == 0 else None


def _worktree(paths: Paths, ref: str) -> dict[str, Any] | None:
    """Recorded ownership survives config changes and cleanup; absent records have no panel."""
    record = worktrees.lookup(paths, ref)
    if record is None:
        return None
    path, branch, base = Path(record["path"]), record["branch"], record.get("base")
    # A missing .git inside a repository-local worktree would make Git walk up and
    # quietly answer from the main checkout. Keep that unavailable, rather than misleading.
    counted = (
        _git(path, "rev-list", "--count", f"{base}..{branch}", "--")
        if base and (path / ".git").exists()
        else None
    )
    return {
        **record,
        "ahead": int(counted) if counted is not None and counted.isdigit() else None,
    }


def task_model(paths: Paths, ref_text: str) -> dict[str, Any] | None:
    """One task: header, spec, refs, worktree, and the timeline."""
    try:
        ref = tasks.parse_ref(ref_text)
        task = tasks.get(paths, ref)
    except tasks.TaskError:
        return None
    except Exception as exc:  # the database is missing or unreadable: say so on the page
        return {
            "config_problems": common.read_config(paths)[1],
            "alarm": common.alarm(paths),
            "ref": ref_text,
            "task": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    config, problems = common.read_config(paths)
    project = _project_of(config, task)
    ctx: dict[str, Any] | None = None
    ctx_error: str | None = None
    if config is not None and project is not None:
        ctx, ctx_error = common.attempt(partial(tasks.context, paths, config, ref, env={}))
    history, events_error = common.attempt(partial(tasks.events, paths, ref))
    attached, refs_error = common.attempt(partial(tasks.refs, paths, ref))
    transactions, workflow_error = common.attempt(partial(workflows.history, paths, ref))
    lifecycle, lifecycle_error = common.attempt(partial(workflows.event_history, paths, ref))
    ids = sorted(
        {event.run_id for event in history or [] if event.run_id}
        | {entry["run_id"] for entry in transactions or [] if entry.get("run_id")}
    )
    live, _runs_error = common.attempt(partial(_existing_runs, paths, ids))
    live = live or {}
    workflow = workflow_rows(transactions or [], live)
    worktree, worktree_error = common.attempt(partial(_worktree, paths, ref))
    names, _names_error = common.attempt(partial(_actor_names, paths, history or []))
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "ref": ref,
        "task": task,
        "claim_is_operator": _operator_verification(task.claim_run_id),
        "project_name": project.name if project else task.project,
        "stages": list(project.stage_names) if project else [],
        "spec": files.render_markdown(task.body) if task.body else None,
        "refs": attached or [],
        "worktree": worktree,
        "workflow": workflow,
        "workflow_notice": _workflow_notice(task, workflow),
        "handoff": ctx["handoff"] if ctx else None,
        "handoff_message": (
            files.render_output(ctx["handoff"]["message"]) if ctx and ctx["handoff"] else None
        ),
        "recovery": ctx["recovery"] if ctx else None,
        # The verdict offers the run only while its row exists; pruned runs are named, not linked.
        "recovery_link": (
            f"/runs/{ctx['recovery']['run_id']}"
            if ctx and ctx["recovery"] and ctx["recovery"]["run_id"] in live
            else None
        ),
        "timeline": timeline.build(
            history or [],
            transactions or [],
            lifecycle or [],
            live,
            project,
            names or {},
            base=(worktree or {}).get("base") or (project.base if project else None),
            finished=task.finished,
            now=datetime.now(UTC),
        ),
        "events": len(history or []),
        # The context read only feeds the handoff and the recovery notice, so it reports last;
        # without this it would fail silently and the page would simply omit both.
        "error": (
            events_error
            or refs_error
            or ctx_error
            or workflow_error
            or lifecycle_error
            or worktree_error
        ),
    }
