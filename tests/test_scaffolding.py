"""Canonical Enso root and managed-workspace scaffolding."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import enso.scaffolding as scaffolding_module
from enso.config import validate_workspace_name
from enso.scaffolding import (
    LinkState,
    ScaffoldError,
    ScaffoldService,
)


@pytest.mark.parametrize("name", ["default", "client-2", "2fa", "a1-b2-c3"])
def test_workspace_names_are_lowercase_kebab_case(name, tmp_path):
    assert validate_workspace_name(name) == name
    assert ScaffoldService(tmp_path).workspace_path(name) == tmp_path / "workspaces" / name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Client",
        "two words",
        "two_words",
        "-client",
        "client-",
        "client--ops",
        ".",
        "..",
        "client/ops",
        "a" * 65,
    ],
)
def test_invalid_workspace_names_fail_before_path_derivation(name, tmp_path):
    with pytest.raises(ScaffoldError, match="lowercase kebab-case"):
        ScaffoldService(tmp_path).workspace_path(name)


def test_fresh_global_seed_creates_canonical_relative_discovery_tree(tmp_path):
    root = tmp_path / "enso"

    report = ScaffoldService(root).seed_fresh_global()

    assert (root / "AGENTS.md").is_file()
    assert (root / "skills").is_dir()
    assert (root / "docs").is_dir()
    assert (root / "jobs").is_dir()
    assert (root / "workspaces").is_dir()
    assert os.readlink(root / "CLAUDE.md") == "AGENTS.md"
    assert os.readlink(root / ".agents" / "skills") == "../skills"
    assert os.readlink(root / ".claude" / "skills") == "../skills"
    assert {entry.name for entry in (root / "skills").iterdir()} == {
        "docs",
        "jobs",
        "policy",
        "slack",
        "tables",
        "workspace",
    }
    assert not report.warnings


def test_bundled_content_is_only_copied_by_explicit_fresh_seed(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)

    report = service.repair_global()

    assert (root / "skills").is_dir()
    assert list((root / "skills").iterdir()) == []
    assert not (root / "AGENTS.md").exists()
    assert any("AGENTS.md" in warning for warning in report.warnings)


def test_fresh_starter_docs_seed_is_exclusive_and_idempotent(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.repair_global()

    first = service.seed_fresh_starter_docs()
    expected = {
        root / "docs" / "enso" / "content_model.md",
        root / "docs" / "enso" / "layout.md",
        root / "docs" / "operator.md",
    }

    assert expected <= set(first.created)
    original = {path: path.read_bytes() for path in expected}

    second = service.seed_fresh_starter_docs()

    assert not second.created
    assert {path: path.read_bytes() for path in expected} == original


def test_fresh_starter_docs_preserve_existing_user_owned_content(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.repair_global()
    customized = root / "docs" / "enso" / "content_model.md"
    customized.parent.mkdir()
    customized.write_text("operator content\n", encoding="utf-8")

    report = service.seed_fresh_starter_docs()

    assert customized.read_text(encoding="utf-8") == "operator content\n"
    assert customized not in set(report.created)
    assert (root / "docs" / "enso" / "layout.md").is_file()
    assert (root / "docs" / "operator.md").is_file()


def test_fresh_starter_docs_refuse_a_symlink_without_touching_its_target(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.repair_global()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    collision = root / "docs" / "operator.md"
    collision.symlink_to(outside)

    with pytest.raises(ScaffoldError, match=r"operator\.md must be a regular file"):
        service.seed_fresh_starter_docs()

    assert collision.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_fresh_starter_docs_refuse_a_symlinked_parent(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.repair_global()
    outside = tmp_path / "outside-docs"
    outside.mkdir()
    (root / "docs" / "enso").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ScaffoldError, match=r"physical directory"):
        service.seed_fresh_starter_docs()

    assert list(outside.iterdir()) == []


def test_interrupted_starter_doc_write_leaves_no_partial_file_and_reruns(
    tmp_path, monkeypatch
):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.repair_global()
    real_fsync = scaffolding_module.os.fsync

    def fail_fsync(_descriptor):
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(scaffolding_module.os, "fsync", fail_fsync)
    with pytest.raises(ScaffoldError, match="interrupted write"):
        service.seed_fresh_starter_docs()

    assert not (root / "docs" / "enso" / "content_model.md").exists()

    monkeypatch.setattr(scaffolding_module.os, "fsync", real_fsync)
    service.seed_fresh_starter_docs()

    assert (root / "docs" / "enso" / "content_model.md").is_file()
    assert (root / "docs" / "enso" / "layout.md").is_file()
    assert (root / "docs" / "operator.md").is_file()


def test_workspace_instructions_require_the_name_placeholder():
    class _BrokenTemplate:
        def read_text(self, encoding=None):
            return "# Broken template\n"

    with pytest.raises(ScaffoldError, match=r"\{\{workspace_name\}\} placeholder"):
        ScaffoldService._workspace_instructions(_BrokenTemplate(), "client-a")


def test_global_repair_never_seeds_starter_docs(tmp_path):
    root = tmp_path / "enso"

    ScaffoldService(root).repair_global()

    assert list((root / "docs").iterdir()) == []


def test_fresh_seed_preserves_existing_user_owned_content(tmp_path):
    root = tmp_path / "enso"
    root.mkdir()
    agents = root / "AGENTS.md"
    agents.write_text("# My instructions\n", encoding="utf-8")
    skill = root / "skills" / "jobs"
    skill.mkdir(parents=True)
    skill_file = skill / "SKILL.md"
    skill_file.write_text("my jobs workflow\n", encoding="utf-8")

    ScaffoldService(root).seed_fresh_global()

    assert agents.read_text(encoding="utf-8") == "# My instructions\n"
    assert skill_file.read_text(encoding="utf-8") == "my jobs workflow\n"
    assert (root / "skills" / "docs" / "SKILL.md").is_file()


def test_bundled_tree_copy_is_recursive(tmp_path):
    source = tmp_path / "source"
    nested = source / "workspace" / "references"
    nested.mkdir(parents=True)
    (nested / "layout.md").write_text("layout\n", encoding="utf-8")
    destination = tmp_path / "destination"
    created = []

    ScaffoldService(tmp_path / "enso")._seed_resource_tree(
        source,
        destination,
        created,
    )

    assert (destination / "workspace" / "references" / "layout.md").read_text() == (
        "layout\n"
    )


def test_fresh_seed_refuses_non_regular_content_collision(tmp_path):
    root = tmp_path / "enso"
    target = tmp_path / "outside.md"
    target.write_text("outside\n", encoding="utf-8")
    root.mkdir()
    (root / "AGENTS.md").symlink_to(target)

    with pytest.raises(ScaffoldError, match=r"AGENTS.md.*regular file"):
        ScaffoldService(root).seed_fresh_global()

    assert target.read_text(encoding="utf-8") == "outside\n"


def test_managed_link_preserves_unknown_path_and_reports_warning(tmp_path):
    root = tmp_path / "enso"
    root.mkdir()
    conflict = root / "CLAUDE.md"
    conflict.write_text("custom\n", encoding="utf-8")

    report = ScaffoldService(root).repair_global()

    result = next(link for link in report.links if link.path == conflict)
    assert result.state is LinkState.CONFLICT
    assert result.warning is not None
    assert conflict.read_text(encoding="utf-8") == "custom\n"


def test_correct_managed_links_are_idempotent(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.seed_fresh_global()

    report = service.repair_global()

    assert all(link.state is LinkState.CORRECT for link in report.links)
    assert not report.created


@pytest.mark.parametrize("component", ["root", "workspaces"])
def test_global_scaffold_rejects_symlinked_managed_directories(tmp_path, component):
    root = tmp_path / "enso"
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "root":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / "workspaces").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ScaffoldError, match="physical directory"):
        ScaffoldService(root).repair_global()


def test_create_workspace_builds_complete_tree_with_local_routing_content(tmp_path):
    root = tmp_path / "enso"

    report = ScaffoldService(root).create_workspace("client-ops")
    workspace = root / "workspaces" / "client-ops"

    assert workspace == report.workspace
    assert workspace.is_dir() and not workspace.is_symlink()
    assert not (workspace / ".git").exists()
    assert (workspace / "skills").is_dir()
    assert list((workspace / "skills").iterdir()) == []
    for directory in ("knowledge", "drafts", "uploads", ".agents", ".claude"):
        assert (workspace / directory).is_dir()
    assert os.readlink(workspace / "CLAUDE.md") == "AGENTS.md"
    assert os.readlink(workspace / ".agents" / "skills") == "../skills"
    assert os.readlink(workspace / ".claude" / "skills") == "../skills"

    instructions = (workspace / "AGENTS.md").read_text(encoding="utf-8")
    assert "client-ops" in instructions
    assert "purpose" in instructions.lower()
    assert "scope" in instructions.lower()
    assert "critical approval" in instructions.lower()
    assert "knowledge/README.md" in instructions

    knowledge = (workspace / "knowledge" / "README.md").read_text(encoding="utf-8")
    assert "authoritative" in knowledge.lower()
    assert "link" in knowledge.lower()
    assert "when to read" in knowledge.lower()
    assert all(part in knowledge.lower() for part in ("added", "moved", "removed"))


def test_workspace_is_complete_before_atomic_publish(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    real_publish = scaffolding_module._publish_exclusive
    observed = {}

    def inspect_then_publish(source, destination):
        staged = Path(source)
        observed["destination_absent"] = not os.path.lexists(destination)
        observed["tree_complete"] = all(
            (staged / path).exists() or (staged / path).is_symlink()
            for path in (
                "AGENTS.md",
                "CLAUDE.md",
                "skills",
                ".agents/skills",
                ".claude/skills",
                "knowledge/README.md",
                "drafts",
                "uploads",
            )
        )
        real_publish(source, destination)

    monkeypatch.setattr(scaffolding_module, "_publish_exclusive", inspect_then_publish)

    ScaffoldService(root).create_workspace("atomic")

    assert observed == {"destination_absent": True, "tree_complete": True}
    assert not list((root / "workspaces").glob(".atomic.tmp-*"))


def test_workspace_publish_failure_removes_only_its_staging_directory(
    tmp_path, monkeypatch
):
    root = tmp_path / "enso"

    def fail_publish(_source, _destination):
        raise OSError("publish failed")

    monkeypatch.setattr(scaffolding_module, "_publish_exclusive", fail_publish)

    with pytest.raises(ScaffoldError, match=r"publish.*atomic"):
        ScaffoldService(root).create_workspace("atomic")

    assert not (root / "workspaces" / "atomic").exists()
    assert not list((root / "workspaces").glob(".atomic.tmp-*"))


def test_atomic_publish_refuses_destination_created_during_publish(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    real_publish = scaffolding_module._publish_exclusive

    def race_publish(source, destination):
        Path(destination).mkdir()
        real_publish(source, destination)

    monkeypatch.setattr(scaffolding_module, "_publish_exclusive", race_publish)

    with pytest.raises(ScaffoldError, match="could not publish"):
        ScaffoldService(root).create_workspace("atomic")

    destination = root / "workspaces" / "atomic"
    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    assert not list((root / "workspaces").glob(".atomic.tmp-*"))


def test_create_workspace_refuses_existing_destination_without_merging(tmp_path):
    root = tmp_path / "enso"
    destination = root / "workspaces" / "client"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")

    with pytest.raises(ScaffoldError, match="already exists"):
        ScaffoldService(root).create_workspace("client")

    assert marker.read_text(encoding="utf-8") == "keep\n"
    assert list(destination.iterdir()) == [marker]


def test_repair_workspace_restores_structure_but_not_seeded_content(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.create_workspace("client")
    workspace = root / "workspaces" / "client"
    (workspace / "AGENTS.md").unlink()
    (workspace / "CLAUDE.md").unlink()
    (workspace / "knowledge" / "README.md").unlink()
    (workspace / "drafts").rmdir()

    report = service.repair_workspace("client")

    assert (workspace / "drafts").is_dir()
    assert not (workspace / "AGENTS.md").exists()
    assert not (workspace / "CLAUDE.md").exists()
    assert not (workspace / "knowledge" / "README.md").exists()
    assert any("AGENTS.md" in warning for warning in report.warnings)
    assert any("knowledge/README.md" in warning for warning in report.warnings)


def test_repair_workspace_preserves_unknown_discovery_link(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.create_workspace("client")
    workspace = root / "workspaces" / "client"
    discovery = workspace / ".agents" / "skills"
    discovery.unlink()
    discovery.symlink_to("../../other-skills")

    report = service.repair_workspace("client")

    assert os.readlink(discovery) == "../../other-skills"
    assert any(link.state is LinkState.CONFLICT for link in report.links)
    assert any("../../other-skills" in warning for warning in report.warnings)


def test_repair_rejects_symlinked_workspace_root(tmp_path):
    root = tmp_path / "enso"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "workspaces").mkdir(parents=True)
    (root / "workspaces" / "client").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ScaffoldError, match="physical directory"):
        ScaffoldService(root).repair_workspace("client")


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_workspace_root_git_entry_is_rejected_but_deeper_repo_is_allowed(tmp_path, kind):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.create_workspace("client")
    workspace = root / "workspaces" / "client"
    nested = workspace / "project" / ".git"
    nested.mkdir(parents=True)

    assert service.validate_workspace("client").valid

    git_entry = workspace / ".git"
    if kind == "file":
        git_entry.write_text("gitdir: elsewhere\n", encoding="utf-8")
    elif kind == "directory":
        git_entry.mkdir()
    else:
        git_entry.symlink_to(nested, target_is_directory=True)

    report = service.validate_workspace("client")

    assert not report.valid
    assert any(".git" in error for error in report.errors)
    with pytest.raises(ScaffoldError, match=r"\.git"):
        service.repair_workspace("client")


def test_duplicate_root_and_workspace_skill_names_are_invalid(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.seed_fresh_global()
    service.create_workspace("client")
    duplicate = root / "workspaces" / "client" / "skills" / "jobs"
    duplicate.mkdir()
    (duplicate / "SKILL.md").write_text("# Workspace jobs\n", encoding="utf-8")

    assert service.duplicate_skill_names("client") == ("jobs",)
    report = service.validate_workspace("client")
    assert not report.valid
    assert any("jobs" in error and "duplicate" in error for error in report.errors)


def test_workspace_with_unique_skill_names_is_valid(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.seed_fresh_global()
    service.create_workspace("client")
    skill = root / "workspaces" / "client" / "skills" / "client-release"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Client release\n", encoding="utf-8")

    report = service.validate_workspace("client")

    assert report.valid
    assert not report.errors


def test_global_validation_is_read_only_and_reports_discovery_errors(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.seed_fresh_global()

    assert service.validate_global().valid
    discovery = root / ".agents" / "skills"
    discovery.unlink()
    discovery.symlink_to("../../wrong")
    report = service.validate_global()

    assert not report.valid
    assert any("expected relative target" in error for error in report.errors)
    assert os.readlink(discovery) == "../../wrong"


def test_workspace_validation_reports_missing_discovery_link_without_repair(tmp_path):
    root = tmp_path / "enso"
    service = ScaffoldService(root)
    service.seed_fresh_global()
    service.create_workspace("client")
    discovery = root / "workspaces" / "client" / ".claude" / "skills"
    discovery.unlink()

    report = service.validate_workspace("client")

    assert not report.valid
    assert any(str(discovery) in error for error in report.errors)
    assert not os.path.lexists(discovery)


def test_scaffolding_does_not_discover_or_migrate_legacy_workspace(tmp_path):
    root = tmp_path / "enso"
    legacy = tmp_path / "legacy-workspace"
    legacy.mkdir()
    marker = legacy / "AGENTS.md"
    marker.write_text("legacy\n", encoding="utf-8")

    workspace = ScaffoldService(root).create_workspace("default").workspace

    assert workspace == root / "workspaces" / "default"
    assert marker.read_text(encoding="utf-8") == "legacy\n"
