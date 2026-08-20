"""Tests for the canonical prelaunch discovery boundary."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from enso import config, instructions
from enso.instructions import (
    InstructionError,
    validate_launch_discovery,
    validate_shared_instructions,
)
from enso.repository import EnsoRepository
from enso.scaffolding import ScaffoldService
from enso.teams import Workspace


@pytest.fixture
def instruction_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "enso"
    config_dir.mkdir(mode=0o700)
    monkeypatch.setattr(config, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(instructions, "CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture
def launch_workspace(
    instruction_home: Path,
) -> tuple[Path, Workspace]:
    scaffold = ScaffoldService(instruction_home)
    scaffold.seed_fresh_global()
    scaffold.create_workspace("default")
    EnsoRepository(str(instruction_home)).ensure()
    workspace_path = instruction_home / "workspaces" / "default"
    return instruction_home, Workspace("default", str(workspace_path), "admin", 1)


def _write_source(config_dir: Path, content: str = "# Shared\n\nBe helpful.\n") -> Path:
    source = config_dir / "AGENTS.md"
    source.write_text(content, encoding="utf-8")
    source.chmod(0o600)
    return source


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", "--template="],
        cwd=path,
        check=True,
    )


def _assert_chmod_rejected(
    path: Path,
    workspace: Workspace,
    *,
    match: str,
) -> None:
    original_mode = path.stat().st_mode & 0o777
    path.chmod(0)
    try:
        with pytest.raises(InstructionError, match=match):
            validate_launch_discovery(workspace)
    finally:
        path.chmod(original_mode)


def test_validate_launch_discovery_returns_fresh_shared_instructions(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace

    first = validate_launch_discovery(workspace)
    (root / "AGENTS.md").write_text("# Revised\n", encoding="utf-8")
    second = validate_launch_discovery(workspace)

    assert first.source_path == str(root / "AGENTS.md")
    assert first.content != second.content
    assert first.revision != second.revision
    assert second.content == "# Revised\n"
    assert not (root / "runtime" / "instructions").exists()


def test_validate_launch_discovery_rejects_non_derived_workspace_path(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    wrong = dataclasses.replace(
        workspace,
        path=str(root / "workspaces" / ".." / "workspaces" / "default"),
    )

    with pytest.raises(InstructionError, match="exact name-derived path"):
        validate_launch_discovery(wrong)


def test_validate_launch_discovery_rejects_invalid_workspace_name(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    _root, workspace = launch_workspace
    invalid = dataclasses.replace(workspace, name="Not-Canonical")

    with pytest.raises(InstructionError, match="workspace names must be"):
        validate_launch_discovery(invalid)


def test_validate_launch_discovery_rejects_symlinked_config_root(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    physical = root.with_name("enso-physical")
    root.rename(physical)
    root.symlink_to(physical, target_is_directory=True)

    with pytest.raises(InstructionError, match="physical directory"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_symlinked_workspaces_root(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    workspaces = root / "workspaces"
    physical = root / "physical-workspaces"
    workspaces.rename(physical)
    workspaces.symlink_to(physical, target_is_directory=True)

    with pytest.raises(InstructionError, match="physical directory"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_symlinked_workspace(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    _root, workspace = launch_workspace
    workspace_path = Path(workspace.path)
    physical = workspace_path.with_name("default-physical")
    workspace_path.rename(physical)
    workspace_path.symlink_to(physical, target_is_directory=True)

    with pytest.raises(InstructionError, match="physical directory"):
        validate_launch_discovery(workspace)


@pytest.mark.parametrize("entry_kind", ["directory", "file", "symlink", "dangling"])
def test_validate_launch_discovery_rejects_any_direct_workspace_git_entry(
    launch_workspace: tuple[Path, Workspace],
    entry_kind: str,
) -> None:
    _root, workspace = launch_workspace
    git_entry = Path(workspace.path) / ".git"
    if entry_kind == "directory":
        git_entry.mkdir()
    elif entry_kind == "file":
        git_entry.write_text("gitdir: elsewhere\n", encoding="utf-8")
    elif entry_kind == "symlink":
        target = Path(workspace.path) / "git-target"
        target.mkdir()
        git_entry.symlink_to(target, target_is_directory=True)
    else:
        git_entry.symlink_to("missing-git-target")

    with pytest.raises(InstructionError, match=r"forbidden \.git entry"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_allows_deeper_repository(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    _root, workspace = launch_workspace
    nested = Path(workspace.path) / "project"
    nested.mkdir()
    _git_init(nested)

    assert validate_launch_discovery(workspace).content.startswith("# Enso")


def test_validate_launch_discovery_rejects_missing_root_repository(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    shutil.rmtree(root / ".git")

    with pytest.raises(InstructionError, match=r"missing its required \.git entry"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_corrupt_root_repository(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    shutil.rmtree(root / ".git")
    (root / ".git").write_text("not a gitfile\n", encoding="utf-8")

    with pytest.raises(InstructionError, match="valid Git repository"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_outer_repository_as_root(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    shutil.rmtree(root / ".git")
    _git_init(root.parent)

    with pytest.raises(InstructionError, match="outer Git repository"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_invalid_global_scaffold(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    (root / "CLAUDE.md").unlink()

    with pytest.raises(InstructionError, match=r"CLAUDE\.md"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_invalid_workspace_scaffold(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    _root, workspace = launch_workspace
    (Path(workspace.path) / ".agents" / "skills").unlink()

    with pytest.raises(InstructionError, match=r"\.agents/skills"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_unreadable_workspace_instructions(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    _root, workspace = launch_workspace
    _assert_chmod_rejected(
        Path(workspace.path) / "AGENTS.md",
        workspace,
        match="instruction source is not readable",
    )


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_validate_launch_discovery_rejects_inaccessible_skill_scope(
    launch_workspace: tuple[Path, Workspace],
    scope: str,
) -> None:
    root, workspace = launch_workspace
    skills = (
        root / "skills" if scope == "global" else Path(workspace.path) / "skills"
    )
    _assert_chmod_rejected(
        skills,
        workspace,
        match="skill directory is not readable and searchable",
    )


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_validate_launch_discovery_rejects_inaccessible_skill_directory(
    launch_workspace: tuple[Path, Workspace],
    scope: str,
) -> None:
    root, workspace = launch_workspace
    if scope == "global":
        skill = root / "skills" / "docs"
    else:
        skill = Path(workspace.path) / "skills" / "local"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# Local\n", encoding="utf-8")
    _assert_chmod_rejected(
        skill,
        workspace,
        match="skill directory is not readable and searchable",
    )


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_validate_launch_discovery_rejects_unreadable_skill_definition(
    launch_workspace: tuple[Path, Workspace],
    scope: str,
) -> None:
    root, workspace = launch_workspace
    if scope == "global":
        skill = root / "skills" / "docs"
    else:
        skill = Path(workspace.path) / "skills" / "local"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# Local\n", encoding="utf-8")
    _assert_chmod_rejected(
        skill / "SKILL.md",
        workspace,
        match="skill definition is not readable",
    )


def test_validate_launch_discovery_rejects_skill_directory_symlink(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    external = root / "external-skill"
    external.mkdir()
    (external / "SKILL.md").write_text("# External\n", encoding="utf-8")
    (Path(workspace.path) / "skills" / "linked").symlink_to(
        external,
        target_is_directory=True,
    )

    with pytest.raises(InstructionError, match="skill must be a physical directory"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_missing_skill_definition(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    _root, workspace = launch_workspace
    (Path(workspace.path) / "skills" / "incomplete").mkdir()

    with pytest.raises(InstructionError, match="skill definition is missing"):
        validate_launch_discovery(workspace)


def test_validate_launch_discovery_rejects_duplicate_skill_names(
    launch_workspace: tuple[Path, Workspace],
) -> None:
    root, workspace = launch_workspace
    global_skill = root / "skills" / "shared-name"
    workspace_skill = Path(workspace.path) / "skills" / "shared-name"
    global_skill.mkdir()
    workspace_skill.mkdir()
    (global_skill / "SKILL.md").write_text("# Shared\n", encoding="utf-8")
    (workspace_skill / "SKILL.md").write_text("# Shared\n", encoding="utf-8")

    with pytest.raises(InstructionError, match=r"duplicate.*shared-name"):
        validate_launch_discovery(workspace)


def test_validate_shared_instructions_is_immutable(instruction_home: Path) -> None:
    _write_source(instruction_home)
    validated = validate_shared_instructions()

    with pytest.raises(dataclasses.FrozenInstanceError):
        validated.content = "changed"  # type: ignore[misc]


def test_validate_shared_instructions_allows_owner_owned_readable_source(
    instruction_home: Path,
) -> None:
    source = _write_source(instruction_home)
    source.chmod(0o644)

    assert validate_shared_instructions().content == "# Shared\n\nBe helpful.\n"


def test_validate_shared_instructions_rejects_missing_file(
    instruction_home: Path,
) -> None:
    with pytest.raises(InstructionError, match="shared instruction file is missing"):
        validate_shared_instructions()


def test_validate_shared_instructions_rejects_symlink(instruction_home: Path) -> None:
    target = instruction_home / "elsewhere.md"
    target.write_text("untrusted", encoding="utf-8")
    (instruction_home / "AGENTS.md").symlink_to(target)

    with pytest.raises(InstructionError, match="regular, non-symlink file"):
        validate_shared_instructions()


def test_validate_shared_instructions_rejects_oversized_file(
    instruction_home: Path,
) -> None:
    source = instruction_home / "AGENTS.md"
    source.write_bytes(b"x" * (instructions.MAX_SHARED_INSTRUCTION_BYTES + 1))

    with pytest.raises(InstructionError, match="exceeds the 20480-byte limit"):
        validate_shared_instructions()


def test_validate_shared_instructions_accepts_exact_size_limit(
    instruction_home: Path,
) -> None:
    source = instruction_home / "AGENTS.md"
    source.write_bytes(b"x" * instructions.MAX_SHARED_INSTRUCTION_BYTES)
    source.chmod(0o600)

    validated = validate_shared_instructions()

    assert len(validated.content.encode("utf-8")) == instructions.MAX_SHARED_INSTRUCTION_BYTES


def test_validate_shared_instructions_rejects_invalid_utf8(
    instruction_home: Path,
) -> None:
    (instruction_home / "AGENTS.md").write_bytes(b"valid\n\xff")

    with pytest.raises(InstructionError, match="valid UTF-8"):
        validate_shared_instructions()


def test_validate_shared_instructions_rejects_nul(instruction_home: Path) -> None:
    (instruction_home / "AGENTS.md").write_bytes(b"before\x00after")

    with pytest.raises(InstructionError, match="NUL bytes"):
        validate_shared_instructions()


def test_validate_shared_instructions_rejects_wrong_owner(
    instruction_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_source(instruction_home)
    current_uid = os.getuid()
    monkeypatch.setattr(instructions.os, "getuid", lambda: current_uid + 1)

    with pytest.raises(InstructionError, match="owned by the current user"):
        validate_shared_instructions()


def test_validate_shared_instructions_rejects_group_writable_source(
    instruction_home: Path,
) -> None:
    source = _write_source(instruction_home)
    source.chmod(0o620)

    with pytest.raises(InstructionError, match="must not be group- or other-writable"):
        validate_shared_instructions()


def test_validate_shared_instructions_rejects_hard_linked_source(
    instruction_home: Path,
) -> None:
    source = _write_source(instruction_home)
    os.link(source, instruction_home / "AGENTS-alias.md")

    with pytest.raises(InstructionError, match="must not have additional hard links"):
        validate_shared_instructions()


def test_validate_shared_instructions_detects_mutation_during_read(
    instruction_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write_source(instruction_home, "initial contents")
    real_read = instructions.os.read
    mutated = False

    def mutate_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if not mutated:
            mutated = True
            source.write_text("changed contents", encoding="utf-8")
        return chunk

    monkeypatch.setattr(instructions.os, "read", mutate_after_first_read)

    with pytest.raises(InstructionError, match="changed while it was being read"):
        validate_shared_instructions()
