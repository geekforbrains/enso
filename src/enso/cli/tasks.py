"""``enso task``: the board from the terminal, chat, and stage jobs alike.

Every rule lives in ``enso.tasks``; this module only reads flags, derives the actor and run
from the environment (never from a flag), and prints. A refused move exits 1 with the reason.
"""

from __future__ import annotations

import contextlib
import os
from datetime import timedelta
from pathlib import Path

import typer

from .. import tasks, worktrees
from ..config import Config, Paths, resolve_workspace
from .common import (
    JSON_FLAG,
    InputError,
    ago,
    columns,
    echo_json,
    fail,
    load,
    parse_duration,
    read_input,
    seconds,
)

task_app = typer.Typer(no_args_is_help=True, help="Tasks: the board stage jobs work from.")

MESSAGE_HELP = "The handoff: what changed, the evidence, what comes next; - reads stdin."
FORCE_HELP = "Override another run's claim (a person only, never inside a run)."
BODY_FILE = typer.Option(None, "--body-file", help="The spec from a file, or - for stdin.")
REFS = typer.Option([], "--ref", help="Evidence as KIND:VALUE; repeatable.")

WORKSPACE = typer.Option(None, "--workspace", help="Owner; defaults to ENSO_WORKSPACE.")
ALL_WORKSPACES = typer.Option(False, "--all-workspaces", help="List across the installation.")


def _scope(
    paths: Paths,
    config: Config,
    workspace: str | None,
    *,
    as_json: bool,
    ref: str | None = None,
    project: str | None = None,
    all_workspaces: bool = False,
) -> str | None:
    """Select CLI context; explicit dependency refs are resolved separately across projects."""
    try:
        if all_workspaces:
            if workspace is not None:
                raise ValueError("give --workspace or --all-workspaces, not both")
            selected = None
        else:
            selected = resolve_workspace(paths, workspace)
        if ref is not None:
            task = tasks.get(paths, ref, workspace=selected)
            tasks._project(config, task.project, task.workspace)
        if project is not None:
            found = tasks._project(config, project.strip().upper())
            if selected is not None and found.workspace != selected:
                raise ValueError(
                    f"project {found.key} belongs to workspace {found.workspace}; "
                    "select it with --workspace"
                )
        return selected
    except (ValueError, tasks.TaskError) as exc:
        fail([str(exc)], as_json=as_json)


def _read(source: str | Path, *, as_json: bool, literal: bool = False) -> str:
    try:
        return read_input(source, literal=literal)
    except InputError as exc:
        fail([str(exc)], as_json=as_json)


def _text(value: str | None, *, as_json: bool) -> str | None:
    """A message flag: the text, stdin for ``-``, or None when the flag was not given."""
    return _read(value, as_json=as_json, literal=value != "-") if value is not None else None


def _file_text(path: Path | None, *, as_json: bool) -> str | None:
    """``--body-file PATH`` or ``-`` for stdin."""
    return _read(path, as_json=as_json) if path is not None else None


def _duration(value: str | None, *, as_json: bool) -> timedelta | None:
    if value is None:
        return None
    span = parse_duration(value)
    if span is None:
        fail([f"{value!r} is not a duration such as 30m, 2h, or 1d"], as_json=as_json)
    return span


def _parse_refs(values: list[str], *, as_json: bool) -> list[tuple[str, str]]:
    pairs = []
    for value in values:
        kind, sep, ref_value = value.partition(":")
        if not sep or not kind or not ref_value:
            fail([f"--ref takes KIND:VALUE, got {value!r}"], as_json=as_json)
        pairs.append((kind, ref_value))
    return pairs


def _emit(task: tasks.Task, *, as_json: bool, verb: str) -> None:
    if as_json:
        echo_json(task.as_dict())
    else:
        typer.echo(f"{verb} {task.ref}: {task.stage} — {task.title}")


def _move(
    ref: str,
    move_id: str,
    *,
    workspace: str | None,
    message: str | None,
    as_json: bool,
    to: str | None = None,
    after: str | None = None,
    force: bool = False,
    refs: list[str] | None = None,
) -> None:
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        task = tasks.move(
            paths,
            config,
            ref,
            move_id,
            actor=tasks.actor_from_env(os.environ),
            run_id=tasks.in_run(os.environ),
            message=_text(message, as_json=as_json) or "",
            to=to,
            after=after,
            force=force,
            refs=_parse_refs(refs or [], as_json=as_json),
        )
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    _emit(task, as_json=as_json, verb=move_id)


