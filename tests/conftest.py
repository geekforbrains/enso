"""Shared fixtures for Enso tests."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def agy_projects_dir(tmp_path, monkeypatch):
    """Point Antigravity project lookups at an isolated, initially absent dir.

    Keeps agy command building deterministic — the real catalog under
    ~/.gemini would otherwise leak the developer's projects into tests.
    """
    projects_dir = tmp_path / "agy-projects"
    monkeypatch.setattr("enso.providers.agy._PROJECTS_DIR", projects_dir)
    return projects_dir


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
        "enso.config.DOCS_DIR": os.path.join(d, "docs"),
        "enso.config.JOBS_DIR": os.path.join(d, "jobs"),
        "enso.config.MESSAGES_FILE": os.path.join(d, "messages.json"),
        "enso.core.CONFIG_DIR": d,
        "enso.core.STATE_FILE": os.path.join(d, "state.json"),
        "enso.messages.MESSAGES_FILE": os.path.join(d, "messages.json"),
        "enso.docs.DOCS_DIR": os.path.join(d, "docs"),
        "enso.jobs.JOBS_DIR": os.path.join(d, "jobs"),
        "enso.slack_cache.CACHE_DIR": os.path.join(d, "cache"),
        "enso.slack_cache.CACHE_FILE": os.path.join(d, "cache", "slack.json"),
    }
    for attr, val in paths.items():
        monkeypatch.setattr(attr, val)

    return d


@pytest.fixture
def sample_config(tmp_enso):
    """Return a minimal config dict for testing."""
    return {
        "working_dir": os.path.join(tmp_enso, "workspace"),
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
            "agy": {"path": "agy", "models": ["gemini-3.6-flash-high"]},
        },
    }
