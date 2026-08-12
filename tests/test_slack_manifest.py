"""Contract tests for Slack capabilities shipped in the app manifest."""

from __future__ import annotations

from importlib import resources

import yaml


def _manifest():
    return yaml.safe_load(
        resources.files("enso").joinpath("slack_manifest.yaml").read_text()
    )


def test_manifest_enables_persistent_surface_capabilities():
    manifest = _manifest()

    assert manifest["features"]["app_home"]["home_tab_enabled"] is True
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert "chat:write" in scopes
    assert "canvases:write" in scopes
    assert "files:read" in scopes
    assert manifest["settings"]["interactivity"]["is_enabled"] is True


def test_manifest_omits_unused_app_home_refresh_event():
    manifest = _manifest()

    events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert "app_home_opened" not in events
