"""Secure filesystem boundary for workspace instruction files."""

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


def test_write_detects_target_replacement_in_the_publication_window(tmp_path, monkeypatch):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)
    racer = _write(root / "racer", "racer")

    def replace_target(_parent_fd: int, _staged: str, _target: str) -> None:
        racer.replace(target)

    monkeypatch.setattr(agents_fs, "_before_publication", replace_target)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(str(root), "AGENTS.md", "web edit", document.revision, 1024)

    assert target.read_text(encoding="utf-8") == "racer"
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_write_detects_staged_name_replacement_in_the_publication_window(tmp_path, monkeypatch):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)
    outside = _write(tmp_path / "outside-stage", "sentinel")
    stolen = root / "stolen-stage"

    def replace_stage(parent_fd: int, staged: str, _target: str) -> None:
        os.rename(staged, stolen.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.symlink(str(outside), staged, dir_fd=parent_fd)

    monkeypatch.setattr(agents_fs, "_before_publication", replace_stage)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(str(root), "AGENTS.md", "web edit", document.revision, 1024)

    assert target.read_text(encoding="utf-8") == "old"
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert stolen.read_bytes() == b""
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_write_rolls_back_an_in_place_change_after_exchange(tmp_path, monkeypatch):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)
    real_exchange = agents_fs._exchange_names
    changed = False

    def exchange_then_change(parent_fd: int, first: str, second: str) -> None:
        nonlocal changed
        real_exchange(parent_fd, first, second)
        if not changed:
            descriptor = os.open(
                second,
                os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                os.write(descriptor, b"bad edit")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            changed = True

    monkeypatch.setattr(agents_fs, "_exchange_names", exchange_then_change)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(str(root), "AGENTS.md", "web edit", document.revision, 1024)

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_write_restores_old_content_when_displaced_name_disappears_after_exchange(
    tmp_path, monkeypatch
):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)
    real_exchange = agents_fs._exchange_names
    exchange_count = 0

    def exchange_then_remove(parent_fd: int, first: str, second: str) -> None:
        nonlocal exchange_count
        real_exchange(parent_fd, first, second)
        exchange_count += 1
        if exchange_count == 1:
            os.unlink(first, dir_fd=parent_fd)

    monkeypatch.setattr(agents_fs, "_exchange_names", exchange_then_remove)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(str(root), "AGENTS.md", "new", document.revision, 1024)

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_write_restores_old_content_without_clobbering_a_replaced_displaced_name(
    tmp_path, monkeypatch
):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)
    racer = _write(root / "racer", "racer")
    detached_old = root / "detached-old"
    real_exchange = agents_fs._exchange_names
    exchange_count = 0
    replaced_name: str | None = None

    def exchange_then_replace(parent_fd: int, first: str, second: str) -> None:
        nonlocal exchange_count, replaced_name
        real_exchange(parent_fd, first, second)
        exchange_count += 1
        if exchange_count == 1:
            replaced_name = first
            os.rename(first, detached_old.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.rename(racer.name, first, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)

    monkeypatch.setattr(agents_fs, "_exchange_names", exchange_then_replace)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(str(root), "AGENTS.md", "new", document.revision, 1024)

    assert replaced_name is not None
    assert target.read_text(encoding="utf-8") == "old"
    assert detached_old.read_text(encoding="utf-8") == "old"
    assert (root / replaced_name).read_text(encoding="utf-8") == "racer"


def test_write_rejects_a_workspace_root_moved_during_publication(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)
    detached = tmp_path / "detached"

    def move_root(_parent_fd: int, _staged: str, _target: str) -> None:
        root.rename(detached)
        root.mkdir(mode=0o700)
        _write(root / "AGENTS.md", "outside sentinel")

    monkeypatch.setattr(agents_fs, "_before_publication", move_root)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(str(root), "AGENTS.md", "web edit", document.revision, 1024)

    assert (detached / "AGENTS.md").read_text(encoding="utf-8") == "old"
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "outside sentinel"


def test_write_rejects_a_nested_parent_moved_during_publication(tmp_path, monkeypatch):
    root = _root(tmp_path)
    parent = root / "child"
    _write(parent / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "child/AGENTS.md", 1024)
    detached = root / "detached-child"

    def move_parent(_parent_fd: int, _staged: str, _target: str) -> None:
        parent.rename(detached)
        parent.mkdir(mode=0o700)
        _write(parent / "AGENTS.md", "outside sentinel")

    monkeypatch.setattr(agents_fs, "_before_publication", move_parent)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(str(root), "child/AGENTS.md", "web edit", document.revision, 1024)

    assert (detached / "AGENTS.md").read_text(encoding="utf-8") == "old"
    assert (parent / "AGENTS.md").read_text(encoding="utf-8") == "outside sentinel"


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


def test_write_securely_creates_only_a_missing_root_agent_file(tmp_path):
    root = _root(tmp_path)

    revision = agents_fs.write_agent(
        str(root), "AGENTS.md", "created\r\n", None, 1024, allow_create_root=True
    )

    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "created\n"
    assert (root / "AGENTS.md").stat().st_mode & 0o777 == 0o644
    assert revision == agents_fs.read_agent(str(root), "AGENTS.md", 1024).revision
    assert not (root / "CLAUDE.md").exists()

    with pytest.raises(agents_fs.AgentNotFound):
        agents_fs.write_agent(
            str(root),
            "child/AGENTS.md",
            "created",
            None,
            1024,
            allow_create_root=True,
        )


def test_missing_root_create_rejects_a_swapped_stage_without_following_it(tmp_path, monkeypatch):
    root = _root(tmp_path)
    outside = _write(tmp_path / "outside-create", "sentinel")
    stolen = root / "stolen-create-stage"

    def replace_stage(parent_fd: int, staged: str, _target: str) -> None:
        os.rename(staged, stolen.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.symlink(str(outside), staged, dir_fd=parent_fd)

    monkeypatch.setattr(agents_fs, "_before_publication", replace_stage)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(
            str(root),
            "AGENTS.md",
            "created",
            None,
            1024,
            allow_create_root=True,
        )

    assert (root / "AGENTS.md").is_symlink()
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert stolen.read_bytes() == b""
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_missing_root_create_never_clobbers_a_racing_target(tmp_path, monkeypatch):
    root = _root(tmp_path)

    def create_target(_parent_fd: int, _staged: str, _target: str) -> None:
        _write(root / "AGENTS.md", "racer")

    monkeypatch.setattr(agents_fs, "_before_publication", create_target)

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


def test_missing_root_create_removes_an_in_place_change_after_link(tmp_path, monkeypatch):
    root = _root(tmp_path)
    real_link = agents_fs.os.link
    changed = False

    def link_then_change(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal changed
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        if not changed:
            assert dst_dir_fd is not None
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, b"corrupt")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            changed = True

    monkeypatch.setattr(agents_fs.os, "link", link_then_change)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(
            str(root),
            "AGENTS.md",
            "created",
            None,
            1024,
            allow_create_root=True,
        )

    assert not (root / "AGENTS.md").exists()
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_missing_root_create_preserves_a_replacement_after_link(tmp_path, monkeypatch):
    root = _root(tmp_path)
    real_link = agents_fs.os.link

    def link_then_replace(
        source: str,
        target: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )
        assert dst_dir_fd is not None
        replacement = ".racing-agents"
        descriptor = os.open(
            replacement,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        try:
            os.write(descriptor, b"racer")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(replacement, target, src_dir_fd=dst_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(agents_fs.os, "link", link_then_replace)

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


def test_missing_root_create_cleans_up_when_final_content_check_fails(tmp_path, monkeypatch):
    root = _root(tmp_path)
    real_verify = agents_fs._verify_staged_content
    verification_count = 0

    def mutate_before_final_check(staged, raw, *, expected_links):
        nonlocal verification_count
        verification_count += 1
        if verification_count == 4:
            os.ftruncate(staged.descriptor, 0)
            os.write(staged.descriptor, b"late mutation")
            os.fsync(staged.descriptor)
        return real_verify(staged, raw, expected_links=expected_links)

    monkeypatch.setattr(agents_fs, "_verify_staged_content", mutate_before_final_check)

    with pytest.raises(agents_fs.AgentConflict):
        agents_fs.write_agent(
            str(root),
            "AGENTS.md",
            "created",
            None,
            1024,
            allow_create_root=True,
        )

    assert not (root / "AGENTS.md").exists()
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_missing_root_create_cleans_up_when_pre_unlink_verification_fails(tmp_path, monkeypatch):
    root = _root(tmp_path)
    real_verify = agents_fs._verify_staged_content
    verification_count = 0

    def fail_pre_unlink_check(staged, raw, *, expected_links):
        nonlocal verification_count
        verification_count += 1
        if verification_count == 3:
            raise agents_fs.AgentConflict("injected verification failure")
        return real_verify(staged, raw, expected_links=expected_links)

    monkeypatch.setattr(agents_fs, "_verify_staged_content", fail_pre_unlink_check)

    with pytest.raises(agents_fs.AgentConflict, match="injected"):
        agents_fs.write_agent(
            str(root),
            "AGENTS.md",
            "created",
            None,
            1024,
            allow_create_root=True,
        )

    assert not (root / "AGENTS.md").exists()
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_missing_root_create_never_scrubs_target_when_rollback_unlink_fails(tmp_path, monkeypatch):
    root = _root(tmp_path)
    real_verify = agents_fs._verify_staged_content
    real_unlink = agents_fs.os.unlink
    verification_count = 0
    target_unlink_failed = False

    def fail_pre_unlink_check(staged, raw, *, expected_links):
        nonlocal verification_count
        verification_count += 1
        if verification_count == 3:
            raise agents_fs.AgentConflict("injected verification failure")
        return real_verify(staged, raw, expected_links=expected_links)

    def fail_target_unlink(path, *, dir_fd=None):
        nonlocal target_unlink_failed
        if path == "AGENTS.md" and dir_fd is not None and not target_unlink_failed:
            target_unlink_failed = True
            raise PermissionError("injected target unlink failure")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(agents_fs, "_verify_staged_content", fail_pre_unlink_check)
    monkeypatch.setattr(agents_fs.os, "unlink", fail_target_unlink)

    with pytest.raises(agents_fs.AgentFilesystemError, match="roll back"):
        agents_fs.write_agent(
            str(root),
            "AGENTS.md",
            "created",
            None,
            1024,
            allow_create_root=True,
        )

    assert target_unlink_failed is True
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "created"
    assert (root / "AGENTS.md").stat().st_nlink == 1
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


def test_existing_write_fails_closed_without_atomic_exchange_support(tmp_path, monkeypatch):
    root = _root(tmp_path)
    target = _write(root / "AGENTS.md", "old")
    document = agents_fs.read_agent(str(root), "AGENTS.md", 1024)
    monkeypatch.setattr(agents_fs.sys, "platform", "unsupported")

    with pytest.raises(agents_fs.AgentFilesystemError, match="exchange is unavailable"):
        agents_fs.write_agent(str(root), "AGENTS.md", "new", document.revision, 1024)

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(root.glob(".enso-agents-*.tmp"))


def test_workspace_and_child_directories_must_be_owner_protected(tmp_path):
    root = _root(tmp_path)
    _write(root / "child" / "AGENTS.md")
    (root / "child").chmod(0o777)

    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.read_agent(str(root), "child/AGENTS.md", 1024)

    root.chmod(0o777)
    with pytest.raises(agents_fs.AgentIntegrityError):
        agents_fs.discover_agents(str(root))
