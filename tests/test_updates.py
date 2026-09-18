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
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
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
    real_release = updates._release
    real_candidate = updates._candidate
    monkeypatch.setattr(updates, "_release", lambda *args, **kwargs: release)
    monkeypatch.setattr(update_services, "discover", lambda *args: dict(services))
    monkeypatch.setattr(update_services, "launch", lambda *args: events.append(("launch", args)))
    monkeypatch.setattr(update_services, "cleanup_finished", lambda *args: True)
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
    monkeypatch.setattr(
        updates, "_candidate_plan", lambda paths, *args: updates.migration_plan(paths)
    )
    return SimpleNamespace(
        paths=paths,
        old=old,
        receipt=receipt,
        release=release,
        services=services,
        events=events,
        real_release=real_release,
        real_candidate=real_candidate,
    )


def queue(managed):
    result = updates.request_apply(managed.paths)
    assert result["operation"]["status"] == "queued"
    return read_json(managed.paths.update_state)


def test_queue_pins_artifacts_and_origin_and_serializes_requests(managed, monkeypatch):
    monkeypatch.setenv("ENSO_ORIGIN_TRANSPORT", "slack")
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C123")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "123.456")
    monkeypatch.setenv("ENSO_WORKSPACE", "team")
    managed.paths.workspace("team").mkdir()
    state = queue(managed)
    assert state["manifest"]["wheel"]["url"] == (
        "https://releases.example.test/v0.2.0/enso-0.2.0-py3-none-any.whl"
    )
    assert state["origin"] == {
        "transport": "slack",
        "channel": "C123",
        "thread": "123.456",
        "workspace": "team",
    }
    with pytest.raises(UpdateError, match="pending"):
        updates.request_apply(managed.paths)
    assert read_json(managed.paths.update_state)["id"] == state["id"]
    assert [name for name, _ in managed.events].count("launch") == 1
    assert not maintenance.paused(managed.paths)


@pytest.mark.parametrize("command", [["apply"], ["check", "--notify"]])
def test_update_notifications_default_to_default_and_save_explicit_override(
    managed, monkeypatch, command
):
    monkeypatch.delenv("ENSO_WORKSPACE")
    cli = CliRunner()
    feed_calls = []

    def release(*args):
        feed_calls.append(args)
        return managed.release

    monkeypatch.setattr(updates, "_release", release)
    args = ["update", *command, "--json"]
    invalid = cli.invoke(app, [*args, "--workspace", "missing"])
    assert invalid.exit_code == 1 and "missing" in json.loads(invalid.stdout)["error"]
    assert not feed_calls and not managed.events and not managed.paths.update_state.exists()

    managed.paths.workspace("team").mkdir()
    sent = []
    monkeypatch.setattr(
        update_services, "run_command", lambda args, **kwargs: sent.append(kwargs["env"]) or ""
    )
    selected = cli.invoke(app, [*args, "--workspace", "team"])
    assert selected.exit_code == 0, selected.output
    if command[0] == "apply":
        state = read_json(managed.paths.update_state)
        assert state["origin"]["workspace"] == "team"
        managed.paths.config.write_text("configured")
        updates._notify_outcome(managed.paths, state | {"status": "succeeded"})
    assert [env["ENSO_WORKSPACE"] for env in sent] == ["team"]


@pytest.mark.parametrize("command", [["apply"], ["check", "--notify", "--quiet"]])
def test_update_commands_work_without_workspace_arguments(managed, monkeypatch, command):
    monkeypatch.delenv("ENSO_WORKSPACE")
    sent = []
    monkeypatch.setattr(updates, "_send", lambda paths, text, origin: sent.append(origin))
    result = CliRunner().invoke(app, ["update", *command, "--json"])
    assert result.exit_code == 0, result.output
    if command[0] == "apply":
        assert read_json(managed.paths.update_state)["origin"]["workspace"] == "default"
    else:
        assert sent == [{"workspace": "default"}]


def test_read_only_update_check_needs_no_workspace(managed, monkeypatch):
    monkeypatch.delenv("ENSO_WORKSPACE")
    result = CliRunner().invoke(app, ["update", "check", "--json"])
    assert result.exit_code == 0 and json.loads(result.stdout)["update_available"]
    assert not managed.events and not managed.paths.update_state.exists()


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


