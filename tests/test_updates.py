"""Managed update transactions against a scratch home and fake external services."""

import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from enso import maintenance, releases, update_services, updates
from enso.cli import app
from enso.maintenance import UpdateError, read_json, write_json


@pytest.fixture
def managed(enso_home, monkeypatch):
    paths = enso_home
    maintenance.prepare(paths)
    old = paths.runtime_dir / "releases/0.1.0-aaaaaaaaaaaa"
    (old / "bin").mkdir(parents=True)
    (old / "bin/enso").write_text("old executable")
    (paths.runtime_dir / "current").symlink_to(old)
    receipt = {
        "schema_version": 1,
        "version": "0.1.0",
        "commit": "a" * 40,
        "release_id": old.name,
        "extras": ["slack"],
        "feed": "https://releases.example.test/stable/release.json",
        "bin_dir": str(paths.home.parent / "bin"),
        "token_file": None,
        "viewer_service": "",
    }
    write_json(paths.runtime_dir / "install.json", receipt)
    release = releases.Release(
        "0.2.0",
        "b" * 40,
        ">=3.14",
        releases.Artifact("enso-0.2.0-py3-none-any.whl", "c" * 64),
        releases.Artifact("constraints.txt", "d" * 64),
        "https://releases.example.test/v0.2.0/release.json",
    )
    services = {
        "daemon": False,
        "daemon_pid": None,
        "viewer": False,
        "viewer_service": "",
        "viewer_host": None,
        "viewer_port": None,
    }
    events = []
    monkeypatch.setattr(updates, "_release", lambda *args, **kwargs: release)
    monkeypatch.setattr(update_services, "discover", lambda *args: dict(services))
    monkeypatch.setattr(update_services, "launch", lambda *args: events.append(("launch", args)))
    monkeypatch.setattr(update_services, "cleanup_finished", lambda *args: None)
    for name in ("stop", "start", "healthy"):
        monkeypatch.setattr(
            update_services,
            name,
            lambda *args, name=name: events.append((name, args)),
        )
    monkeypatch.setattr(
        update_services,
        "run_command",
        lambda *args, **kwargs: events.append(("command", args)) or "",
    )

    def candidate(_paths, selected, _receipt):
        target = paths.runtime_dir / "releases" / selected.release_id
        (target / "bin").mkdir(parents=True, exist_ok=True)
        (target / "bin/enso").write_text("candidate executable")
        return target

    monkeypatch.setattr(updates, "_candidate", candidate)
    return SimpleNamespace(
        paths=paths, old=old, receipt=receipt, release=release, services=services, events=events
    )


def queue(managed):
    result = updates.request_apply(managed.paths)
    assert result["operation"]["status"] == "queued"
    return read_json(managed.paths.update_state)


def test_queue_pins_artifacts_and_origin_and_serializes_requests(managed, monkeypatch):
    monkeypatch.setenv("ENSO_ORIGIN_TRANSPORT", "slack")
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C123")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "123.456")
    state = queue(managed)
    assert state["manifest"]["wheel"]["url"] == (
        "https://releases.example.test/v0.2.0/enso-0.2.0-py3-none-any.whl"
    )
    assert state["origin"] == {"transport": "slack", "channel": "C123", "thread": "123.456"}
    with pytest.raises(UpdateError, match="pending"):
        updates.request_apply(managed.paths)
    assert read_json(managed.paths.update_state)["id"] == state["id"]
    assert [name for name, _ in managed.events].count("launch") == 1
    assert not maintenance.paused(managed.paths)


def test_same_release_is_noop_and_downgrade_or_reused_version_is_refused(managed, monkeypatch):
    same = replace(managed.release, version="0.1.0", commit=managed.receipt["commit"])
    monkeypatch.setattr(updates, "_release", lambda *args: same)
    assert updates.request_apply(managed.paths)["operation"] is None
    assert not managed.events
    reused = replace(same, commit="f" * 40)
    monkeypatch.setattr(updates, "_release", lambda *args: reused)
    with pytest.raises(UpdateError, match="reused"):
        updates.check(managed.paths)
    with pytest.raises(UpdateError, match="newer release"):
        updates.request_apply(managed.paths)
    older = replace(same, version="0.0.9")
    monkeypatch.setattr(updates, "_release", lambda *args: older)
    with pytest.raises(UpdateError, match="newer release"):
        updates.request_apply(managed.paths)


