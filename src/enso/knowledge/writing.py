"""Validated note writes, conservative adoption, and link-aware moves."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import uuid4

from .. import frontmatter
from .. import note_storage as storage
from ..config import Paths
from ..note_storage import (
    NoteError as KnowledgeError,
)
from ..note_storage import (
    Root,
    open_parent,
    read_bytes,
    safe_path,
    split_document,
    valid_id,
    valid_timestamp,
)
from .catalog import (
    SCHEMA,
    Catalog,
    Note,
    Resolution,
    metadata_problems,
    scan,
)


def _document(fields: dict[str, str], body: str) -> str:
    if problems := metadata_problems({"schema": SCHEMA, **fields}):
        raise KnowledgeError("; ".join(problems))
    # These four values have fixed scalar syntax; quoting timestamps avoids YAML date coercion.
    lines = ["---", f"schema: {SCHEMA}", f"id: {fields['id']}"]
    for key in ("created", "updated"):
        if key in fields:
            lines.append(f'{key}: "{fields[key]}"')
    return "\n".join([*lines, "---", "", body.lstrip("\r\n")]).rstrip("\r\n") + "\n"


def normalize_text(text: str) -> str:
    """Adopt a copy without inventing dates or discarding original unfamiliar metadata."""
    document, _ = frontmatter.parse(text)
    old = document.fields if document else {}
    fields = {"schema": SCHEMA, "id": valid_id(old.get("id")) or str(uuid4())}
    for key in ("created", "updated"):
        if value := valid_timestamp(old.get(key)):
            fields[key] = value
    if (
        "created" in fields
        and "updated" in fields
        and datetime.fromisoformat(fields["created"]) > datetime.fromisoformat(fields["updated"])
    ):
        del fields["updated"]
    raw, body = split_document(text)
    if not raw and text.splitlines() and text.splitlines()[0].rstrip() == "---":
        fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", text)), default=0))
        body = (
            "## Imported document\n\n"
            "Original document retained because its frontmatter was unterminated.\n\n"
            f"{fence}markdown\n{text.rstrip(chr(13) + chr(10))}\n{fence}\n"
        )
    if raw and (not document or metadata_problems(old)):
        fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", raw)), default=0))
        body = (
            body.rstrip("\r\n")
            + "\n\n## Imported metadata\n\n"
            + "Original frontmatter retained during import; it is historical data.\n\n"
            + f"{fence}yaml\n{raw.rstrip(chr(13) + chr(10))}\n{fence}\n"
        )
    return _document(fields, body)


def _hash(root: Root, relative: str) -> str | None:
    try:
        return hashlib.sha256(read_bytes(root, relative)).hexdigest()
    except FileNotFoundError:
        return None


def _unlink(root: Root, relative: str, expected_hash: str) -> None:
    directory, name = open_parent(root, relative)
    try:
        if storage.hash_at(directory, name) != expected_hash:
            raise KnowledgeError("note changed before removal")
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def _note_after(paths: Paths, scope: str, relative: str) -> Note:
    return scan(paths).get(relative, scope)


def _expected(note: Note, expected_hash: str | None) -> str:
    if expected_hash is not None and note.sha256 != expected_hash:
        raise KnowledgeError("note changed since it was read; read it again")
    return note.sha256


def create_note(paths: Paths, scope: str, relative: str, body: str) -> Note:
    """Create a new note with exactly four managed fields; never overwrite a note."""
    storage.body_only(body)
    with storage.writer(paths, "knowledge"):
        catalog = scan(paths)
        root = catalog.root(scope)
        _unoccupied(catalog, scope, relative)
        stamp = storage.timestamp()
        text = _document({"id": str(uuid4()), "created": stamp, "updated": stamp}, body)
        storage.publish(root, relative, text, expected_hash=None)
        return _note_after(paths, scope, relative)


def adopt_note(
    paths: Paths, scope: str, relative: str, *, expected_hash: str | None = None
) -> Note:
    """Normalize one existing copied note; preserve unfamiliar frontmatter in its body."""
    with storage.writer(paths, "knowledge"):
        catalog = scan(paths)
        note = catalog.get(relative, scope)
        catalog.require_unique(note)
        expected = _expected(note, expected_hash)
        text = read_bytes(note.root, note.path).decode("utf-8")
        if hashlib.sha256(text.encode()).hexdigest() != expected:
            raise KnowledgeError("note changed since it was read; read it again")
        normalized = normalize_text(text)
        if normalized != text:
            storage.publish(note.root, note.path, normalized, expected_hash=expected)
        return _note_after(paths, scope, note.path)


def update_note(paths: Paths, scope: str, ref: str, body: str, *, expected_hash: str) -> Note:
    """Replace a managed note's body using its last read hash, preserving identity/creation."""
    storage.body_only(body)
    with storage.writer(paths, "knowledge"):
        catalog = scan(paths)
        note = catalog.get(ref, scope)
        catalog.require_unique(note)
        _expected(note, expected_hash)
        if note.problems or not note.id:
            raise KnowledgeError("adopt the note's metadata before updating it")
        if body.strip("\r\n") == note.body.strip("\r\n"):
            return note
        fields = {"id": note.id, "updated": storage.timestamp()}
        if created := valid_timestamp(note.metadata.get("created")):
            fields["created"] = created
        storage.publish(note.root, note.path, _document(fields, body), expected_hash=expected_hash)
        return _note_after(paths, note.scope, note.path)


