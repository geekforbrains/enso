"""Workspace Markdown history: explicit occurrence, preserved provenance, and safe corrections."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import replace
from datetime import date, datetime
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

import yaml

from . import captures, frontmatter
from . import note_storage as storage
from .config import Paths, require_workspace, valid_workspace_name
from .knowledge.catalog import Catalog as NoteCatalog
from .knowledge.catalog import Note, Resolution, scan_roots
from .knowledge.links import extract_links
from .note_storage import NoteError, Root, valid_id, valid_timestamp

SCHEMA = "enso.memory/v1"
CORE_FIELDS = {"schema", "id", "occurred", "sources", "created", "updated"}


def occurrence(value: Any) -> str | None:
    """Normalize a known instant or exact calendar date; never infer missing precision."""
    if value is None:
        return None
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                pass
        elif instant := valid_timestamp(value):
            return instant
    raise NoteError("occurred must be a quoted date, a quoted timestamp with timezone, or null")


def date_folder(occurred: str | None) -> str:
    """UTC dates name folders for instants; date-only and unknown values retain precision."""
    return occurred[:10].replace("-", "/") if occurred is not None else "undated"


def _valid_sources(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(type(source) is int and source > 0 for source in value)
        and len(set(value)) == len(value)
    )


def metadata_problems(fields: dict[str, Any]) -> tuple[str, ...]:
    problems: list[str] = []
    if fields.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    if not valid_id(fields.get("id")):
        problems.append("id must be a UUID")
    if "occurred" not in fields:
        problems.append("occurred is required; use null when unknown")
    else:
        try:
            occurrence(fields["occurred"])
        except NoteError as exc:
            problems.append(str(exc))
    sources = fields.get("sources")
    if not _valid_sources(sources):
        problems.append("sources must be a list of distinct positive integer capture IDs")
    if set(fields) - CORE_FIELDS:
        problems.append(
            "frontmatter supports only schema, id, occurred, sources, created, and updated"
        )
    for key in ("created", "updated"):
        if key in fields and not valid_timestamp(fields[key]):
            problems.append(f"{key} must be an ISO timestamp with a timezone")
    created, updated = (valid_timestamp(fields.get(key)) for key in ("created", "updated"))
    if created and updated and datetime.fromisoformat(created) > datetime.fromisoformat(updated):
        problems.append("updated must not be earlier than created")
    return tuple(problems)


def _placement(relative: str, occurred: str | None) -> str | None:
    folder = date_folder(occurred)
    if PurePosixPath(relative).parent.as_posix() != folder:
        return f"note must be directly under {folder}/ for its occurrence"
    return None


@lru_cache(maxsize=16384)
def _read_note(root: Root, relative: str, signature: tuple[int, ...]) -> Note:
    data = storage.read_bytes(root, relative, limit=storage.MAX_NOTE_BYTES)
    text = data.decode("utf-8")
    document, problem = frontmatter.parse(text)
    original = document.fields if document else {}
    problems = list((problem,) if problem else metadata_problems(original))
    # Expose only supported, serializable properties. The original file remains untouched.
    fields: dict[str, Any] = {
        key: value
        for key, value in original.items()
        if key in CORE_FIELDS and isinstance(value, str)
    }
    if original.get("occurred") is None and "occurred" in original:
        fields["occurred"] = None
    sources = original.get("sources")
    if isinstance(sources, list) and all(type(source) is int for source in sources):
        fields["sources"] = sources
    for key in ("created", "updated"):
        if stamp := valid_timestamp(original.get(key)):
            fields[key] = stamp
    if "occurred" in original:
        try:
            fields["occurred"] = occurrence(original["occurred"])
            if placement := _placement(relative, fields["occurred"]):
                problems.append(placement)
        except NoteError:
            # The metadata finding already explains the unusable occurrence.
            fields.pop("occurred", None)
    body = (storage.split_document(text)[1] if document else text).strip("\r\n")
    return Note(
        root,
        relative,
        PurePosixPath(relative).stem,
        valid_id(original.get("id")),
        fields,
        body,
        hashlib.sha256(data).hexdigest(),
        tuple(problems),
        signature[2] / 1_000_000_000,
        extract_links(body),
    )


class Catalog(NoteCatalog):
    """Reuse note identity and auditing, with memory's strictly relative link contract."""

    def resolve(self, source: Note, target: str, *, wiki: bool = False) -> Resolution:
        target = target.strip()
        if wiki or target.startswith(("general:", "workspace:")):
            return Resolution("missing")
        return super().resolve(source, target)

    def get(self, ref: str, scope: str = "general") -> Note:
        if not valid_id(ref) and not ref.lower().endswith(".md"):
            raise NoteError("use a UUID or an exact .md path relative to the memory root")
        return super().get(ref, scope)


