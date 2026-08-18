"""Tests for configuration management."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from enso.config import (
    DEFAULT_PROVIDERS,
    ConfigError,
    SetupState,
    config_transaction,
    load_config,
    provider_models,
    save_config,
    setup_state,
)
from enso.providers import PROVIDER_NAMES, provider_class


def test_load_missing_fails_closed_without_writing(tmp_enso):
    with pytest.raises(ConfigError, match="missing"):
        load_config()

    assert not Path(tmp_enso, "config.json").exists()


def test_setup_load_returns_fresh_defaults_without_writing(tmp_enso):
    """A missing config is an in-memory fresh-install candidate, not a write."""
    config = load_config(allow_missing=True)
    assert "working_dir" not in config
    assert config["workspaces"] == {
        "default": {
            "path": os.path.join(tmp_enso, "workspaces", "default"),
            "policy": "admin",
            "concurrency": 1,
        },
    }
    assert config["policies"]["admin"] == {
        "unrestricted": True,
        "providers": list(PROVIDER_NAMES),
        "default_provider": "claude",
        "chat_commands": "*",
    }
    assert "transport" in config
    assert config["transport"] == ""
    assert "transports" in config
    assert config["logging"]["level"] == "INFO"
    assert config["logging"]["enso_level"] == "INFO"
    assert config["logging"]["noisy_level"] == "WARNING"
    assert config["logging"]["debug_prompts"] is False
    assert config["logging"]["debug_events"] is False
    assert "providers" in config
    assert config["agent"] == {"timeout": 30 * 60}
    assert config["runs"] == {"keep": 500, "max_age_days": 30}
    assert config["setup"] == {"completed_at": None}
    assert "tasks" not in config
    assert not Path(tmp_enso, "config.json").exists()


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", "null", '"config"'],
)
def test_load_rejects_malformed_or_non_object_config_without_replacing(
    tmp_enso, content
):
    config_file = Path(tmp_enso, "config.json")
    config_file.write_text(content)
    original = config_file.read_bytes()

    with pytest.raises(ConfigError, match=r"config\.json"):
        load_config()

    assert config_file.read_bytes() == original


def test_load_rejects_invalid_utf8_without_replacing(tmp_enso):
    config_file = Path(tmp_enso, "config.json")
    config_file.write_bytes(b"{\xff}")
    original = config_file.read_bytes()

    with pytest.raises(ConfigError, match=r"config\.json"):
        load_config()

    assert config_file.read_bytes() == original


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_load_rejects_non_regular_config_paths(tmp_enso, tmp_path, kind):
    config_file = Path(tmp_enso, "config.json")
    if kind == "symlink":
        target = tmp_path / "outside-config.json"
        target.write_text('{"transport": "telegram"}\n')
        config_file.symlink_to(target)
    else:
        config_file.mkdir()

    with pytest.raises(ConfigError, match="regular file"):
        load_config()

    if kind == "symlink":
        assert target.read_text() == '{"transport": "telegram"}\n'


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, SetupState.PRE_FEATURE),
        ({"setup": {"completed_at": None}}, SetupState.INCOMPLETE),
        (
            {"setup": {"completed_at": "2026-08-18T12:34:56+00:00"}},
            SetupState.COMPLETE,
        ),
    ],
)
def test_setup_state_has_explicit_backward_compatible_meanings(config, expected):
    assert setup_state(config) is expected


@pytest.mark.parametrize(
    "setup",
    [None, [], {}, {"completed_at": 123}, {"completed_at": "yesterday"}],
)
def test_setup_state_rejects_malformed_values(setup):
    with pytest.raises(ConfigError, match=r"setup\.completed_at"):
        setup_state({"setup": setup})


def test_config_transaction_does_not_save_after_failure(tmp_enso):
    save_config({"counter": 1})

    with pytest.raises(RuntimeError, match="abort"), config_transaction() as config:
        config["counter"] = 2
        raise RuntimeError("abort")

    assert load_config()["counter"] == 1


def test_config_transaction_validates_before_creating_lock(tmp_enso):
    config_file = Path(tmp_enso, "config.json")
    config_file.write_text("{malformed")

    with pytest.raises(ConfigError, match=r"config\.json"), config_transaction():
        pytest.fail("a malformed configuration must not enter a transaction")

    assert not Path(f"{config_file}.lock").exists()


def test_config_transaction_rejects_symlink_lock_without_touching_target(
    tmp_enso, tmp_path
):
    save_config({})
    target = tmp_path / "outside-lock"
    target.write_text("outside")
    Path(f"{Path(tmp_enso, 'config.json')}.lock").symlink_to(target)

    with pytest.raises(ConfigError, match="lock"), config_transaction():
        pytest.fail("an unsafe lock must not be acquired")

    assert target.read_text() == "outside"


def test_config_transactions_serialize_cross_process_updates(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".enso"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text('{"counter": 0}\n')
    script = """
