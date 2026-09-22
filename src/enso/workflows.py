"""Durable stage acceptance and lifecycle delivery, outside the model's control path.

A submission is a request, not a move. Only the runner evaluates it after all provider
writers stop. Commands execute outside SQLite transactions; their evidence survives runs.
This controller is not an OS sandbox: same-account unrestricted code can tamper with its
files. See docs/tasks.md for the execution trust boundary.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db, execution, locks, tasks, worktrees
from .config import (
    Config,
    ConfigError,
    Paths,
    ProjectConfig,
    Stage,
    load_config,
    project_directory,
    stage_dict,
)

ACTIVE = ("working", "submitted", "checking", "repairing")
PENDING_EVENTS = ("pending", "running", "failed")
TEST_PATTERNS = (
    "tests/*",
    "test/*",
    "*/tests/*",
    "*/test/*",
    "*.test.*",
    "*.tests.*",
    "*.spec.*",
    "*_test.go",
    "test_*.py",
    "*/test_*.py",
    "*_test.py",
)
RULE_PATTERNS = (
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "pytest.ini",
    "tox.ini",
    "ruff.toml",
    ".github/workflows/*",
    "*eslint*",
    "*prettier*",
    "*conftest.py",
    "*jest.config.*",
    "*vitest.config.*",
    "*vite.config.*",
    "*tsconfig*.json",
    "*biome.json*",
    ".dev/check*",
    "scripts/test*",
    "scripts/lint*",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
)
DEFAULT_PROTECT = (*RULE_PATTERNS, *(f"*/{p}" for p in RULE_PATTERNS), *TEST_PATTERNS)


@dataclass(frozen=True)
class Evaluation:
    status: str
    feedback: str = ""


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _spec(task: tasks.Task) -> str:
    return _hash([task.title, task.body])


def _definition(project: ProjectConfig) -> dict[str, Any]:
    return project.as_dict()


def _save(con: sqlite3.Connection, tx: dict[str, Any]) -> None:
    con.execute(
        "UPDATE _enso_workflow_transactions SET status = ?, data = ? WHERE id = ?",
        (tx["status"], json.dumps(tx), tx["id"]),
    )


def _read(con: sqlite3.Connection, ref: str, run_id: str | None = None) -> dict[str, Any] | None:
    sql = "SELECT data FROM _enso_workflow_transactions WHERE task_ref = ?"
    args: tuple[Any, ...] = (ref,)
    if run_id is not None:
        sql += " AND run_id = ?"
        args += (run_id,)
    row = con.execute(sql + " ORDER BY rowid DESC LIMIT 1", args).fetchone()
    return json.loads(row[0]) if row else None


def history(paths: Paths, ref: str) -> list[dict[str, Any]]:
    with db.reader(paths) as con:
        rows = con.execute(
            "SELECT data FROM _enso_workflow_transactions WHERE task_ref = ? ORDER BY rowid DESC",
            (tasks.parse_ref(ref),),
        ).fetchall()
        events = con.execute(
            "SELECT data FROM _enso_workflow_events WHERE task_ref = ? ORDER BY rowid", (ref,)
        ).fetchall()
    result = [json.loads(r[0]) for r in rows]
    deliveries = [json.loads(r[0]) for r in events]
    for tx in result:
        tx["hooks"] = [event for event in deliveries if event.get("transaction_id") == tx["id"]]
    return result


def event_history(paths: Paths, ref: str) -> list[dict[str, Any]]:
    with db.reader(paths) as con:
        rows = con.execute(
            "SELECT data FROM _enso_workflow_events WHERE task_ref = ? ORDER BY rowid",
            (tasks.parse_ref(ref),),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def current_summaries(paths: Paths, refs: list[str]) -> dict[str, dict[str, Any]]:
    if not refs:
        return {}
    result = {}
    with db.reader(paths) as con:
        for offset in range(0, len(refs), 400):
            chunk = refs[offset : offset + 400]
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"SELECT task_ref,id,stage,status,json_extract(data,'$.error') AS error "
                f"FROM _enso_workflow_transactions WHERE rowid IN "
                f"(SELECT MAX(rowid) FROM _enso_workflow_transactions "
                f"WHERE task_ref IN ({marks}) GROUP BY task_ref)",
                chunk,
            ).fetchall()
            result.update({row["task_ref"]: dict(row) for row in rows})
    return result


def _revision(cwd: Path | None) -> str | None:
    if cwd is None:
        return None
    _, revision = worktrees._git(["rev-parse", "HEAD"], cwd=cwd)
    return revision.strip()


def _cwd(paths: Paths, project: ProjectConfig, stage: Stage, ref: str) -> Path:
    info = worktrees.lookup(paths, ref)
    if project.repo and stage.worktree is not False and info:
        return Path(info["path"])
    return paths.workspace(project.workspace)


def start(paths: Paths, config: Config, ref: str, run_id: str) -> dict[str, Any]:
    task = tasks.get(paths, ref)
    ref = task.ref
    project = tasks._project(config, task.project, task.workspace)
    stage = project.stage(task.stage)
    if stage is None:
        raise tasks.TaskError(f"{ref} has no runnable stage")
    definition = _definition(project)
    cwd = _cwd(paths, project, stage, ref)
    revision = _revision(cwd) if project.repo and stage.worktree is not False else None
    with db.transaction(paths) as con:
        existing = _read(con, ref, run_id)
        if existing:
            return existing
        current = tasks._load(con, ref)
        if current.claim_run_id != run_id or current.stage != task.stage:
            raise tasks.TaskError(f"{ref}: execution claim changed")
        tx: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "task_ref": ref,
            "run_id": run_id,
            "stage": task.stage,
            "to_stage": None,
            "status": "working",
            "candidate": None,
            "starting_revision": revision,
            "trusted_revision": (worktrees.lookup(paths, ref) or {}).get(
                "start_revision", revision
            ),
            "spec_hash": _spec(task),
            "workflow_hash": _hash(definition),
            "definition": definition,
            "stage_definition": stage_dict(stage),
            "started_at": db.now(),
            "ended_at": None,
            "message": "",
            "error": "",
            "repairs": 0,
            "max_repairs": stage.max_repairs,
            "attempts": 0,
            "checks": [],
            "hooks": [],
            "cwd": str(cwd),
            "move": None,
        }
        con.execute(
            "INSERT INTO _enso_workflow_transactions VALUES (?,?,?,?,?,?)",
            (tx["id"], ref, run_id, task.stage, tx["status"], json.dumps(tx)),
        )
        return tx


def submit(
    con: sqlite3.Connection,
    task: tasks.Task,
    move_id: str,
    to: str,
    message: str,
    actor: str,
    run_id: str,
    attached: list[tuple[str, str]],
) -> tasks.Task:
    tx = _read(con, task.ref, run_id)
    if tx is None or tx["status"] not in ACTIVE:
        raise tasks.TaskError("no active stage transaction; run the configured stage job")
    if tx["stage"] != task.stage:
        raise tasks.TaskError("stage changed while submitting")
    tx.update(
        status="submitted", to_stage=to, message=message, move=move_id, actor=actor, refs=attached
    )
    _save(con, tx)
    tasks._record(
        con,
        task.id,
        "submitted",
        actor,
        run_id,
        from_stage=task.stage,
        to_stage=to,
        message=message,
        payload={"transaction_id": tx["id"], "move": move_id},
    )
    return task


def enqueue(
    con: sqlite3.Connection,
    project: ProjectConfig,
    task: tasks.Task,
    to: str,
    run_id: str | None,
    *,
    transaction_id: str | None = None,
) -> None:
    """Called inside the transition transaction, including manual/dependency moves."""
    for name in ("after_transition", f"after:{to}"):
        command = project.hooks.get(name)
        if not command:
            continue
        event_id = uuid.uuid4().hex
        record = con.execute("SELECT * FROM _enso_worktrees WHERE ref = ?", (task.ref,)).fetchone()
        info = dict(record) if record else {}
        data: dict[str, Any] = {
            "event_id": event_id,
            "id": event_id,
            "name": name,
            "task_ref": task.ref,
            "project": task.project,
            "from_stage": task.stage,
            "to_stage": to,
            "transaction_id": transaction_id,
            "run_id": run_id,
            "command": command,
            "cwd": info.get("path", "") if info.get("status") != "removed" else "",
            "workspace": project.workspace,
            "worktree": info,
            "timeout": project.script_timeout,
            "status": "pending",
            "attempts": 0,
            "output": "",
            "error": "",
            "created_at": db.now(),
            "deliveries": [],
        }
        con.execute(
            "INSERT INTO _enso_workflow_events VALUES (?,?,?,?,?,?)",
            (event_id, task.ref, transaction_id, "pending", 0, json.dumps(data)),
        )


def _check_current(
    paths: Paths, config: Config, tx: dict[str, Any]
) -> tuple[tasks.Task, ProjectConfig, Stage]:
    task = tasks.get(paths, tx["task_ref"])
    project = tasks._project(config, task.project, task.workspace)
    # A running controller uses a snapshot, but reject an operator config change instead
    # of accepting stale rules. Scratch/in-memory configs need no file round trip.
    if paths.config.exists():
        fresh = load_config(paths)
        selected = fresh.projects.get(task.project)
        if selected is None or _hash(_definition(selected)) != tx["workflow_hash"]:
            raise tasks.TaskError("workflow changed during execution; reselect it before retrying")
    if _hash(_definition(project)) != tx["workflow_hash"] or _spec(task) != tx["spec_hash"]:
        raise tasks.TaskError("task specification or workflow changed; evidence is stale")
    if task.claim_run_id != tx["run_id"] or task.stage != tx["stage"]:
        raise tasks.TaskError("execution ownership or accepted stage changed")
    stage = project.stage(task.stage)
    assert stage is not None
    return task, project, stage


def _rule_problem(paths: Paths, cwd: Path, tx: dict[str, Any], stage: Stage) -> str | None:
    start_revision = tx["trusted_revision"]
    if not start_revision or not stage.checks:
        return None
    protected = _protected_changes(cwd, start_revision, stage)
    if protected:
        digest = _rule_digest(cwd, start_revision, stage)
        with db.reader(paths) as con:
            rows = con.execute(
                "SELECT payload FROM _enso_task_events WHERE task_id="
                "(SELECT id FROM _enso_tasks WHERE ref=?) AND kind='rules_approved'",
                (tx["task_ref"],),
            ).fetchall()
        if any(
            (approval := json.loads(row[0])).get("digest") == digest
            and approval.get("workflow_hash") == tx["workflow_hash"]
            and approval.get("spec_hash") == tx["spec_hash"]
            for row in rows
        ):
            return None
        return (
            "Acceptance rule inputs changed and need explicit review: "
            + ", ".join(protected)
            + ". Preserve the checks or use workflow approve-rules after operator review."
        )
    return None


async def command(
    command: str, *, cwd: Path, env: dict[str, str], timeout: int, name: str
) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "name": name,
        "status": "running",
        "exit_code": None,
        "output": "",
        "error": "",
        "duration_ms": 0,
    }
    try:
        out, err, code, timed_out = await execution.run_process(
            ["bash", "-c", command],
            cwd=cwd,
            env=env,
            timeout=timeout,
            merge_stderr=True,
            label=f"workflow {name}",
        )
        result.update(
            exit_code=code,
            output=out,
            error=err,
            status=(
                "timeout"
                if timed_out
                else "passed"
                if code == 0
                else "error"
                if code is None or code in (126, 127) or code < 0
                else "failed"
            ),
        )
        if timed_out:
            result["error"] = f"timed out after {timeout}s"
        elif code in (126, 127):
            result["error"] = f"configured command is missing or not executable (exit {code})"
        elif code is None:
            result["error"] = "command ended without an exit status"
        elif code < 0:
            result["error"] = f"command interrupted by signal {-code}"
    except OSError as exc:
        result.update(status="error", error=f"could not execute check: {exc}")
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _update(paths: Paths, tx: dict[str, Any]) -> None:
    with db.transaction(paths) as con:
        _save(con, tx)


def _budget_history(paths: Paths, tx: dict[str, Any]) -> list[dict[str, Any]]:
    with db.reader(paths) as con:
        reset_at = con.execute(
            "SELECT MAX(created_at) FROM _enso_task_events WHERE task_id = "
            "(SELECT id FROM _enso_tasks WHERE ref = ?) AND kind = 'workflow_reset'",
            (tx["task_ref"],),
        ).fetchone()[0]
        rows = con.execute(
            "SELECT data FROM _enso_workflow_transactions WHERE task_ref = ? AND stage = ?",
            (tx["task_ref"], tx["stage"]),
        ).fetchall()
    return [
        r
        for row in rows
        if (r := json.loads(row[0]))["spec_hash"] == tx["spec_hash"]
        and r["workflow_hash"] == tx["workflow_hash"]
        and (reset_at is None or r["started_at"] > reset_at)
    ]


def _attempts_used(paths: Paths, tx: dict[str, Any]) -> int:
    return sum(r["attempts"] for r in _budget_history(paths, tx))


def _returns_used(paths: Paths, tx: dict[str, Any]) -> int:
    return sum(
        1
        for r in _budget_history(paths, tx)
        if r["status"] == "accepted" and r.get("move") == "return"
    )


def _failure(paths: Paths, tx: dict[str, Any], feedback: str, *, repairable: bool) -> Evaluation:
    tx["error"] = feedback
    if repairable and _attempts_used(paths, tx) < tx["max_repairs"] + 1:
        tx["status"] = "repairing"
        tx["repairs"] += 1
        tx["move"] = None
        tx["to_stage"] = None
        _update(paths, tx)
        return Evaluation("repair", feedback)
    tx["status"] = "blocked"
    tx["ended_at"] = db.now()
    _update(paths, tx)
    return Evaluation("failed", feedback)


def _verify_rebased_rules(paths: Paths, cwd: Path, tx: dict[str, Any], stage: Stage) -> None:
    problem = _rule_problem(paths, cwd, tx, stage)
    if problem:
        raise tasks.TaskError(problem)


def _recovery_intent(
    paths: Paths,
    tx: dict[str, Any],
    stage: Stage,
    candidate: str,
) -> dict[str, Any] | None:
    """Find checked intent for this exact branch revision, specification and workflow.

    The intent alone never passes a new stage. It only permits a no-op landing after a
    fresh check of the current target, covering a crash between Git and SQLite acceptance.
    """
    expected = [check.name for check in stage.checks]
    for prior in history(paths, tx["task_ref"]):
        intent = prior.get("integration")
        if not intent or intent.get("phase") not in ("applying", "applied"):
            continue
        if prior["stage"] != tx["stage"] or intent.get("candidate") != candidate:
            continue
        if any(
            intent.get(key) != tx[key] or prior[key] != tx[key]
            for key in ("spec_hash", "workflow_hash")
        ):
            continue
        evidence = intent.get("checks", [])
        if [item.get("name") for item in evidence] != expected:
            continue
        if not all(
            item.get("status") == "passed"
            and item.get("candidate") == candidate
            and item.get("spec_hash") == tx["spec_hash"]
            and item.get("workflow_hash") == tx["workflow_hash"]
            for item in evidence
        ):
            continue
        return {**intent, "transaction_id": prior["id"]}
    return None


def _candidate_changed(cwd: Path, candidate: str | None) -> bool:
    return _revision(cwd) != candidate or bool(worktrees._unclean(cwd))


def _finish_integration(
    paths: Paths,
    project: ProjectConfig,
    tx: dict[str, Any],
    candidate: str,
    target_sha: str,
) -> None:
    worktrees.finish_land(paths, project, tx["task_ref"], candidate, target_sha)
    # Persist in the worker itself: cancellation cannot lose the observed Git outcome.
    tx["integration"].update(phase="applied", applied_at=db.now())
    _update(paths, tx)


def _finish_blocked_handoff(con: sqlite3.Connection, tx: dict[str, Any]) -> str | None:
    """Keep this writer's explicit block reason without accepting its stage or checks."""
    task = tasks._load(con, tx["task_ref"])
    if (
        task.stage != "blocked"
        or task.previous_stage != tx["stage"]
        or task.claim_run_id != tx["run_id"]
    ):
        return None
    event = con.execute(
        "SELECT * FROM _enso_task_events WHERE task_id=? AND kind='moved' ORDER BY id DESC LIMIT 1",
        (task.id,),
    ).fetchone()
    if (
        event is None
        or event["run_id"] != tx["run_id"]
        or event["from_stage"] != tx["stage"]
        or event["to_stage"] != "blocked"
        or json.loads(event["payload"]).get("move") != "block"
    ):
        return None
    reason = event["message"]
    tx.update(
        status="blocked",
        move="block",
        to_stage="blocked",
        actor=event["actor"],
        message=reason,
        error=reason,
        ended_at=db.now(),
    )
    _save(con, tx)
    return reason


