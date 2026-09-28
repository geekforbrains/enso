"""Workflow presets, durable evidence, and explicit operator recovery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

from .. import maintenance, tasks, workflow_setup, workflows
from ..config import ConfigError, Paths
from .common import JSON_FLAG, WORKSPACE, echo_json, fail, load
from .tasks import _output, _scope, _text

workflow_app = typer.Typer(
    no_args_is_help=True, help="Configure workflows and inspect engine acceptance."
)


OUTPUT_FILE = typer.Option(None, "--output-file", help="Text or JSON stage result.")


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
        {"name": "build", "worktree": True, "checks": checks, "max_repairs": 2},
        {
            "name": "review",
            "inputs": ["request", "build"],
            "worktree": True,
            "return_to": "build",
            "max_returns": 1,
        },
        {"name": "qa", "inputs": ["build", "review"], "human": True, "return_to": "build"},
        {
            "name": "merge",
            "inputs": ["build", "review", "qa"],
            "integrate": True,
            "worktree": True,
            "checks": checks,
        },
    ]


# The development preset tells a person when a task needs them: blocked, or ready for QA.
HOOKS = {"after:blocked": "./notify.sh", "after:qa": "./notify.sh"}
NOTIFY = """#!/usr/bin/env bash
# Lifecycle hook: one line when a task needs a person, because it is blocked or has
# reached a human stage. PROJECT.md runs it from hooks; Enso sets the variables below.
# Without --to, the message goes to the transport's notify target; add
# `--to slack:C…` or `--to telegram:<id>` to the send to choose another.
set -euo pipefail
reason="${ENSO_MESSAGE:-}"
reason="${reason%%$'\\n'*}"
if ((${#reason} > 160)); then reason="${reason:0:157}..."; fi
case "$ENSO_TO_STAGE:${ENSO_BLOCK_KIND:-}" in
  blocked:failure) text="$ENSO_TASK stopped: $reason" ;;
  blocked:*) text="$ENSO_TASK needs you: $reason" ;;
  *) text="$ENSO_TASK is ready for $ENSO_TO_STAGE: ${ENSO_TASK_TITLE:-}" ;;
esac
# Best effort: a failed hook would hold the task until retried, so a lost notice only logs.
if ! enso message send "$text" >/dev/null; then
  echo "notify.sh: not sent: $text" >&2
fi
"""

PROMPTS = {
    "work": (
        "Work on the held task. Follow its scope and project instructions. Submit an honest "
        "handoff with enso task advance, or block with the reason."
    ),
    "build": """\
Build the held task in its worktree (`cd` to the path in the Task block).

1. Read the task and the repository docs that own the area it touches.
2. If it needs a decision the task and docs don't make, don't guess: block with one short
   question, `enso task block REF --message "…"`.
3. Otherwise update the docs and tests first, then the code. Commit everything and leave no
   untracked files. Never push.
4. Never weaken a test to make it pass. Edit existing tests or check configuration only when
   the change requires it; a person approves those edits before the task merges.

After you hand off, Enso runs the required checks. If one fails you get its output: fix the
cause and hand off again.

Hand off with `enso task advance REF --message -` in at most four short lines: what changed,
the commits, the tests added, and the docs touched. If you edited existing tests, add one
line naming them and why.""",
    "review": """\
Review the held task with fresh eyes. `cd` to its worktree and read the task, the commits and
diff against the base branch named in the Task block, and the repository's instructions.

Check that the change does what the task asks and no more, follows the repository's
conventions, has tests that cover it, weakens no existing test, and updates the docs it
affects. Enso already ran the required checks; don't rerun them as evidence.

Needs changes: `enso task return REF --message -` with at most five short bullets.

Good: `enso task advance REF --message -`, written for the person who will try it, under 500
characters:

