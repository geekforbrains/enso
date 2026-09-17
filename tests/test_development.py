"""Development uses real snapshots and revisions, with fake services and checkout commands."""

import sqlite3
from types import SimpleNamespace

import pytest
from conftest import write_config
from typer.testing import CliRunner

from enso import db, development, maintenance, migrations, update_services, updates
from enso.cli import app
from enso.maintenance import UpdateError, read_json, write_json


@pytest.fixture
def local(enso_home, raw_config, monkeypatch, tmp_path):
    paths = enso_home
    write_config(paths, raw_config)
    db.initialize(paths)
    repository = tmp_path / "checkout with spaces"
    (repository / ".venv/bin").mkdir(parents=True)
    (repository / ".venv/bin/enso").write_text("editable CLI")
    launcher = tmp_path / "bin/enso"
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\n# Enso managed launcher\noriginal\n")
    launcher.chmod(0o755)
    receipt = {
        "schema_version": 1,
        "version": "0.2.0",
        "commit": "a" * 40,
        "release_id": "0.2.0-aaaaaaaaaaaa",
        "extras": ["slack", "web"],
        "feed": "https://example.test/releases/release.json",
        "bin_dir": str(launcher.parent),
        "viewer_service": "",
    }
    write_json(paths.runtime_dir / "install.json", receipt)
    services = {"daemon": False, "viewer": False, "viewer_service": ""}
    events = []
    monkeypatch.setattr(update_services, "discover", lambda *args: services.copy())
    monkeypatch.setattr(development, "_check_units", lambda *args: None)

    def command(args, **kwargs):
        events.append(args)
        if args[:3] == ["git", "branch", "--show-current"]:
            return "develop"
        if args[:2] == ["git", "rev-parse"]:
            return "b" * 40
        if args[-1] == "--version":
            return "enso 0.2.1"
        return ""

    monkeypatch.setattr(update_services, "run_command", command)
    for name in ("stop", "start", "healthy"):
        monkeypatch.setattr(update_services, name, lambda *args, name=name: events.append(name))
    return SimpleNamespace(
        paths=paths,
        repository=repository,
        launcher=launcher,
        receipt=receipt,
        events=events,
        services=services,
    )


def test_refresh_switches_only_runtime_and_launcher_and_repeats_same_version(local):
    paths = local.paths
    job = paths.workspace_jobs("default") / "enso-memory/JOB.md"
    job.parent.mkdir(parents=True)
    job.write_text("customized job stays byte-for-byte intact")
    before = {path: path.read_bytes() for path in (paths.config, paths.db, job)}
    original = local.launcher.read_bytes()
    for _ in range(2):
        result = development.run(paths, local.repository)
        assert result["version"] == "0.2.1" and result["commit"] == "b" * 40
        assert "Enso development launcher" in local.launcher.read_text()
        assert f"'{local.repository}/.venv/bin/enso'" in local.launcher.read_text()
        assert updates.installed(paths) == {}
        assert not maintenance.paused(paths)
        assert {path: path.read_bytes() for path in before} == before
        assert not (paths.home / migrations.MARKER).exists()
        assert local.events.index("stop") < local.events.index(
            ["uv", "sync", "--all-extras", "--locked"]
        )
        assert local.events.index("start") < local.events.index("healthy")
        local.events.clear()
    copies = list((paths.runtime_dir / "development").glob("*/launcher"))
    assert original in [path.read_bytes() for path in copies]


def registry(local, monkeypatch, *, fail=False):
    paths = local.paths
    with sqlite3.connect(paths.db) as connection:
        connection.execute("CREATE TABLE example (name TEXT)")
        connection.execute("INSERT INTO example VALUES ('original')")
    source = paths.workspace("default") / "old"
    source.mkdir()
    (source / "note.txt").write_text("kept")

    def database(paths):
        with sqlite3.connect(paths.db) as connection:
            connection.execute("ALTER TABLE example ADD COLUMN priority INTEGER DEFAULT 0")

    def move(paths):
        source.rename(paths.workspace("default") / "new")
        if fail:
            raise ValueError("simulated failure after moving data")

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (
            migrations.Migration(1, "add priority", lambda _: ("enso.db",), database),
            migrations.Migration(
                2,
                "move files",
                lambda _: ("workspaces/default/old", "workspaces/default/new"),
                move,
            ),
        ),
    )


def test_migration_preview_is_read_only_and_apply_is_revision_based(local, monkeypatch):
    registry(local, monkeypatch)
    preview = development.migration_preview(local.paths)
    assert [step["revision"] for step in preview["pending"]] == [1, 2]
    assert {"enso.db", "enso.db-wal", "enso.db-shm", "enso.db-journal"} <= set(preview["paths"])
    assert not development.state_path(local.paths).exists()
    assert not (local.paths.home / migrations.MARKER).exists()
    with pytest.raises(UpdateError, match="migrations are pending"):
        development.run(local.paths, local.repository)
    assert not local.events

    original = local.launcher.read_bytes()
    result = development.run(local.paths, local.repository, migrate=True)
    assert result["version"] == "0.2.1"
    assert migrations.read_revision(local.paths) == 2
    assert local.launcher.read_bytes() == original
    assert updates.installed(local.paths) == local.receipt
    assert (local.paths.workspace("default") / "new/note.txt").read_text() == "kept"
    with sqlite3.connect(local.paths.db) as connection:
        assert connection.execute("SELECT * FROM example").fetchall() == [("original", 0)]
    assert not list((local.paths.runtime_dir / "development").glob("*/backup"))
    local.events.clear()
    assert (
        development.run(local.paths, local.repository, migrate=True)["message"]
        == "No migrations pending."
    )
    assert not local.events


