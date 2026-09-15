"""Read and maintain filesystem knowledge without loading transport configuration."""

from __future__ import annotations

from typing import Any

import typer

from .. import knowledge
from ..config import Paths
from .common import JSON_FLAG, InputError, columns, echo_json, fail, read_input

knowledge_app = typer.Typer(no_args_is_help=True, help="Browse and maintain Markdown knowledge.")


def _summary(note: knowledge.Note) -> dict[str, Any]:
    return {
        "scope": note.scope,
        "path": note.path,
        "title": note.title,
        "id": note.id,
        "metadata": note.metadata,
        "sha256": note.sha256,
        "problems": list(note.problems),
    }


@knowledge_app.command("roots")
def roots(as_json: bool = JSON_FLAG) -> None:
    """Show shared knowledge and automatically discovered workspace roots."""
    try:
        result = [
            {"scope": root.scope, "label": root.label, "path": str(root.path)}
            for root in knowledge.discover_roots(Paths.from_env())
        ]
    except OSError as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        typer.echo(
            columns([["SCOPE", "PATH"], *[[root["scope"], root["path"]] for root in result]])
        )


def _listing(
    query: str, scope: str | None, folder: str, limit: int, offset: int, as_json: bool
) -> None:
    try:
        catalog = knowledge.scan(Paths.from_env())
        if scope:
            catalog.root(scope)
        if folder:
            from ..knowledge.storage import relative_parts

            relative_parts(folder)
        prefix = folder.strip("/") + "/" if folder else ""
        terms = query.casefold().split()
        notes = [
            note
            for note in catalog.notes
            if (not scope or note.scope == scope)
            and note.path.startswith(prefix)
            and all(term in f"{note.path}\n{note.body}".casefold() for term in terms)
        ]
        notes.sort(key=lambda note: (note.scope, note.path.casefold()))
        result: dict[str, Any] = {
            "total": len(notes),
            "offset": offset,
            "limit": limit,
            "notes": [_summary(note) for note in notes[offset : offset + limit]],
            "problems": list(catalog.problems),
        }
    except (OSError, knowledge.KnowledgeError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        typer.echo(
            columns(
                [
                    ["SCOPE", "PATH", "ID"],
                    *[
                        [note["scope"], note["path"], note["id"] or "legacy"]
                        for note in result["notes"]
                    ],
                ]
            )
        )
        typer.echo(f"{len(result['notes'])} of {result['total']} notes")


@knowledge_app.command("list")
def list_notes(
    scope: str | None = typer.Option(None, "--scope"),
    folder: str = typer.Option("", "--folder", help="Include this folder and its descendants."),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    as_json: bool = JSON_FLAG,
) -> None:
    """List a bounded page of notes and their revision hashes."""
    _listing("", scope, folder, limit, offset, as_json)


@knowledge_app.command("search")
def search(
    query: str,
    scope: str | None = typer.Option(None, "--scope"),
    folder: str = typer.Option("", "--folder"),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
    offset: int = typer.Option(0, "--offset", min=0),
    as_json: bool = JSON_FLAG,
) -> None:
    """Search filenames and note bodies within optional scope and folder boundaries."""
    _listing(query, scope, folder, limit, offset, as_json)


@knowledge_app.command("show")
def show(
    ref: str, scope: str = typer.Option("general", "--scope"), as_json: bool = JSON_FLAG
) -> None:
    """Read a UUID or exact path, including its hash and backlinks."""
    try:
        catalog = knowledge.scan(Paths.from_env())
        note = catalog.get(ref, scope)
        result = {
            **_summary(note),
            "body": note.body,
            "backlinks": [_summary(n) for n in catalog.backlinks(note)],
        }
    except (OSError, knowledge.KnowledgeError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        typer.echo(f"{note.scope}:{note.path}\nsha256: {note.sha256}\n\n{note.body}")


@knowledge_app.command("audit")
def audit(scope: str | None = typer.Option(None, "--scope"), as_json: bool = JSON_FLAG) -> None:
    """Check core metadata, duplicate IDs, link targets, and heading anchors."""
    try:
        catalog = knowledge.scan(Paths.from_env())
        if scope:
            catalog.root(scope)
        problems = catalog.audit(scope)
        count = sum(1 for note in catalog.notes if not scope or note.scope == scope)
        result = {"ok": not problems, "notes": count, "problems": problems}
    except (OSError, knowledge.KnowledgeError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        for problem in problems:
            typer.echo(f"{problem['scope']}:{problem['path']}: {problem['problem']}")
        typer.echo(f"{count} notes, {len(problems)} problems")
    if problems:
        raise typer.Exit(1)


def _input(file: str) -> str:
    return read_input(file, limit=2 * 1024 * 1024)


def _write_result(note: knowledge.Note, as_json: bool) -> None:
    if as_json:
        echo_json({"ok": True, **_summary(note)})
    else:
        typer.echo(f"{note.scope}:{note.path} ({note.id})")


@knowledge_app.command("create")
def create(
    path: str,
    file: str = typer.Option(..., "--file", help="Markdown body file, or - for stdin."),
    scope: str = typer.Option("general", "--scope"),
    as_json: bool = JSON_FLAG,
) -> None:
    """Create a Markdown note with automatically maintained core metadata."""
    try:
        note = knowledge.create_note(Paths.from_env(), scope, path, _input(file))
    except (OSError, InputError, knowledge.KnowledgeError) as exc:
        fail([str(exc)], as_json=as_json)
    _write_result(note, as_json)


@knowledge_app.command("adopt")
def adopt(
    path: str,
    scope: str = typer.Option("general", "--scope"),
    expected_hash: str | None = typer.Option(None, "--expected-hash"),
    as_json: bool = JSON_FLAG,
) -> None:
    """Normalize one existing note while preserving unfamiliar original metadata."""
    try:
        note = knowledge.adopt_note(Paths.from_env(), scope, path, expected_hash=expected_hash)
    except (OSError, knowledge.KnowledgeError) as exc:
        fail([str(exc)], as_json=as_json)
    _write_result(note, as_json)


@knowledge_app.command("update")
def update(
    ref: str,
    file: str = typer.Option(..., "--file", help="Replacement Markdown body, or - for stdin."),
    expected_hash: str = typer.Option(
        ..., "--expected-hash", help="SHA256 from the last show/list."
    ),
    scope: str = typer.Option("general", "--scope"),
    as_json: bool = JSON_FLAG,
) -> None:
    """Update a managed note, refusing an edit based on stale contents."""
    try:
        note = knowledge.update_note(
            Paths.from_env(), scope, ref, _input(file), expected_hash=expected_hash
        )
    except (OSError, InputError, knowledge.KnowledgeError) as exc:
        fail([str(exc)], as_json=as_json)
    _write_result(note, as_json)


@knowledge_app.command("move")
def move(
    ref: str,
    destination: str,
    scope: str = typer.Option("general", "--scope"),
    to_scope: str | None = typer.Option(None, "--to-scope"),
    expected_hash: str | None = typer.Option(None, "--expected-hash"),
    as_json: bool = JSON_FLAG,
) -> None:
    """Move one note and repair resolved incoming and outgoing note/asset links."""
    try:
        result = knowledge.move_note(
            Paths.from_env(),
            scope,
            ref,
            destination,
            to_scope=to_scope,
            expected_hash=expected_hash,
        )
    except (OSError, knowledge.KnowledgeError) as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(result)
    else:
        typer.echo(
            f"Moved to {result['scope']}:{result['path']}; "
            f"updated {result['links_updated']} linked notes"
        )
