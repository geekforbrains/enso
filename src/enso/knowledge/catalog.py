"""Incrementally read Markdown roots and resolve their paths, identities, and links."""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from .. import frontmatter
from ..config import Paths
from ..note_storage import (
    EXCLUDED_DIRS,
    MAX_NOTE_BYTES,
    Root,
    read_bytes,
    safe_path,
    split_document,
    valid_id,
    valid_timestamp,
)
from ..note_storage import (
    NoteError as KnowledgeError,
)
from ..note_storage import (
    discover_roots as note_roots,
)
from .links import Link, extract_links, heading_ids, slug_heading

SCHEMA = "enso.note/v1"
CORE_FIELDS = {"schema", "id", "created", "updated"}


@dataclass(frozen=True)
class Note:
    """A readable note; malformed metadata stays visible with explicit problems."""

    root: Root
    path: str
    title: str
    id: str | None
    metadata: dict[str, Any]
    body: str
    sha256: str
    problems: tuple[str, ...]
    mtime: float
    links: tuple[Link, ...] = ()

    @property
    def scope(self) -> str:
        return self.root.scope


@dataclass(frozen=True)
class Resolution:
    """The deterministic destination of one link, with ambiguity left unresolved."""

    status: str
    note: Note | None = None
    asset: Path | None = None
    fragment: str = ""
    candidates: tuple[Note, ...] = ()


def metadata_problems(fields: dict[str, Any]) -> tuple[str, ...]:
    problems: list[str] = []
    if fields.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    if not valid_id(fields.get("id")):
        problems.append("id must be a UUID")
    for key in ("created", "updated"):
        if key in fields and not valid_timestamp(fields[key]):
            problems.append(f"{key} must be an ISO timestamp with a timezone")
    if set(fields) - CORE_FIELDS:
        problems.append("frontmatter supports only schema, id, created, and updated")
    created, updated = (valid_timestamp(fields.get(key)) for key in ("created", "updated"))
    if created and updated and datetime.fromisoformat(created) > datetime.fromisoformat(updated):
        problems.append("updated must not be earlier than created")
    return tuple(problems)


def discover_roots(paths: Paths) -> tuple[Root, ...]:
    """Shared knowledge and visible workspace knowledge, discovered without config loading."""
    return note_roots(paths, "knowledge", shared=True)[0]


@lru_cache(maxsize=16384)
def _read_note(root: Root, relative: str, signature: tuple[int, ...]) -> Note:
    data = read_bytes(root, relative, limit=MAX_NOTE_BYTES)
    text = data.decode("utf-8")
    document, problem = frontmatter.parse(text)
    original = dict(document.fields) if document else {}
    fields = {
        key: value
        for key, value in original.items()
        if key in CORE_FIELDS and isinstance(value, str | datetime)
    }
    for key in ("created", "updated"):
        if value := valid_timestamp(fields.get(key)):
            fields[key] = value
    problems = (problem,) if problem else metadata_problems(original)
    body = (split_document(text)[1] if document else text).strip("\r\n")
    return Note(
        root,
        relative,
        PurePosixPath(relative).stem,
        valid_id(fields.get("id")),
        fields,
        body,
        hashlib.sha256(data).hexdigest(),
        problems,
        signature[2] / 1_000_000_000,
        extract_links(body),
    )


def scan(paths: Paths) -> Catalog:
    """Stat all files, reuse unchanged parsed notes, and report independent read problems."""
    roots, problems = note_roots(paths, "knowledge", shared=True)
    notes, read_problems, assets = scan_roots(roots, _read_note)
    return Catalog(roots, notes, problems + read_problems, assets)


