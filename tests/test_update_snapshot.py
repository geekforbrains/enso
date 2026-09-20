"""Snapshot declared state and restore the original across partial migration and recovery."""

import shutil

import pytest

from enso import update_snapshot
from enso.maintenance import UpdateError, read_json, write_json


@pytest.fixture
def operation(tmp_path):
    directory = tmp_path / "operation"
    directory.mkdir()
    return directory


@pytest.mark.parametrize(
    "names",
    [
        ["/outside"],
        ["../outside"],
        ["runtime/releases"],
        [".git/config"],
        ["cache/private"],
        ["web.pid"],
        [".config.lock"],
    ],
)
def test_invalid_or_operational_paths_are_rejected(enso_home, names):
    with pytest.raises(UpdateError, match="snapshot paths are invalid"):
        update_snapshot.plan(enso_home, names)


def test_plan_collapses_overlaps_and_includes_absent_new_parents(enso_home):
    old = enso_home.home / "old-workflows"
    old.mkdir()
    (old / "daily.json").write_text("original")
    assert update_snapshot.plan(
        enso_home,
        ["old-workflows/daily.json", "automation/workflows", "old-workflows", "old-workflows"],
    ) == ["automation", "old-workflows"]
    assert not (enso_home.home / "automation").exists()


def test_capture_and_restore_recover_partial_file_move_and_format(enso_home, operation):
    old = enso_home.home / "old-workflows"
    old.mkdir(mode=0o700)
    workflow = old / "daily.json"
    workflow.write_text('{"active": true}')
    workflow.chmod(0o640)
    original = workflow.read_bytes()
    enso_home.config.write_text('{"version": 2}')
    names = update_snapshot.plan(
        enso_home, ["config.json", "old-workflows", "automation/workflows"]
    )
    update_snapshot.capture(enso_home, operation, names)

    target = enso_home.home / "automation/workflows"
    target.parent.mkdir()
    old.rename(target)
    (target / "daily.json").write_text('{"enabled": true}')
    (target / "new.txt").write_text("new content")
    enso_home.config.write_text("partially converted config")
    update_snapshot.restore(enso_home, operation)

    assert workflow.read_bytes() == original
    assert workflow.stat().st_mode & 0o777 == 0o640
    assert old.stat().st_mode & 0o777 == 0o700
    assert not target.parent.exists()
    assert enso_home.config.read_text() == '{"version": 2}'
    assert (operation / "backup/old-workflows/daily.json").read_bytes() == original
    failed = next(operation.glob("failed-state-*"))
    assert (failed / "automation/workflows/new.txt").read_text() == "new content"
    assert (failed / "config.json").read_text() == "partially converted config"


def test_restore_recovers_both_occupied_destinations_and_sources(enso_home, operation):
    source = enso_home.home / "source"
    destination = enso_home.home / "nested/destination"
    destination.parent.mkdir()
    source.write_text("source content")
    destination.write_text("destination content")
    sibling = destination.parent / "keep"
    sibling.write_text("unrelated content")
    names = update_snapshot.plan(enso_home, ["source", "nested/destination"])
    update_snapshot.capture(enso_home, operation, names)
    source.replace(destination)

    update_snapshot.restore(enso_home, operation)

    assert source.read_text() == "source content"
    assert destination.read_text() == "destination content"
    assert sibling.read_text() == "unrelated content"


def test_interrupted_capture_never_changes_home_or_publishes_snapshot(
    enso_home, operation, monkeypatch
):
    enso_home.config.write_text("original")

    def fail_copy(source, destination):
        destination.write_text("incomplete copy")
        raise OSError("simulated failure during snapshot")

    monkeypatch.setattr(update_snapshot, "_copy", fail_copy)
    with pytest.raises(OSError, match="simulated failure"):
        update_snapshot.capture(enso_home, operation, ["config.json"])
    assert enso_home.config.read_text() == "original"
    assert not (operation / "snapshot.json").exists()
    with pytest.raises(UpdateError, match="snapshot is incomplete"):
        update_snapshot.restore(enso_home, operation)


@pytest.mark.parametrize("link", ["data", "data/nested"])
def test_declared_symlink_root_or_parent_is_refused(enso_home, tmp_path, link):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    target = enso_home.home / link
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    for declaration in (link, f"{link}/keep.txt"):
        with pytest.raises(UpdateError, match="symbolic link"):
            update_snapshot.plan(enso_home, [declaration])
    assert (outside / "keep.txt").read_text() == "keep"


