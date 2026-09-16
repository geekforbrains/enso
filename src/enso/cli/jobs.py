"""``enso job`` and ``enso runs``."""

from __future__ import annotations

import asyncio
from datetime import datetime

import typer

from .. import jobs, runs
from .. import log as logsetup
from ..config import Paths, resolve_workspace, split_job_ref
from ..formatting import format_elapsed
from ..jobs.runner import JobRunner, RunResult
from .common import (
    ALL_WORKSPACES,
    JSON_FLAG,
    WORKSPACE,
    ago,
    columns,
    echo_json,
    fail,
    load,
    seconds,
    workspace_scope,
)

job_app = typer.Typer(no_args_is_help=True, help="Workspace-owned scheduled jobs.")
runs_app = typer.Typer(no_args_is_help=True, help="Job run history.")


def _schedule(job: jobs.Job) -> str:
    """The cron line, or what fires a stage job without one."""
    if job.schedule is not None:
        return job.schedule
    return f"ready ({job.project}/{job.stage})"


def _job_summary(job: jobs.Job, problems: list[str], last: runs.Run | None) -> dict:
    return {
        **job.as_dict(),
        "problems": problems,
        "last_run": {"status": last.status, "started_at": last.started_at} if last else None,
    }


@job_app.command("list")
def job_list(
    workspace: str | None = WORKSPACE,
    all_workspaces: bool = ALL_WORKSPACES,
    as_json: bool = JSON_FLAG,
) -> None:
    """List jobs with their schedule, agent, workspace, and last run."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    selected = workspace_scope(paths, workspace, all_workspaces=all_workspaces, as_json=as_json)
    found, problems = jobs.load_jobs(paths, config)
    if selected is not None:
        found = [job for job in found if job.workspace == selected]
        problems = {
            ref: issues for ref, issues in problems.items() if ref.startswith(f"{selected}:")
        }
    last = runs.latest(paths)
    if as_json:
        parsed = {job.ref for job in found}
        echo_json(
            [_job_summary(j, problems.get(j.ref, []), last.get(j.ref)) for j in found]
            + [{"ref": n, "problems": p} for n, p in problems.items() if n not in parsed]
        )
        return
    if not found and not problems:
        typer.echo("no jobs yet; run `enso job create`")
        return
    rows = [["JOB", "SCHEDULE", "AGENT", "WORKSPACE", "ENABLED", "LAST RUN"]]
    for job in found:
        run = last.get(job.ref)
        rows.append(
            [
                job.ref,
                _schedule(job),
                f"{job.provider}/{job.model}/{job.effort}",
                job.workspace,
                "yes" if job.enabled else "no",
                f"{run.status} {ago(run.started_at)}" if run else "-",
            ]
        )
    typer.echo(columns(rows))
    for name, found_problems in problems.items():
        typer.echo(f"{name}: {'; '.join(found_problems)}", err=True)


@job_app.command("create")
def job_create(
    name: str = typer.Option(..., "--name", help="Display name; the directory is its slug."),
    provider: str = typer.Option(..., "--provider"),
    model: str = typer.Option(..., "--model"),
    effort: str = typer.Option(..., "--effort"),
    schedule: str | None = typer.Option(
        None, "--schedule", help="Cron, local time, e.g. '0 9 * * *'; optional with --stage."
    ),
    workspace: str | None = typer.Option(None, "--workspace", help="Defaults to ENSO_WORKSPACE."),
    project: str | None = typer.Option(None, "--project", help="Serve a task board project."),
    stage: str | None = typer.Option(None, "--stage", help="The project's agent stage to serve."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Scaffold a disabled JOB.md; edit the prompt, test with `job run`, then enable it."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    try:
        job = jobs.create_job(
            paths,
            config,
            name=name,
            provider=provider,
            model=model,
            effort=effort,
            schedule=schedule,
            workspace=resolve_workspace(paths, workspace),
            project=project,
            stage=stage,
        )
    except (ValueError, FileExistsError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(job.as_dict())
        return
    typer.echo(f"created {job.ref}: {job.path} (disabled)")


@job_app.command("show")
def job_show(
    name: str = typer.Argument(..., help="Qualified reference, e.g. team:digest."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Print one job's fields, problems, next and last run, and prompt."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    job, problems = jobs.find_job(paths, config, name)
    if job is None:
        fail(problems, as_json=as_json)
    last = runs.list_runs(paths, job=name, limit=1)
    now = datetime.now().astimezone()
    next_run = (
        job.next_run(now).isoformat(timespec="minutes")
        if not problems and job.schedule is not None
        else None
    )
    if as_json:
        echo_json({**_job_summary(job, problems, last[0] if last else None), "next_run": next_run})
        return
    fields = {key: value for key, value in job.as_dict().items() if key != "prompt"}
    for key, value in fields.items():
        typer.echo(f"{key}: {value if value is not None else '-'}")
    typer.echo(f"next_run: {next_run or '-'}")
    typer.echo(f"last_run: {f'{last[0].status} {ago(last[0].started_at)}' if last else '-'}")
    for problem in problems:
        typer.echo(f"problem: {problem}")
    typer.echo(f"\n{job.prompt}")


@job_app.command("run")
def job_run(
    name: str = typer.Argument(..., help="Qualified reference, e.g. team:digest."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Run a job now; prints the result and never sends alerts."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    job, problems = jobs.find_job(paths, config, name)
    if job is None or problems:
        fail(problems, as_json=as_json)
    logsetup.setup(paths, config.logging)
    result: RunResult = asyncio.run(JobRunner(config).run(job, trigger="manual"))
    if as_json:
        echo_json(result.as_dict())
    elif result.status == "no_work":
        reason = (
            "prerun exited 1"
            if result.exit_code == 1
            else f"no task is ready in {job.project}/{job.stage}"
        )
        typer.echo(f"no work ({reason}); the provider was not run")
    elif result.status == "skipped":
        typer.echo(f"skipped: {result.error}")
    else:
        if result.task:
            typer.echo(f"task: {result.task}")
        if result.output:
            typer.echo(result.output)
        if result.error and result.error != result.postrun_error:
            typer.echo(f"{result.status}: {result.error}", err=True)
    if result.postrun_error and not as_json:
        typer.echo(f"postrun failed: {result.postrun_error}", err=True)
    if result.status not in ("ok", "no_work"):
        raise typer.Exit(1)


# -- runs -----------------------------------------------------------------------


def _duration(run: runs.Run) -> str:
    return format_elapsed(run.duration_ms // 1000) if run.duration_ms is not None else "-"


@runs_app.command("list")
def runs_list(
    job: str | None = typer.Option(
        None, "--job", help="Only this qualified job's runs, e.g. team:digest."
    ),
    limit: int = typer.Option(20, "-n", help="How many, newest first."),
    workspace: str | None = WORKSPACE,
    all_workspaces: bool = ALL_WORKSPACES,
    as_json: bool = JSON_FLAG,
) -> None:
    """List recent runs."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)
    selected = workspace_scope(paths, workspace, all_workspaces=all_workspaces, as_json=as_json)
    if job is not None:
        try:
            split_job_ref(job)
        except ValueError as exc:
            fail([str(exc)], as_json=as_json)
    found = runs.list_runs(paths, job=job, limit=limit, workspace=selected)
    if as_json:
        echo_json([run.as_dict() for run in found])
        return
    if not found:
        typer.echo("no runs yet")
        return
    rows = [["ID", "STARTED", "STATUS", "DURATION", "JOB", "TRIGGER"]]
    rows.extend(
        [run.id, seconds(run.started_at), run.status, _duration(run), run.job, run.trigger]
        for run in found
    )
    typer.echo(columns(rows))