@pytest.mark.parametrize("feed", [None, "https://private.example.test/stable/release.json"])
@pytest.mark.parametrize(
    "manifest", [None, "release.json", "https://private.example.test/release.json"]
)
def test_direct_install_persists_default_or_explicit_feed(
    enso_home, monkeypatch, tmp_path, feed, manifest
):
    source = manifest or releases.DEFAULT_FEED
    selected = releases.Release(
        "0.2.0",
        "a" * 40,
        ">=3.14",
        releases.Artifact("enso-0.2.0-py3-none-any.whl", "b" * 64),
        releases.Artifact("constraints.txt", "c" * 64),
        source,
    )
    sources = []
    monkeypatch.setattr(
        releases, "load_release", lambda source, **kwargs: sources.append(source) or selected
    )
    monkeypatch.setattr(updates, "receiver_active", lambda paths: False)
    monkeypatch.setattr(updates.web, "status", lambda paths: SimpleNamespace(running=False))

    def candidate(paths, release, receipt):
        target = paths.runtime_dir / "releases" / release.release_id
        (target / "bin").mkdir(parents=True)
        (target / "bin/enso").write_text("#!/bin/sh\nexit 0\n")
        (target / "bin/enso").chmod(0o755)
        return target

    monkeypatch.setattr(updates, "_candidate", candidate)
    monkeypatch.setattr(releases, "ensure_uv", lambda runtime: str(runtime / "tools/uv"))
    monkeypatch.chdir(tmp_path)
    token = tmp_path / "private-token"
    token.write_text("private-value")
    updates.install(
        enso_home, manifest, bin_dir=tmp_path / "bin", extras=(), feed=feed, token_file=token
    )
    assert sources == [manifest or releases.DEFAULT_FEED]
    receipt = updates.installed(enso_home)
    assert receipt["feed"] == (feed or releases.DEFAULT_FEED)
    assert receipt["token_origin"] == (
        None
        if manifest == "release.json"
        else "https://private.example.test:443"
        if manifest
        else "https://github.com:443"
    )


@pytest.mark.parametrize("version", ["0.2.0.dev1", "0.3.0rc1", "0.2.0+local"])
def test_unmanaged_development_check_reports_versions_without_upgrade_order(
    enso_home, monkeypatch, version
):
    monkeypatch.setattr(updates, "__version__", version)
    selected = releases.Release(
        "0.2.0",
        "a" * 40,
        ">=3.14",
        releases.Artifact("enso.whl", "b" * 64),
        releases.Artifact("constraints.txt", "c" * 64),
        releases.DEFAULT_FEED,
    )
    monkeypatch.setattr(releases, "load_release", lambda *args, **kwargs: selected)
    result = updates.check(enso_home)
    assert result["current"] == version and result["available"] == "0.2.0"
    assert result["development"] and result["adoption_required"]
    assert not result["update_available"]
    text = updates.check_message(result)
    assert version in text and "0.2.0" in text and "compatible published release" in text
    assert "ahead" not in text and "up to date" not in text and "--adopt" not in text
    monkeypatch.setattr(
        releases, "load_release", lambda *args, **kwargs: replace(selected, version="0.2.1rc1")
    )
    with pytest.raises(UpdateError, match="stable"):
        updates.check(enso_home)


@pytest.mark.parametrize("version", ["0.2.0", "0.2.1", "0.1.9"])
def test_unmanaged_checks_default_feed_and_reports_adoption(enso_home, monkeypatch, version):
    monkeypatch.setattr(updates, "__version__", "0.2.0")
    selected = releases.Release(
        version,
        "a" * 40,
        ">=3.14",
        releases.Artifact("enso.whl", "b" * 64),
        releases.Artifact("constraints.txt", "c" * 64),
        releases.DEFAULT_FEED,
    )
    sources = []
    monkeypatch.setattr(
        releases, "load_release", lambda source, **kwargs: sources.append(source) or selected
    )
    result = updates.check(enso_home)
    assert sources == [releases.DEFAULT_FEED]
    assert result["available"] == version
    assert result["update_available"] is (version == "0.2.1")
    assert result["adoption_required"] and not result["managed"]
    text = updates.check_message(result)
    assert "unmanaged" in text
    assert ("enso update install --adopt" in text) is (version != "0.1.9")
    sent = []
    monkeypatch.setattr(updates, "_send", lambda *args: sent.append(args))
    assert updates.notify_available(enso_home, result, workspace="default")
    assert not updates.notify_available(enso_home, result, workspace="default")
    assert len(sent) == 1