def _after_move(
    catalog: Catalog, moved: Note, target_root: Root, destination: str
) -> tuple[Catalog, Note]:
    """The catalog as the move would leave it, so every link resolves from both sides."""
    moved_after = replace(
        moved, root=target_root, path=destination, title=PurePosixPath(destination).stem
    )
    notes = tuple(moved_after if note is moved else note for note in catalog.notes)
    return Catalog(catalog.roots, notes, catalog.problems, catalog.assets), moved_after


def _relinked_body(
    before: Catalog, after: Catalog, note: Note, moved: Note, moved_after: Note
) -> str:
    """Qualify only the resolved links whose destination the move would change or break.

    Links that still reach the same note or attachment afterwards, such as bare wiki names
    within one scope or a fragment-only link, stay as written so the file remains portable.
    """
    source_after = moved_after if note is moved else note
    replacements: list[tuple[int, int, str]] = []
    for link in note.links:
        result = before.resolve(note, link.target, wiki=link.wiki)
        if result.status not in {"note", "asset"}:
            continue
        outcome = after.resolve(source_after, link.target, wiki=link.wiki)
        if _same_destination(result, outcome, moved, moved_after):
            continue
        if result.note:
            target_note = moved_after if result.note is moved else result.note
            scope, path = target_note.scope, target_note.path
            if link.wiki:
                path = path.removesuffix(".md")
        else:
            assert result.asset is not None
            asset_root = next(
                root for root in before.roots if result.asset.is_relative_to(root.path)
            )
            scope, path = asset_root.scope, result.asset.relative_to(asset_root.path).as_posix()
        target = f"{scope}:{quote(path, safe='/-._~')}"
        if result.fragment:
            target += f"#{quote(result.fragment, safe='-._~')}"
        replacements.append((link.start, link.end, target))
    body = note.body
    for start, end, value in reversed(replacements):
        body = body[:start] + value + body[end:]
    return body


def _same_destination(
    before: Resolution, after: Resolution, moved: Note, moved_after: Note
) -> bool:
    if before.status != after.status:
        return False
    if before.note is None or after.note is None:
        return before.asset == after.asset
    return _identity(before.note, moved, moved_after) == _identity(after.note, moved, moved_after)


def _identity(note: Note, moved: Note, moved_after: Note) -> tuple[str, str]:
    if note is moved_after:
        note = moved
    return note.scope, note.path


def _unoccupied(catalog: Catalog, scope: str, relative: str) -> None:
    if any(
        note.scope == scope and note.path.casefold() == relative.casefold()
        for note in catalog.notes
    ):
        raise KnowledgeError("destination already exists (note paths are case insensitive)")


def _with_body(note: Note, raw: str, body: str) -> str:
    if body == note.body:
        return raw
    if note.problems or not note.id:
        raise KnowledgeError("adopt linked notes with invalid metadata before moving this note")
    fields = {"id": note.id, "updated": storage.timestamp()}
    if created := valid_timestamp(note.metadata.get("created")):
        fields["created"] = created
    return _document(fields, body)