def test_staging_failure_leaves_active_code_and_data_untouched(managed, monkeypatch):
    managed.paths.config.write_text("before config")
    state = queue(managed)

    def fail(*args):
        raise releases.ReleaseError("bad wheel private-credential")

    monkeypatch.setattr(updates, "_candidate", fail)
    updates.run_update(managed.paths, state["id"])
    assert read_json(managed.paths.update_state)["status"] == "failed"
    assert (managed.paths.runtime_dir / "current").resolve() == managed.old
    assert managed.paths.config.read_text() == "before config"
    assert not maintenance.paused(managed.paths)
    assert not any(name == "stop" for name, _ in managed.events)
    assert "private-credential" not in json.dumps(updates.status(managed.paths))


def test_active_work_deadline_defers_without_stopping_or_switching(managed, monkeypatch):
    managed.services.update(daemon=True, daemon_pid=123)
    state = queue(managed)
    ticks = iter([0.0, 0.0, 301.0])
    monkeypatch.setattr(updates.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(updates.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(updates, "daemon", lambda paths: {"pid": 123, "paused": True, "active": 1})
    updates.run_update(managed.paths, state["id"])
    assert read_json(managed.paths.update_state)["status"] == "deferred"
    assert (managed.paths.runtime_dir / "current").resolve() == managed.old
    assert not maintenance.paused(managed.paths)
    assert not any(name == "stop" for name, _ in managed.events)


def test_success_admits_work_only_after_health_and_commits_version(managed, monkeypatch):
    state = queue(managed)
    observations = []

    def healthy(paths, services, version, timeout):
        observations.append(
            (maintenance.paused(paths), updates.installed(paths)["version"], version)
        )

    monkeypatch.setattr(update_services, "healthy", healthy)
    updates.run_update(managed.paths, state["id"])
    assert observations == [(True, "0.1.0", "0.2.0")]
    assert updates.installed(managed.paths)["version"] == "0.2.0"
    assert read_json(managed.paths.update_state)["status"] == "succeeded"
    assert not maintenance.paused(managed.paths)
    assert (managed.paths.runtime_dir / "current").resolve().name == managed.release.release_id
    assert (managed.old / "bin/enso").read_text() == "old executable"


def test_interruption_after_commit_clears_gate_without_restoring_old_data(managed):
    state = queue(managed)
    updates.run_update(managed.paths, state["id"])
    committed = read_json(managed.paths.update_state)
    assert committed["status"] == "succeeded"
    # A crash can leave the gate after the success record reached disk. New
    # application writes must not later be overwritten by the old snapshot.
    write_json(managed.paths.maintenance, {"operation_id": state["id"]})
    managed.paths.config.write_text("committed configuration")
    managed.events.clear()
    updates.run_update(managed.paths, state["id"])
    assert updates.installed(managed.paths)["version"] == "0.2.0"
    assert managed.paths.config.read_text() == "committed configuration"
    assert not maintenance.paused(managed.paths)
    assert not any(name in {"stop", "start", "healthy"} for name, _ in managed.events)


def test_new_request_waits_for_previous_worker_to_finish_notification(managed):
    state = queue(managed)
    updates.run_update(managed.paths, state["id"])
    with (
        maintenance.lock(managed.paths, "worker"),
        pytest.raises(UpdateError, match="in progress"),
    ):
        updates.request_apply(managed.paths)
    assert read_json(managed.paths.update_state)["id"] == state["id"]


def test_direct_install_persists_a_canonical_feed(enso_home, monkeypatch, tmp_path):
    source = tmp_path / "release.json"
    selected = releases.Release(
        "0.2.0",
        "a" * 40,
        ">=3.14",
        releases.Artifact("enso-0.2.0-py3-none-any.whl", "b" * 64),
        releases.Artifact("constraints.txt", "c" * 64),
        str(source),
    )
    monkeypatch.setattr(releases, "load_release", lambda *args, **kwargs: selected)
    monkeypatch.setattr(updates, "receiver_active", lambda paths: False)
    monkeypatch.setattr(updates.web, "status", lambda paths: SimpleNamespace(running=False))

    def candidate(paths, release, receipt):
        target = paths.runtime_dir / "releases" / release.release_id
        (target / "bin").mkdir(parents=True)
        (target / "bin/enso").write_text("executable")
        return target

    monkeypatch.setattr(updates, "_candidate", candidate)
    monkeypatch.chdir(tmp_path)
    updates.install(enso_home, "release.json", bin_dir=tmp_path / "bin", extras=())
    assert updates.installed(enso_home)["feed"] == str(source)


def test_upgrade_reuses_bootstrapped_uv_without_requiring_it_on_path(enso_home, monkeypatch):
    private_uv = enso_home.runtime_dir / "tools/uv"
    private_uv.parent.mkdir(parents=True)
    private_uv.write_text("private uv")
    captured = []
    monkeypatch.setattr(
        releases, "prepare_release", lambda release, target, **kwargs: captured.append(kwargs)
    )
    selected = SimpleNamespace(release_id="0.2.0-" + "a" * 12)
    updates._candidate(enso_home, selected, {"extras": ["slack"], "token_file": None})
    assert captured[0]["uv"] == str(private_uv)


def test_failed_migration_restores_database_config_bundles_and_wal_but_keeps_workspace(
    managed, monkeypatch
):
    paths = managed.paths
    before = {
        "config.json": b"old config",
        "enso.db": b"old database",
        "enso.db-wal": b"old wal",
        "AGENTS.md": b"old instructions",
        ".bundles.json": b'{"files":{}}',
        "workspaces/default/jobs/user/JOB.md": b"custom job",
        "skills/user/SKILL.md": b"custom skill",
        "slack/manifest.json": b"old slack manifest",
    }
    for name, content in before.items():
        target = paths.home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    workspace = paths.home / "workspaces/default/AGENTS.md"
    workspace.write_text("workspace before")
    state = queue(managed)

    def run(args, **kwargs):
        if args[-1] == "_prepare-home":
            for name in before:
                (paths.home / name).write_bytes(b"migrated")
            (paths.home / "enso.db-shm").write_bytes(b"new shared memory")
            workspace.write_text("workspace changed independently")
            raise UpdateError("migration failed")
        return ""

    monkeypatch.setattr(update_services, "run_command", run)
    updates.run_update(paths, state["id"])
    assert read_json(paths.update_state)["status"] == "rolled_back"
    assert (paths.runtime_dir / "current").resolve() == managed.old
    assert updates.installed(paths)["version"] == "0.1.0"
    assert {name: (paths.home / name).read_bytes() for name in before} == before
    assert not (paths.home / "enso.db-shm").exists()
    assert workspace.read_text() == "workspace changed independently"
    assert not maintenance.paused(paths)


def test_failed_recovery_keeps_gate_and_retry_uses_original_snapshot(managed, monkeypatch):
    paths = managed.paths
    paths.config.write_text("original")
    state = queue(managed)

    def fail_health(*args):
        raise UpdateError("service did not become ready")

    monkeypatch.setattr(update_services, "healthy", fail_health)
    updates.run_update(paths, state["id"])
    assert read_json(paths.update_state)["status"] == "recovery_failed"
    assert maintenance.paused(paths)
    paths.config.write_text("partially restored")
    monkeypatch.setattr(update_services, "healthy", lambda *args: None)
    recovered = updates.recover(paths)
    assert recovered["operation"]["id"] == state["id"]
    updates.run_update(paths, state["id"])
    assert read_json(paths.update_state)["status"] == "rolled_back"
    assert paths.config.read_text() == "original"
    assert not maintenance.paused(paths)


def test_selection_refuses_external_directory_and_external_symlink(managed, tmp_path):
    external = tmp_path / "external"
    (external / "bin").mkdir(parents=True)
    (external / "bin/enso").write_text("external executable")
    linked = managed.paths.runtime_dir / "releases/link"
    linked.symlink_to(external)
    for candidate in (external, linked):
        with pytest.raises(UpdateError, match="prepared Enso release"):
            updates._select(managed.paths, candidate)
    assert (managed.paths.runtime_dir / "current").resolve() == managed.old


def test_snapshot_rejects_database_symlink_before_target_can_be_migrated(managed, tmp_path):
    state = queue(managed)
    external = tmp_path / "external.db"
    external.write_bytes(b"external database")
    (managed.paths.home / "enso.db").symlink_to(external)
    with pytest.raises(UpdateError, match=r"symbolic|symlink"):
        updates._snapshot(managed.paths, state)
    assert external.read_bytes() == b"external database"


def test_snapshot_path_tampering_is_rejected_before_restoring_anything(managed):
    state = queue(managed)
    managed.paths.config.write_text("configuration to keep")
    updates._snapshot(managed.paths, state)
    snapshot = managed.paths.runtime_dir / "operations" / state["id"] / "backup/snapshot.json"
    write_json(snapshot, {"paths": ["config.json", "../../outside"]})
    with pytest.raises(UpdateError, match="snapshot is incomplete"):
        updates._restore(managed.paths, state)
    assert managed.paths.config.read_text() == "configuration to keep"


def test_operation_directory_symlink_is_rejected_before_writing_outside_home(managed, tmp_path):
    outside = tmp_path / "outside-operations"
    outside.mkdir()
    (managed.paths.runtime_dir / "operations").symlink_to(outside)
    with pytest.raises(UpdateError, match=r"symbolic|symlink"):
        updates.request_apply(managed.paths)
    assert not list(outside.iterdir())


def test_notification_receipt_advances_only_after_send_and_retries(managed, monkeypatch):
    result = {"managed": True, "update_available": True, "available": "0.2.0", "current": "0.1.0"}
    attempts = []

    def send(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise UpdateError("chat is temporarily unavailable")

    monkeypatch.setattr(updates, "_send", send)
    with pytest.raises(UpdateError, match="temporarily"):
        updates.notify_available(managed.paths, result)
    assert not (managed.paths.runtime_dir / "notification.json").exists()
    assert updates.notify_available(managed.paths, result)
    assert not updates.notify_available(managed.paths, result)
    assert len(attempts) == 2


def test_outcome_origin_is_preserved_but_nightly_notification_uses_default(managed, monkeypatch):
    calls = []
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "unrelated-channel")
    monkeypatch.setenv("ENSO_JOB", "enso-update")
    monkeypatch.setattr(
        update_services, "run_command", lambda args, **kwargs: calls.append((args, kwargs)) or ""
    )
    updates._send(
        managed.paths, "update completed", {"transport": "slack", "channel": "C1", "thread": "1.2"}
    )
    env = calls[0][1]["env"]
    assert env["ENSO_ORIGIN_CHANNEL"] == "C1"
    assert env["ENSO_ORIGIN_THREAD_TS"] == "1.2"
    assert "ENSO_JOB" not in env
    updates._send(managed.paths, "new release available")
    assert not any(key.startswith("ENSO_ORIGIN_") for key in calls[1][1]["env"])


def test_gate_fails_closed_for_broken_symlink_and_state_parse_errors(enso_home, tmp_path):
    maintenance.prepare(enso_home)
    enso_home.maintenance.symlink_to(tmp_path / "missing")
    assert maintenance.paused(enso_home)
    malformed = enso_home.runtime_dir / "state.json"
    for data in ("[]", "{invalid", " " * 262145):
        malformed.write_text(data)
        with pytest.raises(UpdateError):
            read_json(malformed)
    with (
        maintenance.lock(enso_home),
        pytest.raises(UpdateError, match="in progress"),
        maintenance.lock(enso_home),
    ):
        pytest.fail("concurrent update entered the lock")


@pytest.mark.parametrize(
    "document",
    [
        {"version": 123},
        {"version": "0.1.0", "release_id": "../../outside"},
    ],
)
def test_corrupt_install_metadata_returns_one_json_error(managed, document):
    write_json(managed.paths.runtime_dir / "install.json", document)
    result = CliRunner().invoke(app, ["update", "status", "--json"])
    assert result.exit_code == 1
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert "Traceback" not in result.output


def test_corrupt_operation_metadata_returns_one_json_error(managed):
    write_json(managed.paths.update_state, {"id": "../outside"})
    result = CliRunner().invoke(app, ["update", "apply", "--json"])
    assert result.exit_code == 1
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert "Traceback" not in result.output


def test_service_command_cleans_descendants_even_when_parent_exits(tmp_path):
    pid_file = tmp_path / "child.pid"
    child = f"import os,time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    parent = (
        "import subprocess,sys; "
        f"p=subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "print(p.pid, flush=True)"
    )
    child_pid = None
    try:
        output = update_services.run_command(
            [sys.executable, "-c", parent], cwd=tmp_path, timeout=2
        )
        child_pid = int(output)
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child_pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        # An orphan can briefly remain a zombie until the OS reaps it; it must not be live.
        assert not status or status.startswith("Z")
    finally:
        if child_pid is None and pid_file.exists():
            child_pid = int(pid_file.read_text())
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


def test_snapshot_and_restore_refuse_linked_workspace_parents(managed, tmp_path):
    state = queue(managed)
    paths = managed.paths
    paths.config.write_text("before config")
    updates._snapshot(paths, state)
    before = paths.config.read_bytes()
    root = paths.workspace("default")
    root.rename(tmp_path / "original-workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    for operation in (updates._snapshot, updates._restore):
        with pytest.raises(UpdateError, match="symbolic link"):
            operation(paths, state)
    assert paths.config.read_bytes() == before
    assert not list(outside.iterdir())
