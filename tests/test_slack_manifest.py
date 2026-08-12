"""Contract tests for Slack capabilities shipped in the app manifest."""

from __future__ import annotations

from importlib import resources

import yaml


def test_manifest_enables_persistent_surfaces_without_unused_refresh_event():
    manifest = yaml.safe_load(
        resources.files("enso").joinpath("slack_manifest.yaml").read_text()
    )

    assert manifest["features"]["app_home"]["home_tab_enabled"] is True
    scopes = manifest["oauth_config"]["scopes"]["bot"]
    assert "canvases:write" in scopes
    assert "files:read" in scopes
    events = manifest["settings"]["event_subscriptions"]["bot_events"]
    assert "app_home_opened" not in events
    assert manifest["settings"]["interactivity"]["is_enabled"] is True
