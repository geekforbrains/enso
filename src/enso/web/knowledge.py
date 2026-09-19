"""Read-only knowledge page models and safe, scope-aware Markdown presentation.

Catalog discovery and link identity belong to ``enso.knowledge``. This module translates
that shared model into bounded folder/search pages, ordinary browser links and local assets.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token
from markupsafe import Markup, escape

from .. import knowledge as kb
from ..config import Paths, WebConfig
from . import common, files, filters

SIDEBAR_SIZE = 20
BACKLINK_SIZE = 50
ASSET_LIMIT = 20 * 1024 * 1024
# Only passive raster formats render inline; SVG/HTML and everything else download.
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
}
VIEWS = ("browse", "all")
SHARED = "shared"


def browse_url(**values: str | int) -> str:
    """A knowledge location using query parameters so spaces and Unicode stay intact."""
    query = urlencode({key: value for key, value in values.items() if value != ""})
    return "/knowledge" + ("?" + query if query else "")


def folder_url(scope: str, folder: str = "") -> str:
    """Shared's top level is the Knowledge home itself, not a separate root page."""
    if scope == SHARED and not folder:
        return browse_url()
    return browse_url(scope=scope, folder=folder)


def note_url(note: kb.Note, catalog: kb.Catalog | None = None) -> str:
    """Prefer permanent identity; legacy or duplicate identities use their explicit path."""
    unique = bool(note.id)
    if catalog and note.id:
        try:
            catalog.by_id(note.id)
        except kb.KnowledgeError:
            unique = False
    if unique and note.id:
        return "/knowledge/notes/" + quote(note.id, safe="")
    return "/knowledge/file?" + urlencode({"scope": note.scope, "path": note.path})


def asset_url(root: kb.Root, path: str) -> str:
    """The GET endpoint revalidates this visible root-relative attachment path."""
    return "/knowledge/asset?" + urlencode({"scope": root.scope, "path": path})


def _root(catalog: kb.Catalog, scope: str) -> kb.Root | None:
    return next((root for root in catalog.roots if root.scope == scope), None)


def _updated(note: kb.Note) -> datetime:
    return _stamp(note.metadata.get("updated")) or datetime.fromtimestamp(note.mtime, UTC)


def _stamp(value: Any) -> datetime | None:
    return filters.parse_time(value) if isinstance(value, (str, datetime)) else None


def _recent_first(notes: Iterable[kb.Note]) -> list[kb.Note]:
    """Every note list is newest updated first; equal dates stay in path order."""
    return sorted(sorted(notes, key=lambda note: note.path), key=_updated, reverse=True)


def _note_row(note: kb.Note, query: str = "", *, catalog: kb.Catalog) -> dict[str, Any]:
    excerpt = ""
    if query:
        plain = " ".join(note.body.split())
        match = plain.casefold().find(query.casefold())
        start = max(0, match - 55)
        excerpt = ("…" if start else "") + plain[start : start + 180]
        if len(plain) > start + 180:
            excerpt += "…"
    return {
        "title": note.title,
        "href": note_url(note, catalog),
        "path": note.path,
        "scope": note.root.label,
        "kind": "note",
        "updated": _updated(note),
        "excerpt": excerpt,
        "count": None,
    }


def _workspace_rows(catalog: kb.Catalog) -> list[dict[str, Any]]:
    """Retained workspace roots; shared knowledge is the home listing, not a row here."""
    return [
        {
            "title": root.label,
            "kind": "folder",
            "href": browse_url(scope=root.scope),
            "count": sum(note.scope == root.scope for note in catalog.notes),
        }
        for root in catalog.roots
        if root.scope != SHARED
    ]


def _folder_rows(catalog: kb.Catalog, scope: str, folder: str) -> list[dict[str, Any]]:
    root = _root(catalog, scope)
    if root is None:
        return []
    prefix = folder + "/" if folder else ""
    counts: dict[str, int] = {}
    try:
        directory = kb.safe_path(root, folder) if folder else root.path
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    try:
                        kb.safe_path(root, prefix + entry.name)
                    except kb.KnowledgeError:
                        continue
                    counts[entry.name] = 0
    except OSError:
        pass
    # Derive immediate branches from the catalog, never recursively expand a tree in HTML.
    for note in catalog.notes:
        if note.scope == scope and note.path.startswith(prefix):
            tail = note.path[len(prefix) :]
            if "/" in tail:
                name = tail.split("/", 1)[0]
                counts[name] = counts.get(name, 0) + 1
    return [
        {
            "title": name,
            "kind": "folder",
            "count": count,
            "href": browse_url(scope=scope, folder=prefix + name),
        }
        for name, count in sorted(counts.items(), key=lambda item: item[0].casefold())
    ]