def _evaluation_input(paths: Paths, ref: str, run_id: str) -> dict[str, Any] | Evaluation:
    with db.transaction(paths) as con:
        tx = _read(con, ref, run_id)
        blocked = _finish_blocked_handoff(con, tx) if tx else None
    if blocked is not None:
        return Evaluation("failed", blocked)
    if tx is None or tx["status"] != "submitted":
        return Evaluation(
            "no_submission",
            "The run ended without submitting a handoff with enso task advance/return.",
        )
    return tx


async def evaluate(
    paths: Paths, config: Config, ref: str, run_id: str, env: dict[str, str]
) -> Evaluation:
    tx = _evaluation_input(paths, ref, run_id)
    if isinstance(tx, Evaluation):
        return tx
    lease = None
    candidate: str | None = None
    try:
        _task, project, stage = _check_current(paths, config, tx)
        cwd = Path(tx["cwd"])
        if tx["move"] == "return":
            returns = _returns_used(paths, tx)
            if returns >= stage.max_returns:
                return _failure(
                    paths,
                    tx,
                    "Stage return limit exhausted; needs a human decision.",
                    repairable=False,
                )
        else:
            if _attempts_used(paths, tx) >= stage.max_repairs + 1:
                return _failure(
                    paths,
                    tx,
                    "Verification budget exhausted; review the failure before resetting it.",
                    repairable=False,
                )
            tx["attempts"] += 1  # reserve before any repairable candidate validation
            tx["status"] = "checking"
            tx["error"] = ""
            _update(paths, tx)
            if project.repo and stage.worktree is not False:
                dirty = await execution.run_sync(worktrees._unclean, cwd)
                if dirty:
                    return _failure(
                        paths,
                        tx,
                        "Candidate has uncommitted or untracked files: " + ", ".join(dirty),
                        repairable=True,
                    )
                problem = await execution.run_sync(_rule_problem, paths, cwd, tx, stage)
                if problem:
                    return _failure(paths, tx, problem, repairable=False)
            target_sha = None
            if stage.integrate:
                lease = worktrees.landing_context(paths, project, ref)
                await execution.run_sync(lease.__enter__)
                current_revision = await execution.run_sync(_revision, cwd)
                recovery = _recovery_intent(paths, tx, stage, current_revision or "")
                candidate, target_sha = await execution.run_sync(
                    worktrees.prepare_land,
                    paths,
                    project,
                    ref,
                    recovery_candidate=recovery["candidate"] if recovery else None,
                )
                tx["recovery_of"] = (recovery or {}).get("transaction_id")
                await execution.run_sync(_verify_rebased_rules, paths, cwd, tx, stage)
            else:
                candidate = (
                    await execution.run_sync(_revision, cwd)
                    if project.repo and stage.worktree is not False
                    else _hash([tx["spec_hash"], tx["message"]])
                )
            tx["candidate"] = candidate
            _update(paths, tx)
            check_env = {
                **env,
                "ENSO_HOME": str(paths.home),
                "ENSO_WORKSPACE": project.workspace,
                "ENSO_TASK": ref,
                "ENSO_TASK_DIR": str(cwd) if project.repo and stage.worktree is not False else "",
                "ENSO_PROJECT_REPO": str(project.repo) if project.repo else "",
                "ENSO_TRANSACTION_ID": tx["id"],
                "ENSO_CANDIDATE": candidate or "",
                "ENSO_ATTEMPT": str(tx["attempts"]),
            }
            for check in stage.checks:
                result = {
                    "name": check.name,
                    "status": "running",
                    "attempt": tx["attempts"],
                    "candidate": candidate,
                    "spec_hash": tx["spec_hash"],
                    "workflow_hash": tx["workflow_hash"],
                    "exit_code": None,
                    "output": "",
                    "error": "",
                    "duration_ms": 0,
                }
                tx["checks"].append(result)
                _update(paths, tx)
                measured = await command(
                    check.command,
                    cwd=project_directory(paths, project.workspace, project.key),
                    env=check_env,
                    timeout=check.timeout,
                    name=check.name,
                )
                result.update(measured)
                _update(paths, tx)
                if result["status"] != "passed":
                    feedback = (
                        f"Required check {check.name} {result['status']} "
                        f"(exit {result['exit_code']}):\n{result['error']}\n{result['output']}"
                    )
                    return _failure(
                        paths,
                        tx,
                        feedback,
                        repairable=result["status"] == "failed"
                        and not stage.integrate
                        and stage.command is None,
                    )
            _check_current(paths, config, tx)
            if (
                project.repo
                and stage.worktree is not False
                and await execution.run_sync(_candidate_changed, cwd, candidate)
            ):
                return _failure(
                    paths,
                    tx,
                    "Candidate changed while checks ran; evidence is stale.",
                    repairable=False,
                )
            if stage.integrate:
                assert candidate and target_sha
                tx["integration"] = {
                    "phase": "applying",
                    "candidate": candidate,
                    "target_sha": target_sha,
                    "spec_hash": tx["spec_hash"],
                    "workflow_hash": tx["workflow_hash"],
                    "checks": copy.deepcopy(
                        [result for result in tx["checks"] if result["attempt"] == tx["attempts"]]
                    ),
                    "created_at": db.now(),
                    "applied_at": None,
                    "recovery_of": tx.get("recovery_of"),
                }
                _update(paths, tx)  # durable intent before the local Git ref update
                await execution.run_sync(
                    _finish_integration, paths, project, tx, candidate, target_sha
                )
                _check_current(paths, config, tx)
        _check_current(paths, config, tx)
        _accept(paths, config, tx)
        await drain_events(paths, config, ref=ref)
        return Evaluation("accepted")
    except (tasks.TaskError, worktrees.WorktreeError, ConfigError, ValueError, OSError) as exc:
        return _failure(paths, tx, str(exc), repairable=False)
    finally:
        if lease is not None:
            lease.__exit__(None, None, None)