**Changed:** one or two short lines.
**Try it:** up to three steps and what they should see, or one line saying nothing visible
changed.
**Edited tests:** only if existing tests or check files changed: which, and whether each edit
is sound.""",
    "merge": (
        "Enso rebases the task branch onto its base, reruns the required checks, and "
        "fast-forwards the base branch. Changed tests or check files need a person's approval "
        "first (enso workflow approve-rules). Nothing is pushed, and no model runs here."
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
        False,
        "--migrate",
        help="Preserve and retire existing stage jobs when replacing the workflow.",
    ),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Configure an existing project and create disabled, editable stage jobs."""
    paths = Paths.from_env()
    key = key.upper()
    try:
        gate = maintenance.read_json(paths.maintenance)
        resume = gate.get("kind") == "workflow-init" and gate.get("project") == key
        stages = [] if resume else preset(preset_name, lint, test)
        development = preset_name == "dev"
        result = workflow_setup.initialize(
            paths,
            key,
            stages,
            PROMPTS,
            hooks=HOOKS if development else {},
            scripts={"notify.sh": NOTIFY} if development else {},
            development=development,
            migrate=migrate,
            base=base,
            worktree_root=worktree_root,
            workspace=workspace,
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
def show(ref: str, workspace: str | None = WORKSPACE, as_json: bool = JSON_FLAG) -> None:
    """Show persistent acceptance/check and lifecycle evidence for a task."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        ref = tasks.get(paths, ref).ref
        result = {
            "contract": tasks.context(paths, config, ref, env=os.environ)["contract"],
            "transactions": workflows.history(paths, ref),
            "events": workflows.event_history(paths, ref),
        }
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        typer.echo("\n".join(tasks._contract_lines(result["contract"])))
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


def _recovery(ref: str, message: str, action: str, as_json: bool, workspace: str | None) -> None:
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    text = _text(message, as_json=as_json) or ""
    continued = False
    try:
        ref = tasks.get(paths, ref).ref
        if action == "retry":
            workflows.reset(paths, ref, text)
            asyncio.run(workflows.drain_events(paths, config, ref=ref))
            result = None
        elif action == "approve-rules":
            result = asyncio.run(workflows.approve_rules(paths, config, ref, text))
            continued = result is not None
        else:
            result = asyncio.run(workflows.verify_manual(paths, config, ref, text))
        if result is not None and result.status != "accepted":
            raise tasks.TaskError(result.feedback)
        stage = tasks.get(paths, ref).stage
    except (tasks.TaskError, ValueError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(
            {"ok": True, "ref": ref, "action": action, "continued": continued, "stage": stage}
        )
    elif continued:
        typer.echo(f"{action}: {ref}; the held handoff passed its checks and moved to {stage}")
    else:
        typer.echo(f"{action}: {ref}")


@workflow_app.command("verify")
def verify(
    ref: str,
    message: str = typer.Option(..., "--message"),
    route: str | None = typer.Option(None, "--route"),
    output_file: Path | None = OUTPUT_FILE,
    approve: str | None = typer.Option(None, "--approve"),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Check and accept an operator result against the current input revisions."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        result = asyncio.run(
            workflows.verify_manual(
                paths,
                config,
                ref,
                message,
                route=route,
                output=_output(output_file),
                approve=approve,
            )
        )
        if result.status != "accepted":
            raise tasks.TaskError(result.feedback)
    except (tasks.TaskError, ValueError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    echo_json(
        {"ok": True, "ref": ref, "stage": tasks.get(paths, ref).stage}
    ) if as_json else typer.echo(f"Accepted {ref}")


@workflow_app.command("retry")
def retry(
    ref: str,
    message: str = typer.Option(..., "--message"),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Record a reason to replenish budgets and retry failed lifecycle delivery."""
    _recovery(ref, message, "retry", as_json, workspace)


@workflow_app.command("approve-rules")
def approve_rules(
    ref: str,
    message: str = typer.Option(..., "--message"),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Approve changed tests or check files; a handoff blocked on them continues its checks."""
    _recovery(ref, message, "approve-rules", as_json, workspace)


@workflow_app.command("enable")
def enable(key: str, workspace: str | None = WORKSPACE, as_json: bool = JSON_FLAG) -> None:
    """Validate and activate a new workflow; preserved legacy work remains paused."""
    try:
        result = workflow_setup.enable(Paths.from_env(), key, workspace)
    except (ConfigError, tasks.TaskError, ValueError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    echo_json(result) if as_json else typer.echo(f"Enabled workflow {key}")


def _route(
    ref: str, route: str, message: str, workspace: str | None, as_json: bool, adopt: bool
) -> None:
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        task = workflows.reroute(paths, config, ref, route, message, adopt=adopt)
    except (tasks.TaskError, ValueError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    echo_json(task.as_dict()) if as_json else typer.echo(f"{task.ref}: {task.route} → {task.stage}")


@workflow_app.command("reroute")
def reroute(
    ref: str,
    route: str = typer.Option(..., "--route"),
    message: str = typer.Option(..., "--message"),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Select a declared path and revisit its first missing or stale result."""
    _route(ref, route, message, workspace, as_json, False)


@workflow_app.command("adopt")
def adopt(
    ref: str,
    route: str = typer.Option(..., "--route"),
    message: str = typer.Option(..., "--message"),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Deliberately restart preserved legacy work on a new path, keeping its history."""
    _route(ref, route, message, workspace, as_json, True)


@workflow_app.command("resolve-event")
def resolve_event(
    ref: str,
    event: str,
    decision: str,
    message: str = typer.Option(..., "--message"),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Record delivery or authorize retry for an uncertain external lifecycle action."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        workflows.resolve_event(paths, tasks.parse_ref(ref), event, decision, message)
    except (tasks.TaskError, ValueError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    echo_json({"ok": True, "event": event, "decision": decision}) if as_json else typer.echo(
        f"Resolved {event}"
    )
