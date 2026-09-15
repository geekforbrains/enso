"""Knowledge browsing, note navigation and asset safety through the GET-only viewer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_web_navigation import Document

from enso import knowledge as kb
from enso.config import Paths
from enso.web import knowledge, server


@pytest.fixture
async def client(enso_home: Paths) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(server.create_app(enso_home))) as current:
        yield current


def note(
    root: Path, relative: str, body: str = "", *, identity: str | None = None, extra: str = ""
) -> tuple[Path, str]:
    identity = identity or str(uuid4())
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nschema: enso.note/v1\nid: {identity}\n"
        f'updated: "2026-09-15T12:00:00Z"\n{extra}---\n\n{body}'
    )
    return path, identity


def knowledge_rows(html: str):
    return Document(html).root.find("a", "knowledge-row")


async def test_mixed_folders_pagination_and_scoped_search(client, enso_home):
    root = enso_home.knowledge
    note(root, "Overview.md", "shared context")
    note(root, "Mixed/Overview.md", "shared context")
    note(root, "Mixed/Nested/Deep.md", "needle in deep content")
    note(root, "Outside.md", "needle outside the folder")
    (root / "Empty").mkdir()
    for index in range(83):
        note(root, f"Large/Note {index:03d}.md", "content")
    note(enso_home.workspace("autodiscovered") / "knowledge", "Work.md", "needle in workspace")

    response = await client.get("/knowledge")
    assert response.status == 200
    html = await response.text()
    rows = knowledge_rows(html)
    assert [row.text.strip().splitlines()[0].strip() for row in rows][:3] == [
        "Empty",
        "Large",
        "Mixed",
    ]
    assert "autodiscovered" in html and "View source" not in html
    assert "Deep" not in " ".join(row.text for row in rows)

    response = await client.get("/knowledge?scope=general&folder=Mixed")
    rows = knowledge_rows(await response.text())
    assert len(rows) == 2 and "Nested" in rows[0].text and "Overview" in rows[1].text

    first = await client.get("/knowledge?scope=general&folder=Large")
    html = await first.text()
    assert len(knowledge_rows(html)) == 50 and "Showing 1\u201350" in html
    last = await client.get("/knowledge?scope=general&folder=Large&page=999")
    html = await last.text()
    assert (
        len(knowledge_rows(html)) == 33 and "Showing 51\u201383" in html and "Page 2 of 2" in html
    )

    scoped = await client.get("/knowledge?scope=general&folder=Mixed&q=needle")
    assert len(knowledge_rows(await scoped.text())) == 1
    across = await client.get("/knowledge?scope=general&folder=Mixed&q=needle&across=1")
    assert len(knowledge_rows(await across.text())) == 3
    all_notes = await client.get("/knowledge?scope=all&view=all&q=needle")
    assert len(knowledge_rows(await all_notes.text())) == 3


async def test_note_links_anchors_backlinks_and_stable_id_after_move(client, enso_home):
    root = enso_home.knowledge
    target, target_id = note(
        root, "Topics/Target Note.md", "## Details\n\nReadable content.\n\n## Details\n"
    )
    _, work_id = note(enso_home.workspace("work") / "knowledge", "Work.md", "## Heading\n")
    _, source_id = note(
        root,
        "Source.md",
        """[[Target Note]]

[[Topics/Target Note|Friendly label]]