def _accept(paths: Paths, config: Config, tx: dict[str, Any]) -> None:
    with db.transaction(paths) as con:
        task = tasks._load(con, tx["task_ref"])
        if (
            task.claim_run_id != tx["run_id"]
            or task.stage != tx["stage"]
            or _spec(task) != tx["spec_hash"]
        ):
            raise tasks.TaskError("task changed before acceptance; evidence is stale")
        tx["status"], tx["ended_at"], tx["error"] = "accepted", db.now(), ""
        _save(con, tx)
        moved = tasks._apply_move(
            con,
            task,
            tx["move"],
            tx["to_stage"],
            actor=tx.get("actor", tasks.ENSO_ACTOR),
            run_id=tx["run_id"],
            message=tx["message"],
            after_ref=task.after_ref,
            config=config,
            transaction_id=tx["id"],
        )
        tasks._record(
            con,
            task.id,
            "accepted",
            tasks.ENSO_ACTOR,
            tx["run_id"],
            from_stage=task.stage,
            to_stage=moved.stage,
            message="Stage accepted"
            + (" after executable checks" if tx["checks"] else " without configured checks"),
            payload={"transaction_id": tx["id"], "candidate": tx["candidate"]},
        )
        for kind, value in tx.get("refs", []):
            tasks._attach(
                con,
                task.id,
                kind,
                value,
                actor=tx.get("actor", tasks.ENSO_ACTOR),
                run_id=tx["run_id"],
            )
        if moved.finished:
            tasks._settle_waiting(con, config, moved, moved.stage)


