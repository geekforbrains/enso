"""Tests for the `enso slack history` / `enso slack thread` reading commands.

These are the commands the Slack transport points an agent at instead of
pushing channel history into every prompt, so their rendering has to match
what the transport's own injector produced: resolved names, flattened
mention tokens, forwarded-message bodies, and no lifecycle noise.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from enso import slack_cache
from enso.cli import app

runner = CliRunner()

CACHE = {
    "team_id": "T0ENSO",
    "users": {
        "fetched_at": 0.0,
        "items": {
            "U02DEV": {"id": "U02DEV", "real_name": "Gavin Vickery"},
            "UBOT": {"id": "UBOT", "real_name": "Enso"},
        },
    },
    "channels": {"fetched_at": 0.0, "items": {"C0OPS": {"id": "C0OPS", "name": "ops"}}},
    "dm_cache": {},
}


@pytest.fixture
def slack_cli(tmp_enso, monkeypatch):
    """Stub the token and capture every Slack API call the command makes."""
    slack_cache.save(CACHE)
    monkeypatch.setattr("enso.cli._slack_token_or_exit", lambda: "xoxb-test")
    calls: list[tuple[str, dict]] = []
    box: dict[str, dict] = {"response": {"ok": True, "messages": []}}

    def fake_api_get(_token, method, params=None):
        calls.append((method, params or {}))
        return box["response"]

    monkeypatch.setattr("enso.slack_cache.api_get", fake_api_get)
    return box, calls


def _flat(result) -> str:
    """Collapse rich's line wrapping so substring assertions are stable."""
    return " ".join(result.stdout.split())


def test_history_resolves_names_and_flattens_mention_tokens(slack_cli):
    box, _calls = slack_cli
    box["response"] = {
        "ok": True,
        "messages": [
            {
                "user": "U02DEV",
                "ts": "1786644773.757009",
                "text": "<@UBOT> is Richard still overdue?",
                "thread_ts": "1786644773.757009",
                "reply_count": 3,
            }
        ],
    }

    result = runner.invoke(app, ["slack", "history", "C0OPS"])

    assert result.exit_code == 0
    out = _flat(result)
    assert "Gavin Vickery" in out
    assert "ts=1786644773.757009" in out
    assert "3 replies" in out
    assert "1 replies" not in out
    # The raw token would ping a real person if the agent echoed it back.
    assert "<@UBOT>" not in out
    assert "@Enso" in out
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", out)


def test_history_counts_a_single_reply_in_the_singular(slack_cli):
    box, _calls = slack_cli
    box["response"] = {
        "ok": True,
        "messages": [{"user": "U02DEV", "ts": "100.1", "text": "one", "reply_count": 1}],
    }

    assert "1 reply]" in _flat(runner.invoke(app, ["slack", "history", "C0OPS"]))


def test_history_drops_lifecycle_noise_unless_all_is_asked_for(slack_cli):
    box, _calls = slack_cli
    box["response"] = {
        "ok": True,
        "messages": [
            {"user": "U02DEV", "ts": "100.2", "text": "real question"},
            {
                "subtype": "channel_join",
                "user": "U02DEV",
                "ts": "100.1",
                "text": "<@U02DEV> has joined the channel",
            },
        ],
    }

    filtered = runner.invoke(app, ["slack", "history", "C0OPS"])
    assert "real question" in _flat(filtered)
    assert "joined the channel" not in _flat(filtered)

    everything = runner.invoke(app, ["slack", "history", "C0OPS", "--all"])
    assert "joined the channel" in _flat(everything)


def test_history_since_bounds_the_window(slack_cli):
    _box, calls = slack_cli

    result = runner.invoke(app, ["slack", "history", "C0OPS", "--since", "24h"])

    assert result.exit_code == 0
    _method, params = calls[-1]
    assert "oldest" in params
    assert float(params["oldest"]) > 0


def test_history_rejects_an_unparseable_since(slack_cli):
    result = runner.invoke(app, ["slack", "history", "C0OPS", "--since", "soon"])

    assert result.exit_code == 1
    assert "soon" in _flat(result)


def test_history_surfaces_forwarded_message_bodies(slack_cli):
    box, _calls = slack_cli
    box["response"] = {
        "ok": True,
        "messages": [
            {
                "user": "U02DEV",
                "ts": "100.1",
                "text": "worth a look",
                "attachments": [
                    {
                        "is_msg_unfurl": True,
                        "author_name": "Melissa",
                        "text": "invoice is still open",
                    }
                ],
            }
        ],
    }

    result = runner.invoke(app, ["slack", "history", "C0OPS"])

    out = _flat(result)
    assert "worth a look" in out
    assert "invoice is still open" in out


def test_history_reports_a_slack_error_without_a_traceback(slack_cli):
    box, _calls = slack_cli
    box["response"] = {"ok": False, "error": "channel_not_found"}

    result = runner.invoke(app, ["slack", "history", "C0NOPE"])

    assert result.exit_code == 1
    assert "channel_not_found" in _flat(result)


def test_thread_renders_with_the_same_treatment(slack_cli):
    box, calls = slack_cli
    box["response"] = {
        "ok": True,
        "messages": [
            {"user": "U02DEV", "ts": "100.1", "text": "<@UBOT> take a look"},
            {"user": "UBOT", "ts": "100.2", "text": "on it"},
        ],
    }

    result = runner.invoke(app, ["slack", "thread", "C0OPS", "100.1"])

    assert result.exit_code == 0
    out = _flat(result)
    assert "Gavin Vickery" in out
    assert "<@UBOT>" not in out
    assert "@Enso" in out
    _method, params = calls[-1]
    assert params["ts"] == "100.1"
