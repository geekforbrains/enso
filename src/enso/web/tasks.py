"""Read-only task board and detail models: grouping, timelines, and worktree inspection.

Task persistence and search belong to ``enso.tasks``; this module owns their presentation.
The server builds these models in a worker thread, including the bounded Git calls.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from .. import db, tasks, workflows, worktrees
from ..config import BUILTIN_STAGES, Config, Paths, ProjectConfig
from . import common, files

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


@dataclass(frozen=True)
class TimelineEntry:
    """One event as a row: a label for what happened, and whether its run can be opened."""

    event: tasks.TaskEvent
    label: str
    tone: str
    run: str | None  # ``operator``, or ``live``/``pruned`` provider execution


def _operator_verification(run_id: str | None) -> bool:
    """Manual checks have an execution ID but never create a provider run row."""
    return bool(run_id and re.fullmatch(r"manual-[0-9a-f]{32}", run_id))


def _run_kind(run_id: str | None, live: set[str]) -> str | None:
    if run_id is None:
        return None
    if _operator_verification(run_id):
        return "operator"
    return "live" if run_id in live else "pruned"


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


def _existing_runs(paths: Paths, ids: list[str]) -> set[str]:
    """Which of these run ids still have a row; task events outlive pruned runs."""
    if not ids:
        return set()
    with db.reader(paths) as con:
        rows = con.execute(
            f"SELECT id FROM runs WHERE id IN ({', '.join('?' for _ in ids)})", ids
        ).fetchall()
    return {row["id"] for row in rows}


def _timeline(history: list[tasks.TaskEvent], live: set[str]) -> list[TimelineEntry]:
    entries = []
    for event in history:
        label, tone = _event_label(event)
        run = _run_kind(event.run_id, live)
        entries.append(TimelineEntry(event, label, tone, run))
    return entries


def _event_label(event: tasks.TaskEvent) -> tuple[str, str]:
    payload = event.payload
    match event.kind:
        case "moved":
            move = str(payload.get("move") or "moved")
            tone = {"blocked": "warning", "cancelled": "muted"}.get(event.to_stage or "", "ok")
            return f"{move}: {event.from_stage} → {event.to_stage}", tone
        case "taken":
            return "taken", "running"
        case "released":
            reason = str(payload.get("reason") or "").replace("_", " ")
            return (f"released ({reason})" if reason else "released"), (
                "warning" if reason == "run ended" else "muted"
            )
        case "created":
            return (f"created in {event.to_stage}" if event.to_stage else "created"), "muted"
        case "noted":
            return ("note, needs attention" if payload.get("attention") else "note"), (
                "warning" if payload.get("attention") else "muted"
            )
        case "ref":
            return f"ref {payload.get('kind', '')} {payload.get('value', '')}".strip(), "muted"
        case "submitted":
            return f"handoff submitted: {event.from_stage} → {event.to_stage}", "running"
        case "accepted":
            return f"handoff accepted: {event.from_stage} → {event.to_stage}", "ok"
        case "check_failed" | "workflow_blocked" | "interrupted":
            return event.kind.replace("_", " "), "warning"
        case _:
            return event.kind.replace("_", " "), "muted"


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


def workflow_rows(history: list[dict[str, Any]], live: set[str]) -> list[dict[str, Any]]:
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
    workflow = workflow_rows(transactions or [], live or set())
    worktree, worktree_error = common.attempt(partial(_worktree, paths, ref))
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
        "lifecycle": lifecycle or [],
        "workflow_notice": _workflow_notice(task, workflow),
        "handoff": ctx["handoff"] if ctx else None,
        "recovery": ctx["recovery"] if ctx else None,
        # The verdict offers the run only while its row exists; pruned runs are named, not linked.
        "recovery_link": (
            f"/runs/{ctx['recovery']['run_id']}"
            if ctx and ctx["recovery"] and ctx["recovery"]["run_id"] in (live or set())
            else None
        ),
        "timeline": _timeline(history or [], live or set()),
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
