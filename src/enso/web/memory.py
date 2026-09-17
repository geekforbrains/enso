"""Read-only workspace memory and capture audit pages."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from .. import captures, memory, workspaces
from ..config import Config, Paths
from ..jobs import parse_job
from ..knowledge.catalog import Note, slug_heading
from ..note_storage import NoteError, valid_id
from . import common, filters

PAGE_SIZE = 50
TRANSPORTS = ("slack", "telegram")


def browse_url(*, workspace: str, view: str = "memories", **values: str | int) -> str:
    query = urlencode({"workspace": workspace, "view": view, **values})
    return "/memory?" + query


def note_url(note: Note, catalog: memory.Catalog) -> str:
    """Use stable identity when unique; a path keeps invalid or duplicate notes readable."""
    if note.id:
        try:
            if catalog.by_id(note.id) == note:
                return f"/memory/notes/{quote(note.root.label)}/{quote(note.id)}"
        except NoteError:
            pass
    return "/memory/file?" + urlencode({"workspace": note.root.label, "path": note.path})


def capture_url(workspace: str, capture_id: int) -> str:
    return f"/memory/captures/{quote(workspace)}/{capture_id}"


def _page(raw: str, pages: int) -> int:
    value = int(raw) if raw.isdecimal() and len(raw) <= 9 else 1
    return min(max(value, 1), pages)


def _occurred(note: Note) -> datetime:
    value = note.metadata.get("occurred")
    if isinstance(value, str):
        parsed = filters.parse_time(value)
        if parsed:
            return parsed.astimezone(UTC)
    return datetime.min.replace(tzinfo=UTC)


def _job_state(paths: Paths, workspace: str, config: Config | None) -> str:
    path = paths.workspace_jobs(workspace) / "enso-memory" / "JOB.md"
    if not path.exists() and not path.is_symlink():
        return "Not installed"
    job, problems = parse_job(path, config)
    if job is None or problems:
        return "Installed, needs attention"
    return "Installed and enabled" if job.enabled else "Installed but disabled"


def _base(paths: Paths, workspace: str, catalog: memory.Catalog) -> dict[str, Any]:
    config, problems = common.read_config(paths)
    total, unprocessed = captures.audit_counts(paths, workspace) if workspace else (0, 0)
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "workspaces": workspaces.list_workspaces(paths),
        "workspace": workspace,
        "capture_count": total,
        "unprocessed_count": unprocessed,
        "memory_count": sum(note.root.label == workspace for note in catalog.notes),
        "job_state": _job_state(paths, workspace, config) if workspace else "No workspace",
        "catalog_problems": catalog.problems,
    }


def listing_model(paths: Paths, query: Mapping[str, str]) -> dict[str, Any] | None:
    """A bounded page of current Markdown or stored captures in one workspace."""
    names = workspaces.list_workspaces(paths)
    workspace = query.get("workspace") or (
        "default" if "default" in names else names[0] if names else ""
    )
    if workspace and workspace not in names:
        return None
    catalog = memory.scan(paths, workspace or None)
    model = _base(paths, workspace, catalog)
    view = query.get("view", "memories")
    if view not in ("memories", "captures"):
        view = "memories"
    search = query.get("q", "").strip()[:200]
    transport = query.get("transport", "") if view == "captures" else ""
    if transport not in ("", *TRANSPORTS):
        transport = ""
    raw_page = query.get("page", "1")
    rows: list[dict[str, Any]] | tuple[captures.AuditCapture, ...]
    if view == "memories":
        notes = [note for note in catalog.notes if note.root.label == workspace]
        if search:
            needle = search.casefold()
            notes = [
                note
                for note in notes
                if needle in (note.title + " " + note.path + " " + note.body).casefold()
            ]
        notes.sort(key=lambda note: (_occurred(note), note.path), reverse=True)
        total = len(notes)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = _page(raw_page, pages)
        rows = [
            {"note": note, "href": note_url(note, catalog)}
            for note in notes[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        ]
    else:
        # The count and the selected page come from the same read-only API; no full text
        # history is loaded into the page model.
        requested = _page(raw_page, 999_999_999)
        rows, total = captures.audit_page(paths, workspace, transport=transport, page=requested)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(requested, pages)
    values = {"transport": transport} if view == "captures" else {"q": search}
    model.update(
        view=view,
        q=search,
        transport=transport,
        transports=TRANSPORTS,
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        start=(page - 1) * PAGE_SIZE + 1 if rows else 0,
        end=(page - 1) * PAGE_SIZE + len(rows) if rows else 0,
        prev_link=browse_url(workspace=workspace, view=view, **values, page=page - 1)
        if page > 1
        else None,
        next_link=browse_url(workspace=workspace, view=view, **values, page=page + 1)
        if page < pages
        else None,
        tabs=[
            (label, browse_url(workspace=workspace, view=name), None, None)
            for name, label in (("memories", "Memories"), ("captures", "Captures"))
        ],
        current_tab=browse_url(workspace=workspace, view=view),
    )
    return model


def _safe_external(target: str) -> bool:
    parts = urlsplit(target)
    return parts.scheme.lower() in ("http", "https", "mailto") and not any(
        ord(char) < 32 for char in target
    )


def render_note(catalog: memory.Catalog, note: Note) -> Markup:
    """Render Markdown without source HTML, active images, or unsafe link targets."""
    md = MarkdownIt("commonmark", {"html": False, "linkify": False})
    md.enable(["table", "strikethrough"])
    tokens = md.parse(note.body)
    headings: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and index + 1 < len(tokens):
            slug = slug_heading(tokens[index + 1].content)
            count = headings.get(slug, 0)
            headings[slug] = count + 1
            token.attrSet("id", slug + (f"-{count}" if count else ""))
        if not token.children:
            continue
        closing: list[str] = []
        for child in token.children:
            if child.type == "image":
                child.type = "html_inline"
                child.content = str(escape(child.content)) + " (image reference)"
            elif child.type == "link_open":
                target = str(child.attrGet("href") or "")
                result = catalog.resolve(note, target)
                if result.status == "note" and result.note:
                    href = note_url(result.note, catalog)
                    if result.fragment:
                        href += "#" + quote(result.fragment, safe="")
                    child.attrSet("href", href)
                elif result.status == "external" and _safe_external(target):
                    child.attrSet("rel", "noreferrer")
                else:
                    child.tag = "span"
                    child.attrs = {"class": "knowledge-unresolved", "title": "Unresolved link"}
                closing.append(child.tag)
            elif child.type == "link_close" and closing:
                child.tag = closing.pop()
    return Markup(md.renderer.render(tokens, md.options, {}))


def note_model(
    paths: Paths, workspace: str, *, note_id: str = "", path: str = ""
) -> dict[str, Any] | None:
    if workspace not in workspaces.list_workspaces(paths):
        return None
    catalog = memory.scan(paths, workspace)
    scope = f"workspace:{workspace}"
    try:
        if note_id:
            note = catalog.by_id(note_id) if valid_id(note_id) else None
            if note is None or note.scope != scope:
                return None
        else:
            note = catalog.get(path, scope)
    except NoteError:
        return None
    model = _base(paths, workspace, catalog)
    sources = note.metadata.get("sources", [])
    model.update(
        note=note,
        html=render_note(catalog, note),
        sources=[
            {
                "id": source,
                "capture": captures.audit_get(paths, workspace, source),
                "href": capture_url(workspace, source),
            }
            for source in sources
            if type(source) is int and source > 0
        ],
    )
    return model


def capture_model(paths: Paths, workspace: str, capture_id: int) -> dict[str, Any] | None:
    if workspace not in workspaces.list_workspaces(paths):
        return None
    audited = captures.audit_get(paths, workspace, capture_id)
    if audited is None:
        return None
    catalog = memory.scan(paths, workspace)
    capture = audited.capture
    linked = [
        note
        for note in catalog.notes
        if note.root.label == workspace and capture_id in note.metadata.get("sources", [])
    ]
    parent = captures.audit_get(paths, workspace, capture.parent_id) if capture.parent_id else None
    reply = (
        captures.audit_reply(paths, workspace, capture.id) if capture.kind == "addressed" else None
    )
    model = _base(paths, workspace, catalog)
    model.update(
        audited=audited,
        linked_notes=[
            {"note": note, "href": note_url(note, catalog)} for note in linked[:PAGE_SIZE]
        ],
        linked_count=len(linked),
        parent=parent,
        reply=reply,
        context=captures.audit_context(paths, workspace, capture.conversation, capture_id),
    )
    return model
