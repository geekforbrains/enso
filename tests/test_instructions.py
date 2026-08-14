"""Tests for securely loading Enso's shared agent instructions."""

from __future__ import annotations

import dataclasses
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from enso import instructions
from enso.instructions import (
    InstructionError,
    load_shared_instructions,
    validate_shared_instructions,
)


@pytest.fixture
def instruction_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "enso"
    config_dir.mkdir(mode=0o700)
    monkeypatch.setattr(instructions, "CONFIG_DIR", str(config_dir))
    return config_dir


def _write_source(config_dir: Path, content: str = "# Shared\n\nBe helpful.\n") -> Path:
    source = config_dir / "AGENTS.md"
    source.write_text(content, encoding="utf-8")
    source.chmod(0o600)
    return source


def test_load_shared_instructions_publishes_content_addressed_snapshot(
    instruction_home: Path,
) -> None:
    source = _write_source(instruction_home)

    bundle = load_shared_instructions()

    assert bundle.source_path == str(source)
    assert bundle.content == "# Shared\n\nBe helpful.\n"
    assert bundle.revision == (
        "c3b3c33c151d149796692f274f872059e3b75a8b21bf982e07c5a27bb77b05b6"
    )
    snapshot = Path(bundle.snapshot_path)
    assert snapshot == instruction_home / "runtime" / "instructions" / f"{bundle.revision}.md"
    assert snapshot.read_bytes() == source.read_bytes()
    assert snapshot.stat().st_mode & 0o777 == 0o400
    assert snapshot.stat().st_uid == os.getuid()
    assert snapshot.parent.stat().st_mode & 0o777 == 0o700
    publish_lock = snapshot.parent / ".publish.lock"
    assert publish_lock.stat().st_mode & 0o777 == 0o600
    assert publish_lock.stat().st_uid == os.getuid()


def test_validate_shared_instructions_does_not_publish_snapshot(
    instruction_home: Path,
) -> None:
    source = _write_source(instruction_home)

    validated = validate_shared_instructions()

    assert validated.source_path == str(source)
    assert validated.content == "# Shared\n\nBe helpful.\n"
    assert len(validated.revision) == 64
    assert not (instruction_home / "runtime").exists()


def test_validate_shared_instructions_allows_owner_owned_readable_source(
    instruction_home: Path,
) -> None:
    source = _write_source(instruction_home)
    source.chmod(0o644)

    assert validate_shared_instructions().content == "# Shared\n\nBe helpful.\n"


def test_instruction_bundle_is_immutable(instruction_home: Path) -> None:
    _write_source(instruction_home)
    bundle = load_shared_instructions()

    with pytest.raises(dataclasses.FrozenInstanceError):
        bundle.content = "changed"  # type: ignore[misc]


def test_load_shared_instructions_deduplicates_verified_snapshot(
    instruction_home: Path,
) -> None:
    _write_source(instruction_home)
    first = load_shared_instructions()
    before = os.stat(first.snapshot_path)

    second = load_shared_instructions()
    after = os.stat(second.snapshot_path)

    assert second == first
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )


