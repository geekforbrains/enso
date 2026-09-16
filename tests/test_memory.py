"""Dated workspace memory remains portable, inspectable, and safe to correct."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from uuid import uuid4

import pytest
import yaml
from typer.testing import CliRunner

from enso import captures, db, locks, memory
from enso import note_storage as storage
from enso.cli import app
from enso.config import Paths
from enso.note_storage import NoteError


@pytest.fixture
def paths(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    for workspace in ("team", "personal"):
        paths.workspace(workspace).mkdir(parents=True)
    monkeypatch.setenv("ENSO_HOME", str(tmp_path))
    monkeypatch.setenv("ENSO_WORKSPACE", "team")
    return paths


def imported(paths, relative, *, workspace="team", body="Recalled discussion.", **fields):
    data = {"schema": memory.SCHEMA, "id": str(uuid4()), "occurred": None, "sources": [], **fields}
    target = paths.workspace_memory(workspace) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\n{yaml.safe_dump(data, sort_keys=False)}---\n\n{body}\n")
    return target


def get(paths, ref, workspace="team"):
    return memory.scan(paths, workspace).get(ref, f"workspace:{workspace}")


@pytest.mark.parametrize(
    ("occurred", "normalized", "folder"),
    [
        ("2026-09-16T23:30:00-07:00", "2026-09-17T06:30:00Z", "2026/09/17"),
        ("2026-09-16", "2026-09-16", "2026/09/16"),
        (None, None, "undated"),
    ],
)
def test_create_preserves_occurrence_precision_and_uses_utc_placement(
    paths, occurred, normalized, folder
):
    note = memory.create_note(
        paths, "team", "Launch.md", "A proposal, not a decision.", occurred=occurred
    )
    assert note.path == f"{folder}/Launch.md"
    assert note.metadata["occurred"] == normalized
    assert set(note.metadata) == memory.CORE_FIELDS
    assert note.metadata["sources"] == []
    assert not memory.scan(paths, "team").audit()
    assert not paths.db.exists() and not (paths.home / "memory").exists()
    raw = (paths.workspace_memory("team") / note.path).read_text()
    assert yaml.safe_load(raw.split("---")[1])["occurred"] == normalized
    assert get(paths, note.id).id == note.id
    with pytest.raises(NoteError, match="belongs to workspace:team"):
        get(paths, note.id, "personal")


@pytest.mark.parametrize(
    "occurred", ["2026-02-30", "2026-09-16T10:00:00", "yesterday", True, date(2026, 9, 16)]
)
def test_occurrence_never_guesses_a_date_or_timezone(paths, occurred):
    with pytest.raises(NoteError, match="occurred"):
        memory.create_note(paths, "team", "Bad.md", "Text", occurred=occurred)
    assert not paths.workspace_memory("team").exists()


def test_imports_unknown_dates_noop_updates_and_corrections(paths, monkeypatch):
    target = imported(paths, "undated/Recall.md", body="    preserved code  \n\nProposal.")
    original = target.read_bytes()
    note = get(paths, "undated/Recall.md")
    assert set(note.metadata) == {"schema", "id", "occurred", "sources"}
    assert not note.problems
    assert (
        memory.update_note(paths, "team", note.id, note.body, expected_hash=note.sha256).sha256
        == note.sha256
    )
    assert target.read_bytes() == original
    monkeypatch.setattr(storage, "timestamp", lambda: "2026-09-20T00:00:00Z")
    corrected = memory.update_note(
        paths,
        "team",
        note.id,
        note.body + "\n\nSeptember 20 correction: confirmed later.",
        expected_hash=note.sha256,
    )
    assert corrected.id == note.id and corrected.metadata["occurred"] is None
    assert corrected.metadata["sources"] == [] and "created" not in corrected.metadata
    assert corrected.metadata["updated"] == "2026-09-20T00:00:00Z"
    assert corrected.body.startswith("    preserved code  \n")
    with pytest.raises(NoteError, match="changed"):
        memory.update_note(paths, "team", note.id, "Stale", expected_hash=note.sha256)
    assert get(paths, note.id).body == corrected.body


def test_explicit_occurrence_correction_requires_matching_folder(paths):
    note = memory.create_note(paths, "team", "Event.md", "Recollection.", occurred=None)
    root = paths.workspace_memory("team")
    original = (root / note.path).read_bytes()
    with pytest.raises(NoteError, match="relocate"):
        memory.update_note(
            paths,
            "team",
            note.id,
            "Date found in diary.",
            expected_hash=note.sha256,
            occurred="2026-09-16",
        )
    assert (root / note.path).read_bytes() == original
    destination = root / "2026/09/16/Event.md"
    destination.parent.mkdir(parents=True)
    (root / note.path).rename(destination)
    assert get(paths, note.id).problems
    corrected = memory.update_note(
        paths,
        "team",
        note.id,
        "Date found in diary: September 16.",
        expected_hash=note.sha256,
        occurred="2026-09-16",
    )
    assert corrected.id == note.id and corrected.metadata["occurred"] == "2026-09-16"
    assert corrected.path == "2026/09/16/Event.md" and not corrected.problems


@pytest.mark.parametrize(
    "fields",
    [
        {"schema": "enso.note/v1"},
        {"id": "invalid"},
        {"occurred": date(2026, 9, 16)},
        {"occurred": "2026-02-30"},
        {"occurred": "2026-09-16T12:00:00"},
        {"sources": None},
        {"sources": [True]},
        {"sources": [0]},
        {"sources": [1, 1]},
        {"sources": [{"capture": 1}]},
        {"tags": ["extra"]},
        {"created": "2026-09-16"},
        {"created": "2026-09-20T00:00:00Z", "updated": "2026-09-19T00:00:00Z"},
    ],
)
def test_invalid_imports_are_reported_without_rewriting_or_managed_updates(paths, fields):
    target = imported(paths, "undated/Invalid.md", **fields)
    original = target.read_bytes()
    catalog = memory.scan(paths, "team")
    note = catalog.get("undated/Invalid.md", "workspace:team")
    assert catalog.audit("workspace:team")
    assert note.body == "Recalled discussion."
    with pytest.raises(NoteError, match="cannot update"):
        memory.update_note(paths, "team", note.path, "Changed", expected_hash=note.sha256)
    assert target.read_bytes() == original


def test_readable_malformed_import_and_cache_refresh_without_invented_metadata(paths):
    target = imported(paths, "undated/Import.md")
    for raw in (
        "Unmanaged recollection.\n",
        "---\ninvalid: [\n---\n\nText\n",
        "---\nid: a\nid: b\n---\nText\n",
    ):
        target.write_text(raw)
        note = get(paths, "undated/Import.md")
        assert note.problems and note.id is None
        assert note.sha256 == hashlib.sha256(raw.encode()).hexdigest()
        assert target.read_text() == raw


def test_duplicate_identity_across_workspaces_blocks_lookup_and_writes(paths):
    target = imported(paths, "undated/Original.md")
    copy = imported(paths, "undated/Copy.md", workspace="personal")
    copy.write_bytes(target.read_bytes())
    note = get(paths, "undated/Original.md")
    with pytest.raises(NoteError, match="duplicate"):
        get(paths, note.id)
    with pytest.raises(NoteError, match="duplicate"):
        memory.update_note(paths, "team", note.path, "Changed", expected_hash=note.sha256)
    found = memory.scan(paths, "team").audit()
    assert {p["path"] for p in found if p["problem"] == "duplicate note id"} == {
        "undated/Original.md",
        "undated/Copy.md",
    }


def test_missing_capture_references_are_reported_preserved_and_not_writable(paths):
    target = imported(paths, "undated/Sourced.md", sources=[1201, 1202])
    original = target.read_bytes()
    note = get(paths, "undated/Sourced.md")
    assert note.metadata["sources"] == [1201, 1202]
    assert note.problems == (
        "capture 1201 does not exist",
        "capture 1202 does not exist",
    )
    with pytest.raises(NoteError, match="cannot update"):
        memory.update_note(paths, "team", note.id, "Correction", expected_hash=note.sha256)
    assert target.read_bytes() == original
    assert not paths.db.exists()
    result = CliRunner().invoke(app, ["memory", "audit", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["problems"][0]["path"] == note.path


def test_links_are_relative_with_headings_assets_and_no_workspace_or_wiki_guessing(paths):
    imported(paths, "2026/09/20/Decision.md", occurred="2026-09-20", body="## Confirmed")
    target = imported(
        paths,
        "2026/09/16/Proposal.md",
        occurred="2026-09-16",
        body="[Decision](../20/Decision.md#confirmed) ![Receipt](receipt.png)",
    )
    target.with_name("receipt.png").write_bytes(b"receipt")
    catalog = memory.scan(paths, "team")
    assert not catalog.audit()
    note = get(paths, "2026/09/16/Proposal.md")
    for ref in (
        "Decision",
        "workspace:team:2026/09/20/Decision.md",
        " workspace:team:2026/09/20/Decision.md",
        "general:Page.md",
        "../../../../secret.md",
    ):
        assert catalog.resolve(note, ref).status == "missing"
    assert catalog.resolve(note, "Decision", wiki=True).status == "missing"
    target.write_text(target.read_text().replace("#confirmed", "#absent"))
    assert any("missing heading" in p["problem"] for p in memory.scan(paths, "team").audit())
    (target.parent.parent / "20/Decision.md").unlink()
    assert any("missing link" in p["problem"] for p in memory.scan(paths, "team").audit())


def test_safe_reads_writes_conflicts_and_lock_contention(paths, monkeypatch):
    note = memory.create_note(paths, "team", "Note.md", "Original", occurred=None)
    root = paths.workspace_memory("team")
    for name in ("../escape.md", ".hidden.md", "/absolute.md", "undated/extra.md"):
        with pytest.raises(NoteError):
            memory.create_note(paths, "team", name, "Unsafe", occurred=None)
    with pytest.raises(NoteError, match="already exists"):
        memory.create_note(paths, "team", "NOTE.md", "Overwrite", occurred=None)
    outside = paths.home / "outside.md"
    outside.write_text("Untouched")
    (root / "undated/Link.md").symlink_to(outside)
    assert any("symbolic link" in p["problem"] for p in memory.scan(paths, "team").audit())
    with pytest.raises((OSError, NoteError)):
        memory.create_note(paths, "team", "Link.md", "Unsafe", occurred=None)
    assert outside.read_text() == "Untouched"
    fd = locks.acquire(paths.home / ".memory.lock")
    try:
        with pytest.raises(NoteError, match="another memory write"):
            memory.update_note(paths, "team", note.id, "Concurrent", expected_hash=note.sha256)
    finally:
        os.close(fd)
    original_hash = storage.hash_at
    calls = 0
    target = root / note.path
    human = target.read_text().replace("Original", "Human correction")

    def edited(directory, name):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_text(human)
        return original_hash(directory, name)

    monkeypatch.setattr(storage, "hash_at", edited)
    with pytest.raises(NoteError, match="changed during write"):
        memory.update_note(paths, "team", note.id, "Agent edit", expected_hash=note.sha256)
    assert target.read_text() == human
    assert not list(root.rglob(".enso-note-*"))


def test_root_audit_and_missing_memory_reads_do_not_create_files(paths):
    assert not memory.scan(paths, "team").notes
    assert not paths.workspace_memory("team").exists()
    root = paths.workspace_memory("team")
    root.symlink_to(paths.workspace("personal"))
    findings = memory.scan(paths, "team").audit("workspace:team")
    assert len(findings) == 1 and "real directory" in findings[0]["problem"]


def test_cli_manual_recall_correction_and_scope_selection(paths):
    runner = CliRunner()

    def run(*args, body=None):
        result = runner.invoke(app, ["memory", *args, "--json"], input=body)
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    note = run(
        "create", "Proposal.md", "--occurred", "2026-09-16", "--file", "-", body="Launch proposal."
    )
    run(
        "create",
        "Recollection.md",
        "--occurred",
        "unknown",
        "--file",
        "-",
        body="Launch discussion.",
    )
    run(
        "create",
        "Other.md",
        "--occurred",
        "2026-09-20",
        "--file",
        "-",
        "--workspace",
        "personal",
        body="Other launch discussion.",
    )
    page = run("list", "--limit", "1")
    assert page["total"] == 2 and page["notes"][0]["id"] == note["id"]
    assert run("list", "--offset", "1")["notes"][0]["metadata"]["occurred"] is None
    assert run("search", "launch proposal")["total"] == 1
    shown = run("show", note["id"])
    updated = run(
        "update",
        note["id"],
        "--expected-hash",
        shown["sha256"],
        "--file",
        "-",
        body="Launch proposal.\n\nSeptember 20: a later confirmation.",
    )
    assert updated["id"] == note["id"] and updated["metadata"]["occurred"] == "2026-09-16"
    assert run("audit")["ok"]
    assert not paths.db.exists()
    for args in (
        ["list", "--workspace", "missing"],
        ["show", note["id"], "--workspace", "personal"],
        ["update", note["id"], "--file", "-", "--expected-hash", shown["sha256"]],
    ):
        result = runner.invoke(app, ["memory", *args, "--json"], input="Stale")
        assert result.exit_code == 1 and not json.loads(result.output)["ok"]


def test_source_validity_is_not_cached_with_the_file(paths):
    imported(paths, "undated/Sourced.md", sources=[1])
    assert get(paths, "undated/Sourced.md").problems == ("capture 1 does not exist",)
    db.initialize(paths)
    source, _ = captures.record(
        paths,
        captures.Message(
            "slack",
            "team",
            "slack:C1:1",
            "C1",
            "1",
            "1",
            "U1",
            "Person",
            "2026-09-16T12:00:00Z",
            "A proposal",
            kind="ambient",
        ),
    )
    assert source.id == 1
    note = get(paths, "undated/Sourced.md")
    assert note.problems == ()
    corrected = memory.update_note(
        paths, "team", note.id, "A correction", expected_hash=note.sha256
    )
    assert corrected.metadata["sources"] == [1]
    imported(paths, "undated/Other.md", workspace="personal", sources=[1])
    assert get(paths, "undated/Other.md", "personal").problems == (
        "capture 1 belongs to another workspace",
    )
