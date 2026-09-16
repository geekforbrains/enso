"""Read and maintain dated Markdown memory in an explicitly selected workspace."""

from __future__ import annotations

from typing import Any

import typer

from .. import memory
from ..config import Paths, resolve_workspace
from ..knowledge.catalog import Note
from ..note_storage import MAX_NOTE_BYTES, NoteError
from .common import JSON_FLAG, WORKSPACE, InputError, columns, echo_json, fail, read_input

memory_app = typer.Typer(no_args_is_help=True, help="Find and maintain dated workspace memory.")


def _summary(note: Note) -> dict[str, Any]:
    return {
        "workspace": note.root.label,
        "path": note.path,
        "title": note.title,
        "id": note.id,
        "metadata": note.metadata,
        "sha256": note.sha256,
        "problems": list(note.problems),
    }


def _listing(query: str, workspace: str | None, limit: int, offset: int, as_json: bool) -> None:
    try:
        paths = Paths.from_env()
        selected = resolve_workspace(paths, workspace)
        catalog = memory.scan(paths, selected)
        scope = f"workspace:{selected}"
        catalog.root(scope)
        terms = query.casefold().split()
        notes = [
            note
            for note in catalog.notes
            if note.scope == scope
            and all(term in f"{note.path}\n{note.body}".casefold() for term in terms)
        ]
        # Known events sort newest first; unknown or invalid occurrence stays at the end.
        notes.sort(key=lambda note: note.path.casefold())
        notes.sort(key=lambda note: note.metadata.get("occurred") or "", reverse=True)
        result: dict[str, Any] = {
            "workspace": selected,
            "total": len(notes),
            "offset": offset,
            "limit": limit,
            "notes": [_summary(note) for note in notes[offset : offset + limit]],
            "problems": [p for p in catalog.problems if p.startswith(f"{scope}:")],
        }
    except (OSError, ValueError, NoteError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        typer.echo(
            columns(
                [
                    ["OCCURRED", "PATH", "ID"],
                    *[
                        [
                            n["metadata"].get("occurred") or "unknown",
                            n["path"],
                            n["id"] or "invalid",
                        ]
                        for n in result["notes"]
                    ],
                ]
            )
        )
        for problem in result["problems"]:
            typer.echo(problem)
        typer.echo(f"{len(result['notes'])} of {result['total']} notes")


@memory_app.command("list")
def list_notes(
    workspace: str | None = WORKSPACE,
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    as_json: bool = JSON_FLAG,
) -> None:
    """List a bounded page, newest event first and unknown dates last."""
    _listing("", workspace, limit, offset, as_json)


@memory_app.command("search")
def search(
    query: str,
    workspace: str | None = WORKSPACE,
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    as_json: bool = JSON_FLAG,
) -> None:
    """Find every query term in filenames or bodies within one workspace."""
    _listing(query, workspace, limit, offset, as_json)


@memory_app.command("show")
def show(ref: str, workspace: str | None = WORKSPACE, as_json: bool = JSON_FLAG) -> None:
    """Read a UUID or exact .md path, including metadata problems and the revision hash."""
    try:
        paths = Paths.from_env()
        selected = resolve_workspace(paths, workspace)
        note = memory.scan(paths, selected).get(ref, f"workspace:{selected}")
    except (OSError, ValueError, NoteError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json({**_summary(note), "body": note.body})
    else:
        typer.echo(f"{selected}:{note.path}\nsha256: {note.sha256}")
        for problem in note.problems:
            typer.echo(f"problem: {problem}")
        typer.echo(f"\n{note.body}")


@memory_app.command("audit")
def audit(workspace: str | None = WORKSPACE, as_json: bool = JSON_FLAG) -> None:
    """Validate metadata, occurrence folders, identities, and relative links without writes."""
    try:
        paths = Paths.from_env()
        selected = resolve_workspace(paths, workspace)
        catalog = memory.scan(paths, selected)
        scope = f"workspace:{selected}"
        problems = catalog.audit(scope)
        count = sum(note.scope == scope for note in catalog.notes)
    except (OSError, ValueError, NoteError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json({"ok": not problems, "workspace": selected, "notes": count, "problems": problems})
    else:
        for problem in problems:
            typer.echo(f"{problem['scope']}:{problem['path']}: {problem['problem']}")
        typer.echo(f"{count} notes, {len(problems)} problems")
    if problems:
        raise typer.Exit(1)


def _written(note: Note, as_json: bool) -> None:
    if as_json:
        echo_json({"ok": True, **_summary(note)})
    else:
        typer.echo(f"{note.root.label}:{note.path} ({note.id})")


@memory_app.command("create")
def create(
    name: str,
    occurred: str = typer.Option(
        ..., "--occurred", help="Event date, timestamp with timezone, or unknown."
    ),
    file: str = typer.Option(..., "--file", help="Markdown body file, or - for stdin."),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Create a manual note with a .md filename, placed by its event date."""
    try:
        paths = Paths.from_env()
        selected = resolve_workspace(paths, workspace)
        note = memory.create_note(
            paths,
            selected,
            name,
            read_input(file, limit=MAX_NOTE_BYTES),
            occurred=None if occurred == "unknown" else occurred,
        )
    except (OSError, ValueError, InputError, NoteError) as exc:
        fail([str(exc)], as_json=as_json)
    _written(note, as_json)


@memory_app.command("update")
def update(
    ref: str,
    file: str = typer.Option(
        ..., "--file", help="Replacement body including historical context and corrections."
    ),
    expected_hash: str = typer.Option(..., "--expected-hash", help="SHA256 from the last read."),
    occurred: str | None = typer.Option(
        None, "--occurred", help="Explicit event-time correction; use unknown if necessary."
    ),
    workspace: str | None = WORKSPACE,
    as_json: bool = JSON_FLAG,
) -> None:
    """Correct a note without changing its identity or silently redating the event."""
    try:
        paths = Paths.from_env()
        selected = resolve_workspace(paths, workspace)
        note = memory.update_note(
            paths,
            selected,
            ref,
            read_input(file, limit=MAX_NOTE_BYTES),
            expected_hash=expected_hash,
            occurred=occurred,
        )
    except (OSError, ValueError, InputError, NoteError) as exc:
        fail([str(exc)], as_json=as_json)
    _written(note, as_json)
