"""Fresh knowledge is seeded once; interruption and later customization preserve ownership."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enso import audit, initialization, knowledge, knowledge_starter, migrations, workspaces
from enso.cli import app
from enso.config import Agent, Paths
from enso.maintenance import write_json
from enso.note_storage import writer


def contents(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_fresh_cli_init_seeds_valid_unique_notes_and_empty_subject_folders(
    enso_home, tmp_path, monkeypatch
):
    first = Paths(tmp_path / "first")
    second = Paths(tmp_path / "second")
    identities = set()
    for paths in (first, second):
        monkeypatch.setenv("ENSO_HOME", str(paths.home))
        result = CliRunner().invoke(app, ["init", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ok"]
        catalog = knowledge.scan(paths)
        assert catalog.audit() == []
        assert {note.path for note in catalog.notes} == set(knowledge_starter.NOTES)
        for note in catalog.notes:
            assert set(note.metadata) == {"schema", "id", "created", "updated"}
            assert note.id not in identities
            identities.add(note.id)
        assert {p.name for p in paths.knowledge.iterdir()} == set(knowledge_starter.FOLDERS)
        assert all(
            not list((paths.knowledge / name).iterdir())
            for name in knowledge_starter.FOLDERS
            if name != "Meta"
        )
        receipt = paths.runtime_dir / knowledge_starter.RECEIPT
        assert json.loads(receipt.read_text())["state"] == "complete"
        assert receipt.stat().st_mode & 0o777 == 0o600
        bundles = json.loads((paths.home / ".bundles.json").read_text())["files"]
        assert not any(name.startswith("shared/knowledge/") for name in bundles)


def test_repeat_init_updates_and_repairs_preserve_changed_moved_and_deleted_starter(
    enso_home, tmp_path
):
    paths = Paths(tmp_path / "fresh")
    assert initialization.initialize_home(paths)["ok"]
    guide = knowledge.scan(paths).get("Meta/Guide.md")
    knowledge.update_note(
        paths, "shared", guide.path, "My own organization.\n", expected_hash=guide.sha256
    )
    knowledge.move_note(paths, "shared", "Meta/Index.md", "Navigation.md")
    (paths.knowledge / "Meta/Templates/Person.md").unlink()
    (paths.knowledge / "!Inbox").rmdir()
    before = contents(paths.knowledge)

    assert initialization.initialize_home(paths)["ok"]
    workspaces.reconcile_bundles(paths, Agent("claude", "opus", "high"))
    audit.audit_home(paths, fix=True, user_dirs=[])

    assert contents(paths.knowledge) == before
    assert not (paths.knowledge / "!Inbox").exists()
    shutil.rmtree(paths.knowledge)
    assert initialization.initialize_home(paths)["ok"]
    assert list(paths.knowledge.iterdir()) == []


@pytest.mark.parametrize("populated", [False, True])
def test_existing_homes_never_receive_starter_even_with_empty_knowledge(enso_home, populated):
    enso_home.knowledge.mkdir(parents=True)
    if populated:
        (enso_home.knowledge / "Personal.md").write_text("Existing personal conventions.\n")
    before = contents(enso_home.knowledge)

    assert initialization.initialize_home(enso_home)["ok"]

    assert contents(enso_home.knowledge) == before
    assert not (enso_home.runtime_dir / knowledge_starter.RECEIPT).exists()


def test_failure_during_staging_publishes_no_partial_notes_and_resumes(
    enso_home, tmp_path, monkeypatch
):
    paths = Paths(tmp_path / "fresh")
    original = knowledge.create_note
    called = 0

    def interrupt(*args):
        nonlocal called
        called += 1
        if called == 2:
            raise OSError("disk failure")
        return original(*args)

    monkeypatch.setattr(knowledge, "create_note", interrupt)
    assert not initialization.initialize_home(paths)["ok"]
    assert not paths.knowledge.exists()
    assert not list(paths.runtime_dir.glob(".knowledge-starter-*"))
    assert (
        json.loads((paths.runtime_dir / knowledge_starter.RECEIPT).read_text())["state"]
        == "pending"
    )

    monkeypatch.setattr(knowledge, "create_note", original)
    assert initialization.initialize_home(paths)["ok"]
    assert len(knowledge.scan(paths).notes) == len(knowledge_starter.NOTES)
    assert knowledge.scan(paths).audit() == []


def test_failure_before_migration_marker_retains_fresh_starter_intent(
    enso_home, tmp_path, monkeypatch
):
    paths = Paths(tmp_path / "fresh")
    original = initialization.write_json

    def interrupt(path, value):
        if path.name == migrations.MARKER:
            raise OSError("interrupted before migration marker")
        return original(path, value)

    monkeypatch.setattr(initialization, "write_json", interrupt)
    assert not initialization.initialize_home(paths)["ok"]
    assert initialization.is_fresh_home(paths)
    monkeypatch.setattr(initialization, "write_json", original)
    assert initialization.initialize_home(paths)["ok"]
    assert len(knowledge.scan(paths).notes) == len(knowledge_starter.NOTES)


def test_failed_receipt_write_publishes_nothing_and_retries(enso_home, tmp_path, monkeypatch):
    paths = Paths(tmp_path / "fresh")
    original = knowledge_starter.write_json

    def interrupt(path, value):
        if value.get("state") == "complete":
            raise OSError("could not consume starter receipt")
        return original(path, value)

    monkeypatch.setattr(knowledge_starter, "write_json", interrupt)
    assert not initialization.initialize_home(paths)["ok"]
    assert not paths.knowledge.exists()
    assert (
        json.loads((paths.runtime_dir / knowledge_starter.RECEIPT).read_text())["state"]
        == "pending"
    )
    monkeypatch.setattr(knowledge_starter, "write_json", original)

    assert initialization.initialize_home(paths)["ok"]
    assert len(knowledge.scan(paths).notes) == len(knowledge_starter.NOTES)


@pytest.mark.parametrize("published", [False, True])
def test_consumed_receipt_prevents_reseeding_after_publication_interruption(
    enso_home, tmp_path, monkeypatch, published
):
    paths = Paths(tmp_path / "fresh")
    original = knowledge_starter.os.rename

    def interrupt(source, destination, **kwargs):
        if published:
            original(source, destination, **kwargs)
        raise OSError("interrupted at publication")

    monkeypatch.setattr(knowledge_starter.os, "rename", interrupt)
    assert not initialization.initialize_home(paths)["ok"]
    assert (
        json.loads((paths.runtime_dir / knowledge_starter.RECEIPT).read_text())["state"]
        == "complete"
    )
    assert paths.knowledge.exists() == published
    if published:
        shutil.rmtree(paths.knowledge)
    monkeypatch.setattr(knowledge_starter.os, "rename", original)

    assert initialization.initialize_home(paths)["ok"]
    assert list(paths.knowledge.iterdir()) == []


def test_external_parent_alias_is_allowed_but_home_symlink_is_rejected(enso_home, tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    paths = Paths(alias / "fresh")

    assert initialization.initialize_home(paths)["ok"]
    assert len(knowledge.scan(paths).notes) == len(knowledge_starter.NOTES)
    assert knowledge.scan(paths).audit() == []

    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(actual, target_is_directory=True)
    before = contents(actual)

    assert not initialization.initialize_home(Paths(linked_home))["ok"]
    assert contents(actual) == before


@pytest.mark.parametrize("target", ["populated", "symlink"])
def test_pending_starter_preserves_collections_that_appeared_before_retry(
    enso_home, tmp_path, target
):
    knowledge_starter.begin(enso_home)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Personal.md").write_text("Existing personal notes.\n")
    enso_home.knowledge.parent.mkdir(parents=True)
    if target == "symlink":
        enso_home.knowledge.symlink_to(outside, target_is_directory=True)
    else:
        shutil.copytree(outside, enso_home.knowledge)
    before = contents(enso_home.knowledge)

    report = initialization.initialize_home(enso_home)

    assert report["ok"] == (target != "symlink")
    assert contents(enso_home.knowledge) == before
    assert contents(outside) == {Path("Personal.md"): b"Existing personal notes.\n"}


def test_busy_knowledge_writer_returns_retryable_init_failure(enso_home, tmp_path):
    paths = Paths(tmp_path / "fresh")
    with writer(paths):
        report = initialization.initialize_home(paths)
    assert not report["ok"] and "another knowledge write" in report["problems"][0]
    assert initialization.initialize_home(paths)["ok"]
    assert len(knowledge.scan(paths).notes) == len(knowledge_starter.NOTES)


def test_completed_receipt_prevents_reseeding_after_home_content_is_removed(enso_home, tmp_path):
    paths = Paths(tmp_path / "fresh")
    paths.runtime_dir.mkdir(parents=True)
    write_json(paths.runtime_dir / knowledge_starter.RECEIPT, {"version": 1, "state": "complete"})
    assert initialization.is_fresh_home(paths)

    assert initialization.initialize_home(paths)["ok"]

    assert list(paths.knowledge.iterdir()) == []


@pytest.mark.parametrize("value", ["{", "{}", '{"version": 1, "state": []}'])
def test_invalid_starter_receipt_fails_without_changing_it(enso_home, value):
    enso_home.runtime_dir.mkdir()
    receipt = enso_home.runtime_dir / knowledge_starter.RECEIPT
    receipt.write_text(value)

    report = initialization.initialize_home(enso_home)

    assert not report["ok"]
    assert receipt.read_text() == value
    assert not enso_home.knowledge.exists()


def test_starter_receipt_symlink_is_not_read_or_replaced(enso_home, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"version": 1, "state": "pending"}')
    enso_home.runtime_dir.mkdir()
    receipt = enso_home.runtime_dir / knowledge_starter.RECEIPT
    receipt.symlink_to(outside)

    report = initialization.initialize_home(enso_home)

    assert not report["ok"]
    assert receipt.is_symlink()
    assert outside.read_text() == '{"version": 1, "state": "pending"}'
    assert not enso_home.knowledge.exists()


def test_shared_parent_swapped_after_preflight_cannot_redirect_publication(
    enso_home, tmp_path, monkeypatch
):
    paths = Paths(tmp_path / "fresh")
    outside = tmp_path / "outside"
    outside.mkdir()
    original = knowledge_starter.begin

    def swap_parent(paths):
        original(paths)
        paths.knowledge.parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(knowledge_starter, "begin", swap_parent)

    report = initialization.initialize_home(paths)

    assert not report["ok"]
    assert list(outside.iterdir()) == []
    assert paths.knowledge.parent.is_symlink()