def test_recorded_feed_wins_and_manifest_override_does_not_replace_it(managed, monkeypatch):
    sources = []
    monkeypatch.setattr(
        releases, "load_release", lambda source, **kwargs: sources.append(source) or managed.release
    )
    # Restore the real resolver that the managed fixture replaces for transaction tests.
    resolver = managed.real_release
    resolver(managed.paths)
    resolver(managed.paths, "https://staging.example.test/release.json")
    assert sources == [managed.receipt["feed"], "https://staging.example.test/release.json"]
    assert updates.installed(managed.paths)["feed"] == managed.receipt["feed"]


@pytest.mark.parametrize("origin_recorded", [True, False])
def test_saved_release_token_stays_at_install_origin_for_feed_and_candidate(
    managed, monkeypatch, origin_recorded
):
    receipt = dict(managed.receipt, token_file=str(managed.paths.runtime_dir / "release.token"))
    if origin_recorded:
        receipt.update(feed=releases.DEFAULT_FEED, token_origin="https://releases.example.test:443")
    write_json(managed.paths.runtime_dir / "install.json", receipt)
    sent = []
    monkeypatch.setattr(
        releases,
        "load_release",
        lambda source, **kwargs: sent.append((source, kwargs["token_file"])) or managed.release,
    )
    managed.real_release(managed.paths)
    assert sent[-1][1] == (None if origin_recorded else managed.paths.runtime_dir / "release.token")
    for source, authorized in (
        ("https://releases.example.test:443/another/release.json", True),
        ("https://releases.example.test:444/release.json", False),
        ("https://staging.example.test/release.json", False),
        (str(managed.paths.home / "release.json"), False),
    ):
        managed.real_release(managed.paths, source)
        assert sent[-1] == (
            source,
            managed.paths.runtime_dir / "release.token" if authorized else None,
        )
        monkeypatch.setattr(
            releases,
            "prepare_release",
            lambda release, target, **kwargs: sent.append((release.source, kwargs["token_file"])),
        )
        # The managed fixture replaces candidate; call the production function retained below.
        candidate = managed.real_candidate
        candidate(managed.paths, replace(managed.release, source=source), receipt)
        assert sent[-1][1] == (managed.paths.runtime_dir / "release.token" if authorized else None)


def test_upgrade_reuses_bootstrapped_uv_without_requiring_it_on_path(enso_home, monkeypatch):
    private_uv = enso_home.runtime_dir / "tools/uv"
    private_uv.parent.mkdir(parents=True)
    private_uv.write_text("private uv")
    captured = []
    monkeypatch.setattr(
        releases, "prepare_release", lambda release, target, **kwargs: captured.append(kwargs)
    )
    selected = SimpleNamespace(release_id="0.2.0-" + "a" * 12, source=releases.DEFAULT_FEED)
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
    snapshot = managed.paths.runtime_dir / "operations" / state["id"] / "snapshot.json"
    write_json(snapshot, {"paths": ["config.json", "../../outside"]})
    with pytest.raises(UpdateError, match="snapshot is incomplete"):
        updates._restore(managed.paths, state)
    assert managed.paths.config.read_text() == "configuration to keep"


def test_rollback_restores_all_workspace_jobs_without_replacing_memories(managed):
    state = queue(managed)
    paths = managed.paths
    for name in ("team", "research"):
        paths.workspace(name).mkdir(parents=True)
    job = paths.workspace_jobs("team") / "memory/JOB.md"
    job.parent.mkdir(parents=True)
    job.write_text("customized job")
    note = paths.workspace_memory("team") / "Keep.md"
    note.parent.mkdir()
    note.write_text("original note")
    updates._snapshot(paths, state)
    job.write_text("changed during preparation")
    fresh = paths.workspace_jobs("research") / "memory/JOB.md"
    fresh.parent.mkdir(parents=True)
    fresh.write_text("new bundled job")
    note.write_text("human correction")
    updates._restore(paths, state)
    assert job.read_text() == "customized job"
    assert not fresh.exists()
    assert note.read_text() == "human correction"


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
        updates.notify_available(managed.paths, result, workspace="default")
    assert not (managed.paths.runtime_dir / "notification.json").exists()
    assert updates.notify_available(managed.paths, result, workspace="default")
    assert not updates.notify_available(managed.paths, result, workspace="default")
    assert len(attempts) == 2


