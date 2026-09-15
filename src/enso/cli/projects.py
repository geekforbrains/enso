"""``enso project``: the ``projects`` section of config.json, listed and extended."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import tasks
from ..config import ConfigError, Paths, parse_config, parse_stages, read_raw_config, save_config
from .common import JSON_FLAG, columns, echo_json, fail, load

project_app = typer.Typer(no_args_is_help=True, help="Projects that tasks belong to.")
REPO = typer.Option(None, "--repo", help="A Git checkout; its tasks get worktrees.")
COPY = typer.Option([], "--copy", help="A path copied into new worktrees; repeatable.")


@project_app.command("list")
def project_list(as_json: bool = JSON_FLAG) -> None:
    """List projects with their workspace, repository, and stages."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    if as_json:
        echo_json([project.as_dict() for project in config.projects.values()])
        return
    if not config.projects:
        typer.echo("no projects yet; run `enso project add`")
        return
    rows = [["KEY", "NAME", "WORKSPACE", "REPO", "STAGES"]]
    rows.extend(
        [
            project.key,
            project.name,
            project.workspace,
            str(project.repo) if project.repo else "-",
            ", ".join(f"{s.name}:human" if s.human else s.name for s in project.stages),
        ]
        for project in config.projects.values()
    )
    typer.echo(columns(rows))


@project_app.command("add")
def project_add(
    key: str,
    name: str = typer.Option(..., "--name"),
    workspace: str = typer.Option(..., "--workspace", help="Where its stage jobs run."),
    repo: Path | None = REPO,
    stages: str | None = typer.Option(None, "--stages", help="Comma-separated: a,b,c:human."),
    flow: str | None = typer.Option(None, "--flow", help=f"A preset: {', '.join(tasks.FLOWS)}."),
    setup: str | None = typer.Option(None, "--setup", help="Bash command run in a new worktree."),
    copy: list[str] = COPY,
    as_json: bool = JSON_FLAG,
) -> None:
    """Add a project to config.json; validated first, written atomically."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)  # the current file must be sound before it is rewritten
    if (stages is None) == (flow is None):
        fail(["give --stages or --flow, not both and not neither"], as_json=as_json)
    if flow == "dev":
        fail(
            [
                "create the project with --flow basic, then use enso workflow init KEY "
                "--preset dev --lint COMMAND --test COMMAND for the development workflow"
            ],
            as_json=as_json,
        )
    if flow is not None and flow not in tasks.FLOWS:
        fail([f"--flow must be one of {', '.join(tasks.FLOWS)}"], as_json=as_json)
    names = (
        list(tasks.FLOWS[flow])
        if flow is not None
        else [item.strip() for item in (stages or "").split(",") if item.strip()]
    )
    problems: list[str] = []
    parse_stages(names, "--stages", problems)
    if problems:
        fail(problems, as_json=as_json)
    try:
        raw = read_raw_config(paths)
    except ConfigError as exc:
        fail(exc.problems, as_json=as_json)
    projects = raw.setdefault("projects", {})
    if not isinstance(projects, dict):
        fail(["projects in config.json is not an object"], as_json=as_json)
    if key in projects:
        fail([f"project {key} already exists; edit {paths.config} to change it"], as_json=as_json)
    entry: dict[str, object] = {"name": name, "workspace": workspace, "stages": names}
    if repo is not None:
        # A relative path is only meaningful from this cwd; config.json is read from anywhere.
        entry["repo"] = str(repo if repo.expanduser().is_absolute() else repo.resolve())
    if setup is not None:
        entry["setup"] = setup
    if copy:
        entry["copy"] = list(copy)
    projects[key] = entry
    config, problems, _warnings = parse_config(raw, paths)
    if config is None:
        fail(problems, as_json=as_json)
    save_config(paths, raw)
    project = config.projects[key]
    if as_json:
        echo_json(project.as_dict())
        return
    typer.echo(f"added project {key} ({', '.join(project.stage_names)}) to {paths.config}")