def test_failed_migration_recovery_restores_db_files_and_marker(local, monkeypatch):
    registry(local, monkeypatch, fail=True)
    with pytest.raises(UpdateError, match="work remains paused"):
        development.run(local.paths, local.repository, migrate=True)
    assert maintenance.paused(local.paths) and "start" not in local.events
    assert migrations.read_revision(local.paths) == 1
    assert development.recover(local.paths)["phase"] == "restored"
    assert migrations.read_revision(local.paths) == 0
    assert (local.paths.workspace("default") / "old/note.txt").read_text() == "kept"
    assert not (local.paths.workspace("default") / "new").exists()
    with sqlite3.connect(local.paths.db) as connection:
        assert connection.execute("SELECT * FROM example").fetchall() == [("original",)]
    assert maintenance.paused(local.paths)  # No incompatible source resumes work.

    second = migrations.MIGRATIONS[1]
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (
            migrations.MIGRATIONS[0],
            migrations.Migration(
                2,
                second.name,
                second.paths,
                lambda paths: (paths.workspace("default") / "old").rename(
                    paths.workspace("default") / "new"
                ),
            ),
        ),
    )
    development.run(local.paths, local.repository, migrate=True)
    assert migrations.read_revision(local.paths) == 2
    assert not maintenance.paused(local.paths)


def test_failed_start_restores_managed_launcher_without_reverting_later_success(local, monkeypatch):
    original = local.launcher.read_bytes()

    def fail(*args):
        raise UpdateError("not ready")

    monkeypatch.setattr(update_services, "healthy", fail)
    with pytest.raises(UpdateError, match="work remains paused"):
        development.run(local.paths, local.repository)
    assert not updates.installed(local.paths)
    development.recover(local.paths)
    assert local.launcher.read_bytes() == original
    assert updates.installed(local.paths) == local.receipt
    monkeypatch.setattr(update_services, "healthy", lambda *args: None)
    development.run(local.paths, local.repository)
    with pytest.raises(UpdateError, match="no development operation"):
        development.recover(local.paths)
    assert "Enso development launcher" in local.launcher.read_text()


def test_development_cli_admission_shares_release_access_lock(local):
    paths = local.paths
    development.run(paths, local.repository)
    with maintenance.exclusive_access(paths, timeout=0):
        result = CliRunner().invoke(app, ["config", "check", "--json"])
    assert result.exit_code == 1 and "another command" in result.output
    write_json(paths.maintenance, {"kind": "development", "phase": "draining"})
    assert CliRunner().invoke(app, ["config", "check", "--json"]).exit_code == 0
    write_json(paths.maintenance, {"kind": "development", "phase": "migrating"})
    denied = CliRunner().invoke(app, ["config", "check", "--json"])
    assert denied.exit_code == 1 and "Enso is updating" in denied.output


def test_invalid_configuration_and_foreign_gate_do_not_stop_services(local):
    before = local.paths.config.read_text()
    local.paths.config.write_text("{}")
    with pytest.raises(Exception, match=r"configuration|version"):
        development.run(local.paths, local.repository)
    assert "stop" not in local.events and not development.state_path(local.paths).exists()
    local.paths.config.write_text(before)
    write_json(local.paths.maintenance, {"kind": "workflow-init", "operation_id": "foreign"})
    with pytest.raises(UpdateError, match="another maintenance"):
        development.run(local.paths, local.repository)
    assert "stop" not in local.events
    assert read_json(local.paths.maintenance)["operation_id"] == "foreign"


def test_first_switch_refuses_busy_work_without_stopping_it(local, monkeypatch):
    monkeypatch.setattr(maintenance, "daemon", lambda _: {"active": 1})
    original = local.launcher.read_bytes()
    with pytest.raises(UpdateError, match="wait for active work"):
        development.run(local.paths, local.repository)
    assert "stop" not in local.events and not maintenance.paused(local.paths)
    assert local.launcher.read_bytes() == original
    assert updates.installed(local.paths) == local.receipt


def test_migrated_home_with_failed_readiness_requires_recovery(local, monkeypatch):
    registry(local, monkeypatch)

    def fail(*args):
        raise UpdateError("new services failed")

    monkeypatch.setattr(update_services, "healthy", fail)
    with pytest.raises(UpdateError, match="work remains paused"):
        development.run(local.paths, local.repository, migrate=True)
    assert migrations.read_revision(local.paths) == 2
    with pytest.raises(UpdateError, match="recover"):
        development.run(local.paths, local.repository, migrate=True)
    development.recover(local.paths)
    assert migrations.read_revision(local.paths) == 0
    assert (local.paths.workspace("default") / "old/note.txt").read_text() == "kept"


def test_committed_migration_recovers_only_its_gate_never_its_data(local, monkeypatch):
    registry(local, monkeypatch)

    def fail(*args):
        raise OSError("interrupted clearing maintenance")

    clear = development._ungate
    monkeypatch.setattr(development, "_ungate", fail)
    with pytest.raises(OSError, match="interrupted clearing"):
        development.run(local.paths, local.repository, migrate=True)
    with sqlite3.connect(local.paths.db) as connection:
        connection.execute("INSERT INTO example VALUES ('newer', 1)")
    monkeypatch.setattr(development, "_ungate", clear)
    assert development.recover(local.paths)["phase"] == "ready"
    assert not maintenance.paused(local.paths)
    assert migrations.read_revision(local.paths) == 2
    with sqlite3.connect(local.paths.db) as connection:
        assert connection.execute("SELECT name FROM example ORDER BY name").fetchall() == [
            ("newer",),
            ("original",),
        ]
