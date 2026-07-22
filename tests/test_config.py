"""Tests for configuration management."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from enso.config import load_config, save_config


def test_load_creates_default(tmp_enso):
    """Loading with no config file creates a default."""
    config = load_config()
    assert "working_dir" in config
    assert "transport" in config
    assert config["transport"] == ""
    assert "transports" in config
    assert config["logging"]["level"] == "INFO"
    assert config["logging"]["enso_level"] == "INFO"
    assert config["logging"]["noisy_level"] == "WARNING"
    assert config["logging"]["debug_prompts"] is False
    assert config["logging"]["debug_events"] is False
    assert "providers" in config
    assert config["runs"] == {"keep": 500, "max_age_days": 30}
    assert "tasks" not in config


def test_save_and_load_roundtrip(tmp_enso):
    """Config survives a save/load roundtrip."""
    config = {
        "working_dir": "/tmp/test",
        "transport": "telegram",
        "transports": {"telegram": {"bot_token": "test-token"}},
        "providers": {"claude": {"path": "claude", "models": ["opus"]}},
    }
    save_config(config)
    loaded = load_config()
    assert loaded["transport"] == "telegram"
    assert loaded["transports"]["telegram"]["bot_token"] == "test-token"
    assert loaded["providers"]["claude"]["models"] == ["opus"]
    assert loaded["logging"]["level"] == "INFO"
    assert loaded["logging"]["debug_prompts"] is False


def test_load_merges_missing_logging_defaults(tmp_enso):
    """Existing configs get logging defaults without losing user choices."""
    config = {
        "working_dir": "/tmp/test",
        "transport": "telegram",
        "transports": {},
        "logging": {"level": "ERROR"},
        "providers": {},
    }
    save_config(config)
    loaded = load_config()
    assert loaded["logging"]["level"] == "ERROR"
    assert loaded["logging"]["enso_level"] == "INFO"
    assert loaded["logging"]["noisy_level"] == "WARNING"
    assert loaded["logging"]["debug_prompts"] is False
    assert loaded["logging"]["debug_events"] is False
    assert loaded["logging"]["loggers"] == {}


def test_default_config_has_job_runner(tmp_enso):
    """Fresh configs expose an independent job_runner, defaulting to print."""
    config = load_config()
    claude = config["providers"]["claude"]
    assert claude["runner"] == "print"
    assert claude["job_runner"] == "print"


def test_default_config_has_codex_model_aliases(tmp_enso):
    config = load_config()
    assert config["providers"]["codex"]["models"] == ["sol", "terra", "luna"]


def test_load_backfills_job_runner_without_clobbering(tmp_enso):
    """An existing claude provider gains job_runner but keeps custom values."""
    config = {
        "working_dir": "/tmp/test",
        "transport": "telegram",
        "transports": {},
        "providers": {
            "claude": {
                "path": "/custom/claude",
                "runner": "kage",
                "models": ["opus"],
            },
        },
    }
    save_config(config)
    loaded = load_config()
    claude = loaded["providers"]["claude"]
    # Backfilled default.
    assert claude["job_runner"] == "print"
    # User choices preserved.
    assert claude["path"] == "/custom/claude"
    assert claude["runner"] == "kage"
    assert claude["models"] == ["opus"]


def test_load_backfills_codex_aliases_and_preserves_custom_models(tmp_enso):
    config = {
        "working_dir": "/tmp/test",
        "transport": "telegram",
        "transports": {},
        "providers": {
            "codex": {
                "path": "/custom/codex",
                "models": ["gpt-5.6-sol", "gpt-5.5", "custom-codex-model"],
            },
        },
    }
    save_config(config)
    loaded = load_config()
    codex = loaded["providers"]["codex"]
    assert codex["path"] == "/custom/codex"
    assert codex["models"] == [
        "sol", "terra", "luna", "gpt-5.6-sol", "gpt-5.5", "custom-codex-model",
    ]


def test_load_removes_unsupported_provider_config(tmp_enso):
    config_file = Path(tmp_enso) / "config.json"
    config_file.write_text(json.dumps({
        "providers": {
            "claude": {"path": "claude", "models": ["opus"]},
            "retired": {"path": "retired", "models": ["old-model"]},
        },
    }))

    loaded = load_config()

    assert set(loaded["providers"]) == {"claude"}
    assert set(json.loads(config_file.read_text())["providers"]) == {"claude"}


def test_load_replaces_invalid_logging_with_defaults(tmp_enso):
    """Invalid logging config is normalized to defaults."""
    config = {
        "working_dir": "/tmp/test",
        "transport": "telegram",
        "transports": {},
        "logging": None,
        "providers": {},
    }
    save_config(config)
    loaded = load_config()
    assert loaded["logging"]["level"] == "INFO"
    assert loaded["logging"]["debug_prompts"] is False


def test_config_file_permissions(tmp_enso):
    """Config file has restricted permissions."""
    config = load_config()
    save_config(config)
    config_file = os.path.join(tmp_enso, "config.json")
    stat = os.stat(config_file)
    assert stat.st_mode & 0o777 == 0o600


def test_load_migrates_legacy_task_retention_and_drops_tasks(tmp_enso):
    """Task retention survives the task-system removal."""
    config_file = os.path.join(tmp_enso, "config.json")
    with open(config_file, "w") as f:
        json.dump({
            "tasks": {
                "enabled": False,
                "runs_keep": 123,
                "runs_max_age_days": 45,
            },
        }, f)

    loaded = load_config()

    assert loaded["runs"] == {"keep": 123, "max_age_days": 45}
    assert "tasks" not in loaded
    with open(config_file) as f:
        persisted = json.load(f)
    assert persisted["runs"] == {"keep": 123, "max_age_days": 45}
    assert "tasks" not in persisted


def test_explicit_runs_config_wins_over_legacy_task_retention(tmp_enso):
    """New retention choices win while missing values still migrate."""
    config_file = os.path.join(tmp_enso, "config.json")
    with open(config_file, "w") as f:
        json.dump({
            "tasks": {
                "runs_keep": 123,
                "runs_max_age_days": 45,
            },
            "runs": {"keep": 7},
        }, f)

    loaded = load_config()

    assert loaded["runs"] == {"keep": 7, "max_age_days": 45}
    assert "tasks" not in loaded


def test_save_removes_obsolete_tasks_block(tmp_enso):
    save_config({
        "tasks": {"enabled": True, "runs_keep": 12},
        "runs": {"keep": 8, "max_age_days": 3},
    })

    with open(os.path.join(tmp_enso, "config.json")) as f:
        persisted = json.load(f)

    assert persisted["runs"] == {"keep": 8, "max_age_days": 3}
    assert "tasks" not in persisted


def test_save_failure_preserves_existing_config(tmp_enso, monkeypatch):
    config_file = Path(tmp_enso, "config.json")
    original = b'{"working_dir": "/keep/me"}\n'
    config_file.write_bytes(original)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("enso.config.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_config({"working_dir": "/new/value"})

    assert config_file.read_bytes() == original
    assert list(Path(tmp_enso).glob("*.tmp")) == []


def test_load_uses_migrated_config_when_persistence_fails(tmp_enso, monkeypatch):
    config_file = Path(tmp_enso, "config.json")
    config_file.write_text(json.dumps({
        "tasks": {"runs_keep": 17, "runs_max_age_days": 4},
    }))

    def fail_save(_config):
        raise OSError("read-only config")

    monkeypatch.setattr("enso.config.save_config", fail_save)

    loaded = load_config()

    assert loaded["runs"] == {"keep": 17, "max_age_days": 4}
    assert "tasks" not in loaded