# -- Commands -------------------------------------------------------------------


@task_app.command("add")
def task_add(
    title: str,
    project: str = typer.Option(..., "--project", help="The project key, such as EN."),
    body: str | None = typer.Option(None, "--body", help="The spec."),
    body_file: Path | None = BODY_FILE,
    priority: int = typer.Option(0, "--priority", help="Higher runs first within a stage."),
    backlog: bool = typer.Option(False, "--backlog", help="Park it; not ready for any stage."),
    after: str | None = typer.Option(
        None, "--after", help="Start blocked on this task; resumed when it is done."
    ),
    from_ref: str | None = typer.Option(None, "--from", help="The task this was found in."),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Create a task in the project's first stage (or its backlog)."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, project=project, as_json=as_json)
    if body is not None and body_file is not None:
        fail(["give --body or --body-file, not both"], as_json=as_json)
    try:
        task = tasks.create(
            paths,
            config,
            project,
            title,
            body=(
                _read(body, as_json=as_json, literal=True)
                if body is not None
                else _file_text(body_file, as_json=as_json) or ""
            ),
            priority=priority,
            backlog=backlog,
            after=after,
            from_ref=from_ref,
            actor=tasks.actor_from_env(os.environ),
        )
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    _emit(task, as_json=as_json, verb="created")


@task_app.command("list")
def task_list(
    project: str | None = typer.Option(None, "--project"),
    stage: str | None = typer.Option(None, "--stage"),
    ready: bool = typer.Option(False, "--ready", help="Unclaimed, in an agent stage."),
    claimed: bool = typer.Option(False, "--claimed", help="Held by a run."),
    attention: bool = typer.Option(False, "--attention", help="Flagged for a person."),
    show_all: bool = typer.Option(False, "--all", help="Include done and cancelled."),
    idle_for: str | None = typer.Option(None, "--idle-for", help="In its stage this long: 2h."),
    all_workspaces: bool = ALL_WORKSPACES,
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """List tasks; finished ones are hidden unless --all or --stage names them."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    selected = _scope(
        paths, config, workspace, project=project, all_workspaces=all_workspaces, as_json=as_json
    )
    found = tasks.list_tasks(
        paths,
        project=project,
        stage=stage,
        ready=ready,
        claimed=claimed,
        attention=attention,
        all=show_all,
        idle_for=_duration(idle_for, as_json=as_json),
        config=config,
        workspace=selected,
    )
    if as_json:
        echo_json([task.as_dict() for task in found])
        return
    if not found:
        typer.echo("no tasks match")
        return
    rows = [["REF", "STAGE", "PRIORITY", "CLAIM", "IN STAGE", "TITLE"]]
    rows.extend(
        [
            task.ref,
            task.stage + (" !" if task.attention else ""),
            str(task.priority),
            task.claim_run_id or "-",
            ago(task.entered_stage_at),
            task.title,
        ]
        for task in found
    )
    typer.echo(columns(rows))


@task_app.command("show")
def task_show(ref: str, workspace: str | None = WORKSPACE, as_json: bool = JSON_FLAG) -> None:
    """One task: its fields, the moves and why, refs, and the timeline."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        ctx = tasks.context(paths, config, ref, env=os.environ)
        history = tasks.events(paths, ref)
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json({**ctx, "events": [event.as_dict() for event in history]})
        return
    typer.echo(f"{ctx['ref']}: {ctx['title']}")
    stages = ", ".join(ctx["stages"])
    typer.echo(f"project: {ctx['project']} ({ctx['project_name']}; stages {stages})")
    typer.echo(f"stage: {ctx['stage']}{' (needs attention)' if ctx['attention'] else ''}")
    typer.echo(f"priority: {ctx['priority']}")
    for key in ("after", "from"):
        if ctx[key]:
            typer.echo(f"{key}: {ctx[key]}")
    claim = ctx["claim"]
    typer.echo(f"claim: {f'run {claim["run_id"]} by {claim["actor"]}' if claim else '-'}")
    entered = ctx["entered_stage_at"]
    typer.echo(f"in stage since: {seconds(entered)} ({ago(entered)})")
    typer.echo("moves:")
    for item in ctx["moves"]:
        target = f" to {item['to']}" if item["to"] else ""
        state = "available" if item["allowed"] else "not available: " + "; ".join(item["missing"])
        typer.echo(f"  {item['id']}{target}: {state}")
    if ctx["refs"]:
        typer.echo("refs:")
        for item in ctx["refs"]:
            typer.echo(f"  {item['kind']} {item['value']}")
    if ctx["body"]:
        typer.echo(f"\n{ctx['body']}\n")
    typer.echo("timeline:")
    for event in history:
        stage = f" {event.from_stage} → {event.to_stage}" if event.kind == "moved" else ""
        run = f" (run {event.run_id})" if event.run_id else ""
        line = f"  {seconds(event.created_at)} {event.kind}{stage} by {event.actor}{run}"
        typer.echo(f"{line}: {event.message}" if event.message else line)


@task_app.command("advance")
def task_advance(
    ref: str,
    message: str = typer.Option(..., "--message", help=MESSAGE_HELP),
    refs: list[str] = REFS,
    force: bool = typer.Option(False, "--force", help=FORCE_HELP),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Hand the task to the next stage (or finish it from the last one)."""
    _move(
        ref,
        "advance",
        workspace=workspace,
        message=message,
        as_json=as_json,
        force=force,
        refs=refs,
    )


@task_app.command("return")
def task_return(
    ref: str,
    message: str = typer.Option(..., "--message", help=MESSAGE_HELP),
    force: bool = typer.Option(False, "--force", help=FORCE_HELP),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Send the task back to the previous stage."""
    _move(ref, "return", workspace=workspace, message=message, as_json=as_json, force=force)


@task_app.command("block")
def task_block(
    ref: str,
    message: str = typer.Option(..., "--message", help="What is needed and what unblocks it."),
    after: str | None = typer.Option(None, "--after", help="Resume when this task is done."),
    force: bool = typer.Option(False, "--force", help=FORCE_HELP),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Stop on the task until a person decides or another task finishes."""
    _move(
        ref,
        "block",
        workspace=workspace,
        message=message,
        as_json=as_json,
        after=after,
        force=force,
    )


@task_app.command("resume")
def task_resume(
    ref: str,
    message: str | None = typer.Option(None, "--message", help="Optional; - reads stdin."),
    to: str | None = typer.Option(None, "--to", help="A project stage; default is where it left."),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Put a blocked task back into its pipeline."""
    _move(ref, "resume", workspace=workspace, message=message, as_json=as_json, to=to)


@task_app.command("drop")
def task_drop(
    ref: str,
    message: str = typer.Option(..., "--message", help="Why it will not be done."),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Cancel a task; a person only."""
    _move(ref, "drop", workspace=workspace, message=message, as_json=as_json)


@task_app.command("release")
def task_release(
    ref: str,
    message: str = typer.Option(..., "--message", help="Why the claim is let go."),
    force: bool = typer.Option(False, "--force", help=FORCE_HELP),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Clear the claim without moving: the claiming run, or a person with --force."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        task = tasks.release(
            paths,
            ref,
            actor=tasks.actor_from_env(os.environ),
            run_id=tasks.in_run(os.environ),
            message=_text(message, as_json=as_json) or "",
            reason="manual",  # only the runner's finalise records run_ended
            force=force,
        )
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    _emit(task, as_json=as_json, verb="released")


@task_app.command("edit")
def task_edit(
    ref: str,
    title: str | None = typer.Option(None, "--title"),
    body_file: Path | None = BODY_FILE,
    priority: int | None = typer.Option(None, "--priority"),
    after: str | None = typer.Option(
        None, "--after", help="A task to wait on while blocked; '' clears it."
    ),
    force: bool = typer.Option(False, "--force", help=FORCE_HELP),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Change the title, spec, priority, or dependency; old values stay in the timeline."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        task = tasks.edit(
            paths,
            ref,
            actor=tasks.actor_from_env(os.environ),
            run_id=tasks.in_run(os.environ),
            title=title,
            body=_file_text(body_file, as_json=as_json),
            priority=priority,
            after=after,
            force=force,
        )
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    _emit(task, as_json=as_json, verb="edited")


@task_app.command("note")
def task_note(
    ref: str,
    text: str = typer.Argument(..., help="The note; - reads stdin."),
    attention: bool = typer.Option(False, "--attention", help="Flag it for a person."),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Add to the timeline without moving the task."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        event = tasks.note(
            paths,
            ref,
            actor=tasks.actor_from_env(os.environ),
            run_id=tasks.in_run(os.environ),
            message=_text(text, as_json=as_json) or "",
            attention=attention,
        )
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(event.as_dict())
    else:
        typer.echo(f"noted {tasks.parse_ref(ref)}")


@task_app.command("ref")
def task_ref(
    ref: str, kind: str, value: str, workspace: str | None = WORKSPACE, as_json: bool = JSON_FLAG
) -> None:
    """Attach evidence: a commit, path, url, page, or anything else worth finding again."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        attached = tasks.add_ref(
            paths,
            ref,
            kind,
            value,
            actor=tasks.actor_from_env(os.environ),
            run_id=tasks.in_run(os.environ),
        )
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(attached.as_dict())
    else:
        typer.echo(f"{tasks.parse_ref(ref)}: {attached.kind} {attached.value}")


@task_app.command("land")
def task_land(ref: str, workspace: str | None = WORKSPACE, as_json: bool = JSON_FLAG) -> None:
    """Rebase the task's branch onto its base and fast-forward the main checkout.

    Inside a run, only the project's last agent stage lands; a person may land from anywhere.
    """
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    _scope(paths, config, workspace, ref=ref, as_json=as_json)
    try:
        task = tasks.get(paths, ref)
    except tasks.TaskError as exc:
        fail([str(exc)], as_json=as_json)
    project = config.projects.get(task.project)
    if project is None or project.repo is None:
        fail([f"project {task.project} has no repository; nothing to land"], as_json=as_json)
    run_id = tasks.in_run(os.environ)
    try:
        with worktrees.execution_context(paths, task.ref):
            task = tasks.get(paths, ref)
            tasks.ensure_no_pending(paths, task.ref)
            tasks.check_land(config, task, run_id)
            head = worktrees.land(paths, project, task.ref)
    except (tasks.TaskError, worktrees.WorktreeError) as exc:
        fail([str(exc)], as_json=as_json)
    base = (worktrees.lookup(paths, task.ref) or {}).get("base") or worktrees.base_branch(
        project.repo
    )
    with contextlib.suppress(tasks.TaskError):  # a finished task takes no refs; it landed anyway
        tasks.add_ref(
            paths, task.ref, "commit", head, actor=tasks.actor_from_env(os.environ), run_id=run_id
        )
    if as_json:
        echo_json({"ok": True, "ref": task.ref, "base": base, "head": head})
    else:
        typer.echo(f"landed {task.ref}: {base} is now at {head}")


@task_app.command("sweep")
def task_sweep(
    project: str | None = typer.Option(None, "--project", help="One project; default all."),
    all_workspaces: bool = ALL_WORKSPACES,
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Remove the worktrees of finished, clean tasks and their merged branches."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    selected = _scope(
        paths, config, workspace, project=project, all_workspaces=all_workspaces, as_json=as_json
    )
    keys = (
        [project.strip().upper()]
        if project
        else [
            key
            for key, item in config.projects.items()
            if selected is None or item.workspace == selected
        ]
    )
    removed: dict[str, list[str]] = {}
    for key in keys:
        found = config.projects.get(key)
        if found is None:
            fail([f"project {key} is not configured"], as_json=as_json)
        if found.repo is None:
            continue
        try:
            removed[key] = worktrees.sweep(paths, found)
        except worktrees.WorktreeError as exc:
            fail([f"{key}: {exc}"], as_json=as_json)
    if as_json:
        echo_json({"ok": True, "removed": removed})
        return
    swept = [f"{ref} ({key})" for key, refs in removed.items() for ref in refs]
    typer.echo("removed " + ", ".join(swept) if swept else "nothing to sweep")