def interrupt(paths: Paths, config: Config, ref: str, run_id: str, reason: str) -> None:
    with db.transaction(paths) as con:
        tx = _read(con, ref, run_id)
        if tx is not None and tx["status"] == "accepted":
            return
        task = tasks._load(con, ref)
        if tx:
            _finish_blocked_handoff(con, tx)
            for result in tx["checks"]:
                if result["status"] == "running":
                    result.update(status="interrupted", error=reason)
            tx.update(status="blocked", error=tx["error"] or reason, ended_at=db.now())
            _save(con, tx)
        if task.claim_run_id == run_id and task.stage in config.projects[task.project].stage_names:
            tasks._apply_move(
                con,
                task,
                "block",
                "blocked",
                actor=tasks.ENSO_ACTOR,
                run_id=run_id,
                message=tx["error"] if tx else reason,
                after_ref=None,
                attention=True,
                config=config,
                transaction_id=tx["id"] if tx else None,
            )


async def drain_events(paths: Paths, config: Config, *, ref: str | None = None) -> None:
    """At-least-once delivery, three attempts, stable IDs; failed events keep ownership."""
    try:
        lock = locks.acquire(paths.lock("workflow-events"))
    except BlockingIOError:
        return
    try:
        with db.reader(paths) as con:
            rows = con.execute(
                "SELECT data FROM _enso_workflow_events "
                "WHERE status IN ('pending','running','failed') ORDER BY rowid"
            ).fetchall()
        stalled: set[str] = set()
        for row in rows:
            event = json.loads(row[0])
            if event["task_ref"] in stalled:
                continue
            if event["attempts"] >= 3:
                stalled.add(event["task_ref"])
                continue
            if ref and event["task_ref"] != ref:
                continue
            task = tasks.get(paths, event["task_ref"])
            if task.claim_run_id:
                continue
            event["status"] = "running"
            event["attempts"] += 1
            _save_event(paths, event)
            info = event["worktree"]
            env = {
                **os.environ,
                "ENSO_HOME": str(paths.home),
                "ENSO_EVENT_ID": event["event_id"],
                "ENSO_TASK": event["task_ref"],
                "ENSO_PROJECT": event["project"],
                "ENSO_FROM_STAGE": event["from_stage"],
                "ENSO_TO_STAGE": event["to_stage"],
                "ENSO_RUN_ID": event["run_id"] or "",
                "ENSO_ATTEMPT": str(event["attempts"]),
                "ENSO_TASK_DIR": event["cwd"],
                "ENSO_WORKSPACE": event["workspace"],
                "ENSO_PROJECT_REPO": info.get("repo", ""),
                "ENSO_BRANCH": info.get("branch", ""),
                "ENSO_BASE": info.get("base", ""),
                "ENSO_LIFECYCLE": "1",
            }
            try:
                cwd = project_directory(paths, event["workspace"], event["project"])
            except (OSError, ValueError) as exc:
                result: dict[str, Any] = {
                    "status": "error",
                    "exit_code": None,
                    "output": "",
                    "error": str(exc),
                    "duration_ms": 0,
                }
            else:
                result = await command(
                    event["command"], cwd=cwd, env=env, timeout=event["timeout"], name=event["name"]
                )
            event["deliveries"].append({**result, "attempt": event["attempts"], "at": db.now()})
            event.update(result)
            event["status"] = "delivered" if result["status"] == "passed" else "failed"
            _save_event(paths, event)
            if event["status"] == "failed":
                stalled.add(event["task_ref"])
                tasks.note(
                    paths,
                    event["task_ref"],
                    actor=tasks.ENSO_ACTOR,
                    run_id=None,
                    message=(
                        f"Lifecycle {event['name']} failed (event {event['event_id']}, "
                        f"attempt {event['attempts']}/3): "
                        f"{result['error'] or result['output'][-1000:]}"
                    ),
                    attention=True,
                )
    finally:
        os.close(lock)
    tasks.settle_dependencies(paths, config)


