"""Filesystem knowledge boundaries, metadata, deterministic linking, and safe writes."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from enso import frontmatter, knowledge, locks
from enso import note_storage as storage
from enso.cli import app
from enso.config import Paths


def put(paths, path, body, scope="shared"):
    root = paths.knowledge if scope == "shared" else paths.workspaces / scope / "knowledge"
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
    assert [r.scope for r in catalog.roots] == ["shared", "workspace:development"]
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
            knowledge.read_bytes(catalog.root("shared"), bad)


def test_new_home_is_empty_and_reads_do_not_create_directories(tmp_path):
    paths = Paths(tmp_path / "absent")
    catalog = knowledge.scan(paths)
    assert [r.scope for r in catalog.roots] == ["shared"]
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
    assert note.metadata["updated"] == "2020-01-03T10:00:00Z"
    assert 'updated: "2020-01-03T10:00:00Z"' in target.read_text()
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
        "`[[wrapped]] inline\ncode`\n\n"
        '```md\n[[example]]\n```\n\n[ref]: <Folder/A Page.md> "title"\n\n[x](A_(B).md)\n'
        # Only CR and LF end a line for Markdown, so these never shift a fence.
        "\f\u2028\n\n```\n[[fenced]]\n```\n"
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
    note = knowledge.create_note(paths, "shared", "Folder/New.md", "## Body\n\nText")
    assert set(note.metadata) == {"schema", "id", "created", "updated"}
    assert not note.problems
    with pytest.raises(knowledge.KnowledgeError, match="already exists"):
        knowledge.create_note(paths, "shared", "Folder/New.md", "Overwrite")
    with pytest.raises(knowledge.KnowledgeError, match="changed"):
        knowledge.update_note(paths, "shared", note.id, "New body", expected_hash="stale")
    updated = knowledge.update_note(paths, "shared", note.id, "New body", expected_hash=note.sha256)
    assert updated.body == "New body"
    assert updated.id == note.id and updated.metadata["created"] == note.metadata["created"]
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.create_note(paths, "shared", "../outside.md", "Escape")
    (paths.knowledge / "Legacy.md").write_text("Legacy body\n")
    adopted = knowledge.adopt_note(paths, "shared", "Legacy.md")
    assert set(adopted.metadata) == {"schema", "id"}
    assert adopted.body == "Legacy body"


def test_import_edits_preserve_unknown_creation_and_moves_preserve_dates(tmp_path):
    paths = Paths(tmp_path)
    target = put(paths, "Imported.md", "A recollection without known document dates.")
    imported = knowledge.scan(paths).get("Imported")
    updated = knowledge.update_note(
        paths, "shared", imported.id, "Confirmed current fact.", expected_hash=imported.sha256
    )
    assert updated.id == imported.id
    assert set(updated.metadata) == {"schema", "id", "updated"}
    unchanged = knowledge.update_note(
        paths, "shared", updated.id, updated.body, expected_hash=updated.sha256
    )
    assert unchanged.sha256 == updated.sha256
    original = target.read_bytes()
    knowledge.move_note(paths, "shared", updated.id, "Reference/Imported.md")
    assert (paths.knowledge / "Reference/Imported.md").read_bytes() == original


def test_edit_refuses_to_write_an_update_before_document_creation(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    note = knowledge.create_note(paths, "shared", "Page.md", "Original")
    target = paths.knowledge / note.path
    original = target.read_bytes()
    monkeypatch.setattr(storage, "timestamp", lambda: "2000-01-01T00:00:00Z")
    with pytest.raises(knowledge.KnowledgeError, match="earlier than created"):
        knowledge.update_note(paths, "shared", note.id, "Changed", expected_hash=note.sha256)
    assert target.read_bytes() == original


@pytest.mark.parametrize("operation", ["adopt", "update", "move", "linked-move"])
def test_managed_writes_refuse_duplicate_ids_even_by_path(tmp_path, operation):
    paths = Paths(tmp_path)
    target = put(paths, "Page.md", "[[Target]]")
    put(paths, "Target.md", "Target")
    copy = put(paths, "Copy.md", "Copy", "team")
    copy.write_bytes(target.read_bytes())
    original = target.read_bytes()
    with pytest.raises(knowledge.KnowledgeError, match="duplicate"):
        if operation == "adopt":
            knowledge.adopt_note(paths, "shared", "Page.md")
        elif operation == "update":
            knowledge.update_note(
                paths,
                "shared",
                "Page.md",
                "Changed",
                expected_hash=hashlib.sha256(original).hexdigest(),
            )
        elif operation == "move":
            knowledge.move_note(paths, "shared", "Page.md", "Moved.md")
        else:
            knowledge.move_note(paths, "shared", "Target.md", "Moved.md")
    assert target.read_bytes() == copy.read_bytes() == original
    assert not (paths.knowledge / "Moved.md").exists()


def test_publication_detects_a_direct_edit_during_write(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    note = knowledge.create_note(paths, "shared", "Page.md", "Original")
    target = paths.knowledge / note.path
    edited = target.read_text().replace("Original", "Human correction")
    original_hash_at = storage.hash_at
    calls = 0

    def concurrent_hash(directory, name):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_text(edited)
        return original_hash_at(directory, name)

    monkeypatch.setattr(storage, "hash_at", concurrent_hash)
    with pytest.raises(knowledge.KnowledgeError, match="changed during write"):
        knowledge.update_note(paths, "shared", note.id, "Agent edit", expected_hash=note.sha256)
    assert target.read_text() == edited
    assert not list(paths.knowledge.glob(".enso-note-*"))


def test_writers_refuse_contention_and_symlink_lock(tmp_path):
    paths = Paths(tmp_path)
    fd = locks.acquire(paths.lock("knowledge"))
    try:
        with pytest.raises(knowledge.KnowledgeError, match="another knowledge"):
            knowledge.create_note(paths, "shared", "No.md", "body")
    finally:
        import os

        os.close(fd)
    (paths.lock("knowledge")).unlink()
    (paths.lock("knowledge")).symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        knowledge.create_note(paths, "shared", "No.md", "body")


def test_writes_reject_second_frontmatter_oversize_and_case_collisions(tmp_path):
    paths = Paths(tmp_path)
    with pytest.raises(knowledge.KnowledgeError, match="without frontmatter"):
        knowledge.create_note(paths, "shared", "New.md", "---\ntags: []\n---\nBody")
    with pytest.raises(knowledge.KnowledgeError, match="at most"):
        knowledge.create_note(paths, "shared", "Huge.md", "a" * (2 * 1024 * 1024))
    assert not (paths.knowledge / "Huge.md").exists()
    note = knowledge.create_note(paths, "shared", "Case.md", "Body")
    with pytest.raises(knowledge.KnowledgeError, match="already exists"):
        knowledge.create_note(paths, "shared", "CASE.md", "Other")
    with pytest.raises(knowledge.KnowledgeError, match="without frontmatter"):
        knowledge.update_note(
            paths, "shared", note.id, "---\nid: wrong\n---\n", expected_hash=note.sha256
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
    knowledge.move_note(paths, "shared", "Folder/Target", "Renamed.md")
    assert "[[shared:Renamed\\|Display text]]" in knowledge.scan(paths).get("Index").body


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
    assert catalog.resolve(source, "shared:Compliance/HIPAA", wiki=True).status == "missing"
    # The old scope name has no alias: it is an ordinary broken link.
    assert catalog.resolve(source, "shared:Knowledge/Compliance/HIPAA").note == resolved.note
    assert catalog.resolve(source, "general:Knowledge/Compliance/HIPAA").status == "missing"
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
    knowledge.move_note(paths, "shared", "Knowledge/Compliance/HIPAA", "Compliance/Policy.md")
    assert "[[shared:Compliance/Policy|Policy]]" in knowledge.scan(paths).get("Index").body


def test_move_escapes_filename_delimiters_separately_from_heading_fragment(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "Index.md", "[[Target#Heading|label]] [target](Target.md#Heading)")
    put(paths, "Target.md", "## Heading")
    knowledge.move_note(paths, "shared", "Target", "C# 100% [new]|plan.md")
    catalog = knowledge.scan(paths)
    body = catalog.get("Index").body
    assert "[[shared:C%23%20100%25%20%5Bnew%5D%7Cplan#heading|label]]" in body
    assert "(shared:C%23%20100%25%20%5Bnew%5D%7Cplan.md#heading)" in body
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
        storage.publish(root, "Injected.md", "Unsafe", expected_hash=None)
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
    result = knowledge.move_note(paths, "shared", "Target", "Renamed.md")
    catalog = knowledge.scan(paths)
    assert result["links_updated"] == 1
    assert "    - [[shared:Renamed]]" in catalog.get("Index").body
    assert "[[Example]]" in catalog.get("Index").body
    assert not catalog.audit()


def test_code_indentation_and_eof_spaces_survive_read_move_and_substantive_update(tmp_path):
    paths = Paths(tmp_path)
    body = "    print('  ')\n\n[[Target]]\n\n```text\ntrailing  "
    put(paths, "Index.md", body)
    put(paths, "Target.md", "Body")
    note = knowledge.scan(paths).get("Index")
    assert note.body == body
    knowledge.move_note(paths, "shared", "Target", "Renamed.md")
    moved = knowledge.scan(paths).get("Index")
    assert moved.body.startswith("    print('  ')")
    assert moved.body.endswith("trailing  ")
    changed = knowledge.update_note(
        paths, "shared", moved.id, moved.body[4:], expected_hash=moved.sha256
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
        "shared",
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
    assert "[[shared:Old/Sibling]]" in moved.body
    assert "![[shared:Old/image.png]]" in moved.body
    assert not catalog.audit()
    assert result["links_updated"] == 1
    assert not list(paths.home.glob(".knowledge-move-*"))


def test_same_folder_rename_rewrites_only_links_it_breaks(tmp_path):
    paths = Paths(tmp_path)
    kept = (
        "## Overview\n\n[top](#overview) [Beta](Beta.md) [[Beta]] [[shared:F/Beta]] ![[image.png]]"
    )
    put(paths, "F/Alpha.md", f"{kept} [self](Alpha.md)")
    put(paths, "F/Beta.md", "[[Alpha]] [alpha](Alpha.md) [alpha](../F/Alpha.md#overview)")
    put(paths, "Other.md", "[[Beta]] [alpha](F/Alpha.md)")
    (paths.knowledge / "F/image.png").write_bytes(b"asset")
    result = knowledge.move_note(paths, "shared", "F/Alpha", "F/Alpha2.md")
    catalog = knowledge.scan(paths)
    assert catalog.get("F/Alpha2").body == f"{kept} [self](shared:F/Alpha2.md)"
    assert catalog.get("F/Beta").body == (
        "[[shared:F/Alpha2]] [alpha](shared:F/Alpha2.md) [alpha](shared:F/Alpha2.md#overview)"
    )
    assert catalog.get("Other").body == "[[Beta]] [alpha](shared:F/Alpha2.md)"
    assert result["links_updated"] == 2
    assert not catalog.audit()


def test_folder_move_keeps_scope_wide_names_and_qualifies_relative_paths(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "A/Page.md", "[[Sibling]] [sib](Sibling.md) ![[image.png]] ![img](image.png)")
    put(paths, "A/Sibling.md", "[[Page]] [page](Page.md)")
    (paths.knowledge / "A/image.png").write_bytes(b"asset")
    result = knowledge.move_note(paths, "shared", "A/Page", "B/Page.md")
    catalog = knowledge.scan(paths)
    assert catalog.get("B/Page").body == (
        "[[Sibling]] [sib](shared:A/Sibling.md) ![[image.png]] ![img](shared:A/image.png)"
    )
    assert catalog.get("A/Sibling").body == "[[Page]] [page](shared:B/Page.md)"
    assert result["links_updated"] == 1
    assert not catalog.audit()


def test_move_qualifies_bare_links_the_new_name_would_make_ambiguous(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "A/Plan.md", "A")
    put(paths, "B/Roadmap.md", "[[Plan]]")
    put(paths, "Index.md", "[[Plan]] [[Roadmap]]")
    put(paths, "Plan.md", "[[Plan]]", "dev")  # another scope is never involved
    result = knowledge.move_note(paths, "shared", "B/Roadmap", "B/Plan.md")
    catalog = knowledge.scan(paths)
    assert catalog.get("Index").body == "[[shared:A/Plan]] [[shared:B/Plan]]"
    assert catalog.get("B/Plan").body == "[[shared:A/Plan]]"
    assert catalog.get("Plan", "workspace:dev").body == "[[Plan]]"
    assert result["links_updated"] == 1
    assert not catalog.audit()


@pytest.mark.parametrize("kind", ["file", "symlink", "linked shared"])
def test_occupied_shared_root_is_reported_by_kind(tmp_path, kind):
    paths = Paths(tmp_path / "home")
    if kind == "linked shared":
        paths.home.mkdir()
        (tmp_path / "knowledge").mkdir()
        paths.shared.symlink_to(tmp_path, target_is_directory=True)
    else:
        paths.shared.mkdir(parents=True)
    if kind == "file":
        paths.knowledge.write_text("in the way")
    elif kind == "symlink":
        paths.knowledge.symlink_to(tmp_path, target_is_directory=True)
    catalog = knowledge.scan(paths)
    assert catalog.roots == ()
    described = "file" if kind == "file" else "symbolic link"
    assert catalog.problems == (
        f"shared: {paths.knowledge}: knowledge root must be a directory, not a {described}",
    )


def test_move_refuses_ambiguous_incoming_and_existing_targets(tmp_path):
    paths = Paths(tmp_path)
    put(paths, "A/Page.md", "A")
    put(paths, "B/Page.md", "B")
    put(paths, "Index.md", "[[Page]]")
    with pytest.raises(knowledge.KnowledgeError, match="ambiguous"):
        knowledge.move_note(paths, "shared", "A/Page", "New.md")
    with pytest.raises(knowledge.KnowledgeError, match="already exists"):
        knowledge.move_note(paths, "shared", "A/Page", "B/Page.md")


def test_failed_move_rolls_back_without_losing_notes(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    files = [
        put(paths, "A.md", "[[Target]]"),
        put(paths, "B.md", "[[Target]]"),
        put(paths, "Target.md", "Target"),
    ]
    originals = {file: file.read_bytes() for file in files}
    publish = storage.publish
    failed = False

    def fail_one(root, relative, text, *, expected_hash):
        nonlocal failed
        if relative == "B.md" and not failed:
            failed = True
            raise OSError("simulated disk failure")
        publish(root, relative, text, expected_hash=expected_hash)

    monkeypatch.setattr(storage, "publish", fail_one)
    with pytest.raises(OSError, match="simulated"):
        knowledge.move_note(paths, "shared", "Target", "Moved.md")
    assert all(file.read_bytes() == original for file, original in originals.items())
    assert not (paths.knowledge / "Moved.md").exists()


@pytest.mark.parametrize("options", [[], ["--shared"]])
def test_cli_json_read_write_audit_and_errors(tmp_path, monkeypatch, options):
    monkeypatch.setenv("ENSO_HOME", str(tmp_path))
    runner = CliRunner()
    created = runner.invoke(
        app,
        ["knowledge", "create", "Folder/One.md", "--file", "-", *options, "--json"],
        input="Body text\n",
    )
    assert created.exit_code == 0, created.output
    note = json.loads(created.output)
    assert note["ok"] and note["scope"] == "shared" and len(note["sha256"]) == 64
    listing = runner.invoke(app, ["knowledge", "search", "body", *options, "--json"])
    assert json.loads(listing.output)["total"] == 1
    shown = runner.invoke(app, ["knowledge", "show", note["id"], *options, "--json"])
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
            *options,
            "--json",
        ],
        input="[[Missing]]\n",
    )
    assert updated.exit_code == 0, updated.output
    audited = runner.invoke(app, ["knowledge", "audit", *options, "--json"])
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
            *options,
            "--json",
        ],
        input="Stale\n",
    )
    assert conflict.exit_code == 1 and not json.loads(conflict.output)["ok"]
    assert (
        hashlib.sha256((Paths(tmp_path).knowledge / "Folder/One.md").read_bytes()).hexdigest()
        != note["sha256"]
    )
    (Paths(tmp_path).knowledge / "Missing.md").write_text("Imported reference\n")
    adopted = runner.invoke(app, ["knowledge", "adopt", "Missing.md", *options, "--json"])
    assert adopted.exit_code == 0, adopted.output
    moved = runner.invoke(app, ["knowledge", "move", note["id"], "Moved.md", *options, "--json"])
    assert moved.exit_code == 0 and json.loads(moved.output)["scope"] == "shared", moved.output
    assert runner.invoke(app, ["knowledge", "audit", *options, "--json"]).exit_code == 0


@pytest.mark.parametrize(
    ("environment", "options", "selected"),
    [
        ("team", [], "shared"),
        ("missing", [], "shared"),
        ("team", ["--workspace", "team"], "workspace:team"),
        ("team", ["--workspace", "personal"], "workspace:personal"),
        ("missing", ["--workspace", "team"], "workspace:team"),
        ("missing", ["--shared"], "shared"),
        (None, ["--shared"], "shared"),
        (None, [], "shared"),
        ("team", ["--workspace", "missing"], None),
        ("team", ["--workspace", "team", "--shared"], None),
    ],
)
def test_cli_knowledge_context_selection(tmp_path, monkeypatch, environment, options, selected):
    paths = Paths(tmp_path)
    monkeypatch.setenv("ENSO_HOME", str(tmp_path))
    if environment is None:
        monkeypatch.delenv("ENSO_WORKSPACE", raising=False)
    else:
        monkeypatch.setenv("ENSO_WORKSPACE", environment)
    for scope in ("shared", "team", "personal"):
        put(paths, "Note.md", "Reference", scope)
    result = CliRunner().invoke(app, ["knowledge", "list", *options, "--json"])
    data = json.loads(result.output)
    if selected is None:
        assert result.exit_code == 1 and not data["ok"]
    else:
        assert result.exit_code == 0, result.output
        assert data["total"] == 1
        assert data["notes"][0]["scope"] == selected
    assert not paths.db.exists()


def test_cli_workspace_writes_scoped_ids_and_cross_root_moves(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    monkeypatch.setenv("ENSO_HOME", str(tmp_path))
    monkeypatch.setenv("ENSO_WORKSPACE", "team")
    put(paths, "Index.md", "[[Page]]", "team")
    put(paths, "Index.md", "Personal reference", "personal")
    runner = CliRunner()
    created = runner.invoke(
        app,
        ["knowledge", "create", "Page.md", "--workspace", "team", "--file", "-", "--json"],
        input="Reference",
    )
    assert created.exit_code == 0, created.output
    note = json.loads(created.output)
    assert note["scope"] == "workspace:team"
    for command in (
        ["show", note["id"]],
        ["update", note["id"], "--file", "-", "--expected-hash", note["sha256"]],
        ["move", note["id"], "Moved.md"],
    ):
        refused = runner.invoke(app, ["knowledge", *command, "--shared", "--json"], input="Wrong")
        assert refused.exit_code == 1, refused.output
        assert "belongs to workspace:team" in json.loads(refused.output)["error"]
    conflict = runner.invoke(
        app,
        [
            "knowledge",
            "move",
            "Page.md",
            "Page.md",
            "--workspace",
            "team",
            "--to-shared",
            "--to-workspace",
            "personal",
        ],
    )
    assert conflict.exit_code == 1 and "not both" in conflict.output
    moved = runner.invoke(
        app,
        [
            "knowledge",
            "move",
            note["id"],
            "Page.md",
            "--workspace",
            "team",
            "--to-shared",
            "--json",
        ],
    )
    assert moved.exit_code == 0, moved.output
    assert json.loads(moved.output)["scope"] == "shared"
    again = runner.invoke(
        app,
        [
            "knowledge",
            "move",
            note["id"],
            "Page.md",
            "--shared",
            "--to-workspace",
            "personal",
            "--json",
        ],
    )
    assert again.exit_code == 0, again.output
    catalog = knowledge.scan(paths)
    assert catalog.by_id(note["id"]).scope == "workspace:personal"
    assert "[[workspace:personal:Page]]" in catalog.get("Index", "workspace:team").body
    assert not catalog.audit()


def test_cli_scoped_audit_and_search_report_only_selected_root(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    monkeypatch.setenv("ENSO_HOME", str(tmp_path))
    monkeypatch.setenv("ENSO_WORKSPACE", "team")
    put(paths, "Note.md", "Reference", "team")
    paths.shared.mkdir()
    paths.knowledge.write_text("Invalid shared root")
    runner = CliRunner()
    for command in (["audit"], ["search", "Reference"]):
        result = runner.invoke(app, ["knowledge", *command, "--workspace", "team", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["problems"] == []