def test_outcome_origin_is_preserved_but_nightly_notification_uses_default(managed, monkeypatch):
    calls = []
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "unrelated-channel")
    monkeypatch.setenv("ENSO_JOB", "enso-update")
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
    monkeypatch.setattr(
        update_services, "run_command", lambda args, **kwargs: calls.append((args, kwargs)) or ""
    )
    updates._send(
        managed.paths,
        "update completed",
        {"transport": "slack", "channel": "C1", "thread": "1.2", "workspace": "team"},
    )
    env = calls[0][1]["env"]
    assert env["ENSO_ORIGIN_CHANNEL"] == "C1"
    assert env["ENSO_ORIGIN_THREAD_TS"] == "1.2"
    assert env["ENSO_WORKSPACE"] == "team"
    assert "ENSO_JOB" not in env
    updates._send(managed.paths, "new release available", {"workspace": "default"})
    assert not any(key.startswith("ENSO_ORIGIN_") for key in calls[1][1]["env"])
    assert calls[1][1]["env"]["ENSO_WORKSPACE"] == "default"


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


def test_repeated_updates_bound_snapshots_operations_and_runtimes(managed, monkeypatch):
    paths = managed.paths
    for index in range(3):
        release = replace(managed.release, version=f"0.2.{index}", commit=f"{index + 1:x}" * 40)
        monkeypatch.setattr(updates, "_release", lambda *args, selected=release: selected)
        state = queue(managed)
        updates.run_update(paths, state["id"])
        assert read_json(paths.update_state)["status"] == "succeeded"
        assert list((paths.runtime_dir / "operations").iterdir()) == [
            paths.runtime_dir / "operations" / state["id"]
        ]
        assert not list((paths.runtime_dir / "operations").glob("*/backup"))
        assert not list((paths.runtime_dir / "operations").glob("*/failed-state-*"))
        assert len(list((paths.runtime_dir / "releases").iterdir())) == 2
        assert (paths.runtime_dir / "current").resolve().name == release.release_id


def test_cleanup_failure_never_rolls_back_success_and_retries(managed, monkeypatch):
    state = queue(managed)
    remove = updates._remove_owned

    def cannot_remove(path):
        if path.name == "backup":
            raise OSError("disk problem")
        remove(path)

    monkeypatch.setattr(updates, "_remove_owned", cannot_remove)
    updates.run_update(managed.paths, state["id"])
    outcome = read_json(managed.paths.update_state)
    assert outcome["status"] == "succeeded" and outcome["cleanup_error"]
    assert updates.installed(managed.paths)["version"] == managed.release.version
    managed.paths.config.write_text("new work after successful update")
    managed.events.clear()
    monkeypatch.setattr(updates, "_remove_owned", remove)
    updates.run_update(managed.paths, state["id"])
    assert "cleanup_error" not in read_json(managed.paths.update_state)
    assert managed.paths.config.read_text() == "new work after successful update"
    assert not any(name in {"stop", "start", "healthy"} for name, _ in managed.events)


def test_gate_removal_error_after_durable_success_never_enters_rollback(managed, monkeypatch):
    from pathlib import Path

    state = queue(managed)
    unlink = Path.unlink

    def broken_unlink(path, *args, **kwargs):
        if path == managed.paths.maintenance:
            raise OSError("interrupted gate cleanup")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", broken_unlink)
    with pytest.raises(OSError, match="gate cleanup"):
        updates.run_update(managed.paths, state["id"])
    assert read_json(managed.paths.update_state)["status"] == "succeeded"
    assert updates.installed(managed.paths)["version"] == managed.release.version
    monkeypatch.setattr(Path, "unlink", unlink)
    managed.events.clear()
    updates.run_update(managed.paths, state["id"])
    assert not maintenance.paused(managed.paths)
    assert not any(name in {"stop", "start", "healthy"} for name, _ in managed.events)