def _save_event(paths: Paths, event: dict[str, Any]) -> None:
    with db.transaction(paths) as con:
        con.execute(
            "UPDATE _enso_workflow_events SET status = ?, attempts = ?, data = ? WHERE id = ?",
            (event["status"], event["attempts"], json.dumps(event), event["event_id"]),
        )


def _operator() -> None:
    if tasks.in_run(os.environ) or os.environ.get("ENSO_LIFECYCLE"):
        raise tasks.TaskError("this recovery action is for the operator, outside an agent run")


def _recover_manual_claim(con: sqlite3.Connection, task: tasks.Task) -> None:
    """The caller owns the execution lock, which a live manual verifier holds throughout."""
    run_id = task.claim_run_id
    if run_id is None or not run_id.startswith("manual-"):
        raise tasks.TaskError("stop the active run before resetting its budget")
    previous = _read(con, task.ref, run_id)
    if previous and previous["status"] != "accepted":
        reason = "Operator recovered an interrupted manual verification"
        for result in previous["checks"]:
            if result["status"] == "running":
                result.update(status="interrupted", error=reason)
        previous.update(status="blocked", error=reason, ended_at=db.now())
        _save(con, previous)
    con.execute(
        "UPDATE _enso_tasks SET claim_run_id=NULL,claim_actor=NULL,claim_at=NULL,updated_at=? "
        "WHERE id=?",
        (db.now(), task.id),
    )
    tasks._record(
        con,
        task.id,
        "workflow_recovered",
        tasks.actor_from_env(os.environ),
        None,
        message="Recovered manual verification after acquiring its execution lock",
        payload={"run_id": run_id},
    )


