"""Memory viewer keeps file truth, receipt truth, and workspace ownership separate."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_web_navigation import Document

from enso import captures, db, memory
from enso.config import Paths
from enso.web import server


@pytest.fixture
async def client(enso_home: Paths) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(server.create_app(enso_home))) as current:
        yield current


def record(
    paths: Paths, ident: str, *, transport: str = "slack", workspace: str = "default", **values
):
    channel = "C1" if transport == "slack" else "123"
    return captures.record(
        paths,
        captures.Message(
            transport,
            workspace,
            f"{transport}:{channel}",
            channel,
            "thread-1" if transport == "slack" else None,
            ident,
            "sender",
            "<script>alert(1)</script>",
            "2026-09-16T12:00:00Z",
            values.get("text", "Stored <b>evidence</b>"),
            values.get("kind", "ambient"),
            values.get("attachments", ()),
        ),
    )[0]


def sourced_note(
    paths: Paths, capture_id: int, *, title: str = "Remembered"
) -> tuple[Path, str, str]:
    ident = str(uuid4())
    fields = {
        "schema": memory.SCHEMA,
        "id": ident,
        "occurred": "2026-09-16T12:00:00Z",
        "sources": [capture_id],
        "created": "2026-09-16T12:01:00Z",
        "updated": "2026-09-16T12:01:00Z",
    }
    body = "Remembered [safe](https://example.com) and <script>alert(1)</script>."
    text = memory.document(fields, body)
    path = paths.workspace_memory("default") / "2026/09/16" / f"{title}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path, ident, text


async def test_empty_missing_job_and_get_only(client: TestClient, enso_home: Paths):
    response = await client.get("/memory")
    html = await response.text()
    assert response.status == 200 and "Not installed" in html
    shown = " ".join(Document(html).root.text.split())
    assert "0 captures" in shown and "0 memories" in shown
    assert "Memories" in html and "Captures" in html
    assert response.headers["Cache-Control"] == "no-store"
    assert (await client.get("/memory?view=captures")).status == 200
    assert not enso_home.db.exists()
    job = enso_home.workspace_jobs("default") / "memory" / "JOB.md"
    job.parent.mkdir(parents=True)
    job.write_text(
        '---\nname: Memory\nschedule: "*/15 * * * *"\nprovider: claude\n'
        "model: sonnet\neffort: low\nenabled: true\n---\n\nRemember events.\n"
    )
    assert "Installed and enabled" in await (await client.get("/memory")).text()
    job.write_text(job.read_text().replace("enabled: true", "enabled: false"))
    assert "Installed but disabled" in await (await client.get("/memory")).text()
    denied = await client.post("/memory")
    assert denied.status == 405 and denied.headers["Allow"] == "GET"


async def test_receipts_links_removed_memory_and_workspace_boundaries(
    client: TestClient, enso_home: Paths
):
    enso_home.workspace("other").mkdir()
    db.initialize(enso_home)
    with_note = record(enso_home, "1")
    no_note = record(enso_home, "2", transport="telegram")
    pending = record(enso_home, "3")
    foreign = record(enso_home, "4", workspace="other")
    unprocessed = record(enso_home, "5")
    path, ident, text = sourced_note(enso_home, with_note.id)
    manual = memory.create_note(enso_home, "default", "Manual.md", "Manual body", occurred=None)
    receipt = captures.prepare_receipt(
        enso_home,
        "default",
        (with_note.id,),
        ({"id": ident, "path": "2026/09/16/Remembered.md", "text": text},),
    )
    captures.complete_receipt(enso_home, "default", receipt.id)
    no_memory = captures.prepare_receipt(enso_home, "default", (no_note.id,), ())
    captures.complete_receipt(enso_home, "default", no_memory.id)
    captures.prepare_receipt(enso_home, "default", (pending.id,), ())

    memories = await client.get("/memory?workspace=default&q=Remembered")
    html = await memories.text()
    shown = " ".join(Document(html).root.text.split())
    assert memories.status == 200 and "1 memory" in shown and "1 unprocessed" in shown
    assert f"/memory/notes/default/{ident}" in html
    note = await client.get(f"/memory/notes/default/{ident}")
    html = await note.text()
    assert f"/memory/captures/default/{with_note.id}" in html
    article = Document(html).root.find("article", "markdown")[0]
    assert "&lt;script&gt;" in html and not article.find("script")
    manual_page = await client.get(f"/memory/notes/default/{manual.id}")
    assert "Manual memory: no capture sources" in await manual_page.text()

    captures_page = await client.get("/memory?workspace=default&view=captures&transport=telegram")
    html = await captures_page.text()
    assert f"/memory/captures/default/{no_note.id}" in html
    assert f"/memory/captures/default/{with_note.id}" not in html
    for source, status in (
        (with_note, "processed with memory"),
        (no_note, "processed with no memory"),
        (pending, "publication pending"),
        (unprocessed, "unprocessed"),
    ):
        detail = await client.get(f"/memory/captures/default/{source.id}")
        body = await detail.text()
        assert detail.status == 200 and status in body
        assert "Stored evidence from live capture" in body
        assert "&lt;b&gt;evidence&lt;/b&gt;" in body and "<b>evidence</b>" not in body
    with_note_page = await client.get(f"/memory/captures/default/{with_note.id}")
    assert f"/memory/notes/default/{ident}" in await with_note_page.text()
    path.unlink()
    removed = await client.get(f"/memory/captures/default/{with_note.id}")
    body = await removed.text()
    assert "processed with memory" in body and "No current memory cites" in body
    assert (await client.get(f"/memory/notes/default/{ident}")).status == 404
    assert (await client.get(f"/memory/captures/default/{foreign.id}")).status == 404
    assert (await client.get(f"/memory/captures/other/{with_note.id}")).status == 404
    assert (await client.get(f"/memory/notes/other/{manual.id}")).status == 404
    assert (await client.get("/memory?workspace=missing")).status == 404


async def test_incomplete_reply_truncation_context_and_pages(client: TestClient, enso_home: Paths):
    db.initialize(enso_home)
    parent = record(
        enso_home,
        "1",
        kind="addressed",
        text="x" * 70_000,
        attachments=(captures.Attachment("F1", "brief.pdf", "application/pdf"),),
    )
    reply = captures.reply(
        enso_home,
        parent.id,
        text="Answer",
        outcome="completed",
        delivery="sending",
        parts=(captures.Part(0, 6, "sending"),),
        final=False,
    )
    captures.recover(enso_home)
    for index in range(2, 54):
        record(enso_home, str(index))
    first = await client.get("/memory?view=captures")
    html = await first.text()
    assert (
        len(
            [
                node
                for node in Document(html).root.find("a", "row")
                if (node.attrs.get("href") or "").startswith("/memory/captures/default/")
            ]
        )
        == 50
    )
    assert "Page 1 of 2" in html
    older = await client.get("/memory?view=captures&page=2")
    assert "Page 2 of 2" in await older.text()
    too_far = await client.get("/memory?view=captures&page=999")
    assert "Page 2 of 2" in await too_far.text()
    detail = await client.get(f"/memory/captures/default/{parent.id}")
    html = await detail.text()
    assert "Truncated at 65,536 UTF-8 bytes" in html
    assert "brief.pdf" in html and "attachment" in html.lower()
    assert f"/memory/captures/default/{reply.id}" in html
    assert "interrupted" in html and "Nearby captured context" in html
    reply_page = await client.get(f"/memory/captures/default/{reply.id}")
    html = await reply_page.text()
    assert "uncertain" in html and f"/memory/captures/default/{parent.id}" in html
