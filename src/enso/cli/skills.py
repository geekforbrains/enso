"""List home skills and install from Enso's fixed official catalog."""

from __future__ import annotations

import typer

from .. import skill_catalog
from ..config import Paths
from .common import JSON_FLAG, columns, echo_json, fail

skill_app = typer.Typer(no_args_is_help=True, help="Home skills and the official Enso catalog.")


@skill_app.command("list")
def skill_list(
    available: bool = typer.Option(False, "--available", help="Fetch the official skill catalog."),
    as_json: bool = JSON_FLAG,
) -> None:
    """List installed home skills offline, or fetch available official skills."""
    try:
        if available:
            catalog = skill_catalog.available(Paths.from_env())
            if as_json:
                echo_json(catalog)
                return
            rows = [["SKILL", "INSTALLED", "REQUIRES", "DESCRIPTION"]]
            rows.extend(
                [
                    entry["name"],
                    "yes" if entry["installed"] else "no",
                    ", ".join(entry["requires"]) or "-",
                    " ".join(entry["description"].split()),
                ]
                for entry in catalog["skills"]
            )
            typer.echo(columns(rows) if catalog["skills"] else "no official skills available yet")
            return
        result = skill_catalog.installed(Paths.from_env())
    except (skill_catalog.SkillError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    elif result:
        rows = [["SKILL", "ORIGIN", "STATUS"]]
        rows.extend(
            [entry["name"], entry["origin"], "invalid" if entry["problems"] else "ok"]
            for entry in result
        )
        typer.echo(columns(rows))
    else:
        typer.echo("no home skills yet; use enso init or enso skill list --available")


@skill_app.command("show")
def skill_show(name: str, as_json: bool = JSON_FLAG) -> None:
    """Fetch one official skill's description, files, prerequisites, and pinned source."""
    try:
        result = skill_catalog.show(name)
    except (skill_catalog.SkillError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        typer.echo(f"{result['name']}: {' '.join(result['description'].split())}")
        typer.echo(f"Source: {result['source']} at {result['commit']}")
        typer.echo(f"Requires: {', '.join(result['requires']) or 'none'}")
        typer.echo("Files: " + ", ".join(result["files"]))


@skill_app.command("install")
def skill_install(name: str, as_json: bool = JSON_FLAG) -> None:
    """Add one official skill to this home's skills; existing files are preserved."""
    try:
        result = skill_catalog.install(Paths.from_env(), name)
    except (skill_catalog.SkillError, OSError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        typer.echo(f"Installed {name}: {result['path']}")