def test_load_shared_instructions_rejects_missing_file(instruction_home: Path) -> None:
    with pytest.raises(InstructionError, match="shared instruction file is missing"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_symlink(instruction_home: Path) -> None:
    target = instruction_home / "elsewhere.md"
    target.write_text("untrusted", encoding="utf-8")
    (instruction_home / "AGENTS.md").symlink_to(target)

    with pytest.raises(InstructionError, match="regular, non-symlink file"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_oversized_file(instruction_home: Path) -> None:
    source = instruction_home / "AGENTS.md"
    source.write_bytes(b"x" * (instructions.MAX_SHARED_INSTRUCTION_BYTES + 1))

    with pytest.raises(InstructionError, match="exceeds the 20480-byte limit"):
        load_shared_instructions()


def test_load_shared_instructions_accepts_exact_size_limit(
    instruction_home: Path,
) -> None:
    source = instruction_home / "AGENTS.md"
    source.write_bytes(b"x" * instructions.MAX_SHARED_INSTRUCTION_BYTES)
    source.chmod(0o600)

    bundle = load_shared_instructions()

    assert len(bundle.content.encode("utf-8")) == instructions.MAX_SHARED_INSTRUCTION_BYTES


def test_load_shared_instructions_rejects_invalid_utf8(instruction_home: Path) -> None:
    source = instruction_home / "AGENTS.md"
    source.write_bytes(b"valid\n\xff")

    with pytest.raises(InstructionError, match="valid UTF-8"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_nul(instruction_home: Path) -> None:
    source = instruction_home / "AGENTS.md"
    source.write_bytes(b"before\x00after")

    with pytest.raises(InstructionError, match="NUL bytes"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_wrong_owner(
    instruction_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_source(instruction_home)
    current_uid = os.getuid()
    monkeypatch.setattr(instructions.os, "getuid", lambda: current_uid + 1)

    with pytest.raises(InstructionError, match="owned by the current user"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_group_writable_source(
    instruction_home: Path,
) -> None:
    source = _write_source(instruction_home)
    source.chmod(0o620)

    with pytest.raises(InstructionError, match="must not be group- or other-writable"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_hard_linked_source(
    instruction_home: Path,
) -> None:
    source = _write_source(instruction_home)
    os.link(source, instruction_home / "AGENTS-alias.md")

    with pytest.raises(InstructionError, match="must not have additional hard links"):
        load_shared_instructions()


def test_load_shared_instructions_detects_mutation_during_read(
    instruction_home: Path, monkeypatch: pytest.MonkeyPatch
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
        load_shared_instructions()


def test_load_shared_instructions_rejects_tampered_snapshot(instruction_home: Path) -> None:
    _write_source(instruction_home)
    first = load_shared_instructions()
    snapshot = Path(first.snapshot_path)
    snapshot.chmod(0o600)
    snapshot.write_text("tampered", encoding="utf-8")

    with pytest.raises(InstructionError, match="snapshot does not match"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_writable_snapshot(instruction_home: Path) -> None:
    _write_source(instruction_home)
    first = load_shared_instructions()
    Path(first.snapshot_path).chmod(0o600)

    with pytest.raises(InstructionError, match="snapshot permissions"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_hard_linked_snapshot(
    instruction_home: Path,
) -> None:
    _write_source(instruction_home)
    first = load_shared_instructions()
    os.link(first.snapshot_path, instruction_home / "snapshot-alias.md")

    with pytest.raises(InstructionError, match="snapshot must not have additional hard links"):
        load_shared_instructions()


def test_load_shared_instructions_rejects_symlink_snapshot(instruction_home: Path) -> None:
    _write_source(instruction_home)
    validated = validate_shared_instructions()
    snapshot_dir = instruction_home / "runtime" / "instructions"
    snapshot_dir.mkdir(parents=True)
    target = snapshot_dir / "target.md"
    target.write_text(validated.content, encoding="utf-8")
    (snapshot_dir / f"{validated.revision}.md").symlink_to(target)

    with pytest.raises(InstructionError, match="regular, non-symlink file"):
        load_shared_instructions()


def test_load_shared_instructions_serializes_concurrent_publish(
    instruction_home: Path,
) -> None:
    _write_source(instruction_home)
    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def load_together() -> instructions.InstructionBundle:
        barrier.wait()
        return load_shared_instructions()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        bundles = list(executor.map(lambda _index: load_together(), range(worker_count)))

    assert len({bundle.snapshot_path for bundle in bundles}) == 1
    assert Path(bundles[0].snapshot_path).stat().st_nlink == 1


def test_load_shared_instructions_rejects_symlink_publish_lock(
    instruction_home: Path,
) -> None:
    _write_source(instruction_home)
    snapshot_dir = instruction_home / "runtime" / "instructions"
    snapshot_dir.mkdir(parents=True)
    lock_target = snapshot_dir / "elsewhere.lock"
    lock_target.touch()
    (snapshot_dir / ".publish.lock").symlink_to(lock_target)

    with pytest.raises(InstructionError, match="open snapshot publish lock safely"):
        load_shared_instructions()
