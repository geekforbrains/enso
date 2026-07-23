"""Tests for deterministic Enso self-updates."""

from __future__ import annotations

import asyncio

import pytest

from enso import __version__, updater
from enso.commands import cmd_update_async
from enso.core import Runtime

REVISION = "a" * 40
OLD_REVISION = "b" * 40


def test_already_current_skips_build(tmp_enso, sample_config, monkeypatch):
    monkeypatch.setattr(updater, "_resolve_remote_revision", lambda: REVISION)
    monkeypatch.setattr(updater, "_installed_revision", lambda: REVISION)
    monkeypatch.setattr(
        updater,
        "_build_wheel",
        lambda *_args, **_kwargs: pytest.fail("current install should not rebuild"),
    )

    result = updater.update_enso(sample_config)

    assert result.status == "current"
    assert "Already up to date" in result.message
    assert result.revision == REVISION
    assert updater._load_state()["revision"] == REVISION


def test_editable_checkout_ahead_of_stable_is_not_downgraded(
    tmp_enso, sample_config, monkeypatch,
):
    monkeypatch.setattr(updater, "_resolve_remote_revision", lambda: REVISION)
    monkeypatch.setattr(updater, "_installed_revision", lambda: OLD_REVISION)
    monkeypatch.setattr(updater, "_editable_install_path", lambda: "/src/enso")
    monkeypatch.setattr(updater, "_checkout_contains_revision", lambda *_args: True)
    monkeypatch.setattr(
        updater,
        "_checkout_revision",
        lambda *_args: pytest.fail("an ahead checkout must not be downgraded"),
    )

    result = updater.update_enso(sample_config)

    assert result.status == "current"
    assert "ahead of stable main" in result.message
    assert result.revision == OLD_REVISION


def test_update_validates_before_live_install(tmp_enso, sample_config, monkeypatch):
    stages: list[str] = []
    monkeypatch.setattr(updater, "_resolve_remote_revision", lambda: REVISION)
    monkeypatch.setattr(updater, "_installed_revision", lambda: OLD_REVISION)
    monkeypatch.setattr(updater, "_editable_install_path", lambda: "")
    monkeypatch.setattr(
        updater,
        "_checkout_revision",
        lambda *_args: stages.append("checkout"),
    )
    monkeypatch.setattr(updater, "_source_version", lambda _source: "9.1.0")
    monkeypatch.setattr(
        updater,
        "_build_wheel",
        lambda *_args: stages.append("build") or "/tmp/enso.whl",
    )
    monkeypatch.setattr(
        updater,
        "_validate_wheel",
        lambda *_args: stages.append("validate"),
    )
    monkeypatch.setattr(
        updater,
        "_install_wheel",
        lambda *_args: stages.append("install"),
    )

    result = updater.update_enso(sample_config)

    assert result.status == "updated"
    assert result.restart_required is True
    assert stages == ["checkout", "build", "validate", "install"]
    assert updater._load_state()["version"] == "9.1.0"


def test_validation_failure_does_not_install(tmp_enso, sample_config, monkeypatch):
    monkeypatch.setattr(updater, "_resolve_remote_revision", lambda: REVISION)
    monkeypatch.setattr(updater, "_installed_revision", lambda: OLD_REVISION)
    monkeypatch.setattr(updater, "_editable_install_path", lambda: "")
    monkeypatch.setattr(updater, "_checkout_revision", lambda *_args: None)
    monkeypatch.setattr(updater, "_source_version", lambda _source: "9.1.0")
    monkeypatch.setattr(updater, "_build_wheel", lambda *_args: "/tmp/enso.whl")
    monkeypatch.setattr(
        updater,
        "_validate_wheel",
        lambda *_args: (_ for _ in ()).throw(updater.UpdateError("running tests", "failed")),
    )
    monkeypatch.setattr(
        updater,
        "_install_wheel",
        lambda *_args: pytest.fail("failed validation must not install"),
    )

    result = updater.update_enso(sample_config)

    assert result.status == "failed"
    assert "installed Enso was not changed" in result.message


def test_validation_environment_removes_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret")
    monkeypatch.setenv("ENSO_ORIGIN_USER_ID", "123")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = updater._sanitized_env(str(tmp_path))

    assert "OPENAI_API_KEY" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "ENSO_ORIGIN_USER_ID" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == str(tmp_path)


def test_live_install_forces_same_version_wheel(sample_config, monkeypatch):
    commands: list[list[str]] = []

    def capture(cmd, **_kwargs):
        commands.append(cmd)
        return ""

    monkeypatch.setattr(updater, "_run_checked", capture)

    updater._install_wheel("/tmp/enso-0.17.0.whl", sample_config, "/tmp")

    assert len(commands) == 3
    assert "--upgrade" in commands[0]
    assert "--force-reinstall" in commands[1]
    assert "--no-deps" in commands[1]
    assert commands[1][-1] == "/tmp/enso-0.17.0.whl"
    assert commands[2][-2:] == ["enso.cli", "--version"]


def test_confirmation_is_private_and_consumed(tmp_enso, monkeypatch):
    result = updater.UpdateResult("updated", "done", REVISION, "9.1.0")
    monkeypatch.setattr(updater, "installed_service_names", lambda: ["agent", "web"])
    monkeypatch.setattr(updater, "_service_running", lambda _service: True)

    updater.queue_update_confirmation(
        result,
        transport="slack",
        channel="C123",
        thread="1234.5",
    )

    pending = updater.pending_update_confirmation("slack")
    assert pending is not None
    assert pending["channel"] == "C123"
    assert "restarted successfully" in updater.update_confirmation_message(pending)
    updater.clear_update_confirmation(pending["id"])
    assert updater.pending_update_confirmation("slack") is None
    assert oct(__import__("os").stat(updater._state_path()).st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
async def test_command_refuses_while_agent_work_is_active(sample_config):
    runtime = Runtime(sample_config)
    task = asyncio.create_task(asyncio.sleep(10))
    runtime.running_task_by_chat["chat"] = task
    try:
        result = await cmd_update_async(runtime)
    finally:
        task.cancel()

    assert result.status == "blocked"
    assert "active agent work" in result.message
    assert runtime._update_in_progress is False


@pytest.mark.asyncio
async def test_command_holds_update_gate_until_restart(sample_config, monkeypatch):
    runtime = Runtime(sample_config)
    installed = updater.UpdateResult("updated", "restarting", REVISION, __version__)
    monkeypatch.setattr(updater, "update_enso", lambda _config: installed)

    result = await cmd_update_async(runtime)

    assert result is installed
    assert runtime._update_in_progress is True
