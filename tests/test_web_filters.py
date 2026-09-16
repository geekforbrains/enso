"""Presentation rules without a database, HTTP server, or page fixtures."""

import pytest

from enso.web import filters


def test_doctor_facts_are_drawn_as_what_they_are() -> None:
    """Every shape ``doctor`` reports, and what the Health page makes of it."""
    assert filters.fact("/Users/x/.enso", "path") == ([("path", "/Users/x/.enso")], [])
    assert filters.fact("claude", "path") == ([("path", "claude")], [])  # resolved on PATH
    assert filters.fact("launchd") == ([("word", "launchd")], [])
    assert filters.fact(True) == ([("word", "yes")], [])
    assert filters.fact(None) == ([("quiet", "none")], [])
    assert filters.fact(["a", "b"]) == ([("names", "a, b")], [])
    assert filters.fact([]) == ([("quiet", "none")], [])
    # A per-item map leads on what the item is and keeps its flags and names under it.
    assert filters.fact({"path": "/bin/x", "executable": True, "models": ["opus"]}) == (
        [("path", "/bin/x")],
        [("word", "executable"), ("names", "opus")],
    )
    assert filters.fact({"path": "/bin/x", "executable": False, "models": []}) == (
        [("path", "/bin/x")],
        [("word", "not executable"), ("quiet", "no models")],
    )
    # A transport has nothing to lead on, so its flags are the line themselves.
    assert filters.fact({"configured": True, "installed": False}) == (
        [("word", "configured"), ("word", "not installed")],
        [],
    )
    assert filters.fact_label("git_root") == "git root"


@pytest.mark.parametrize(
    ("actor", "shown"),
    [
        ("slack:U0AETSSDDEF", "via Slack"),
        ("telegram:8140", "via Telegram"),
        ("job:default:nightly", "job:default:nightly"),
        ("beat:HB-001", "beat:HB-001"),
        ("user:gavin", "user:gavin"),
        ("heartbeat", "heartbeat"),
    ],
)
def test_actor_reads_as_its_origin_and_only_chat_ids_are_replaced(actor, shown):
    assert filters.heartbeat_actor(actor) == shown


def test_unknown_actor_shapes_survive_untouched():
    # A prefix that is not a transport is not a chat identity, and an actor is never blank
    # on the page even if a future writer leaves it so.
    assert filters.heartbeat_actor("mastodon:42") == "mastodon:42"
    assert filters.heartbeat_actor("slack") == "slack"
    assert filters.heartbeat_actor("") == "-" and filters.heartbeat_actor(None) == "-"