def _matching_folders(notes: Iterable[kb.Note], prefix: str, needle: str) -> list[dict[str, Any]]:
    """Folders below ``prefix`` named like the search, derived from the searched notes' paths."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for note in notes:
        parts = note.path.split("/")[:-1]
        for depth, name in enumerate(parts, 1):
            path = "/".join(parts[:depth])
            if not path.startswith(prefix) or needle not in name.casefold():
                continue
            row = found.setdefault(
                (note.scope, path),
                {
                    "title": name,
                    "kind": "folder",
                    "href": browse_url(scope=note.scope, folder=path),
                    "path": path,
                    "scope": note.root.label,
                    "count": 0,
                },
            )
            row["count"] += 1
    return sorted(found.values(), key=lambda row: (row["title"].casefold(), row["path"]))


def _crumbs(root: kb.Root | None, folder: str) -> list[tuple[str, str]]:
    crumbs = [("Index", browse_url())]
    if root is None:
        return crumbs
    if root.scope != SHARED:
        crumbs.append((root.label, browse_url(scope=root.scope)))
    return crumbs + [
        (label, browse_url(scope=root.scope, folder=path)) for label, path in files.crumbs(folder)
    ]


def listing_model(paths: Paths, query: Mapping[str, str]) -> dict[str, Any] | None:
    """One current page of a folder, recent list, or search; unknown locations are absent."""
    catalog = kb.scan(paths)
    config, problems = common.read_config(paths)
    web = config.web if config is not None else WebConfig()
    scope = query.get("scope") or "all"
    root = _root(catalog, scope)
    if scope != "all" and root is None:
        return None
    folder = query.get("folder", "") if scope != "all" else ""
    try:
        folder = "/".join(files.split_relative(folder))
        if folder and (root is None or not kb.safe_path(root, folder).is_dir()):
            return None
    except files.PathRejectedError, kb.KnowledgeError, OSError:
        return None
    search = query.get("q", "").strip()[:200]
    across = query.get("across") == "1" and bool(search)
    view = query.get("view", "browse")
    if view not in VIEWS:
        view = "browse"
    # The home browses shared knowledge directly; retained workspace roots follow it.
    home = scope == "all" and view == "browse" and not search
    listed = SHARED if home else scope
    prefix = folder + "/" if folder else ""
    notes = [
        note
        for note in catalog.notes
        if across or ((scope == "all" or note.scope == scope) and note.path.startswith(prefix))
    ]
    folders: list[dict[str, Any]] = []
    if search:
        needle = search.casefold()
        if view == "browse":
            folders = _matching_folders(notes, "" if across else prefix, needle)
        notes = [
            note
            for note in notes
            if needle in (note.title + " " + note.path + " " + note.body).casefold()
        ]
    elif view == "browse":
        notes = [
            note for note in notes if note.scope == listed and "/" not in note.path[len(prefix) :]
        ]
        folders = _folder_rows(catalog, listed, folder)
    entries: list[dict[str, Any] | kb.Note] = [*folders, *_recent_first(notes)]
    total = len(entries)
    pages = max(1, math.ceil(total / web.knowledge.page_size))
    raw_page = query.get("page", "1")
    page = min(max(1, int(raw_page)), pages) if raw_page.isdecimal() and len(raw_page) < 10 else 1
    shown = [
        _note_row(entry, search, catalog=catalog) if isinstance(entry, kb.Note) else entry
        for entry in entries[(page - 1) * web.knowledge.page_size : page * web.knowledge.page_size]
    ]
    values = {
        "scope": scope,
        "folder": folder,
        "view": view,
        "q": search,
        "across": "1" if across else "",
    }
    recent = _recent_first(catalog.notes)[: web.knowledge.recent_limit] if home else []
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "catalog_problems": catalog.problems,
        "roots": catalog.roots,
        "scope": scope,
        "scope_label": root.label if root else "All knowledge",
        "folder": folder,
        "crumbs": _crumbs(root, folder),
        "q": search,
        "across": across,
        "view": view,
        "rows": shown,
        "recent": [_note_row(note, catalog=catalog) for note in recent],
        "workspaces": _workspace_rows(catalog) if home else [],
        "total": total,
        "page": page,
        "pages": pages,
        "start": (page - 1) * web.knowledge.page_size + 1 if shown else 0,
        "end": (page - 1) * web.knowledge.page_size + len(shown) if shown else 0,
        "prev_link": browse_url(**values, page=page - 1) if page > 1 else None,
        "next_link": browse_url(**values, page=page + 1) if page < pages else None,
        "tabs": [
            (
                "Browse" if name == "browse" else "All notes",
                browse_url(**{**values, "view": name}),
                None,
                None,
            )
            for name in VIEWS
        ],
        "current_tab": browse_url(**values),
        "clear_url": browse_url(scope=scope, folder=folder, view=view),
    }


def _wiki_rule(state: StateInline, silent: bool) -> bool:
    # Markdown's link-label lookahead uses silent parsing. Leave its brackets literal
    # there, or a wiki reference inside a label would prevent the outer link parsing.
    if silent or state.linkLevel:
        return False
    start = state.pos
    embed = state.src.startswith("![[", start)
    opening = "![[" if embed else "[["
    if not state.src.startswith(opening, start):
        return False
    end = state.src.find("]]", start + len(opening))
    if end < 0 or "\n" in state.src[start:end]:
        return False
    content = state.src[start + len(opening) : end]
    target, separator, label = content.partition("|")
    if separator and target.endswith("\\"):
        target = target[:-1]
    if not target.strip():
        return False
    token = state.push("knowledge_link", "", 0)
    token.meta = {
        "target": target.strip(),
        "label": label if separator else target,
        "embed": embed,
    }
    state.pos = end + 2
    return True


def _unresolved(label: str, status: str, detail: str = "") -> str:
    return (
        f'<span class="knowledge-unresolved" title="{escape(detail or status.title() + " link")}">'
        f"{escape(label)} <small>({escape(status)})</small></span>"
    )


def _asset_reference(catalog: kb.Catalog, resolution: kb.Resolution) -> tuple[kb.Root, str] | None:
    if resolution.asset is None:
        return None
    for root in catalog.roots:
        if resolution.asset.is_relative_to(root.path):
            return root, resolution.asset.relative_to(root.path).as_posix()
    return None


def _external_safe(target: str) -> bool:
    if any(ord(char) < 32 for char in target):
        return False
    try:
        return urlsplit(target).scheme.lower() in ("http", "https", "mailto") or target.startswith(
            "//"
        )
    except ValueError:
        return False


def _markdown_link_safe(url: str) -> bool:
    if url.startswith(("shared:", "workspace:")):
        return True
    try:
        return files._safe_link(url)
    except ValueError:
        return False


def _note_link(catalog: kb.Catalog, result: kb.Resolution) -> tuple[str, bool]:
    assert result.note is not None
    href = note_url(result.note, catalog)
    if result.fragment:
        href += "#" + quote(result.fragment, safe="")
    missing_heading = bool(
        result.fragment and result.fragment not in kb.heading_ids(result.note.body)
    )
    return href, missing_heading


def _link_html(
    catalog: kb.Catalog,
    source: kb.Note,
    target: str,
    label: str,
    *,
    wiki: bool = False,
    embed: bool = False,
) -> str:
    result = catalog.resolve(source, target, wiki=wiki)
    if result.status == "note" and result.note:
        href, missing = _note_link(catalog, result)
        attributes = ' class="knowledge-unresolved" title="Missing heading"' if missing else ""
        suffix = " <small>(missing heading)</small>" if missing else ""
        return f'<a href="{escape(href)}"{attributes}>{escape(label)}{suffix}</a>'
    if result.status == "asset" and (asset := _asset_reference(catalog, result)):
        root, relative = asset
        href = asset_url(root, relative)
        if embed and PurePosixPath(relative).suffix.lower() in IMAGE_TYPES:
            return f'<img src="{escape(href)}" alt="{escape(label)}" loading="lazy">'
        return f'<a href="{escape(href)}">{escape(label)}</a>'
    if result.status == "external" and _external_safe(target):
        if embed:
            return _unresolved(label, "remote image", "Remote images are not loaded")
        return f'<a href="{escape(target)}" rel="noreferrer">{escape(label)}</a>'
    detail = ", ".join(note.root.label + "/" + note.path for note in result.candidates)
    return _unresolved(label, result.status, detail)


def _rewrite_inline(catalog: kb.Catalog, source: kb.Note, tokens: list[Token]) -> None:
    closings: list[str] = []
    for token in tokens:
        if token.type == "knowledge_link":
            token.type = "html_inline"
            token.content = _link_html(catalog, source, **token.meta, wiki=True)
        elif token.type == "image":
            token.type = "html_inline"
            token.content = _link_html(
                catalog, source, str(token.attrGet("src") or ""), token.content, embed=True
            )
        elif token.type == "link_open":
            target = str(token.attrGet("href") or "")
            resolved = catalog.resolve(source, target)
            if resolved.status == "note" and resolved.note:
                href, missing = _note_link(catalog, resolved)
                token.attrSet("href", href)
                if missing:
                    token.attrSet("class", "knowledge-unresolved")
                    token.attrSet("title", "Missing heading")
            elif resolved.status == "asset" and (asset := _asset_reference(catalog, resolved)):
                token.attrSet("href", asset_url(*asset))
            elif resolved.status == "external" and _external_safe(target):
                token.attrSet("rel", "noreferrer")
            else:
                token.tag = "span"
                token.attrs = {
                    "class": "knowledge-unresolved",
                    "title": resolved.status.title() + " link",
                }
            closings.append(token.tag)
        elif token.type == "link_close" and closings:
            token.tag = closings.pop()
            if token.tag == "span":
                token.type = "html_inline"
                token.content = " <small>(unresolved)</small></span>"


def render_note(catalog: kb.Catalog, note: kb.Note) -> Markup:
    """Render authored Markdown with scoped links; source HTML remains escaped text."""
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.validateLink = _markdown_link_safe  # type: ignore[method-assign]
    md.inline.ruler.before("image", "knowledge_link", _wiki_rule)
    tokens = md.parse(note.body)
    headings: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and index + 1 < len(tokens):
            slug = kb.slug_heading(tokens[index + 1].content)
            count = headings.get(slug, 0)
            headings[slug] = count + 1
            token.attrSet("id", slug + (f"-{count}" if count else ""))
        if token.children:
            _rewrite_inline(catalog, note, token.children)
    return Markup(md.renderer.render(tokens, md.options, {}))


def note_model(
    paths: Paths, *, note_id: str = "", scope: str = "", path: str = "", raw: bool = False
) -> dict[str, Any] | None:
    """One note with bounded folder context and backlinks, without mutating its source."""
    catalog = kb.scan(paths)
    if note_id:
        try:
            note = catalog.by_id(note_id)
        except kb.KnowledgeError:
            return None
    else:
        note = next(
            (note for note in catalog.notes if note.scope == scope and note.path == path), None
        )
    if note is None:
        return None
    folder = str(PurePosixPath(note.path).parent)
    folder = "" if folder == "." else folder
    prefix = folder + "/" if folder else ""
    siblings = _folder_rows(catalog, note.scope, folder)
    siblings.extend(
        _note_row(other, catalog=catalog)
        for other in _recent_first(catalog.notes)
        if other.scope == note.scope
        and other.path.startswith(prefix)
        and "/" not in other.path[len(prefix) :]
    )
    linked = _recent_first(other for other in catalog.backlinks(note) if other != note)
    _config, problems = common.read_config(paths)
    try:
        source = (
            kb.read_bytes(note.root, note.path, limit=files.MAX_FILE_PREVIEW_BYTES).decode(
                "utf-8", errors="replace"
            )
            if raw
            else None
        )
    except kb.KnowledgeError, OSError:
        return None
    href = note_url(note, catalog)
    note_problems = note.problems
    if note.id and href.startswith("/knowledge/file?"):
        note_problems += ("Duplicate note id; repair metadata to restore a stable URL.",)
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "note": note,
        "html": render_note(catalog, note) if not raw else None,
        "source": source,
        "raw": raw,
        "note_url": href,
        "note_problems": note_problems,
        "raw_url": href + ("&" if "?" in href else "?") + "raw=1",
        "folder_url": folder_url(note.scope, folder),
        "crumbs": _crumbs(note.root, folder),
        "siblings": siblings[:SIDEBAR_SIZE],
        "sibling_count": len(siblings),
        "backlinks": [_note_row(other, catalog=catalog) for other in linked[:BACKLINK_SIZE]],
        "backlink_count": len(linked),
        "updated": _updated(note),
        "created": _stamp(note.metadata.get("created")),
    }


def read_asset(paths: Paths, scope: str, relative: str) -> tuple[bytes, str, str] | None:
    """A bounded local asset; active or unknown formats always receive a download type."""
    root = next((root for root in kb.discover_roots(paths) if root.scope == scope), None)
    if root is None:
        return None
    try:
        body = kb.read_bytes(root, relative, limit=ASSET_LIMIT)
    except kb.KnowledgeError, OSError:
        return None
    suffix = PurePosixPath(relative).suffix.lower()
    content_type = IMAGE_TYPES.get(suffix, "application/octet-stream")
    # An attachment cannot become a same-origin active document, including mislabeled SVG/HTML.
    if content_type != "application/octet-stream" and not _raster_signature(body, suffix):
        content_type = "application/octet-stream"
    return body, content_type, PurePosixPath(relative).name


def _raster_signature(body: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in (".jpg", ".jpeg"):
        return body.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return body.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return body.startswith(b"RIFF") and body[8:12] == b"WEBP"
    return suffix == ".avif" and body[4:12] in (b"ftypavif", b"ftypavis")
