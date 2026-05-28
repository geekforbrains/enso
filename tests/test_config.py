"""Tests for configuration management."""

from __future__ import annotations

import os

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
