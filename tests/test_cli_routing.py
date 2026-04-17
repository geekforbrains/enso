"""Tests for the `enso message send/attach` destination resolver."""

from __future__ import annotations

from enso.cli import _resolve_slack_target


def test_explicit_to_wins_and_clears_thread(monkeypatch):
    """When --to is given we never leak the origin thread (could be a
    different channel)."""
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C_origin")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "1700.1")
    channel, thread_ts = _resolve_slack_target("#other", "C_notify")
    assert channel == "#other"
    assert thread_ts == ""


def test_origin_env_wins_over_notify_channel(monkeypatch):
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C_origin")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "1700.1")
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "C_origin"
    assert thread_ts == "1700.1"


def test_origin_without_thread(monkeypatch):
    """DM origin: channel set, thread empty."""
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "D_dm")
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "D_dm"
    assert thread_ts == ""


def test_falls_back_to_notify_channel(monkeypatch):
    """No --to and no origin env → notify_channel is the last resort."""
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "C_notify"
    assert thread_ts == ""


def test_nothing_configured(monkeypatch):
    """Fully unconfigured — returns empty so caller can error cleanly."""
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "")
    assert channel == ""
    assert thread_ts == ""


# ---------------------------------------------------------------------------
# Slack helper payloads include thread_ts when set
# ---------------------------------------------------------------------------


def test_slack_send_message_includes_thread_ts(monkeypatch):
    """_slack_send_message adds thread_ts to chat.postMessage payload."""
    import json
    from io import BytesIO

    from enso import cli as cli_mod

    captured: dict = {}

    class _FakeResp:
        status = 200

        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"ok": true}'

    def _fake_urlopen(req, timeout=10):
        captured["data"] = json.loads(req.data)
        captured["url"] = req.full_url
        return _FakeResp()

    monkeypatch.setattr(cli_mod.urllib.request, "urlopen", _fake_urlopen)

    ok = cli_mod._slack_send_message(
        "xoxb-fake", "C012345", "hi", thread_ts="1700000000.123",
    )
    assert ok is True
    assert captured["data"] == {
        "channel": "C012345",
        "text": "hi",
        "thread_ts": "1700000000.123",
    }
    _ = BytesIO  # silence unused-import warning on older pytest


def test_slack_send_message_no_thread(monkeypatch):
    """Without thread_ts the payload stays clean."""
    import json

    from enso import cli as cli_mod

    captured: dict = {}

    class _FakeResp:
        status = 200

        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"ok": true}'

    def _fake_urlopen(req, timeout=10):
        captured["data"] = json.loads(req.data)
        return _FakeResp()

    monkeypatch.setattr(cli_mod.urllib.request, "urlopen", _fake_urlopen)

    cli_mod._slack_send_message("xoxb-fake", "C012345", "hi")
    assert "thread_ts" not in captured["data"]