def test_whole_directory_snapshot_preserves_descendant_symlinks(enso_home, operation, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside")
    root = enso_home.home / "data"
    root.mkdir()
    (root / "folder-link").symlink_to(outside, target_is_directory=True)
    (root / "file-link").symlink_to(outside / "keep.txt")
    (root / "dangling").symlink_to("absent")
    update_snapshot.capture(enso_home, operation, ["data"])
    shutil.rmtree(root)
    root.mkdir()
    (root / "new.txt").write_text("failed state")

    update_snapshot.restore(enso_home, operation)

    assert (root / "folder-link").is_symlink()
    assert (root / "folder-link").readlink() == outside
    assert (root / "file-link").is_symlink()
    assert (root / "dangling").readlink().as_posix() == "absent"
    assert (outside / "keep.txt").read_text() == "outside"
    assert not (root / "new.txt").exists()


@pytest.mark.parametrize("missing", ["backup", "present-file"])
def test_missing_snapshot_parts_fail_before_any_home_mutation(enso_home, operation, missing):
    enso_home.config.write_text("original")
    update_snapshot.capture(enso_home, operation, ["config.json", "new"])
    enso_home.config.write_text("changed")
    (enso_home.home / "new").write_text("new data")
    if missing == "backup":
        shutil.rmtree(operation / "backup")
    else:
        (operation / "backup/config.json").unlink()

    with pytest.raises(UpdateError, match="snapshot is incomplete"):
        update_snapshot.restore(enso_home, operation)

    assert enso_home.config.read_text() == "changed"
    assert (enso_home.home / "new").read_text() == "new data"
    assert not list(operation.glob("failed-state-*"))


def test_invalid_snapshot_metadata_fails_before_home_mutation(enso_home, operation):
    (enso_home.home / "data").write_text("original")
    update_snapshot.capture(enso_home, operation, ["data"])
    (enso_home.home / "data").write_text("changed")
    write_json(operation / "snapshot.json", {"paths": ["../outside"], "present": []})
    with pytest.raises(UpdateError, match="snapshot is incomplete"):
        update_snapshot.restore(enso_home, operation)
    assert (enso_home.home / "data").read_text() == "changed"


@pytest.mark.parametrize("kind", ["parent", "file"])
def test_restore_refuses_symlinks_in_backup_paths(enso_home, operation, tmp_path, kind):
    root = enso_home.home / "data"
    root.mkdir()
    (root / "file").write_text("original")
    update_snapshot.capture(enso_home, operation, ["data/file"])
    (root / "file").write_text("changed")
    linked = operation / ("backup/data" if kind == "parent" else "backup/data/file")
    outside = tmp_path / "outside"
    linked.rename(outside)
    linked.symlink_to(outside, target_is_directory=kind == "parent")
    with pytest.raises(UpdateError, match="snapshot is incomplete"):
        update_snapshot.restore(enso_home, operation)
    assert (root / "file").read_text() == "changed"


def test_restore_replaces_a_failed_migration_symlink_without_following_it(
    enso_home, operation, tmp_path
):
    enso_home.config.write_text("original")
    update_snapshot.capture(enso_home, operation, ["config.json"])
    outside = tmp_path / "outside"
    outside.write_text("user content")
    enso_home.config.unlink()
    enso_home.config.symlink_to(outside)
    update_snapshot.restore(enso_home, operation)
    assert not enso_home.config.is_symlink()
    assert enso_home.config.read_text() == "original"
    assert outside.read_text() == "user content"


def test_interrupted_restore_retries_from_original_snapshot(enso_home, operation, monkeypatch):
    root = enso_home.home / "data"
    root.mkdir()
    (root / "one").write_text("original one")
    (root / "two").write_text("original two")
    update_snapshot.capture(enso_home, operation, ["data", "new"])
    (root / "one").write_text("changed one")
    (root / "two").unlink()
    (enso_home.home / "new").write_text("created by migration")
    original_copy = update_snapshot._copy

    def interrupted_copy(source, destination):
        destination.mkdir()
        shutil.copy2(source / "one", destination / "one")
        raise OSError("simulated interruption while restoring directory")

    monkeypatch.setattr(update_snapshot, "_copy", interrupted_copy)
    with pytest.raises(OSError, match="simulated interruption"):
        update_snapshot.restore(enso_home, operation)
    assert (root / "one").read_text() == "original one"
    assert not (root / "two").exists()
    assert (operation / "backup/data/two").read_text() == "original two"

    monkeypatch.setattr(update_snapshot, "_copy", original_copy)
    update_snapshot.restore(enso_home, operation)

    assert (root / "one").read_text() == "original one"
    assert (root / "two").read_text() == "original two"
    assert not (enso_home.home / "new").exists()
    assert read_json(operation / "snapshot.json") == {"paths": ["data", "new"], "present": ["data"]}
    assert (operation / "backup/data/one").read_text() == "original one"