def scan_roots(
    roots: tuple[Root, ...], reader: Callable[[Root, str, tuple[int, ...]], Note]
) -> tuple[tuple[Note, ...], tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Discover portable notes/assets with each format's own parser and metadata rules."""
    notes: list[Note] = []
    assets: dict[str, tuple[str, ...]] = {}
    problems: list[str] = []
    for root in roots:
        root_assets: list[str] = []
        if not root.path.exists():
            assets[root.scope] = ()
            continue

        def onerror(error: OSError, scope: str = root.scope) -> None:
            problems.append(f"{scope}: {error.filename}: cannot read directory ({error.strerror})")

        for directory, dirs, files in os.walk(
            root.path,
            followlinks=False,
            onerror=onerror,
        ):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in EXCLUDED_DIRS)
            dirs[:] = [
                d
                for d in dirs
                if not (root.path / os.path.relpath(directory, root.path) / d).is_symlink()
            ]
            for name in sorted(files):
                if name.startswith("."):
                    continue
                path = root.path / os.path.relpath(directory, root.path) / name
                relative = path.relative_to(root.path).as_posix()
                if path.is_symlink():
                    problems.append(f"{root.scope}: {path}: symbolic link excluded")
                    continue
                if path.suffix.lower() != ".md":
                    root_assets.append(relative)
                    continue
                try:
                    info = path.stat()
                    signature = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    notes.append(reader(root, relative, signature))
                except (OSError, UnicodeError, KnowledgeError) as exc:
                    problems.append(
                        f"{root.scope}: {path}: cannot read note ({type(exc).__name__})"
                    )
        assets[root.scope] = tuple(root_assets)
    return tuple(notes), tuple(problems), assets