[[Target Note#Details]]

[Markdown link](Topics/Target%20Note.md#Details)

[[workspace:work:Work#Heading|Workspace note]]

[Markdown workspace](workspace:work:Work#Heading)

[[Missing note]]

`[[Never resolve code]]`

```markdown
[[Leave fenced code]]
```

<script>alert('unsafe')</script>
""",
    )
    response = await client.get(f"/knowledge/notes/{source_id}")
    assert response.status == 200
    html = await response.text()
    root_node = Document(html).root
    article = root_node.find("article", "markdown")[0]
    links = article.find("a")
    assert [link.attrs["href"] for link in links] == [
        f"/knowledge/notes/{target_id}",
        f"/knowledge/notes/{target_id}",
        f"/knowledge/notes/{target_id}#details",
        f"/knowledge/notes/{target_id}#details",
        f"/knowledge/notes/{work_id}#heading",
        f"/knowledge/notes/{work_id}#heading",
    ]
    assert not article.find("script") and "&lt;script&gt;" in html
    assert "[[Never resolve code]]" in html and "[[Leave fenced code]]" in html
    assert "Missing note" in html and "(missing)" in html
    assert not any(link.find("a") for link in links)
    assert "enso.note/v1" not in article.text

    response = await client.get(f"/knowledge/notes/{target_id}")
    html = await response.text()
    assert 'id="details"' in html and 'id="details-1"' in html
    assert "Linked from" in html and f'href="/knowledge/notes/{source_id}"' in html
    destination = root / "Renamed.md"
    target.rename(destination)
    response = await client.get(f"/knowledge/notes/{target_id}")
    assert response.status == 200 and "Renamed" in await response.text()


async def test_ambiguity_metadata_and_legacy_source_are_readable(client, enso_home):
    root = enso_home.knowledge
    note(root, "One/Same.md")
    note(root, "Two/Same.md")
    _, identity = note(
        root,
        "Source.md",
        "[[Same]]\n\n[[javascript:alert(1)|Click me]]\n\n[[https://example.com|External]]",
        extra="created: [wrong]\n",
    )
    path, _ = note(root, "Malformed.md")
    path.write_text(
        "---\nschema: enso.note/v1\nid: bad\nupdated: [wrong]\ncreated: 3\n---\n\nStill readable.\n"
    )
    legacy = root / "Legacy.md"
    legacy.write_text("Just Markdown.\n")
    response = await client.get(f"/knowledge/notes/{identity}")
    html = await response.text()
    article = Document(html).root.find("article")[0]
    assert "(ambiguous)" in html and "One/Same.md" in html and "Two/Same.md" in html
    assert [node.attrs["href"] for node in article.find("a")] == ["https://example.com"]
    assert "metadata finding" in html
    for relative in ("Malformed.md", "Legacy.md"):
        url = "/knowledge/file?" + urlencode({"scope": "general", "path": relative})
        response = await client.get(url)
        assert response.status == 200
        html = await response.text()
        assert "&amp;raw=1" in html
        response = await client.get(url + "&raw=1")
        assert response.status == 200 and "Read note" in await response.text()


async def test_local_assets_remote_images_and_unsafe_content(client, enso_home):
    root = enso_home.knowledge
    _, identity = note(
        root,
        "Pictures.md",
        """![[pixel.png]]

![relative](Assets/pixel.png)

![remote](https://example.com/track.png)

![[https://example.com/other.png]]

[Attachment](Assets/document.html)
""",
    )
    (root / "Assets").mkdir()
    image = b"\x89PNG\r\n\x1a\n" + b"fake fixture"
    (root / "Assets/pixel.png").write_bytes(image)
    (root / "Assets/document.html").write_text("<script>alert(1)</script>")
    (root / "Assets/spoof.png").write_text("<html>active</html>")
    response = await client.get(f"/knowledge/notes/{identity}")
    html = await response.text()
    article = Document(html).root.find("article")[0]
    images = article.find("img")
    assert len(images) == 2
    assert all(node.attrs["src"].startswith("/knowledge/asset?") for node in images)
    assert "remote image" in html
    response = await client.get(images[0].attrs["src"])
    assert (
        response.status == 200
        and response.content_type == "image/png"
        and await response.read() == image
    )
    for relative in ("Assets/document.html", "Assets/spoof.png"):
        response = await client.get(
            "/knowledge/asset?" + urlencode({"scope": "general", "path": relative})
        )
        assert response.status == 200 and response.content_type == "application/octet-stream"
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_paths_symlinks_read_only_and_missing_home(client, enso_home, tmp_path):
    empty = await client.get("/knowledge")
    assert empty.status == 200 and not enso_home.knowledge.exists()
    root = enso_home.knowledge
    note(root, "Safe.md")
    outside = tmp_path / "secret.txt"
    outside.write_text("private secret")
    (root / "Escape.md").symlink_to(outside)
    (root / "Escape").symlink_to(tmp_path, target_is_directory=True)
    (root / ".hidden").mkdir()
    (root / ".hidden/secret.txt").write_text("private")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    for relative in (
        "../secret.txt",
        "/etc/passwd",
        "Escape.md",
        "Escape/secret.txt",
        ".hidden/secret.txt",
        "a\\b",
        "a\0b",
    ):
        response = await client.get(
            "/knowledge/asset?" + urlencode({"scope": "general", "path": relative})
        )
        assert response.status == 404, relative
    for folder in ("..", "Escape", ".hidden", "missing"):
        response = await client.get("/knowledge?" + urlencode({"folder": folder}))
        assert response.status == 404, folder
    for route in (
        "/knowledge",
        "/knowledge/asset?scope=general&path=Safe.md",
        "/knowledge/notes/none",
    ):
        for method in ("POST", "HEAD", "DELETE", "PUT"):
            response = await client.request(method, route)
            assert response.status == 405 and response.headers["Allow"] == "GET"
    assert not enso_home.db.exists()
    assert before == {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_recent_uses_metadata_then_mtime_and_current_scan(enso_home):
    root = enso_home.knowledge
    first, _ = note(root, "First.md")
    second, _ = note(root, "Second.md")
    second.write_text(second.read_text().replace("2026-09-15", "2026-09-16"))
    model = knowledge.listing_model(enso_home, {"view": "recent"})
    assert [row["title"] for row in model["rows"]] == ["Second", "First"]
    first.write_text(first.read_text().replace("2026-09-15", "2026-09-17"))
    model = knowledge.listing_model(enso_home, {"view": "recent"})
    assert model["rows"][0]["title"] == "First"


async def test_duplicate_id_is_not_arbitrarily_resolved(client, enso_home):
    _, identity = note(enso_home.knowledge, "First.md")
    note(enso_home.knowledge, "Second.md", identity=identity)
    response = await client.get(f"/knowledge/notes/{identity}")
    assert response.status == 404
    assert len(kb.scan(enso_home).notes) == 2
    response = await client.get("/knowledge")
    links = [row.attrs["href"] for row in knowledge_rows(await response.text())]
    assert len(links) == 2 and all(link.startswith("/knowledge/file?") for link in links)
    response = await client.get(links[0])
    assert response.status == 200 and "Duplicate note id" in await response.text()


async def test_heading_warnings_code_titles_and_literal_wiki_labels(client, enso_home):
    _, target_id = note(
        enso_home.knowledge, "Target.md", "## `Code` heading\n\n## Repeated\n\n## Repeated\n"
    )
    _, source_id = note(
        enso_home.knowledge,
        "Source.md",
        """[[Target#Code heading]]

[[Target#Repeated-1]]

[[Target#Absent]]

[Missing section](Target.md#Absent)

[A [[Target]] label](https://example.com)
""",
    )
    response = await client.get(f"/knowledge/notes/{source_id}")
    article = Document(await response.text()).root.find("article")[0]
    links = article.find("a")
    assert links[0].attrs["href"] == f"/knowledge/notes/{target_id}#code-heading"
    assert "class" not in links[0].attrs
    assert links[1].attrs["href"] == f"/knowledge/notes/{target_id}#repeated-1"
    assert "class" not in links[1].attrs
    assert links[2].attrs["title"] == "Missing heading"
    assert links[3].attrs["title"] == "Missing heading"
    assert links[4].text == "A [[Target]] label" and not links[4].find("a")


async def test_escaped_wiki_aliases_work_in_tables_and_ordinary_paragraphs(client, enso_home):
    _, target_id = note(enso_home.knowledge, "Target.md", "Body")
    _, source_id = note(
        enso_home.knowledge,
        "Source.md",
        "[[Target\\|Paragraph label]]\n\n| Note |\n| --- |\n| [[Target\\|Table label]] |\n",
    )
    response = await client.get(f"/knowledge/notes/{source_id}")
    article = Document(await response.text()).root.find("article")[0]
    links = article.find("a")
    assert [link.text for link in links] == ["Paragraph label", "Table label"]
    assert all(link.attrs["href"] == f"/knowledge/notes/{target_id}" for link in links)