@runs_app.command("show")
def runs_show(run_id: str, as_json: bool = JSON_FLAG) -> None:
    """Print one run (a unique id prefix is enough) with its output."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)
    run = runs.get(paths, run_id)
    if run is None:
        fail([f"no run matches {run_id}"], as_json=as_json)
    attempts = runs.attempts(paths, run.id)
    if as_json:
        echo_json({**run.as_dict(), "attempts": [attempt.as_dict() for attempt in attempts]})
        return
    stamps = {"started_at": seconds(run.started_at), "ended_at": seconds(run.ended_at)}
    for key, value in (run.as_dict() | stamps).items():
        if key not in ("output", "error", "postrun_error"):
            typer.echo(f"{key}: {value if value is not None else '-'}")
    if run.error and run.error != run.postrun_error:
        typer.echo(f"error: {run.error}")
    if run.postrun_error:
        typer.echo(f"postrun failed: {run.postrun_error}")
    if run.output:
        typer.echo(f"\n{run.output}")
    for attempt in attempts:
        label = f"Attempt {attempt.number}" if attempt.number else "Postrun without a provider turn"
        typer.echo(f"\n{label}: {attempt.status}")
        for key, value in attempt.as_dict().items():
            if key not in (
                "number",
                "status",
                "output",
                "error",
                "postrun_output",
                "postrun_error",
            ):
                typer.echo(f"{key}: {value if value is not None else '-'}")
        if attempt.output:
            typer.echo(f"output:\n{attempt.output}")
        if attempt.error:
            typer.echo(f"error: {attempt.error}")
        if attempt.postrun_output:
            typer.echo(f"postrun output:\n{attempt.postrun_output}")
        if attempt.postrun_error:
            typer.echo(f"postrun failed: {attempt.postrun_error}")
