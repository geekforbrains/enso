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


def test_config_file_permissions(tmp_enso):
    """Config file has restricted permissions."""
    config = load_config()
    save_config(config)
    config_file = os.path.join(tmp_enso, "config.json")
    stat = os.stat(config_file)
    assert stat.st_mode & 0o777 == 0o600