import time
from enso.config import config_transaction

for _ in range(12):
    with config_transaction() as config:
        current = config["counter"]
        time.sleep(0.003)
        config["counter"] = current + 1
"""
    env = {**os.environ, "HOME": str(home)}
    processes = [
        subprocess.Popen([sys.executable, "-c", script], env=env)
        for _ in range(2)
    ]

    for process in processes:
        assert process.wait(timeout=20) == 0

    assert json.loads((config_dir / "config.json").read_text())["counter"] == 24


def test_save_and_load_roundtrip(tmp_enso):
    """Config survives a save/load roundtrip."""
    config = {
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


def test_load_backfills_agent_timeout_without_persisting(tmp_enso):
    config_file = Path(tmp_enso) / "config.json"
    original = json.dumps({"providers": DEFAULT_PROVIDERS})
    config_file.write_text(original)

    loaded = load_config()

    assert loaded["agent"] == {"timeout": 1800}
    assert config_file.read_text() == original


@pytest.mark.parametrize("timeout", [0, 75])
def test_agent_timeout_preserves_explicit_values(tmp_enso, timeout):
    save_config({"agent": {"timeout": timeout}})

    assert load_config()["agent"]["timeout"] == timeout


@pytest.mark.parametrize("timeout", [-1, True, "1800", None])
def test_agent_timeout_replaces_invalid_values(tmp_enso, timeout):
    save_config({"agent": {"timeout": timeout}})

    assert load_config()["agent"]["timeout"] == 1800


def test_load_merges_missing_logging_defaults(tmp_enso):
    """Existing configs get logging defaults without losing user choices."""
    config = {
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


def test_default_config_has_codex_model_aliases(tmp_enso):
    config = load_config(allow_missing=True)
    assert config["providers"]["codex"]["models"] == ["sol", "terra", "luna"]


def test_existing_config_backfills_new_registry_providers_in_memory_only(tmp_enso):
    config_file = Path(tmp_enso) / "config.json"
    config_file.write_text(json.dumps({
        "providers": {
            "claude": {"path": "/custom/claude", "models": ["opus"]},
            "codex": {"path": "/custom/codex", "models": ["gpt-5.5"]},
        },
    }))

    loaded = load_config()

    assert loaded["providers"]["agy"] == DEFAULT_PROVIDERS["agy"]
    persisted = json.loads(config_file.read_text())
    assert set(persisted["providers"]) == {"claude", "codex"}
    assert persisted["providers"]["codex"]["path"] == "/custom/codex"
    assert persisted["providers"]["codex"]["models"] == ["gpt-5.5"]


def test_default_providers_derive_from_registry():
    """Provider names and default models have one source of truth: the registry."""
    assert list(DEFAULT_PROVIDERS) == PROVIDER_NAMES
    for name, defaults in DEFAULT_PROVIDERS.items():
        assert set(defaults) == {"path", "models"}
        assert defaults["path"] == name
        assert defaults["models"] == provider_class(name).default_models


def test_provider_models_filters_unsupported_and_malformed():
    config = {
        "providers": {
            "claude": {"models": ["opus"]},
            "codex": "broken",
            "retired": {"models": ["old"]},
        },
    }
    assert provider_models(config) == {"claude": ["opus"]}


def test_provider_models_normalizes_malformed_model_lists():
    """Non-list or mixed-type models must not enable substring matching or
    TypeErrors downstream — only well-formed string lists come through."""
    config = {
        "providers": {
            "claude": {"models": "sonnet"},          # string, not list
            "codex": {"models": [123, "sol", None]},  # mixed types
        },
    }
    assert provider_models(config) == {"claude": [], "codex": ["sol"]}
    assert provider_models({"providers": {"claude": {"models": None}}}) == {"claude": []}


def test_load_strips_retired_provider_keys(tmp_enso):
    """Keys dropped from a provider's defaults (e.g. the old kage runner set)
    are removed in memory without mutating a read."""
    config_file = Path(tmp_enso) / "config.json"
    config_file.write_text(json.dumps({
        "providers": {
            "claude": {
                "path": "/custom/claude",
                "runner": "kage",
                "job_runner": "print",
                "kage_path": "kage",
                "kage_timeout": 900,
                "kage_restart": False,
                "models": ["opus"],
            },
        },
    }))

    loaded = load_config()

    claude = loaded["providers"]["claude"]
    assert set(claude) == {"path", "models"}
    assert claude["path"] == "/custom/claude"
    assert claude["models"] == ["opus"]
    persisted = json.loads(config_file.read_text())
    assert "runner" in persisted["providers"]["claude"]


def test_load_preserves_unknown_provider_keys(tmp_enso):
    """Only explicitly retired keys are stripped — unknown keys (e.g. from a
    newer version after a rollback) survive load and are not migrated away."""
    config_file = Path(tmp_enso) / "config.json"
    config_file.write_text(json.dumps({
        "providers": {
            "claude": {"path": "claude", "models": ["opus"], "future_option": True},
        },
    }))

    loaded = load_config()

    assert loaded["providers"]["claude"]["future_option"] is True
    # No migration was persisted — the raw file keeps the key too.
    assert json.loads(config_file.read_text())["providers"]["claude"]["future_option"] is True


def test_load_backfills_codex_aliases_and_preserves_custom_models(tmp_enso):
    config = {
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

    assert set(loaded["providers"]) == set(PROVIDER_NAMES)
    assert set(json.loads(config_file.read_text())["providers"]) == {"claude", "retired"}


def test_load_replaces_invalid_logging_with_defaults(tmp_enso):
    """Invalid logging config is normalized to defaults."""
    config = {
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
    config = load_config(allow_missing=True)
    save_config(config)
    config_file = os.path.join(tmp_enso, "config.json")
    stat = os.stat(config_file)
    assert stat.st_mode & 0o777 == 0o600


def test_load_migrates_legacy_task_retention_and_drops_tasks(tmp_enso):
    """Task retention can be interpreted without mutating the source file."""
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
    assert "runs" not in persisted
    assert "tasks" in persisted


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
    original = b'{"transport": "slack"}\n'
    config_file.write_bytes(original)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("enso.config.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_config({"transport": "telegram"})

    assert config_file.read_bytes() == original
    assert list(Path(tmp_enso).glob("*.tmp")) == []


def test_load_never_calls_save_for_in_memory_defaults(tmp_enso, monkeypatch):
    config_file = Path(tmp_enso, "config.json")
    config_file.write_text(json.dumps({
        "tasks": {"runs_keep": 17, "runs_max_age_days": 4},
    }))

    def fail_save(_config):
        pytest.fail("a config read must never save")

    monkeypatch.setattr("enso.config.save_config", fail_save)

    loaded = load_config()

    assert loaded["runs"] == {"keep": 17, "max_age_days": 4}
    assert "tasks" not in loaded


def test_codex_alias_removal_is_respected(tmp_enso):
    """A config that already knows the aliases keeps its list verbatim."""
    config = {
        "transport": "telegram",
        "transports": {},
        "providers": {
            "codex": {
                "path": "/custom/codex",
                # User deliberately removed terra/luna and reordered.
                "models": ["custom-codex-model", "sol"],
            },
        },
    }
    save_config(config)
    loaded = load_config()
    assert loaded["providers"]["codex"]["models"] == ["custom-codex-model", "sol"]