def reset(paths: Paths, ref: str, message: str) -> None:
    """Replenish budgets and recover abandoned manual ownership; never fabricate evidence."""
    _operator()
    text = tasks.clean_text(message)
    if not text:
        raise tasks.TaskError("a recovery reason is required")
    try:
        with worktrees.execution_context(paths, ref):
            _reset_locked(paths, ref, text)
    except worktrees.WorktreeBusyError:
        raise tasks.TaskError("stop the active execution before resetting its budget") from None


def _reset_locked(paths: Paths, ref: str, text: str) -> None:
    with db.transaction(paths) as con:
        task = tasks._load(con, ref)
        if task.claim_run_id:
            _recover_manual_claim(con, task)
        tasks._record(
            con, task.id, "workflow_reset", tasks.actor_from_env(os.environ), None, message=text
        )
        rows = con.execute(
            "SELECT data FROM _enso_workflow_events WHERE task_ref = ? AND status = 'failed'",
            (task.ref,),
        ).fetchall()
        for row in rows:
            event = json.loads(row[0])
            event.update(status="pending", attempts=0)
            con.execute(
                "UPDATE _enso_workflow_events SET status='pending', attempts=0, data=? WHERE id=?",
                (json.dumps(event), event["event_id"]),
            )


def _protected_changes(cwd: Path, baseline: str, stage: Stage) -> list[str]:
    _, output = worktrees._git(
        ["diff", "--no-renames", "--name-status", "-z", baseline, "HEAD", "--"], cwd=cwd
    )
    parts = output.rstrip("\0").split("\0") if output else []
    explicit = tuple(p for c in stage.checks for p in c.protect)
    protected = []
    for status, name in zip(parts[::2], parts[1::2], strict=True):
        if any(fnmatch.fnmatchcase(name, p) for p in explicit) or (
            any(fnmatch.fnmatchcase(name, p) for p in DEFAULT_PROTECT)
            and (
                status != "A"
                or any(
                    fnmatch.fnmatchcase(name, p)
                    for p in (*RULE_PATTERNS, *(f"*/{p}" for p in RULE_PATTERNS))
                )
            )
        ):
            protected.append(name)
    return sorted(protected)


