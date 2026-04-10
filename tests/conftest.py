"""Shared fixtures for Enso tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def tmp_enso(tmp_path, monkeypatch):
    """Set up a temporary ~/.enso directory for testing.

    Returns the path. All config/state/messages/jobs go here.
    """
    d = str(tmp_path / "enso")
    os.makedirs(d)
    os.makedirs(os.path.join(d, "workspace"))

    paths = {
        "enso.config.CONFIG_DIR": d,
        "enso.config.CONFIG_FILE": os.path.join(d, "config.json"),
        "enso.config.STATE_FILE": os.path.join(d, "state.json"),
        "enso.config.JOBS_DIR": os.path.join(d, "jobs"),
        "enso.config.MESSAGES_FILE": os.path.join(d, "messages.json"),
        "enso.core.CONFIG_DIR": d,
        "enso.core.STATE_FILE": os.path.join(d, "state.json"),
        "enso.messages.MESSAGES_FILE": os.path.join(d, "messages.json"),
        "enso.jobs.JOBS_DIR": os.path.join(d, "jobs"),
    }
    for attr, val in paths.items():
        monkeypatch.setattr(attr, val)

    return d


@pytest.fixture
def sample_config():
    """Return a minimal config dict for testing."""
    return {
        "working_dir": "/tmp/enso-test",
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "fake-token",
                "allowed_users": ["12345"],
            }
        },
        "providers": {
            "claude": {"path": "claude", "models": ["opus", "sonnet"]},
            "codex": {"path": "codex", "models": ["gpt-5.3-codex"]},
            "gemini": {"path": "gemini", "models": ["gemini-2.5-pro"]},
        },
    }