def move_note(
    paths: Paths,
    scope: str,
    ref: str,
    destination: str,
    *,
    to_scope: str | None = None,
    expected_hash: str | None = None,
) -> dict[str, object]:
    """Move one note and repair resolved inbound/outbound links, preserving its stable ID.

    Destination is published first and the source is removed last. Every participating
    revision is checked before publication. Failures roll back only untouched writes;
    originals remain in a recovery directory if a concurrent edit prevents rollback.
    """
    with storage.writer(paths, "knowledge"):
        catalog = scan(paths)
        moved = catalog.get(ref, scope)
        catalog.require_unique(moved)
        _expected(moved, expected_hash)
        target_root = catalog.root(to_scope or moved.scope)
        safe_path(target_root, destination)
        if target_root.scope == moved.scope and destination == moved.path:
            raise KnowledgeError("source and destination are the same note")
        _unoccupied(catalog, target_root.scope, destination)
        if _hash(target_root, destination) is not None:
            raise KnowledgeError("move destination already exists")
        if any(
            catalog.resolve(note, link.target, wiki=link.wiki).status == "ambiguous"
            and moved in catalog.resolve(note, link.target, wiki=link.wiki).candidates
            for note in catalog.notes
            for link in note.links
        ):
            raise KnowledgeError(
                "ambiguous inbound links must be qualified before moving this note"
            )
        after, moved_after = _after_move(catalog, moved, target_root, destination)
        changes: list[tuple[Note, str, str]] = []
        for note in catalog.notes:
            body = _relinked_body(catalog, after, note, moved, moved_after)
            if note is moved or body != note.body:
                catalog.require_unique(note)
                raw = read_bytes(note.root, note.path).decode("utf-8")
                if hashlib.sha256(raw.encode()).hexdigest() != note.sha256:
                    raise KnowledgeError("a linked note changed; retry the move")
                changes.append((note, raw, _with_body(note, raw, body)))
        return _apply_move(paths, moved, target_root, destination, changes)


def _apply_move(
    paths: Paths,
    moved: Note,
    target_root: Root,
    destination: str,
    changes: list[tuple[Note, str, str]],
) -> dict[str, object]:
    recovery = Path(tempfile.mkdtemp(prefix=".knowledge-move-", dir=paths.home))
    applied: list[tuple[Note, str, str]] = []
    destination_hash: str | None = None
    try:
        for number, (_note, raw, _) in enumerate(changes):
            (recovery / f"{number}.md").write_text(raw, encoding="utf-8")
        import json

        (recovery / "manifest.json").write_text(
            json.dumps(
                {
                    "source": str(safe_path(moved.root, moved.path)),
                    "destination": str(safe_path(target_root, destination)),
                    "originals": [
                        {
                            "backup": f"{i}.md",
                            "path": str(safe_path(n.root, n.path)),
                            "sha256": n.sha256,
                        }
                        for i, (n, _, _) in enumerate(changes)
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        for note, _, _ in changes:
            if _hash(note.root, note.path) != note.sha256:
                raise KnowledgeError("a linked note changed; retry the move")
        moved_text = next(new for note, _, new in changes if note == moved)
        storage.publish(target_root, destination, moved_text, expected_hash=None)
        destination_hash = hashlib.sha256(moved_text.encode()).hexdigest()
        for note, raw, new in changes:
            if note != moved:
                storage.publish(note.root, note.path, new, expected_hash=note.sha256)
                applied.append((note, raw, new))
        if _hash(moved.root, moved.path) != moved.sha256:
            raise KnowledgeError("source changed during move")
        _unlink(moved.root, moved.path, moved.sha256)
    except Exception as exc:
        rollback_ok = _rollback(applied, target_root, destination, destination_hash)
        if not rollback_ok:
            raise KnowledgeError(
                f"move interrupted; recover originals from {recovery}, preserving concurrent edits"
            ) from exc
        _remove_recovery(recovery)
        raise
    _remove_recovery(recovery)
    return {
        "ok": True,
        "id": moved.id,
        "scope": target_root.scope,
        "path": destination,
        "links_updated": len(applied),
    }


def _rollback(
    applied: list[tuple[Note, str, str]], root: Root, destination: str, destination_hash: str | None
) -> bool:
    ok = True
    for note, raw, new in reversed(applied):
        try:
            storage.publish(
                note.root, note.path, raw, expected_hash=hashlib.sha256(new.encode()).hexdigest()
            )
        except OSError, KnowledgeError:
            ok = False
    if destination_hash:
        try:
            if _hash(root, destination) != destination_hash:
                return False
            _unlink(root, destination, destination_hash)
        except OSError, KnowledgeError:
            ok = False
    return ok


def _remove_recovery(path: Path) -> None:
    for child in path.iterdir():
        child.unlink()
    path.rmdir()
