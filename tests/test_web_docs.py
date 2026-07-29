"""Tests for the dashboard reference-doc routes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jinja2")

from enso.web import app as web_app


def _write_doc(
    docs_dir: Path,
    rel_path: str,
    name: str = "",
    description: str = "",
    body: str = "Reference body.",
) -> Path:
    """Write a doc under the docs root, creating parents; no name means no frontmatter."""
    path = docs_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"---\nname: {name}\ndescription: {description}\n---\n\n" if name else ""
    path.write_text(f"{header}{body}\n", encoding="utf-8")
    return path


def _docs_web_app(tmp_path, monkeypatch):
    """Build the web app with the docs root pointed at a temp dir."""
    from starlette.testclient import TestClient

    from enso import docs as docs_mod

    config_dir = tmp_path / "enso"
    docs_dir = config_dir / "docs"
    working_dir = tmp_path / "workspace"
    working_dir.mkdir()
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(docs_mod, "DOCS_DIR", str(docs_dir))
    monkeypatch.setattr(web_app, "load_jobs", lambda: [])
    monkeypatch.setattr(web_app.runs, "list_runs", lambda **_kwargs: [])
    runtime = SimpleNamespace(working_dir=str(working_dir), config={"web": {}})
    return docs_dir, TestClient(
        web_app.create_app(runtime), base_url="http://127.0.0.1"
    )


def test_docs_list_groups_docs_by_directory_sorted_by_path(tmp_path, monkeypatch):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    _write_doc(docs_dir, "homelab.md", "Homelab Network", "How the rack is wired.")
    _write_doc(docs_dir, "stuff/sub_stuff.md", "Sub Stuff", "One level down.")
    _write_doc(docs_dir, "stuff/deeper/notes.md", "Nested Notes", "Two levels down.")
    (docs_dir / "stuff" / "diagram.png").write_bytes(b"not a doc")

    response = client.get("/docs")

    assert response.status_code == 200
    assert "Homelab Network" in response.text
    assert "How the rack is wired." in response.text
    assert 'href="/docs/stuff/deeper/notes.md"' in response.text
    # The relative path is shown beside the link, not only inside the href.
    assert response.text.count("stuff/sub_stuff.md") >= 2
    assert "Top level" in response.text
    assert "Stuff / Deeper" in response.text
    assert "diagram.png" not in response.text
    assert (
        response.text.index("/docs/homelab.md")
        < response.text.index("/docs/stuff/sub_stuff.md")
        < response.text.index("/docs/stuff/deeper/notes.md")
    )


def test_docs_list_flags_broken_frontmatter_without_hiding_the_doc(
    tmp_path, monkeypatch
):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    _write_doc(docs_dir, "stuff/sub_stuff.md", body="Prose with no header.")

    response = client.get("/docs")

    assert response.status_code == 200
    assert 'href="/docs/stuff/sub_stuff.md"' in response.text
    assert "Sub Stuff" in response.text
    assert "needs frontmatter" in response.text


def test_docs_list_empty_state_points_at_both_create_surfaces(tmp_path, monkeypatch):
    _, client = _docs_web_app(tmp_path, monkeypatch)

    response = client.get("/docs")

    assert response.status_code == 200
    assert 'href="/docs/new"' in response.text
    assert "enso doc create" in response.text


def test_doc_detail_renders_nested_doc_in_an_editor_with_breadcrumb(
    tmp_path, monkeypatch
):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    _write_doc(
        docs_dir,
        "stuff/deeper/notes.md",
        "Nested Notes",
        "Notes that live two levels down.",
        body="Rack layout and addresses.",
    )

    response = client.get("/docs/stuff/deeper/notes.md")

    assert response.status_code == 200
    assert "Nested Notes" in response.text
    assert "stuff/deeper/notes.md" in response.text
    assert "Rack layout and addresses." in response.text
    assert 'action="/docs/edit"' in response.text
    assert 'action="/docs/delete"' in response.text
    assert 'value="stuff/deeper/notes.md"' in response.text
    assert client.app.state.csrf_token in response.text
    # Breadcrumb titles are derived from the directory segments.
    assert "Stuff" in response.text
    assert "Deeper" in response.text


def test_doc_edit_replaces_whole_file_and_redirects_back(tmp_path, monkeypatch):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    doc = _write_doc(docs_dir, "stuff/sub_stuff.md", "Sub Stuff", "One level down.")
    sibling = _write_doc(docs_dir, "homelab.md", "Homelab", "Untouched.")
    original_sibling = sibling.read_bytes()
    edited = "---\r\nname: Renamed\r\ndescription: Rewritten.\r\n---\r\n\r\nNew body.\r\n"

    response = client.post(
        "/docs/edit",
        data={
            "path": "stuff/sub_stuff.md",
            "content": edited,
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/docs/stuff/sub_stuff.md"
    assert doc.read_text(encoding="utf-8") == (
        "---\nname: Renamed\ndescription: Rewritten.\n---\n\nNew body.\n"
    )
    assert sibling.read_bytes() == original_sibling


def test_doc_delete_removes_file_and_prunes_only_empty_parents(tmp_path, monkeypatch):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    nested = _write_doc(docs_dir, "stuff/deeper/notes.md", "Nested Notes", "Deep.")
    sibling = _write_doc(docs_dir, "stuff/sub_stuff.md", "Sub Stuff", "Shallow.")
    token = client.app.state.csrf_token

    nested_delete = client.post(
        "/docs/delete",
        data={"path": "stuff/deeper/notes.md", "_csrf": token},
        follow_redirects=False,
    )

    assert nested_delete.status_code == 303
    assert nested_delete.headers["location"] == "/docs?msg=Doc+deleted+from+disk"
    assert not nested.exists()
    assert not nested.parent.exists()
    assert sibling.is_file()

    sibling_delete = client.post(
        "/docs/delete",
        data={"path": "stuff/sub_stuff.md", "_csrf": token},
        follow_redirects=False,
    )

    assert sibling_delete.status_code == 303
    assert not sibling.parent.exists()
    assert docs_dir.is_dir()


def test_doc_create_scaffolds_frontmatter_and_parent_directories(tmp_path, monkeypatch):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)

    form = client.get("/docs/new")
    response = client.post(
        "/docs/new",
        data={
            "path": "stuff/deeper/new_notes.md",
            "name": "New Notes",
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert form.status_code == 200
    assert 'action="/docs/new"' in form.text
    assert client.app.state.csrf_token in form.text
    assert response.status_code == 303
    assert response.headers["location"] == "/docs/stuff/deeper/new_notes.md"
    created = docs_dir / "stuff" / "deeper" / "new_notes.md"
    assert "name: New Notes" in created.read_text(encoding="utf-8")
    assert "description:" in created.read_text(encoding="utf-8")


def test_doc_create_reports_errors_in_the_form_without_writing(tmp_path, monkeypatch):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    existing = _write_doc(docs_dir, "homelab.md", "Homelab", "Keep me.")
    outside = tmp_path / "outside.md"
    outside.write_text("SENTINEL OUTSIDE CONTENT\n", encoding="utf-8")
    token = client.app.state.csrf_token
    errors = {
        "homelab.md": "already exists",
        "../outside.md": "is not allowed",
        str(outside): "relative path",
    }

    for path, message in errors.items():
        response = client.post(
            "/docs/new",
            data={"path": path, "name": "", "_csrf": token},
            follow_redirects=False,
        )

        assert response.status_code == 200, path
        assert 'action="/docs/new"' in response.text, path
        assert path in response.text, path
        assert message in response.text, path

    assert "Keep me." in existing.read_text(encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == "SENTINEL OUTSIDE CONTENT\n"


def test_doc_writes_refuse_paths_that_escape_the_docs_root(tmp_path, monkeypatch):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    _write_doc(docs_dir, "stuff/sub_stuff.md", "Sub Stuff", "Keep me.")
    outside = tmp_path / "outside.md"
    outside.write_text("SENTINEL OUTSIDE CONTENT\n", encoding="utf-8")
    token = client.app.state.csrf_token
    bad_paths = [
        "../outside.md",
        "stuff/../../outside.md",
        str(outside),
        "notes.txt",
        "..",
        "back\\slash.md",
        "nul\x00.md",
        ".hidden.md",
    ]

    for path in bad_paths:
        edit = client.post(
            "/docs/edit",
            data={"path": path, "content": "PWNED", "_csrf": token},
            follow_redirects=False,
        )
        delete = client.post(
            "/docs/delete",
            data={"path": path, "_csrf": token},
            follow_redirects=False,
        )

        assert edit.status_code == 403, path
        assert delete.status_code == 403, path

    assert outside.read_text(encoding="utf-8") == "SENTINEL OUTSIDE CONTENT\n"
    assert (docs_dir / "stuff" / "sub_stuff.md").is_file()


def test_doc_detail_refuses_traversing_and_absolute_urls(tmp_path, monkeypatch):
    _, client = _docs_web_app(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("SENTINEL OUTSIDE CONTENT\n", encoding="utf-8")
    # The client normalizes literal "../" away, so encoded forms carry the traversal.
    refused = {
        "/docs/../../outside.md": 404,
        "/docs/%2e%2e/%2e%2e/outside.md": 403,
        "/docs/..%2f..%2foutside.md": 403,
        f"/docs/{outside}": 403,
        "/docs/notes.txt": 403,
    }

    for url, status in refused.items():
        response = client.get(url, follow_redirects=False)

        assert response.status_code == status, url
        assert "SENTINEL" not in response.text, url


def test_doc_routes_refuse_a_symlink_pointing_outside_the_docs_tree(
    tmp_path, monkeypatch
):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    _write_doc(docs_dir, "homelab.md", "Homelab", "Real doc.")
    outside = tmp_path / "secret.md"
    outside.write_text("SENTINEL SECRET CONTENT\n", encoding="utf-8")
    link = docs_dir / "linked.md"
    link.symlink_to(outside)
    token = client.app.state.csrf_token

    listing = client.get("/docs")
    detail = client.get("/docs/linked.md")
    edit = client.post(
        "/docs/edit",
        data={"path": "linked.md", "content": "PWNED", "_csrf": token},
        follow_redirects=False,
    )
    delete = client.post(
        "/docs/delete",
        data={"path": "linked.md", "_csrf": token},
        follow_redirects=False,
    )

    assert "linked.md" not in listing.text
    assert detail.status_code == 403
    assert edit.status_code == 403
    assert delete.status_code == 403
    assert outside.read_text(encoding="utf-8") == "SENTINEL SECRET CONTENT\n"
    assert link.is_symlink()


def test_docs_list_never_descends_a_symlinked_directory(tmp_path, monkeypatch):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    _write_doc(docs_dir, "homelab.md", "Homelab", "Real doc.")
    outside_dir = tmp_path / "outside-docs"
    outside_dir.mkdir()
    secret = _write_doc(outside_dir, "secret.md", "Secret", "SENTINEL SECRET CONTENT")
    (docs_dir / "linked").symlink_to(outside_dir, target_is_directory=True)

    listing = client.get("/docs")
    detail = client.get("/docs/linked/secret.md")

    assert "SENTINEL" not in listing.text
    assert "linked/secret.md" not in listing.text
    assert detail.status_code == 403
    assert "SENTINEL SECRET CONTENT" in secret.read_text(encoding="utf-8")


def test_doc_routes_404_on_a_missing_doc(tmp_path, monkeypatch):
    _, client = _docs_web_app(tmp_path, monkeypatch)
    token = client.app.state.csrf_token

    detail = client.get("/docs/stuff/missing.md")
    edit = client.post(
        "/docs/edit",
        data={"path": "stuff/missing.md", "content": "x", "_csrf": token},
        follow_redirects=False,
    )
    delete = client.post(
        "/docs/delete",
        data={"path": "stuff/missing.md", "_csrf": token},
        follow_redirects=False,
    )

    assert detail.status_code == 404
    assert edit.status_code == 404
    assert delete.status_code == 404


def test_doc_writes_reject_requests_without_a_csrf_token(tmp_path, monkeypatch):
    docs_dir, client = _docs_web_app(tmp_path, monkeypatch)
    doc = _write_doc(docs_dir, "homelab.md", "Homelab", "Keep me.")
    original = doc.read_bytes()

    edit = client.post(
        "/docs/edit",
        data={"path": "homelab.md", "content": "PWNED"},
        follow_redirects=False,
    )
    delete = client.post(
        "/docs/delete", data={"path": "homelab.md"}, follow_redirects=False
    )
    create = client.post(
        "/docs/new", data={"path": "new_doc.md", "name": ""}, follow_redirects=False
    )

    assert edit.status_code == 403
    assert delete.status_code == 403
    assert create.status_code == 403
    assert doc.read_bytes() == original
    assert not (docs_dir / "new_doc.md").exists()