def _rule_digest(cwd: Path, baseline: str, stage: Stage) -> str:
    blobs = []
    for name in _protected_changes(cwd, baseline, stage):
        code, value = worktrees._git(["ls-tree", "HEAD", "--", name], cwd=cwd, check=False)
        blobs.append([name, value.strip() if code == 0 and value else "deleted"])
    return _hash(blobs)


def approve_rules(paths: Paths, config: Config, ref: str, message: str) -> None:
    _operator()
    task = tasks.get(paths, ref)
    if task.claim_run_id:
        raise tasks.TaskError("stop the active run before approving changed acceptance rules")
    project = tasks._project(config, task.project, task.workspace)
    stage = project.stage((task.previous_stage or "") if task.stage == "blocked" else task.stage)
    info = worktrees.lookup(paths, ref)
    if stage is None or not info:
        raise tasks.TaskError("there is no worktree candidate to review")
    if not message.strip():
        raise tasks.TaskError("describe the acceptance rule review")
    digest = _rule_digest(Path(info["path"]), info["start_revision"], stage)
    with db.transaction(paths) as con:
        current = tasks._load(con, ref)
        if current.claim_run_id:
            raise tasks.TaskError("task was claimed during review")
        tasks._record(
            con,
            task.id,
            "rules_approved",
            tasks.actor_from_env(os.environ),
            None,
            message=message,
            payload={
                "digest": digest,
                "workflow_hash": _hash(_definition(project)),
                "spec_hash": _spec(task),
            },
        )


