"""Validated filesystem boundary for workspace instruction files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from enso.web import workspace_instructions as agents_fs


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    return root


def _write(path: Path, content: str = "instructions\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)
    return path


def test_discovery_is_bounded_and_prunes_generated_and_linked_trees(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write(root / "AGENTS.md", "root")
    _write(root / "src" / "AGENTS.md", "source")
    _write(root / "node_modules" / "pkg" / "AGENTS.md", "dependency")
    _write(root / ".git" / "AGENTS.md", "metadata")
    (root / "zz-unvisited").mkdir()
    outside = tmp_path / "outside"
    _write(outside / "AGENTS.md", "outside")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "linked-file-AGENTS.md").symlink_to(outside / "AGENTS.md")
    monkeypatch.setattr(agents_fs, "MAX_DISCOVERY_DIRECTORIES", 2)

    listing = agents_fs.discover_agents(str(root))

    assert [entry.rel_path for entry in listing.files] == ["AGENTS.md", "src/AGENTS.md"]
    assert listing.truncated is True
    assert all("node_modules" not in item.rel_path for item in listing.files)
    assert all(".git" not in item.rel_path for item in listing.files)


def test_discovery_enforces_depth_and_skips_unsafe_agent_files(tmp_path):
    root = _root(tmp_path)
    allowed = root.joinpath(*(f"d{i}" for i in range(agents_fs.MAX_DISCOVERY_DEPTH)))
    _write(allowed / "AGENTS.md", "at limit")
    _write(allowed / "deeper" / "AGENTS.md", "too deep")
    outside = _write(tmp_path / "secret", "secret")
    (root / "symlink" / "placeholder").parent.mkdir()
    (root / "symlink" / "AGENTS.md").symlink_to(outside)
    hardlink = root / "hardlink" / "AGENTS.md"
    hardlink.parent.mkdir()
    os.link(outside, hardlink)

    listing = agents_fs.discover_agents(str(root))

    assert [entry.rel_path for entry in listing.files] == [
        "/".join([*(f"d{i}" for i in range(agents_fs.MAX_DISCOVERY_DEPTH)), "AGENTS.md"])
    ]
    assert listing.truncated is True
    assert {error.rel_path for error in listing.errors} >= {
        "hardlink/AGENTS.md",
        "symlink/AGENTS.md",
    }


@pytest.mark.parametrize(
    "rel_path",
    [
        "",
        "../AGENTS.md",
        "/tmp/AGENTS.md",
        "a/../../AGENTS.md",
        "a\\AGENTS.md",
        "a//AGENTS.md",
        ".git/AGENTS.md",
        "node_modules/AGENTS.md",
        "README.md",
        "bad\0/AGENTS.md",
    ],
)
def test_read_rejects_unaddressable_paths(tmp_path, rel_path):
    root = _root(tmp_path)
    _write(root / "AGENTS.md")

    with pytest.raises(agents_fs.UnsafeAgentPath):
        agents_fs.read_agent(str(root), rel_path, 1024)


def test_read_rejects_symlinks_hardlinks_oversize_and_invalid_text(tmp_path):
    root = _root(tmp_path)
    outside = _write(tmp_path / "outside", "sentinel")
    (root / "AGENTS.md").symlink_to(outside)
    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.read_agent(str(root), "AGENTS.md", 1024)

    outside_directory = tmp_path / "outside-directory"
    _write(outside_directory / "AGENTS.md", "nested sentinel")
    (root / "linked-parent").symlink_to(outside_directory, target_is_directory=True)
    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.read_agent(str(root), "linked-parent/AGENTS.md", 1024)
    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.write_agent(str(root), "linked-parent/AGENTS.md", "changed", None, 1024)

    (root / "AGENTS.md").unlink()
    os.link(outside, root / "AGENTS.md")
    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.read_agent(str(root), "AGENTS.md", 1024)

    (root / "AGENTS.md").unlink()
    _write(root / "AGENTS.md", "12345")
    with pytest.raises(agents_fs.AgentTooLarge):
        agents_fs.read_agent(str(root), "AGENTS.md", 4)

    (root / "AGENTS.md").write_bytes(b"\xff")
    with pytest.raises(agents_fs.AgentEncodingError):
        agents_fs.read_agent(str(root), "AGENTS.md", 4)

    (root / "AGENTS.md").write_bytes(b"a\0b")
    with pytest.raises(agents_fs.AgentEncodingError):
        agents_fs.read_agent(str(root), "AGENTS.md", 4)

    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert (outside_directory / "AGENTS.md").read_text(encoding="utf-8") == ("nested sentinel")


def test_replaced_workspace_root_symlink_is_never_followed(tmp_path):
    configured = _root(tmp_path)
    _write(configured / "AGENTS.md", "configured")
    outside = tmp_path / "outside-root"
    _write(outside / "AGENTS.md", "sentinel")
    configured.rename(tmp_path / "detached-workspace")
    configured.symlink_to(outside, target_is_directory=True)

    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.read_agent(str(configured), "AGENTS.md", 1024)
    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.discover_agents(str(configured))
    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.write_agent(str(configured), "AGENTS.md", "changed", None, 1024)

    assert (outside / "AGENTS.md").read_text(encoding="utf-8") == "sentinel"


def test_read_returns_frozen_document_and_content_revision(tmp_path):
    root = _root(tmp_path)
    _write(root / "AGENTS.md", "hello\n")

    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)

    assert document.content == "hello\n"
    assert document.rel_path == "AGENTS.md"
    assert len(document.revision) == 64
    assert document.mode == 0o644
    with pytest.raises(AttributeError):
        document.content = "changed"  # type: ignore[misc]


def test_write_is_revision_checked_atomic_and_mode_preserving(tmp_path):
    root = _root(tmp_path)
    target = _write(root / "nested" / "AGENTS.md", "old\n")
    target.chmod(0o640)
    original = agents_fs.read_agent(str(root), "nested/AGENTS.md", 1024)

    revision = agents_fs.write_agent(
        str(root),
        "nested/AGENTS.md",
        "new\r\n",
        original.revision,
        1024,
    )

    assert target.read_text(encoding="utf-8") == "new\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert revision == agents_fs.read_agent(str(root), "nested/AGENTS.md", 1024).revision
    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(
            str(root),
            "nested/AGENTS.md",
            "stale",
            original.revision,
            1024,
        )
    assert target.read_text(encoding="utf-8") == "new\n"
    assert not list((root / "nested").glob(".enso-agents-*.tmp"))


def test_write_detects_target_replacement_during_staging(tmp_path, monkeypatch):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)
    replacement = root / "replacement"
    real_write_all = agents_fs._write_all
    swapped = False

    def replace_target(fd: int, content: bytes) -> None:
        nonlocal swapped
        real_write_all(fd, content)
        if not swapped:
            replacement.write_text("racer", encoding="utf-8")
            replacement.replace(target)
            swapped = True

    monkeypatch.setattr(agents_fs, "_write_all", replace_target)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(str(root), "AGENTS.md", "web edit", document.revision, 1024)

    assert target.read_text(encoding="utf-8") == "racer"
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_failed_staging_removes_its_temporary_file(tmp_path, monkeypatch):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)

    def fail_write(_fd: int, _content: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(agents_fs, "_write_all", fail_write)

    with pytest.raises(agents_fs.AgentFilesystemError):
        agents_fs.write_agent(str(root), "AGENTS.md", "replacement", document.revision, 1024)

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_write_creates_only_a_missing_root_agent_file(tmp_path):
    root = _root(tmp_path)

    revision = agents_fs.write_agent(
        str(root), "AGENTS.md", "created\r\n", None, 1024, allow_create_root=True
    )

    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "created\n"
    assert (root / "AGENTS.md").stat().st_mode & 0o777 == 0o644
    assert (root / "AGENTS.md").stat().st_nlink == 1
    assert revision == agents_fs.read_agent(str(root), "AGENTS.md", 1024).revision
    assert not (root / "CLAUDE.md").exists()
    assert not list(root.glob(".enso-agents-*.tmp"))

    with pytest.raises(agents_fs.AgentNotFound):
        agents_fs.write_agent(
            str(root),
            "child/AGENTS.md",
            "created",
            None,
            1024,
            allow_create_root=True,
        )


def test_missing_root_create_requires_no_expected_revision(tmp_path):
    root = _root(tmp_path)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(
            str(root),
            "AGENTS.md",
            "created",
            "a" * 64,
            1024,
            allow_create_root=True,
        )

    assert not (root / "AGENTS.md").exists()


def test_missing_root_create_never_clobbers_a_racing_target(tmp_path, monkeypatch):
    root = _root(tmp_path)
    real_write_all = agents_fs._write_all
    raced = False

    def create_target(fd: int, content: bytes) -> None:
        nonlocal raced
        real_write_all(fd, content)
        if not raced:
            _write(root / "AGENTS.md", "racer")
            raced = True

    monkeypatch.setattr(agents_fs, "_write_all", create_target)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(
            str(root),
            "AGENTS.md",
            "created",
            None,
            1024,
            allow_create_root=True,
        )

    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "racer"
    assert not list(root.glob(".enso-agents-*.tmp"))


@pytest.mark.parametrize("revision", ["é" * 64, "g" * 64, "a" * 63, 123])
def test_write_rejects_malformed_expected_revisions_without_type_errors(tmp_path, revision):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(
            str(root),
            "AGENTS.md",
            "web edit",
            revision,
            1024,  # type: ignore[arg-type]
        )

    assert target.read_text(encoding="utf-8") == "old"


def test_write_accepts_an_uppercase_hex_revision(tmp_path):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)

    agents_fs.write_agent(str(root), "AGENTS.md", "new", document.revision.upper(), 1024)

    assert target.read_text(encoding="utf-8") == "new"


def test_workspace_and_child_directories_must_be_owner_protected(tmp_path):
    root = _root(tmp_path)
    _write(root / "child" / "AGENTS.md")
    (root / "child").chmod(0o777)

    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.read_agent(str(root), "child/AGENTS.md", 1024)

    root.chmod(0o777)
    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.discover_agents(str(root))
