"""``enso project``: workspace-owned project definitions, listed and created."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import frontmatter, maintenance, tasks
from ..config import (
    ConfigError,
    Paths,
    config_lock,
    load_config,
    parse_project,
    parse_stages,
    resolve_workspace,
)
from .common import JSON_FLAG, columns, echo_json, fail, load
from .tasks import ALL_WORKSPACES, WORKSPACE, _scope

project_app = typer.Typer(no_args_is_help=True, help="Projects that tasks belong to.")
REPO = typer.Option(None, "--repo", help="A Git checkout; its tasks get worktrees.")
COPY = typer.Option([], "--copy", help="A path copied into new worktrees; repeatable.")


@project_app.command("list")
def project_list(
    workspace: str | None = WORKSPACE,
    all_workspaces: bool = ALL_WORKSPACES,
    as_json: bool = JSON_FLAG,
) -> None:
    """List projects with their workspace, repository, and stages."""
    paths = Paths.from_env()
    config = load(paths, as_json=as_json)
    selected = _scope(paths, config, workspace, all_workspaces=all_workspaces, as_json=as_json)
    projects = [
        project
        for project in config.projects.values()
        if selected is None or project.workspace == selected
    ]
    if as_json:
        echo_json([project.as_dict() for project in projects])
        return
    if not projects:
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
        for project in projects
    )
    typer.echo(columns(rows))


@project_app.command("add")
def project_add(
    key: str,
    name: str = typer.Option(..., "--name"),
    workspace: str | None = WORKSPACE,
    repo: Path | None = REPO,
    stages: str | None = typer.Option(None, "--stages", help="Comma-separated: a,b,c:human."),
    flow: str | None = typer.Option(None, "--flow", help=f"A preset: {', '.join(tasks.FLOWS)}."),
    setup: str | None = typer.Option(
        None, "--setup", help="Bash command beside PROJECT.md to prepare ENSO_TASK_DIR."
    ),
    copy: list[str] = COPY,
    as_json: bool = JSON_FLAG,
) -> None:
    """Create PROJECT.md in the selected workspace; validate before writing."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)
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
    entry: dict[str, object] = {"name": name, "stages": names}
    if repo is not None:
        # A relative path is only meaningful from this cwd; PROJECT.md is read from anywhere.
        entry["repo"] = str(repo if repo.expanduser().is_absolute() else repo.resolve())
    if setup is not None:
        entry["setup"] = setup
    if copy:
        entry["copy"] = list(copy)
    try:
        with config_lock(paths):
            config = load_config(paths)
            selected = resolve_workspace(paths, workspace)
            if key in config.projects:
                raise ValueError(f"project {key} already exists; edit its PROJECT.md")
            project = parse_project(paths, selected, key, entry, problems)
            if problems:
                raise ConfigError(problems)
            directory = paths.project(selected, key)
            if directory.parent.is_symlink():
                raise ValueError(f"{directory.parent}: expected a real projects directory")
            directory.mkdir(parents=True)  # Refuse existing directories, including orphans.
            path = directory / "PROJECT.md"
            try:
                maintenance.write_bytes(path, frontmatter.render(entry, "").encode())
            except OSError:
                directory.rmdir()
                raise
    except (ConfigError, ValueError, OSError) as exc:
        fail(exc.problems if isinstance(exc, ConfigError) else [str(exc)], as_json=as_json)
    if as_json:
        echo_json(project.as_dict())
        return
    typer.echo(f"added project {key} ({', '.join(project.stage_names)}) to {path}")