def scan(paths: Paths, workspace: str) -> Catalog:
    """Discover memory from files, including other workspaces for duplicate identity checks."""
    paths = Paths(paths.home.resolve())
    require_workspace(paths, workspace)
    roots: list[Root] = []
    problems: list[str] = []
    for directory in sorted(paths.workspaces.iterdir()):
        if (
            not valid_workspace_name(directory.name)
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            continue
        root = Root(
            f"workspace:{directory.name}", directory.name, paths.workspace_memory(directory.name)
        )
        if root.path.is_symlink() or (root.path.exists() and not root.path.is_dir()):
            problems.append(f"{root.scope}: memory root must be a real directory")
        else:
            roots.append(root)
    notes, read_problems, assets = scan_roots(tuple(roots), _read_note)
    # Database validity is checked outside the file parse cache: a source may arrive later.
    notes = tuple(
        replace(
            note,
            problems=note.problems
            + captures.source_problems(paths, note.root.label, note.metadata["sources"]),
        )
        if note.metadata.get("sources") and _valid_sources(note.metadata["sources"])
        else note
        for note in notes
    )
    return Catalog(tuple(roots), notes, tuple(problems) + read_problems, assets)


def document(fields: dict[str, Any], body: str) -> str:
    """Render validated memory metadata and its Markdown body."""
    if problems := metadata_problems(fields):
        raise NoteError("; ".join(problems))
    # YAML quotes date-like strings. Preserve body indentation and meaningful trailing spaces.
    front = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n\n{body.lstrip(chr(13) + chr(10))}".rstrip("\r\n") + "\n"


def create_note(
    paths: Paths, workspace: str, name: str, body: str, *, occurred: str | None
) -> Note:
    """Create one manual memory in its event-date folder; no event time is invented."""
    storage.body_only(body)
    if len(storage.relative_parts(name)) != 1 or not name.lower().endswith(".md"):
        raise NoteError("give one .md filename; the occurrence determines its folder")
    occurred = occurrence(occurred)
    relative = f"{date_folder(occurred)}/{name}"
    scope = f"workspace:{workspace}"
    with storage.writer(paths, "memory"):
        catalog = scan(paths, workspace)
        root = catalog.root(scope)
        if any(
            note.scope == scope and note.path.casefold() == relative.casefold()
            for note in catalog.notes
        ):
            raise NoteError("destination already exists")
        stamp = storage.timestamp()
        fields: dict[str, Any] = {
            "schema": SCHEMA,
            "id": str(uuid4()),
            "occurred": occurred,
            "sources": [],
            "created": stamp,
            "updated": stamp,
        }
        storage.publish(root, relative, document(fields, body), expected_hash=None)
        return scan(paths, workspace).get(relative, scope)


def update_note(
    paths: Paths,
    workspace: str,
    ref: str,
    body: str,
    *,
    expected_hash: str,
    occurred: str | None = None,
) -> Note:
    """Correct content, optionally fixing occurrence after deliberate file relocation.

    Omit occurred to retain it; the explicit string ``unknown`` restores unknown precision.
    Changing the event date never moves files or rewrites historical meaning implicitly.
    """
    storage.body_only(body)
    scope = f"workspace:{workspace}"
    with storage.writer(paths, "memory"):
        catalog = scan(paths, workspace)
        note = catalog.get(ref, scope)
        catalog.require_unique(note)
        if note.sha256 != expected_hash:
            raise NoteError("note changed since it was read; read it again")
        problems = [
            problem
            for problem in note.problems
            if occurred is None or not problem.startswith("note must be directly under ")
        ]
        if problems:
            raise NoteError("cannot update note: " + "; ".join(problems))
        fields = dict(note.metadata)
        if occurred is not None:
            fields["occurred"] = occurrence(None if occurred == "unknown" else occurred)
        if placement := _placement(note.path, fields["occurred"]):
            raise NoteError(
                f"{placement}; relocate it and maintain relative links explicitly first"
            )
        if body.strip("\r\n") == note.body and fields == note.metadata:
            return note
        fields["updated"] = storage.timestamp()
        storage.publish(note.root, note.path, document(fields, body), expected_hash=expected_hash)
        return scan(paths, workspace).get(note.path, scope)


def remove_note(paths: Paths, workspace: str, path: str, *, expected_hash: str) -> None:
    """Remove exactly the reported file revision, preserving captures and processing state."""
    with storage.writer(paths, "memory"):
        catalog = scan(paths, workspace)
        note = catalog.get(path, f"workspace:{workspace}")
        catalog.require_unique(note)
        if note.sha256 != expected_hash:
            raise NoteError("note changed since the removal report; read it again")
        if captures.pending_note(paths, workspace, note.id, note.path):
            raise NoteError(
                "note creation is still being recorded; run enso memory batch to recover first"
            )
        directory, name = storage.open_parent(note.root, note.path)
        try:
            if storage.hash_at(directory, name) != expected_hash:
                raise NoteError("note changed during removal; read it again")
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)
