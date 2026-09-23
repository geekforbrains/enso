"""Workspace JSON conversion preserves overrides, retires prose, and remains recoverable."""

import json
import stat
from pathlib import Path

import pytest
from conftest import write_config

from enso import frontmatter, migrations, update_snapshot, workspace_migration
from enso.config import load_config
from enso.maintenance import UpdateError, write_json
from enso.routing import resolve_agent


@pytest.fixture
def old_home(enso_home, raw_config):
    write_config(enso_home, raw_config)
    write_json(enso_home.home / migrations.MARKER, {"revision": 6})
    return enso_home


def legacy(paths, workspace="default", fields=None):
    root = paths.workspace(workspace)
    root.mkdir(exist_ok=True)
    source = root / "WORKSPACE.md"
    source.write_text(frontmatter.render(fields or {}, "Prose deliberately discarded.\n"))
    source.chmod(0o640)
    return source


def test_convert_all_workspaces_preserving_settings_permissions_and_unrelated_files(old_home):
    fields = {
        "agent": {"provider": "codex", "model": "sol", "effort": "high"},
        "providers": {"claude": {"args": []}, "codex": {"args": ["--name", "café\n001"]}},
    }
    source = legacy(old_home, fields=fields)
    source.write_text(source.read_text().replace("---\n", "---\n# discarded comment\n", 1))
    empty = legacy(old_home, "unbound")
    inherited = old_home.workspace("inherited")
    inherited.mkdir()
    unrelated = source.with_name("AGENTS.md")
    unrelated.write_text("Preserve instructions exactly.\n")
    assert set(migrations.MIGRATIONS[6].paths(old_home)) == {
        "workspaces/default/WORKSPACE.md",
        "workspaces/default/workspace.json",
        "workspaces/unbound/WORKSPACE.md",
        "workspaces/unbound/workspace.json",
    }
    assert source.exists() and empty.exists()  # preview does not mutate

    migrations.apply(old_home)

    target = old_home.workspace_settings("default")
    assert json.loads(target.read_text()) == fields
    assert "discarded" not in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not source.exists() and not empty.exists()
    assert old_home.workspace_settings("unbound").read_text() == "{}\n"
    assert not old_home.workspace_settings("inherited").exists()
    assert unrelated.read_text() == "Preserve instructions exactly.\n"
    config = load_config(old_home)
    assert resolve_agent(config, "default").provider == "codex"
    assert config.provider_args("default", "claude") == ()
    assert config.provider_args("default", "codex") == ("--name", "café\n001")
    assert migrations.read_revision(old_home) == migrations.latest_revision()
    before = target.stat().st_mtime_ns
    migrations.apply(old_home)
    migrations.MIGRATIONS[6].apply(old_home)
    assert target.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    "content",
    [
        "only prose",
        "---\nagent: {}\n---\n",
        "---\nagent: {}\nagent: {}\n---\n",
        "---\nrestricted: false\n---\n",
        "---\nproviders: {codex: {args: [123]}}\n---\n",
        "---\nproviders: {codex: {path: other, args: []}}\n---\n",
    ],
)
def test_malformed_later_workspace_stops_preview_and_apply_before_any_writes(old_home, content):
    good = legacy(old_home)
    bad = legacy(old_home, "team")
    bad.write_text(content)
    for action in (migrations.plan, migrations.apply):
        with pytest.raises(UpdateError, match=r"team/WORKSPACE\.md"):
            action(old_home)
        assert good.exists() and bad.read_text() == content
        assert not old_home.workspace_settings("default").exists()
        assert migrations.read_revision(old_home) == 6


@pytest.mark.parametrize("name", ["WORKSPACE.md", "workspace.json"])
@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_unsafe_paths_are_rejected_without_changing_other_workspaces(
    old_home, tmp_path, name, kind
):
    good = legacy(old_home)
    source = legacy(old_home, "team")
    path = source.with_name(name)
    path.unlink(missing_ok=True)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "directory":
        path.mkdir()
    else:
        import os

        os.mkfifo(path)
    for action in (migrations.plan, migrations.apply):
        with pytest.raises(UpdateError, match=name.replace(".", r"\.")):
            action(old_home)
        assert good.exists()
        assert not old_home.workspace_settings("default").exists()
        assert outside.read_text() == "untouched"


def test_linked_workspace_is_not_followed(old_home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "WORKSPACE.md"
    source.write_text("---\n{}\n---\n")
    old_home.workspace("linked").symlink_to(outside, target_is_directory=True)
    for action in (migrations.plan, migrations.apply):
        with pytest.raises(UpdateError, match="symbolic link"):
            action(old_home)
    assert source.exists() and not (outside / "workspace.json").exists()


def test_conflicting_destination_is_never_overwritten(old_home):
    source = legacy(old_home)
    target = old_home.workspace_settings("default")
    target.write_text('{"providers": {"codex": {"args": []}}}\n')
    before = target.read_bytes()
    for action in (migrations.plan, migrations.apply):
        with pytest.raises(UpdateError, match="conflicts"):
            action(old_home)
        assert source.exists() and target.read_bytes() == before


def test_retry_after_publication_before_source_removal(old_home, monkeypatch):
    source = legacy(old_home)
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path == source:
            raise OSError("simulated interruption after publication")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(UpdateError, match="simulated interruption"):
            migrations.apply(old_home)
    assert source.exists() and old_home.workspace_settings("default").exists()
    assert migrations.read_revision(old_home) == 6
    migrations.apply(old_home)
    assert not source.exists()
    assert migrations.read_revision(old_home) == migrations.latest_revision()


def test_snapshot_restores_original_prose_and_absent_destinations_after_failure(
    old_home, monkeypatch, tmp_path
):
    sources = [legacy(old_home), legacy(old_home, "team")]
    before = {path: path.read_bytes() for path in sources}
    operation = tmp_path / "operation"
    operation.mkdir()
    names = update_snapshot.plan(old_home, [migrations.MARKER, *migrations.plan(old_home)])
    update_snapshot.capture(old_home, operation, names)
    publish = workspace_migration.write_bytes

    def interrupted(path, *args, **kwargs):
        if path.parent.name == "team":
            raise OSError("simulated second workspace failure")
        return publish(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(workspace_migration, "write_bytes", interrupted)
        with pytest.raises(UpdateError, match="second workspace failure"):
            migrations.apply(old_home)
    assert not sources[0].exists() and sources[1].exists()
    assert migrations.read_revision(old_home) == 6
    update_snapshot.restore(old_home, operation)
    assert {path: path.read_bytes() for path in sources} == before
    assert all(not path.with_name("workspace.json").exists() for path in sources)
    assert migrations.read_revision(old_home) == 6
    migrations.apply(old_home)
    assert load_config(old_home)
