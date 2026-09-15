"""Memory navigation, scoped timeline, source attribution, and read-only safety."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_web_navigation import Document

from enso import db, memory
from enso.config import Paths
from enso.web import memory as memoryviews
from enso.web import server


@pytest.fixture
async def client(enso_home: Paths, raw_config: dict) -> AsyncIterator[TestClient]:
    raw_config["memory"] = {"timezone": "America/Vancouver"}
    enso_home.config.write_text(json.dumps(raw_config))
    async with TestClient(TestServer(server.create_app(enso_home))) as current:
        yield current


def seed(
    paths: Paths,
    summary: str,
    *,
    received_at: str = "2026-09-15T01:00:00+00:00",
    workspace: str = "personal",
    transport: str = "slack",
    channel: str = "C1",
    channel_name: str = "planning",
    request: str = "Keep the weekend free.",
    response: str = "I left the weekend free.",
):
    db.migrate(paths)
    turn_id = memory.start_turn(
        paths,
        conversation=f"{transport}:{channel}",
        workspace=workspace,
        provider="claude",
        model="sonnet",
        effort="high",
        transport=transport,
        channel=channel,
        channel_name=channel_name,
        thread="thread-1",
        message_id="",
        user_id="U1",
        user_name="Gavin",
        request=request,
        files=("itinerary.txt",),
        received_at=received_at,
    )
    memory.finish_turn(paths, turn_id, response=response, status="ok", session_id="session-1")
    batch = f"web-{turn_id}"
    memory.prepare_batch(paths, batch_id=batch)
    return memory.record_batch(paths, batch, [{"summary": summary, "source_ids": [turn_id]}])[0]


def rows(html: str):
    return Document(html).root.find("a", "memory-row")


async def test_timeline_filtering_and_pagination_preserve_query(client, enso_home):
    first = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(53):
        seed(
            enso_home,
            f"Weekend decision {index}",
            received_at=(first + timedelta(hours=index)).isoformat(),
        )
    seed(enso_home, "Work deployment", workspace="work", transport="telegram", channel="123")
    response = await client.get("/memory?workspace=personal&q=Weekend&transport=slack")
    html = await response.text()
    assert response.status == 200
    assert len(rows(html)) == 50 and "Showing 1\u201350 of 53" in html
    assert "Weekend decision 52" in rows(html)[0].text
    document = Document(html).root
    pager = document.find("nav", "pager")[0]
    older = next(link for link in pager.find("a") if "Older" in link.text)
    assert "workspace=personal" in older.attrs["href"] and "q=Weekend" in older.attrs["href"]
    response = await client.get(older.attrs["href"])
    html = await response.text()
    assert len(rows(html)) == 3 and "Showing 51\u201353 of 53" in html
    response = await client.get("/memory?workspace=personal&page=999999")
    assert "Showing 51\u201353 of 53" in await response.text()
    response = await client.get("/memory?transport=telegram&channel=123")
    assert len(rows(await response.text())) == 1
    response = await client.get("/memory?since=2026-09-02&until=2026-09-03&workspace=personal")
    assert len(rows(await response.text())) == 22
    response = await client.get("/memory?workspace=absent")
    html = await response.text()
    assert "No memories match these filters." in html
    assert '<option value="absent" selected>' in html and 'href="/memory">Reset' in html


async def test_event_timezone_source_context_and_safe_rendering(client, enso_home):
    entry = seed(
        enso_home,
        "Gavin **kept the weekend free**. <script>unsafe()</script>",
        request="Keep Saturday free. ![remote](https://example.test/track.png)",
        response="Agreed.\n\n[bad](javascript:alert(1)) <img src=x onerror=alert(1)>",
    )
    response = await client.get("/memory")
    html = await response.text()
    assert "Monday, September 14, 2026" in html and ">18:00</time>" in html
    assert "America/Vancouver" in html and entry.ref in html
    assert '<option value="C1">planning (C1)</option>' in html
    response = await client.get(f"/memory/{entry.ref}")
    assert response.status == 200
    html = await response.text()
    document = Document(html).root
    main = document.find("main")[0]
    assert "2026-09-14 18:00:00 PDT" in main.text
    assert "Memory created" in main.text and "Event started" in main.text
    assert "planning" in main.text and "C1" in main.text and "Gavin" in main.text
    assert "thread-1" in main.text and "session-1" in main.text and "itinerary.txt" in main.text
    assert "kept the weekend free" in main.text.lower()
    assert not main.find("script") and not main.find("img")
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert not any("javascript:" in link.attrs.get("href", "") for link in main.find("a"))
    assert document.find("details", "memory-source")
    assert all(
        link.attrs["href"] == "/memory"
        for link in document.find("a")
        if link.attrs.get("aria-current") == "page"
    )


async def test_long_exchange_preview_is_bounded_and_explains_full_lookup(client, enso_home):
    entry = seed(enso_home, "A long conversation", request="x" * 20_000)
    response = await client.get(f"/memory/{entry.ref}")
    html = await response.text()
    assert "x" * memoryviews.SOURCE_PREVIEW in html
    assert "x" * (memoryviews.SOURCE_PREVIEW + 1) not in html
    assert "Request shortened for this view" in html
    assert f"enso memory show {entry.ref} --sources" in html


async def test_missing_database_older_schema_and_mutations_are_read_only(client, enso_home):
    response = await client.get("/memory")
    assert response.status == 200 and "No memories yet" in await response.text()
    assert not enso_home.db.exists()
    response = await client.get("/memory/MEM-999999")
    assert response.status == 404 and not enso_home.db.exists()
    for method in ("POST", "HEAD", "DELETE", "PUT"):
        response = await client.request(method, "/memory")
        assert response.status == 405 and response.headers["Allow"] == "GET"
    db.migrate(enso_home)
    with db.transaction(enso_home) as connection:
        connection.execute("DROP TABLE _enso_memory_sources")
        connection.execute("DROP TABLE _enso_memories")
        connection.execute("DROP TABLE _enso_memory_batches")
        connection.execute("DROP TABLE _enso_memory_turns")
        connection.execute("PRAGMA user_version = 6")
    before = enso_home.db.read_bytes()
    response = await client.get("/memory")
    assert response.status == 200
    assert enso_home.db.read_bytes() == before
    with db.reader(enso_home) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


async def test_invalid_filter_and_unreadable_source_have_visible_errors(
    client, enso_home, monkeypatch
):
    response = await client.get("/memory?since=invalid")
    assert response.status == 200 and "Memory could not be read" in await response.text()
    entry = seed(enso_home, "A saved memory")

    def unavailable(*args, **kwargs):
        raise memory.MemoryError("Saved exchanges could not be read")

    monkeypatch.setattr(memory, "sources", unavailable)
    response = await client.get(f"/memory/{entry.ref}")
    html = await response.text()
    assert response.status == 200 and "A saved memory" in html
    assert "Saved exchanges could not be read" in html