@dataclass(frozen=True)
class Catalog:
    """One view of discovered roots; all lookups use this consistent filesystem snapshot."""

    roots: tuple[Root, ...]
    notes: tuple[Note, ...]
    problems: tuple[str, ...]
    assets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _ids: dict[str, list[Note]] = field(default_factory=dict, init=False, repr=False)
    _paths: dict[tuple[str, str], list[Note]] = field(default_factory=dict, init=False, repr=False)
    _names: dict[tuple[str, str], list[Note]] = field(default_factory=dict, init=False, repr=False)
    _suffixes: dict[tuple[str, str], list[Note]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        for note in self.notes:
            if note.id:
                self._ids.setdefault(note.id, []).append(note)
            self._paths.setdefault((note.scope, note.path.casefold()), []).append(note)
            self._names.setdefault((note.scope, note.title.casefold()), []).append(note)
            parts = PurePosixPath(note.path).parts
            for index in range(1, len(parts) - 1):
                suffix = "/".join(parts[index:]).casefold()
                self._suffixes.setdefault((note.scope, suffix), []).append(note)

    def root(self, scope: str) -> Root:
        for root in self.roots:
            if root.scope == scope:
                return root
        raise KnowledgeError(f"unknown note root: {scope}")

    def by_id(self, note_id: str) -> Note | None:
        candidates = self._ids.get(valid_id(note_id) or "", [])
        if len(candidates) > 1:
            raise KnowledgeError("duplicate note id; repair the duplicate metadata first")
        return candidates[0] if candidates else None

    def require_unique(self, note: Note) -> None:
        """Refuse managed writes to an identity or path with more than one owner."""
        if note.id:
            self.by_id(note.id)
        if len(self._paths[(note.scope, note.path.casefold())]) > 1:
            raise KnowledgeError("note path is ambiguous")

    def get(self, ref: str, scope: str = "general") -> Note:
        """Address a note by UUID or exact path within the selected scope."""
        if valid_id(ref):
            note = self.by_id(ref)
            if note:
                if note.scope != scope:
                    raise KnowledgeError(
                        f"note belongs to {note.scope}; select that knowledge root explicitly"
                    )
                return note
        path = ref if ref.lower().endswith(".md") else f"{ref}.md"
        safe_path(self.root(scope), path)
        candidates = self._paths.get((scope, path.casefold()), [])
        if len(candidates) == 1:
            return candidates[0]
        raise KnowledgeError("note path is ambiguous" if candidates else "note does not exist")

    def resolve(self, source: Note, target: str, *, wiki: bool = False) -> Resolution:
        """Resolve local names without guessing across roots or duplicate filenames."""
        target = re.sub(r"\\([\\()\[\] ])", r"\1", target.strip())
        scope, target, explicit = _scope_target(source.scope, target)
        if any(ord(char) < 32 for char in target):
            return Resolution("missing")
        if not explicit:
            try:
                scheme = urlsplit(target).scheme.lower()
            except ValueError:
                return Resolution("missing")
            if scheme or target.startswith("//"):
                allowed = scheme in {"http", "https", "mailto"} or target.startswith("//")
                return Resolution("external" if allowed else "missing")
        path, _, fragment = target.partition("#")
        path = unquote(path)
        fragment = slug_heading(unquote(fragment))
        if not path:
            return Resolution("note", note=source, fragment=fragment)
        try:
            root = self.root(scope)
            candidates = self._note_candidates(source, scope, path, wiki=wiki, explicit=explicit)
            if len(candidates) == 1:
                return Resolution("note", note=candidates[0], fragment=fragment)
            if candidates:
                return Resolution("ambiguous", fragment=fragment, candidates=tuple(candidates))
            asset_names = _target_paths(source, path, wiki=wiki, explicit=explicit)
            if wiki and "/" not in path:
                asset_names = [
                    p
                    for p in self.assets.get(scope, ())
                    if PurePosixPath(p).name.casefold() == path.casefold()
                ]
            available = [
                name for name in dict.fromkeys(asset_names) if name in self.assets.get(scope, ())
            ]
            if len(available) == 1:
                return Resolution("asset", asset=safe_path(root, available[0]), fragment=fragment)
            if len(available) > 1:
                return Resolution("ambiguous", fragment=fragment)
        except KnowledgeError, ValueError:
            pass
        return Resolution("missing", fragment=fragment)

    def _note_candidates(
        self, source: Note, scope: str, path: str, *, wiki: bool, explicit: bool
    ) -> list[Note]:
        if wiki and "/" not in path and not explicit:
            name = path[:-3] if path.lower().endswith(".md") else path
            return self._names.get((scope, name.casefold()), [])
        found: dict[str, Note] = {}
        for relative in _target_paths(source, path, wiki=wiki, explicit=explicit):
            safe_path(self.root(scope), relative)
            for name in (relative, f"{relative}.md"):
                for note in self._paths.get((scope, name.casefold()), []):
                    found[note.path] = note
        if not found and wiki and not explicit:
            suffix = posixpath.normpath(path)
            for name in (suffix, f"{suffix}.md"):
                for note in self._suffixes.get((scope, name.casefold()), []):
                    found[note.path] = note
        return list(found.values())

    def backlinks(self, target: Note) -> tuple[Note, ...]:
        return tuple(
            note
            for note in self.notes
            if any(
                self.resolve(note, link.target, wiki=link.wiki).note == target
                for link in note.links
            )
        )

    def audit(self, scope: str | None = None) -> list[dict[str, str]]:
        """Collect metadata, identity, missing-target and missing-heading problems."""
        problems = [
            {"scope": "", "path": "", "problem": p}
            for p in self.problems
            if not scope or p.startswith(f"{scope}:")
        ]
        for note in self.notes:
            if scope and note.scope != scope:
                continue
            found = list(note.problems)
            if note.id and len(self._ids[note.id]) > 1:
                found.append("duplicate note id")
            if len(self._paths[(note.scope, note.path.casefold())]) > 1:
                found.append("case-insensitive note path collision")
            for link in note.links:
                result = self.resolve(note, link.target, wiki=link.wiki)
                if result.status in {"missing", "ambiguous"}:
                    found.append(f"{result.status} link: {link.target!r}")
                elif (
                    result.note
                    and result.fragment
                    and result.fragment not in heading_ids(result.note.body)
                ):
                    found.append(f"missing heading: {link.target!r}")
            problems.extend(
                {"scope": note.scope, "path": note.path, "problem": p} for p in dict.fromkeys(found)
            )
        return problems


def _scope_target(default: str, target: str) -> tuple[str, str, bool]:
    if target.startswith("general:"):
        return "general", target.removeprefix("general:"), True
    match = re.match(r"workspace:([^:]+):(.*)", target, re.S)
    if match:
        return f"workspace:{match[1]}", match[2], True
    return default, target, False


def _target_paths(source: Note, path: str, *, wiki: bool, explicit: bool) -> list[str]:
    if path.startswith("/") or "\\" in path:
        raise KnowledgeError("absolute links are not knowledge paths")
    parent = PurePosixPath(source.path).parent.as_posix()
    candidates = [path] if explicit else [posixpath.join(parent, path)]
    if wiki and not explicit:
        candidates.append(path)
    return list(dict.fromkeys(posixpath.normpath(p) for p in candidates))