async def verify_manual(paths: Paths, config: Config, ref: str, message: str) -> Evaluation:
    """Operator checkpoint/check-only acceptance; no model and no unchecked override."""
    _operator()
    task = tasks.get(paths, ref)
    project = tasks._project(config, task.project, task.workspace)
    stage = project.stage(task.stage)
    if stage is None:
        raise tasks.TaskError("resume the task into its stage before verification")
    if not message.strip():
        raise tasks.TaskError("a handoff message is required")
    run_id = "manual-" + uuid.uuid4().hex
    with worktrees.execution_context(paths, task.ref):
        with db.transaction(paths) as con:
            current = tasks._load(con, ref)
            if current.claim_run_id:
                raise tasks.TaskError("another run holds this task")
            if con.execute(
                "SELECT 1 FROM _enso_workflow_events WHERE task_ref=? "
                "AND status IN ('pending','running','failed')",
                (task.ref,),
            ).fetchone():
                raise tasks.TaskError("finish pending lifecycle scripts before verifying")
            con.execute(
                "UPDATE _enso_tasks SET claim_run_id=?,claim_actor=?,claim_at=? WHERE id=?",
                (run_id, "user:verify", db.now(), task.id),
            )
        try:
            if project.repo and stage.worktree is not False:
                await execution.run_sync(worktrees.prepare, paths, project, ref)
            await execution.run_sync(start, paths, config, ref, run_id)
            with db.transaction(paths) as con:
                current = tasks._load(con, ref)
                submit(
                    con,
                    current,
                    "advance",
                    project.next_stage(current.stage),
                    message,
                    "user:verify",
                    run_id,
                    [],
                )
            result = await evaluate(
                paths,
                config,
                ref,
                run_id,
                {
                    **os.environ,
                    "ENSO_HOME": str(paths.home),
                    "ENSO_TASK": task.ref,
                    "ENSO_RUN_ID": run_id,
                },
            )
            if result.status != "accepted":
                interrupt(paths, config, ref, run_id, result.feedback)
            return result
        except BaseException:
            interrupt(paths, config, ref, run_id, "manual verification interrupted")
            raise
