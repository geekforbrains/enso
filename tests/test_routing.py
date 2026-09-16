"""Binding keys, conversation keys, and agent resolution."""

from __future__ import annotations

import pytest
from conftest import write_workspace

from enso.config import Paths, parse_config
from enso.routing import binding_key, conversation_key, resolve_agent, workspace_for


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"transport": "slack", "channel": "D9", "is_dm": True, "user_id": "U1"}, "slack:dm:U1"),
        ({"transport": "slack", "channel": "D9", "is_dm": True, "user_id": "W1"}, "slack:dm:W1"),
        ({"transport": "slack", "channel": "C1"}, "slack:C1"),
        (
            {"transport": "telegram", "channel": "123", "is_dm": True, "user_id": "123"},
            "telegram:123",
        ),
    ],
)
def test_binding_key(kwargs: dict, key: str) -> None:
    assert binding_key(**kwargs) == key


def test_conversation_key() -> None:
    assert conversation_key("slack", "C1", "171.1") == "slack:C1:171.1"
    assert conversation_key("slack", "D1", None) == "slack:D1"
    assert conversation_key("slack", "D1", "171.1", is_dm=True) == "slack:D1"
    assert conversation_key("telegram", "5", None) == "telegram:5"


def test_workspace_and_agent_resolution(enso_home: Paths, raw_config: dict) -> None:
    (enso_home.workspaces / "meteor").mkdir()
    raw_config["bindings"]["slack:C2"] = "meteor"
    write_workspace(
        enso_home, "meteor", {"agent": {"provider": "codex", "model": "luna", "effort": "ultra"}}
    )
    raw_config["defaults"] = {"provider": "claude", "model": "sonnet", "effort": "max"}
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems
    assert workspace_for(config, "slack:C2") == "meteor"
    assert workspace_for(config, "slack:C9") is None

    default = resolve_agent(config, "default")
    assert (default.provider, default.model, default.effort, default.source) == (
        "claude", "sonnet", "max", "defaults",
    )  # fmt: skip
    meteor = resolve_agent(config, "meteor")
    # luna tops out at max, so the configured ultra is clamped.
    assert (meteor.provider, meteor.model, meteor.effort, meteor.source) == (
        "codex", "luna", "max", "workspace",
    )  # fmt: skip
