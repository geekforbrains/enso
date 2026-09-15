"""Workflow presets, durable evidence, and explicit operator recovery."""

from __future__ import annotations

import asyncio

import typer

from .. import maintenance, tasks, workflow_setup, workflows
from ..config import ConfigError, Paths
from .common import JSON_FLAG, echo_json, fail, load
from .tasks import _text

workflow_app = typer.Typer(
    no_args_is_help=True, help="Configure workflows and inspect engine acceptance."
)


@workflow_app.callback()
def _admission(ctx: typer.Context) -> None:
    paths = Paths.from_env()
    if maintenance.paused(paths) and ctx.invoked_subcommand != "init":
        fail(["workflow migration is paused; rerun workflow init for its project to resume"])


def preset(name: str, lint: str | None, test: str | None) -> list[str | dict]:
    if name == "basic":
        return ["work"]
    if name != "dev":
        raise ValueError("preset must be basic or dev")
    if not lint or not test or not lint.strip() or not test.strip():
        raise ValueError("the development preset requires usable --lint and --test commands")
    checks = [{"name": "lint", "command": lint}, {"name": "tests", "command": test}]
    return [
        {"name": "plan", "worktree": False},
        {"name": "implement", "worktree": True, "checks": checks, "max_repairs": 2},
        {"name": "review", "worktree": True, "return_to": "implement", "max_returns": 2},
        {"name": "integrate", "integrate": True, "worktree": True, "checks": checks},
    ]


PROMPTS = {
    "work": (
        "Work on the held task. Follow its scope and project instructions. Submit an honest "
        "handoff with enso task advance, or block with the reason."
    ),
    "plan": (
        "Read the task and repository context. Record a short scope, acceptance criteria, "
        "and validation approach in a task note. Do not change repository files. If requirements "
        "are unresolved, block. Otherwise submit enso task advance with the plan. The engine "
        "accepts your submission after this run ends."
    ),
    "implement": (
        "Implement the held task in its worktree, including useful tests and related "
        "documentation. Run helpful local checks and commit the candidate. Preserve trusted "
        "acceptance rules; changes to existing tests/check definitions need explicit operator "
        "review. Submit enso task advance with the change and evidence. Enso independently runs "
        "required checks after you stop; repair reported failures and submit again when "
        "requested. Do not land or push."
    ),
    "review": (
        "Independently review the held candidate against the task specification, implementation, "
        "tests and docs. Record concrete review findings. Use enso task return if changes are "
        "needed, or submit enso task advance with your review judgment. Executable check results "
        "are separate evidence. Do not land or push."
    ),
    "integrate": (
        "Enso serializes integration, validates the combined candidate, and lands to the recorded "
        "target. No model runs in this stage."
    ),
}


@workflow_app.command("init")
def init_workflow(
    key: str,
    preset_name: str = typer.Option("dev", "--preset"),
    lint: str | None = typer.Option(None, "--lint"),
    test: str | None = typer.Option(None, "--test"),
    base: str | None = typer.Option(None, "--base"),
    worktree_root: str | None = typer.Option(None, "--worktree-root"),
    migrate: bool = typer.Option(
        False, "--migrate", help="Map triage/todo to plan/implement and retire old stage jobs."
    ),
    as_json: bool = JSON_FLAG,
) -> None:
    """Configure an existing project and create disabled, editable stage jobs."""
    paths = Paths.from_env()
    key = key.upper()
    try:
        gate = maintenance.read_json(paths.maintenance)
        resume = gate.get("kind") == "workflow-init" and gate.get("project") == key
        stages = [] if resume else preset(preset_name, lint, test)
        result = workflow_setup.initialize(
            paths,
            key,
            stages,
            PROMPTS,
            development=preset_name == "dev",
            migrate=migrate,
            base=base,
            worktree_root=worktree_root,
        )
    except (ValueError, tasks.TaskError, OSError, maintenance.UpdateError, ConfigError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        names = result["stages"]
        typer.echo(
            f"Configured {key}: {' → '.join(names)} → done; review and enable its stage jobs."
        )
        typer.echo(f"Migration backup: {result['backup']}")


@workflow_app.command("show")
def show(ref: str, as_json: bool = JSON_FLAG) -> None:
    """Show persistent acceptance/check and lifecycle evidence for a task."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)
    try:
        ref = tasks.get(paths, ref).ref
        result = {
            "transactions": workflows.history(paths, ref),
            "events": workflows.event_history(paths, ref),
        }
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        for tx in result["transactions"]:
            typer.echo(
                f"{tx['stage']} → {tx['to_stage'] or 'pending'}: {tx['status']} ({tx['id'][:8]})"
            )
            if tx["error"]:
                typer.echo(tx["error"])
            for check in tx["checks"]:
                typer.echo(f"  {check['name']}: {check['status']} (attempt {check['attempt']})")
        for event in result["events"]:
            typer.echo(f"{event['name']}: {event['status']} ({event['event_id']})")


def _recovery(ref: str, message: str, action: str, as_json: bool) -> None:
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    text = _text(message, as_json=as_json) or ""
    try:
        ref = tasks.get(paths, ref).ref
        if action == "retry":
            workflows.reset(paths, ref, text)
            asyncio.run(workflows.drain_events(paths, config, ref=ref))
        elif action == "approve-rules":
            workflows.approve_rules(paths, config, ref, text)
        else:
            result = asyncio.run(workflows.verify_manual(paths, config, ref, text))
            if result.status != "accepted":
                raise tasks.TaskError(result.feedback)
    except (tasks.TaskError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json({"ok": True, "ref": ref, "action": action})
    else:
        typer.echo(f"{action}: {ref}")


@workflow_app.command("verify")
def verify(
    ref: str, message: str = typer.Option(..., "--message"), as_json: bool = JSON_FLAG
) -> None:
    """Run the current stage checks and accept an operator handoff; no model or bypass."""
    _recovery(ref, message, "verify", as_json)


@workflow_app.command("retry")
def retry(
    ref: str, message: str = typer.Option(..., "--message"), as_json: bool = JSON_FLAG
) -> None:
    """Record a reason to replenish budgets and retry failed lifecycle delivery."""
    _recovery(ref, message, "retry", as_json)


@workflow_app.command("approve-rules")
def approve_rules(
    ref: str, message: str = typer.Option(..., "--message"), as_json: bool = JSON_FLAG
) -> None:
    """Record operator review of changed acceptance inputs; checks still must pass."""
    _recovery(ref, message, "approve-rules", as_json)
