"""Declared paths and immutable accepted results shared by every workflow surface.

The controller owns execution; this module resolves transitions and stores revisions inside
its short acceptance transaction. Files are submitted as content or revisioned references,
never read again to guess what an earlier worker meant to deliver.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from . import db
from .config import ProjectConfig, Stage, stage_dict

if TYPE_CHECKING:
    from .tasks import Task

MAX_RESULT_BYTES = 256 * 1024


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require_active(project: ProjectConfig, task: Task | None = None) -> None:
    if project.workflow != 2 or (task is not None and task.workflow_version != 2):
        raise ValueError(
            "legacy workflow is paused; replace its definition and explicitly adopt or "
            "recreate work"
        )
    if not project.enabled:
        raise ValueError("workflow is paused; validate it and run enso workflow enable")


def intake(project: ProjectConfig, route: str | None) -> tuple[str | None, str]:
    route = route or project.default_path
    if route is None and len(project.routes) == 1:
        route = next(iter(project.routes))
    if route is not None:
        if route not in project.routes:
            raise ValueError(f"unknown path {route!r}; choose {', '.join(project.routes)}")
        return route, project.routes[route][0]
    first = {path[0] for path in project.routes.values()}
    return None, next(iter(first)) if len(first) == 1 else "backlog"


def path_for(project: ProjectConfig, route: str | None) -> tuple[str, ...]:
    if route:
        if route not in project.routes:
            raise ValueError("selected path is no longer declared; explicitly reroute the task")
        return project.routes[route]
    paths = list(project.routes.values())
    prefix = []
    for items in zip(*paths, strict=False):
        if len(set(items)) != 1:
            break
        prefix.append(items[0])
    return tuple(prefix)


def destination(
    project: ProjectConfig, task: Task, move: str, choice: str | None = None
) -> tuple[str | None, str]:
    """One resolver for intake, submission, manual actions, dependency resume and recovery."""
    require_active(project, task)
    if move in ("drop", "block"):
        return task.route, "cancelled" if move == "drop" else "blocked"
    if move == "resume":
        route, first = intake(project, task.route)
        target = task.previous_stage or first
        if target != "backlog" and target not in path_for(project, route):
            raise ValueError(
                "the interrupted stage is outside the selected path; explicitly reroute"
            )
        return route, target
    if task.stage == "backlog":
        route, first = intake(project, choice or task.route)
        if first == "backlog":
            raise ValueError(f"choose a path with --route: {', '.join(project.routes)}")
        return route, first
    stage = project.stage(task.stage)
    if stage is None:
        raise ValueError("resume the task into its stage before submitting a result")
    route = task.route
    if choice is not None:
        if choice not in stage.routes:
            raise ValueError(f"path {choice!r} is not a declared choice at {stage.name}")
        route = choice
    elif route is None and stage.default_route:
        route = stage.default_route
    path = path_for(project, route)
    if task.stage not in path:
        raise ValueError("current stage is outside the selected path")
    index = path.index(task.stage)
    if move == "return":
        if not stage.return_to or stage.return_to not in path[:index]:
            raise ValueError("this stage has no declared return destination")
        return route, stage.return_to
    if index + 1 < len(path):
        return route, path[index + 1]
    if route is None:
        raise ValueError(f"choose a path with --route: {', '.join(stage.routes or project.routes)}")
    return route, "done"


def result(value: Any) -> dict[str, Any]:
    """A text/data deliverable with optional immutable external references."""
    if isinstance(value, str):
        value = {"text": value}
    if not isinstance(value, dict) or set(value) - {"text", "data", "artifacts"}:
        raise ValueError("output must be text or an object with text, data, and/or artifacts")
    if "text" in value and not isinstance(value["text"], str):
        raise ValueError("output.text must be text")
    artifacts = value.get("artifacts", [])
    if not isinstance(artifacts, list) or any(
        not isinstance(item, dict)
        or set(item) != {"uri", "revision"}
        or not all(isinstance(v, str) and v.strip() for v in item.values())
        for item in artifacts
    ):
        raise ValueError(
            "each artifact requires a uri and a revision (commit, version, or checksum)"
        )
    if not value or not any(value.values()):
        raise ValueError("a stage output is required")
    if len(json.dumps(value, allow_nan=False).encode()) > MAX_RESULT_BYTES:
        raise ValueError("output exceeds 256 KiB; submit a revisioned artifact reference")
    return value


def outputs(
    con: sqlite3.Connection,
    ref: str,
    *,
    current: bool = False,
    project: ProjectConfig | None = None,
) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT o.*,t.data AS transaction_data FROM _enso_workflow_outputs o "
        "LEFT JOIN _enso_workflow_transactions t ON t.id=o.transaction_id "
        "WHERE o.task_ref=? ORDER BY o.rowid",
        (ref,),
    ).fetchall()
    results = []
    invalid: set[str] = set()
    for row in rows:
        item = dict(row)
        captured = item.pop("transaction_data")
        item.update(data=json.loads(item["data"]), inputs=json.loads(item["inputs"]))
        valid = bool(item["valid"])
        if project is not None and captured:
            stage = project.stage(item["stage"])
            valid = (
                valid
                and stage is not None
                and digest(stage_dict(stage)) == digest(json.loads(captured)["stage_definition"])
            )
        # Accepted inputs always precede their consumer, including across returns.
        item["valid"] = valid and not invalid.intersection(item["inputs"].values())
        if not item["valid"]:
            invalid.add(item["id"])
        results.append(item)
    return [item for item in results if item["valid"]] if current else results


def inputs(
    con: sqlite3.Connection, ref: str, stage: Stage, project: ProjectConfig
) -> dict[str, dict[str, Any]]:
    available = {row["stage"]: row for row in outputs(con, ref, current=True, project=project)}
    missing = {name for name in stage.inputs if not name.endswith("?")} - available.keys()
    if missing:
        raise ValueError(
            f"missing accepted inputs: {', '.join(sorted(missing))}; return to their "
            f"producing stage"
        )
    return {
        name: available[name]
        for requested in stage.inputs
        if (name := requested.removesuffix("?")) in available
    }


def input_ids(
    con: sqlite3.Connection, ref: str, stage: Stage, project: ProjectConfig
) -> dict[str, str]:
    return {name: row["id"] for name, row in inputs(con, ref, stage, project).items()}


def invalidate(con: sqlite3.Connection, ref: str, stages: set[str]) -> None:
    """Preserve old versions and invalidate every result that consumed them transitively."""
    rows = outputs(con, ref, current=True)
    invalid = {row["id"] for row in rows if row["stage"] in stages}
    while True:
        affected = {row["id"] for row in rows if invalid.intersection(row["inputs"].values())}
        if affected <= invalid:
            break
        invalid |= affected
    for ident in invalid:
        con.execute("UPDATE _enso_workflow_outputs SET valid=0 WHERE id=?", (ident,))


def accept_output(
    con: sqlite3.Connection,
    ref: str,
    stage: str,
    value: Any,
    consumed: dict[str, str],
    *,
    actor: str,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    value = result(value)
    invalidate(con, ref, {stage})
    revision = con.execute(
        "SELECT coalesce(max(revision),0)+1 FROM _enso_workflow_outputs WHERE "
        "task_ref=? AND stage=?",
        (ref, stage),
    ).fetchone()[0]
    row = {
        "id": uuid.uuid4().hex,
        "task_ref": ref,
        "stage": stage,
        "revision": revision,
        "transaction_id": transaction_id,
        "data": value,
        "inputs": consumed,
        "digest": digest(value),
        "valid": True,
        "actor": actor,
        "created_at": db.now(),
    }
    values = {**row, "data": json.dumps(value), "inputs": json.dumps(consumed)}
    con.execute(
        "INSERT INTO _enso_workflow_outputs VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(values.values())
    )
    return row


def revisit(
    con: sqlite3.Connection, project: ProjectConfig, task: Task, route: str | None, target: str
) -> None:
    path = path_for(project, route)
    invalidate(con, task.ref, set(path[path.index(target) :]))


def reroute_target(con: sqlite3.Connection, project: ProjectConfig, task: Task, route: str) -> str:
    """Reuse only the already accepted prefix; newly required work always runs."""
    if route not in project.routes:
        raise ValueError(f"unknown path {route!r}")
    accepted = {row["stage"] for row in outputs(con, task.ref, current=True, project=project)}
    for name in project.routes[route]:
        if name not in accepted:
            revisit(con, project, task, route, name)
            return name
    raise ValueError("this path is already complete; create a linked follow-up task")


def prior_results(con: sqlite3.Connection, project: ProjectConfig, task: Task) -> None:
    """Every earlier stage on the selected path needs current acceptance, including people."""
    path = path_for(project, task.route)
    if task.stage not in path:
        raise ValueError("current stage is outside the selected path")
    available = {row["stage"] for row in outputs(con, task.ref, current=True, project=project)}
    missing = [name for name in path[: path.index(task.stage)] if name not in available]
    if missing:
        raise ValueError(
            f"required work is missing or stale: {', '.join(missing)}; use workflow "
            f"reroute to revisit it"
        )


def describe(
    con: sqlite3.Connection, project: ProjectConfig, task: Task, candidate: str | None = None
) -> dict[str, Any]:
    """The same assignment, pending decision and revision history for JSON, prompts and UI."""
    stage = project.stage(task.stage)
    status = (
        "legacy"
        if task.workflow_version != 2 or project.workflow != 2
        else "active"
        if project.enabled
        else "paused"
    )
    rows = outputs(con, task.ref, project=project)
    available = {row["stage"]: row for row in rows if row["valid"]}
    required = list(stage.inputs) if stage else []
    selected = {
        name: available[name]
        for requested in required
        if (name := requested.removesuffix("?")) in available
    }
    approval = (
        approval_digest({name: row["id"] for name, row in selected.items()}, candidate, stage)
        if stage and stage.human
        else None
    )
    pending = (
        f"Approve the listed input revisions with --approve {approval}, or return with feedback"
        if approval
        else ""
    )
    if status != "active":
        pending = (
            "Workflow is preserved and paused; explicitly replace/enable it "
            "and adopt or recreate outstanding work"
        )
    elif any(name not in available for name in required if not name.endswith("?")):
        pending = "Required accepted inputs are missing or stale; revisit their producing stages"
    elif not task.route:
        pending = "Select a declared path: " + ", ".join(
            stage.routes if stage and stage.routes else project.routes
        )
    if status == "active" and stage:
        try:
            prior_results(con, project, task)
        except ValueError as exc:
            pending = str(exc)
    return {
        "status": status,
        "route": task.route,
        "paths": {name: list(path) for name, path in project.routes.items()},
        "choices": list(stage.routes) if stage else list(project.routes),
        "required_inputs": required,
        "inputs": selected,
        "outputs": rows,
        "expected_output": stage.output if stage else "",
        "instructions": stage.instructions if stage else "",
        "approval": approval,
        "pending": pending,
    }


def approval_digest(inputs: dict[str, str], candidate: str | None, stage: Stage) -> str:
    return digest({"inputs": inputs, "candidate": candidate, "stage": stage_dict(stage)})