def test_candidate_declared_move_rolls_back_new_parent_and_migration_marker(managed, monkeypatch):
    paths = managed.paths
    legacy = paths.home / "legacy"
    legacy.mkdir()
    (legacy / "workflow.json").write_text('{"old": true}')
    marker = paths.home / ".migrations.json"
    write_json(marker, {"revision": 0})
    destination = paths.home / "new-core/workflows"
    monkeypatch.setattr(
        updates,
        "_candidate_plan",
        lambda paths, *args: updates.update_snapshot.plan(
            paths, [*updates.migration_plan(paths), "legacy", "new-core/workflows"]
        ),
    )

    def run(args, **kwargs):
        if args[-1] == "_prepare-home":
            destination.parent.mkdir()
            legacy.rename(destination)
            (destination / "workflow.json").write_text('{"new": true}')
            write_json(marker, {"revision": 1})
            raise UpdateError("later migration failed")
        return ""

    monkeypatch.setattr(update_services, "run_command", run)
    state = queue(managed)
    updates.run_update(paths, state["id"])
    assert read_json(paths.update_state)["status"] == "rolled_back"
    assert read_json(marker) == {"revision": 0}
    assert not destination.parent.exists()
    assert (legacy / "workflow.json").read_text() == '{"old": true}'
    assert list((paths.runtime_dir / "releases").iterdir()) == [managed.old]
    operation = paths.runtime_dir / "operations" / state["id"]
    assert not (operation / "backup").exists() and not list(operation.glob("failed-state-*"))


def test_preparation_migrates_before_current_config_and_db_readers(managed, monkeypatch):
    paths = managed.paths
    paths.config.write_text("old config")
    write_json(paths.maintenance, {"operation_id": "a" * 32})
    events = []
    monkeypatch.setattr(updates.migrations, "apply", lambda paths: events.append("migrate"))
    monkeypatch.setattr(
        updates,
        "load_config",
        lambda paths: events.append("config") or SimpleNamespace(defaults=None, workspaces={}),
    )
    monkeypatch.setattr(updates.db, "initialize", lambda paths: events.append("database"))
    monkeypatch.setattr(
        updates.workspaces, "reconcile_bundles", lambda *args, **kwargs: events.append("bundles")
    )
    updates.prepare_home(paths)
    assert events == ["migrate", "config", "database", "bundles"]


def test_uninitialized_install_upgrades_directly_to_latest_home_revision(tmp_path, monkeypatch):
    from enso.config import Paths

    paths = Paths(tmp_path / "fresh-home")
    write_json(paths.maintenance, {"operation_id": "a" * 32})
    ran = []
    monkeypatch.setattr(
        updates.migrations,
        "MIGRATIONS",
        (updates.migrations.Migration(1, "old shape", lambda _: (), lambda _: ran.append(True)),),
    )
    updates.prepare_home(paths)
    assert read_json(paths.home / ".migrations.json") == {"revision": 1}
    assert not ran and not paths.config.exists() and not paths.db.exists()


def test_adoption_preflight_refuses_pending_changes_without_mutation(enso_home):
    (enso_home.home / ".migrations.json").unlink()  # an older home with shipped steps pending
    with pytest.raises(UpdateError, match="Adopt a compatible release first"):
        updates.validate_home(enso_home)
    assert not enso_home.config.exists() and not enso_home.db.exists()
    assert not (enso_home.home / ".migrations.json").exists()


def test_gate_cleanup_error_after_rollback_never_restores_over_new_work(managed, monkeypatch):
    paths = managed.paths
    state = queue(managed)
    paths.config.write_text("original data")

    def prepare_fails(args, **kwargs):
        if args[-1] == "_prepare-home":
            paths.config.write_text("migrated data")
            raise UpdateError("migration failure")
        return ""

    monkeypatch.setattr(update_services, "run_command", prepare_fails)
    sync = updates.sync_directory

    def sync_fails_after_admission(path):
        if path == paths.runtime_dir and not maintenance.paused(paths):
            raise UpdateError("gate directory sync failed")
        sync(path)

    monkeypatch.setattr(updates, "sync_directory", sync_fails_after_admission)
    with pytest.raises(UpdateError, match="gate directory sync failed"):
        updates.run_update(paths, state["id"])
    assert read_json(paths.update_state)["status"] == "rolled_back"
    assert paths.config.read_text() == "original data" and not maintenance.paused(paths)
    paths.config.write_text("work accepted after rollback")
    monkeypatch.setattr(updates, "sync_directory", sync)
    managed.events.clear()
    updates.run_update(paths, state["id"])
    assert paths.config.read_text() == "work accepted after rollback"
    assert not any(name in {"stop", "start", "healthy"} for name, _ in managed.events)
