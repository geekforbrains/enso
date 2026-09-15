"""Filesystem knowledge boundaries, metadata, deterministic linking, and safe writes."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from enso import frontmatter, knowledge, locks
from enso.cli import app
from enso.config import Paths
from enso.knowledge import writing


def put(paths, path, body, scope="general"):
    root = paths.knowledge if scope == "general" else paths.workspaces / scope / "knowledge"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(knowledge.normalize_text(body), encoding="utf-8")
    return target


def test_discovery_nested_paths_cache_and_hidden_boundaries(tmp_path):
    paths = Paths(tmp_path)
    first = put(paths, "Projects/Enso/Plan.md", "## Plan\n\nUseful text.")
    put(paths, "Plan.md", "## Workspace", "development")
    put(paths, ".obsidian/Private.md", "hidden")
    (paths.knowledge / "escape.md").symlink_to(tmp_path / "private.md")
    (tmp_path / "private.md").write_text("outside")
    (paths.knowledge / "linked").symlink_to(tmp_path, target_is_directory=True)
    catalog = knowledge.scan(paths)
    assert [r.scope for r in catalog.roots] == ["general", "workspace:development"]
    assert len(catalog.notes) == 2
    assert catalog.get("Projects/Enso/Plan").title == "Plan"
    assert any("symbolic link" in problem for problem in catalog.problems)
    first_note = catalog.get("Projects/Enso/Plan")
    assert knowledge.scan(paths).get(first_note.id) is first_note
    first.write_text(knowledge.normalize_text("Changed"))
    assert knowledge.scan(paths).get("Projects/Enso/Plan").body == "Changed"
    for bad in (
        "../private.md",
        "/private.md",
        ".obsidian/Private.md",
        "linked/private.md",
        "escape.md",
    ):
        with pytest.raises((knowledge.KnowledgeError, OSError)):
            knowledge.read_bytes(catalog.root("general"), bad)


def test_new_home_is_empty_and_reads_do_not_create_directories(tmp_path):
    paths = Paths(tmp_path / "absent")
    catalog = knowledge.scan(paths)
    assert [r.scope for r in catalog.roots] == ["general"]
    assert catalog.notes == ()
    assert catalog.problems == ()
    assert not paths.home.exists()


def test_metadata_adoption_preserves_unknown_fields_and_does_not_invent_dates():
    raw = "---\ntags: [one, two]\ncreated: 2021-01-02\ncustom: value\n---\n\n## Original\n\nText.\n"
    adopted = knowledge.normalize_text(raw)
    document, error = frontmatter.parse(adopted)
    assert error is None
    assert set(document.fields) == {"schema", "id"}
    assert raw.split("\n\n", 1)[0] in document.body
    assert "## Original\n\nText." in document.body
    assert "## Imported metadata" in document.body
    assert knowledge.normalize_text(adopted) == adopted
    malformed = knowledge.normalize_text("---\nbad: [\n---\n\nActual body\n")
    assert "bad: [" in malformed
    assert "Actual body" in malformed


def test_valid_timestamp_preserved_invalid_schema_and_metadata_are_readable(tmp_path):
    paths = Paths(tmp_path)
    text = (
        f'---\nschema: enso.note/v1\nid: {uuid4()}\ncreated: "2020-01-02T12:00:00Z"\n'
        'updated: "2020-01-03T12:00:00+02:00"\n---\n\nBody\n'
    )
    target = put(paths, "Dates.md", text)
    note = knowledge.scan(paths).get("Dates")
    assert not note.problems
    assert note.metadata["created"] == "2020-01-02T12:00:00Z"
    target.write_text("---\nid: no\ntags: [noise]\n---\n\nStill readable\n")
    note = knowledge.scan(paths).get("Dates")
    assert note.id is None and note.body == "Still readable"
    assert len(note.problems) == 3
    assert "tags" not in note.metadata


def test_link_resolution_is_scoped_and_ambiguous_names_are_not_guessed(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "Start.md", "## Start")
    put(paths, "A/Page.md", "## Useful `code`\n\n## Useful `code`\n")
    put(paths, "B/Page.md", "Other")
    put(paths, "A/Dotted.v2.md", "Version")
    put(paths, "A/C#.md", "Language")
    put(paths, "Plan.md", "Workspace", "dev")
    catalog = knowledge.scan(paths)
    source = catalog.get("Start")
    assert catalog.resolve(source, "Page", wiki=True).status == "ambiguous"
    found = catalog.resolve(source, "A/Page#Useful%20code", wiki=True)
    assert found.note.path == "A/Page.md"
    assert found.fragment == "useful-code"
    assert catalog.resolve(source, "workspace:dev:Plan", wiki=True).note.scope == "workspace:dev"
    assert catalog.resolve(source, "Plan", wiki=True).status == "missing"
    assert catalog.resolve(source, "Dotted.v2", wiki=True).note.path == "A/Dotted.v2.md"
    assert catalog.resolve(source, "A/C%23.md").note.path == "A/C#.md"
    assert catalog.resolve(found.note, "../Start.md").note == source
    assert catalog.resolve(source, "../../outside.md").status == "missing"
    for target in ("javascript:alert(1)", "data:text/html,hello", "file:///secret", "a\x00b"):
        assert catalog.resolve(source, target, wiki=True).status == "missing"
    assert catalog.resolve(source, "https://example.test/hello").status == "external"
    assert knowledge.heading_ids(found.note.body) == ("useful-code", "useful-code-1")


def test_links_exclude_code_and_preserve_exact_target_spans():
    body = (
        "[ordinary](A%20Page.md#Heading) [[Page|label]] ![[image.png]]\n\n`[[inline]]`\n\n"
        '```md\n[[example]]\n```\n\n[ref]: <Folder/A Page.md> "title"\n\n[x](A_(B).md)\n'
    )
    links = knowledge.extract_links(body)
    assert [link.target for link in links] == [
        "A%20Page.md#Heading",
        "Page",
        "image.png",
        "Folder/A Page.md",
        "A_(B).md",
    ]
    assert links[2].image
    assert all(body[link.start : link.end] == link.target for link in links)


def test_assets_backlinks_heading_audit_and_duplicate_ids(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "Index.md", "[[Page#Known]] [[Page#Absent]] ![[image.png]]")
    target = put(paths, "Folder/Page.md", "## Known")
    image = paths.knowledge / "Folder/image.png"
    image.write_bytes(b"image")
    catalog = knowledge.scan(paths)
    index, page = catalog.get("Index"), catalog.get("Folder/Page")
    assert catalog.resolve(index, "image.png", wiki=True).asset == image
    assert catalog.backlinks(page) == (index,)
    assert [p["problem"] for p in catalog.audit()] == ["missing heading: 'Page#Absent'"]
    (paths.knowledge / "Copy.md").write_bytes(target.read_bytes())
    catalog = knowledge.scan(paths)
    with pytest.raises(knowledge.KnowledgeError, match="duplicate"):
        catalog.by_id(page.id)
    assert sum(p["problem"] == "duplicate note id" for p in catalog.audit()) == 2


def test_create_update_and_adopt_are_atomic_and_reject_stale_or_occupied_paths(tmp_path):
    paths = Paths(tmp_path)
    note = knowledge.create_note(paths, "general", "Folder/New.md", "## Body\n\nText")
    assert set(note.metadata) == {"schema", "id", "created", "updated"}
    assert not note.problems
    with pytest.raises(knowledge.KnowledgeError, match="already exists"):
        knowledge.create_note(paths, "general", "Folder/New.md", "Overwrite")
    with pytest.raises(knowledge.KnowledgeError, match="changed"):
        knowledge.update_note(paths, "general", note.id, "New body", expected_hash="stale")
    updated = knowledge.update_note(
        paths, "general", note.id, "New body", expected_hash=note.sha256
    )
    assert updated.body == "New body"
    assert updated.id == note.id and updated.metadata["created"] == note.metadata["created"]
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.create_note(paths, "general", "../outside.md", "Escape")
    (paths.knowledge / "Legacy.md").write_text("Legacy body\n")
    adopted = knowledge.adopt_note(paths, "general", "Legacy.md")
    assert set(adopted.metadata) == {"schema", "id"}
    assert adopted.body == "Legacy body"


def test_writers_refuse_contention_and_symlink_lock(tmp_path):
    paths = Paths(tmp_path)
    fd = locks.acquire(paths.home / ".knowledge.lock")
    try:
        with pytest.raises(knowledge.KnowledgeError, match="another knowledge"):
            knowledge.create_note(paths, "general", "No.md", "body")
    finally:
        import os

        os.close(fd)
    (paths.home / ".knowledge.lock").unlink()
    (paths.home / ".knowledge.lock").symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        knowledge.create_note(paths, "general", "No.md", "body")


def test_writes_reject_second_frontmatter_oversize_and_case_collisions(tmp_path):
    paths = Paths(tmp_path)
    with pytest.raises(knowledge.KnowledgeError, match="without frontmatter"):
        knowledge.create_note(paths, "general", "New.md", "---\ntags: []\n---\nBody")
    with pytest.raises(knowledge.KnowledgeError, match="at most"):
        knowledge.create_note(paths, "general", "Huge.md", "a" * (2 * 1024 * 1024))
    assert not (paths.knowledge / "Huge.md").exists()
    note = knowledge.create_note(paths, "general", "Case.md", "Body")
    with pytest.raises(knowledge.KnowledgeError, match="already exists"):
        knowledge.create_note(paths, "general", "CASE.md", "Other")
    with pytest.raises(knowledge.KnowledgeError, match="without frontmatter"):
        knowledge.update_note(
            paths, "general", note.id, "---\nid: wrong\n---\n", expected_hash=note.sha256
        )


def test_unterminated_import_is_preserved_as_inert_document():
    raw = "---\nbad: metadata\n[[Not a real link]]\n"
    normalized = knowledge.normalize_text(raw)
    document, error = frontmatter.parse(normalized)
    assert error is None
    assert raw.rstrip() in document.body
    assert "## Imported document" in document.body
    assert not knowledge.extract_links(document.body)


def test_table_wiki_alias_escape_is_not_part_of_target_or_move_span(tmp_path):
    paths = Paths(tmp_path)
    body = "| Note |\n| --- |\n| [[Folder/Target\\|Display text]] |\n"
    link = knowledge.extract_links(body)[0]
    assert link.target == "Folder/Target"
    assert body[link.end :] == "\\|Display text]] |\n"
    put(paths, "Index.md", body)
    put(paths, "Folder/Target.md", "Body")
    catalog = knowledge.scan(paths)
    assert (
        catalog.resolve(catalog.get("Index"), link.target, wiki=True).note.path
        == "Folder/Target.md"
    )
    knowledge.move_note(paths, "general", "Folder/Target", "Renamed.md")
    assert "[[general:Renamed\\|Display text]]" in knowledge.scan(paths).get("Index").body


def test_shortest_wiki_paths_resolve_unique_suffixes_without_broadening_other_links(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "Index.md", "[[Compliance/HIPAA|Policy]]")
    put(paths, "Knowledge/Compliance/HIPAA.md", "## Scope")
    put(paths, "Archive/Compliance/HIPAA.md", "Workspace", "dev")
    catalog = knowledge.scan(paths)
    source = catalog.get("Index")
    resolved = catalog.resolve(source, "Compliance/HIPAA#Scope", wiki=True)
    assert resolved.note.path == "Knowledge/Compliance/HIPAA.md"
    assert resolved.fragment == "scope"
    assert catalog.resolve(source, "compliance/hipaa.md", wiki=True).note == resolved.note
    assert catalog.resolve(source, "Compliance/HIPAA.md").status == "missing"
    assert catalog.resolve(source, "general:Compliance/HIPAA", wiki=True).status == "missing"
    assert catalog.resolve(source, "workspace:dev:Compliance/HIPAA", wiki=True).status == "missing"
    assert catalog.backlinks(resolved.note) == (source,)
    assert not catalog.audit()

    put(paths, "Other/Compliance/HIPAA.md", "Other")
    catalog = knowledge.scan(paths)
    ambiguous = catalog.resolve(source, "Compliance/HIPAA", wiki=True)
    assert ambiguous.status == "ambiguous"
    assert {note.path for note in ambiguous.candidates} == {
        "Knowledge/Compliance/HIPAA.md",
        "Other/Compliance/HIPAA.md",
    }
    put(paths, "Compliance/HIPAA.md", "Exact match")
    catalog = knowledge.scan(paths)
    assert catalog.resolve(source, "Compliance/HIPAA", wiki=True).note.path == "Compliance/HIPAA.md"


def test_move_repairs_incoming_shortest_wiki_paths(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "Index.md", "[[Compliance/HIPAA|Policy]]")
    put(paths, "Knowledge/Compliance/HIPAA.md", "Body")
    knowledge.move_note(paths, "general", "Knowledge/Compliance/HIPAA", "Compliance/Policy.md")
    assert "[[general:Compliance/Policy|Policy]]" in knowledge.scan(paths).get("Index").body


def test_move_escapes_filename_delimiters_separately_from_heading_fragment(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "Index.md", "[[Target#Heading|label]] [target](Target.md#Heading)")
    put(paths, "Target.md", "## Heading")
    knowledge.move_note(paths, "general", "Target", "C# 100% [new]|plan.md")
    catalog = knowledge.scan(paths)
    body = catalog.get("Index").body
    assert "[[general:C%23%20100%25%20%5Bnew%5D%7Cplan#heading|label]]" in body
    assert "(general:C%23%20100%25%20%5Bnew%5D%7Cplan.md#heading)" in body
    assert not catalog.audit()


def test_captured_root_cannot_follow_replaced_workspace_ancestor_on_read_or_write(tmp_path):
    paths = Paths(tmp_path / "enso")
    put(paths, "Note.md", "Original", "dev")
    root = knowledge.scan(paths).root("workspace:dev")
    workspace = paths.workspaces / "dev"
    workspace.rename(paths.workspaces / "previous-dev")
    outside = tmp_path / "outside"
    (outside / "knowledge").mkdir(parents=True)
    secret = outside / "knowledge/Note.md"
    secret.write_text("Outside data")
    workspace.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, knowledge.KnowledgeError)):
        knowledge.read_bytes(root, "Note.md")
    with pytest.raises((OSError, knowledge.KnowledgeError)):
        knowledge.safe_path(root, "Note.md")
    with pytest.raises((OSError, knowledge.KnowledgeError)):
        writing._publish(root, "Injected.md", "Unsafe", expected_hash=None)
    assert secret.read_text() == "Outside data"
    assert not (outside / "knowledge/Injected.md").exists()


def test_list_links_are_repaired_while_nested_fences_and_indented_code_are_ignored(tmp_path):
    paths = Paths(tmp_path)
    body = (
        "- Parent\n    - [[Target]]\n    - [Markdown](Target.md)\n\n"
        "        ```md\n        [[Example]]\n        ```\n\n"
        "Paragraph.\n\n    [[Code example]]\n"
    )
    assert [link.target for link in knowledge.extract_links(body)] == ["Target", "Target.md"]
    put(paths, "Index.md", body)
    put(paths, "Target.md", "## Heading")
    result = knowledge.move_note(paths, "general", "Target", "Renamed.md")
    catalog = knowledge.scan(paths)
    assert result["links_updated"] == 1
    assert "    - [[general:Renamed]]" in catalog.get("Index").body
    assert "[[Example]]" in catalog.get("Index").body
    assert not catalog.audit()


def test_code_indentation_and_eof_spaces_survive_read_move_and_substantive_update(tmp_path):
    paths = Paths(tmp_path)
    body = "    print('  ')\n\n[[Target]]\n\n```text\ntrailing  "
    put(paths, "Index.md", body)
    put(paths, "Target.md", "Body")
    note = knowledge.scan(paths).get("Index")
    assert note.body == body
    knowledge.move_note(paths, "general", "Target", "Renamed.md")
    moved = knowledge.scan(paths).get("Index")
    assert moved.body.startswith("    print('  ')")
    assert moved.body.endswith("trailing  ")
    changed = knowledge.update_note(
        paths, "general", moved.id, moved.body[4:], expected_hash=moved.sha256
    )
    assert changed.sha256 != moved.sha256
    assert changed.body.startswith("print('  ')")
    assert changed.body.endswith("trailing  ")


def test_move_preserves_identity_and_repairs_incoming_outgoing_and_asset_links(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "Start.md", "[[Old/Page#Heading|A label]] [page](Old/Page.md)")
    put(paths, "Old/Page.md", "## Heading\n\n[[Sibling]] ![[image.png]] [start](../Start.md)")
    put(paths, "Old/Sibling.md", "Sibling")
    put(paths, "Existing.md", "Workspace", "dev")
    (paths.knowledge / "Old/image.png").write_bytes(b"asset")
    previous = knowledge.scan(paths).get("Old/Page")
    result = knowledge.move_note(
        paths,
        "general",
        previous.id,
        "New/Renamed.md",
        to_scope="workspace:dev",
        expected_hash=previous.sha256,
    )
    catalog = knowledge.scan(paths)
    moved = catalog.by_id(previous.id)
    assert moved.scope == "workspace:dev" and moved.path == "New/Renamed.md"
    assert not (paths.knowledge / "Old/Page.md").exists()
    assert "[[workspace:dev:New/Renamed#heading|A label]]" in catalog.get("Start").body
    assert "[[general:Old/Sibling]]" in moved.body
    assert "![[general:Old/image.png]]" in moved.body
    assert not catalog.audit()
    assert result["links_updated"] == 1
    assert not list(paths.home.glob(".knowledge-move-*"))


def test_move_refuses_ambiguous_incoming_and_existing_targets(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "A/Page.md", "A")
    put(paths, "B/Page.md", "B")
    put(paths, "Index.md", "[[Page]]")
    with pytest.raises(knowledge.KnowledgeError, match="ambiguous"):
        knowledge.move_note(paths, "general", "A/Page", "New.md")
    with pytest.raises(knowledge.KnowledgeError, match="already exists"):
        knowledge.move_note(paths, "general", "A/Page", "B/Page.md")


def test_failed_move_rolls_back_without_losing_notes(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    files = [
        put(paths, "A.md", "[[Target]]"),
        put(paths, "B.md", "[[Target]]"),
        put(paths, "Target.md", "Target"),
    ]
    originals = {file: file.read_bytes() for file in files}
    publish = writing._publish
    failed = False

    def fail_one(root, relative, text, *, expected_hash):
        nonlocal failed
        if relative == "B.md" and not failed:
            failed = True
            raise OSError("simulated disk failure")
        publish(root, relative, text, expected_hash=expected_hash)

    monkeypatch.setattr(writing, "_publish", fail_one)
    with pytest.raises(OSError, match="simulated"):
        knowledge.move_note(paths, "general", "Target", "Moved.md")
    assert all(file.read_bytes() == original for file, original in originals.items())
    assert not (paths.knowledge / "Moved.md").exists()


def test_cli_json_read_write_audit_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSO_HOME", str(tmp_path))
    runner = CliRunner()
    created = runner.invoke(
        app, ["knowledge", "create", "Folder/One.md", "--file", "-", "--json"], input="Body text\n"
    )
    assert created.exit_code == 0, created.output
    note = json.loads(created.output)
    assert note["ok"] and len(note["sha256"]) == 64
    listing = runner.invoke(app, ["knowledge", "search", "body", "--scope", "general", "--json"])
    assert json.loads(listing.output)["total"] == 1
    shown = runner.invoke(app, ["knowledge", "show", note["id"], "--json"])
    assert json.loads(shown.output)["body"] == "Body text"
    updated = runner.invoke(
        app,
        [
            "knowledge",
            "update",
            note["id"],
            "--file",
            "-",
            "--expected-hash",
            note["sha256"],
            "--json",
        ],
        input="[[Missing]]\n",
    )
    assert updated.exit_code == 0, updated.output
    audited = runner.invoke(app, ["knowledge", "audit", "--json"])
    assert audited.exit_code == 1 and not json.loads(audited.output)["ok"]
    conflict = runner.invoke(
        app,
        [
            "knowledge",
            "update",
            note["id"],
            "--file",
            "-",
            "--expected-hash",
            note["sha256"],
            "--json",
        ],
        input="Stale\n",
    )
    assert conflict.exit_code == 1 and not json.loads(conflict.output)["ok"]
    assert (
        hashlib.sha256((tmp_path / "knowledge/Folder/One.md").read_bytes()).hexdigest()
        != note["sha256"]
    )
