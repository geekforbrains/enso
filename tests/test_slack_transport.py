"""Tests for the Slack transport."""

from __future__ import annotations

import asyncio
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out slack_bolt / slack_sdk before importing the transport, so tests
# run without those packages installed.
# ---------------------------------------------------------------------------

_slack_bolt = types.ModuleType("slack_bolt")
_slack_bolt.App = MagicMock
_slack_bolt_sm = types.ModuleType("slack_bolt.adapter.socket_mode")
_slack_bolt_sm.SocketModeHandler = MagicMock
_slack_sdk = types.ModuleType("slack_sdk")
_slack_sdk.WebClient = MagicMock

sys.modules.setdefault("slack_bolt", _slack_bolt)
sys.modules.setdefault("slack_bolt.adapter", types.ModuleType("slack_bolt.adapter"))
sys.modules.setdefault("slack_bolt.adapter.socket_mode", _slack_bolt_sm)
sys.modules.setdefault("slack_sdk", _slack_sdk)

from enso.core import Runtime
from enso.transports.slack import (
    MAX_FILE_SIZE,
    SlackContext,
    SlackTransport,
    _safe_filename,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def slack_config():
    """Minimal config with Slack transport."""
    return {
        "working_dir": "/tmp/enso-slack-test",
        "transport": "slack",
        "transports": {
            "slack": {
                "bot_token": "xoxb-fake",
                "app_token": "xapp-fake",
                "allowed_user_ids": ["U_ALLOWED"],
            }
        },
        "providers": {
            "claude": {"path": "claude", "models": ["opus", "sonnet"]},
            "codex": {"path": "codex", "models": ["gpt-5.3-codex"]},
            "gemini": {"path": "gemini", "models": ["gemini-2.5-pro"]},
        },
    }


@pytest.fixture
def transport(slack_config):
    """A SlackTransport wired up with a real Runtime but mocked Slack client."""
    rt = Runtime(slack_config)
    t = SlackTransport(rt)
    t._client = MagicMock()
    t._bot_user_id = "U_BOT"
    t._loop = asyncio.new_event_loop()
    return t


@pytest.fixture
def mock_client():
    """A standalone mock WebClient for SlackContext tests."""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "1234567890.000001"}
    return client


# ---------------------------------------------------------------------------
# _safe_filename
# ---------------------------------------------------------------------------


def test_safe_filename_strips_traversal():
    assert _safe_filename("../../etc/passwd") == "passwd"


def test_safe_filename_strips_leading_dot():
    assert _safe_filename(".hidden") == "hidden"


def test_safe_filename_normal():
    assert _safe_filename("report.pdf") == "report.pdf"


# ---------------------------------------------------------------------------
# SlackContext
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_reply_posts_message(mock_client):
    ctx = SlackContext(mock_client, "C_CHAN", "1234.5678")
    await ctx.reply("hello")
    mock_client.chat_postMessage.assert_called_once()
    call_kwargs = mock_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C_CHAN"
    assert call_kwargs["text"] == "hello"
    assert call_kwargs["thread_ts"] == "1234.5678"
    # Should include mrkdwn block for rich formatting
    assert call_kwargs["blocks"][0]["type"] == "section"
    assert call_kwargs["blocks"][0]["text"]["type"] == "mrkdwn"


@pytest.mark.asyncio
async def test_context_reply_splits_long_messages(mock_client):
    ctx = SlackContext(mock_client, "C_CHAN")
    long_text = "a" * 8000
    await ctx.reply(long_text)
    assert mock_client.chat_postMessage.call_count >= 2
    # All chunks together should equal the original text
    sent = "".join(
        call.kwargs["text"] for call in mock_client.chat_postMessage.call_args_list
    )
    assert sent == long_text


@pytest.mark.asyncio
async def test_context_reply_status_uses_thread_status(mock_client):
    """When in a thread, reply_status uses assistant.threads.setStatus."""
    ctx = SlackContext(mock_client, "C_CHAN", "1234.5678")
    handle = await ctx.reply_status("working...")
    assert handle == "thread_status"
    mock_client.api_call.assert_called_once()
    call_args = mock_client.api_call.call_args
    assert call_args[0][0] == "assistant.threads.setStatus"
    assert call_args[1]["json"]["status"] == "working..."


@pytest.mark.asyncio
async def test_context_reply_status_falls_back_to_message(mock_client):
    """Without a thread_ts, falls back to posting a regular message."""
    ctx = SlackContext(mock_client, "C_CHAN")  # no thread_ts
    ts = await ctx.reply_status("working...")
    assert ts == "1234567890.000001"
    mock_client.chat_postMessage.assert_called_once()


@pytest.mark.asyncio
async def test_context_edit_status_thread(mock_client):
    """edit_status with thread_status handle updates thread status."""
    ctx = SlackContext(mock_client, "C_CHAN", "1234.5678")
    await ctx.edit_status("thread_status", "still working...")
    mock_client.api_call.assert_called_once()
    assert mock_client.api_call.call_args[1]["json"]["status"] == "still working..."


@pytest.mark.asyncio
async def test_context_edit_status_message(mock_client):
    """edit_status with a ts handle updates the message."""
    ctx = SlackContext(mock_client, "C_CHAN")
    await ctx.edit_status("1234.5678", "still working...")
    mock_client.chat_update.assert_called_once_with(
        channel="C_CHAN", ts="1234.5678", text="still working...",
    )


@pytest.mark.asyncio
async def test_context_delete_status_thread(mock_client):
    """delete_status with thread_status handle clears thread status."""
    ctx = SlackContext(mock_client, "C_CHAN", "1234.5678")
    await ctx.delete_status("thread_status")
    mock_client.api_call.assert_called_once()
    assert mock_client.api_call.call_args[1]["json"]["status"] == ""


@pytest.mark.asyncio
async def test_context_delete_status_message(mock_client):
    """delete_status with a ts handle deletes the message."""
    ctx = SlackContext(mock_client, "C_CHAN")
    await ctx.delete_status("1234.5678")
    mock_client.chat_delete.assert_called_once_with(
        channel="C_CHAN", ts="1234.5678",
    )


@pytest.mark.asyncio
async def test_context_delete_status_suppresses_errors(mock_client):
    mock_client.chat_delete.side_effect = Exception("API error")
    ctx = SlackContext(mock_client, "C_CHAN")
    # Should not raise
    await ctx.delete_status("1234.5678")


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_authorized_user(transport):
    assert transport._is_authorized("U_ALLOWED") is True


def test_unauthorized_user(transport):
    assert transport._is_authorized("U_STRANGER") is False


def test_empty_allowlist_allows_all(slack_config):
    slack_config["transports"]["slack"]["allowed_user_ids"] = []
    rt = Runtime(slack_config)
    t = SlackTransport(rt)
    assert t._is_authorized("U_ANYONE") is True


# ---------------------------------------------------------------------------
# _handle_event — message routing
# ---------------------------------------------------------------------------


def test_handle_event_ignores_bot_message(transport):
    """Bot messages (subtype=bot_message) are ignored."""
    event = {"subtype": "bot_message", "user": "U_ALLOWED", "text": "hi"}
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_not_called()


def test_handle_event_ignores_message_changed(transport):
    event = {"subtype": "message_changed", "user": "U_ALLOWED", "text": "hi"}
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_not_called()


def test_handle_event_ignores_own_messages(transport):
    event = {"user": "U_BOT", "text": "hi", "channel": "C1", "ts": "1"}
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_not_called()


def test_handle_event_rejects_unauthorized(transport):
    event = {
        "user": "U_STRANGER",
        "text": "hi",
        "channel": "C1",
        "ts": "1",
        "channel_type": "im",
    }
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_not_called()


def test_handle_event_dm_dispatches(transport):
    """DM messages from authorized users are dispatched."""
    event = {
        "user": "U_ALLOWED",
        "text": "build a website",
        "channel": "D_DM",
        "ts": "100.1",
        "channel_type": "im",
    }
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_called_once_with(
        "D_DM", "100.1", "build a website", "U_ALLOWED"
    )


def test_handle_event_channel_requires_mention(transport):
    """Channel messages without @mention are ignored."""
    event = {
        "user": "U_ALLOWED",
        "text": "just chatting",
        "channel": "C_PUB",
        "ts": "100.1",
        "channel_type": "channel",
    }
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_not_called()


def test_handle_event_channel_mention_dispatches(transport):
    """Channel messages with @mention are dispatched with mention stripped."""
    event = {
        "user": "U_ALLOWED",
        "text": "<@U_BOT> build a website",
        "channel": "C_PUB",
        "ts": "100.1",
        "channel_type": "channel",
    }
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_called_once_with(
        "C_PUB", "100.1", "build a website", "U_ALLOWED"
    )


def test_handle_event_uses_thread_ts(transport):
    """When thread_ts is present, it's used instead of ts."""
    event = {
        "user": "U_ALLOWED",
        "text": "followup",
        "channel": "D_DM",
        "ts": "200.1",
        "thread_ts": "100.1",
        "channel_type": "im",
    }
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_called_once_with(
        "D_DM", "100.1", "followup", "U_ALLOWED"
    )


def test_handle_event_empty_text_ignored(transport):
    """Empty messages are ignored."""
    event = {
        "user": "U_ALLOWED",
        "text": "",
        "channel": "D_DM",
        "ts": "1",
        "channel_type": "im",
    }
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_event(event, say=None)
    transport._dispatch_to_runtime.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_mention_event
# ---------------------------------------------------------------------------


def test_handle_mention_dispatches(transport):
    event = {
        "user": "U_ALLOWED",
        "text": "<@U_BOT> do something",
        "channel": "C_PUB",
        "ts": "300.1",
    }
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_mention_event(event, say=None)
    transport._dispatch_to_runtime.assert_called_once_with(
        "C_PUB", "300.1", "do something", "U_ALLOWED"
    )


def test_handle_mention_rejects_unauthorized(transport):
    event = {
        "user": "U_STRANGER",
        "text": "<@U_BOT> do something",
        "channel": "C_PUB",
        "ts": "300.1",
    }
    transport._dispatch_to_runtime = MagicMock()
    transport._handle_mention_event(event, say=None)
    transport._dispatch_to_runtime.assert_not_called()


# ---------------------------------------------------------------------------
# Command parsing via _dispatch_to_runtime
# ---------------------------------------------------------------------------


def test_dispatch_routes_use_command(transport):
    """!use claude is routed to _cmd_use."""
    transport._cmd_use = MagicMock()
    # Make it a coroutine so run_coroutine_threadsafe works
    async def fake_use(channel, thread_ts, args):
        pass
    transport._cmd_use = fake_use
    with patch.object(asyncio, "run_coroutine_threadsafe") as mock_run:
        transport._dispatch_to_runtime("C1", "1.0", "!use claude", "U_ALLOWED")
        mock_run.assert_called_once()
        coro = mock_run.call_args[0][0]
        # The coroutine should be from _cmd_use
        assert coro is not None


def test_dispatch_routes_plain_message(transport):
    """Regular text is dispatched to _dispatch (not a command)."""
    with patch.object(asyncio, "run_coroutine_threadsafe") as mock_run:
        transport._dispatch_to_runtime("C1", "1.0", "hello world", "U_ALLOWED")
        mock_run.assert_called_once()


def test_dispatch_unknown_command_treated_as_message(transport):
    """!unknown is not a known command, so it goes to the agent."""
    with patch.object(asyncio, "run_coroutine_threadsafe") as mock_run:
        transport._dispatch_to_runtime("C1", "1.0", "!unknown foo", "U_ALLOWED")
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Commands (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_use_sets_provider(transport):
    await transport._cmd_use("C1", "1.0", "claude")
    assert transport.runtime.active_provider_by_chat["C1"] == "claude"
    transport._client.chat_postMessage.assert_called_once()
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "claude" in msg


@pytest.mark.asyncio
async def test_cmd_use_no_args_lists_providers(transport):
    await transport._cmd_use("C1", "1.0", "")
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "!use" in msg
    assert "claude" in msg


@pytest.mark.asyncio
async def test_cmd_status_shows_info(transport):
    await transport._cmd_status("C1", "1.0", "")
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "Provider:" in msg
    assert "Model:" in msg


@pytest.mark.asyncio
async def test_cmd_model_sets_model(transport):
    await transport._cmd_model("C1", "1.0", "sonnet")
    assert transport.runtime.active_model_by_chat_provider[("C1", "claude")] == "sonnet"
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "sonnet" in msg


@pytest.mark.asyncio
async def test_cmd_model_by_index(transport):
    await transport._cmd_model("C1", "1.0", "2")
    assert transport.runtime.active_model_by_chat_provider[("C1", "claude")] == "sonnet"


@pytest.mark.asyncio
async def test_cmd_model_invalid_index(transport):
    await transport._cmd_model("C1", "1.0", "99")
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "Invalid index" in msg


@pytest.mark.asyncio
async def test_cmd_model_unknown(transport):
    await transport._cmd_model("C1", "1.0", "nonexistent")
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "Unknown model" in msg


@pytest.mark.asyncio
async def test_cmd_stop_no_process(transport):
    await transport._cmd_stop("C1", "1.0", "")
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "No process running" in msg


@pytest.mark.asyncio
async def test_cmd_help_lists_commands(transport):
    await transport._cmd_help("C1", "1.0", "")
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "!use" in msg
    assert "!stop" in msg
    assert "!help" in msg


# ---------------------------------------------------------------------------
# _dispatch — concurrency guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_rejects_when_locked(transport):
    """Second request while first is running gets a rejection message."""
    chat_id = "C_LOCKED"
    lock = transport.runtime.get_chat_lock(chat_id)

    # Simulate a held lock
    await lock.acquire()
    try:
        await transport._dispatch("C_LOCKED", "1.0", chat_id, "hello")
        transport._client.chat_postMessage.assert_called_once()
        msg = transport._client.chat_postMessage.call_args.kwargs["text"]
        assert "already running" in msg
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------


def test_handle_event_routes_files(transport):
    """Messages with files are routed to _handle_files."""
    transport._handle_files = MagicMock()
    event = {
        "user": "U_ALLOWED",
        "text": "check this",
        "channel": "D_DM",
        "ts": "1.0",
        "channel_type": "im",
        "files": [{"name": "test.txt", "url_private_download": "https://files.slack.com/test.txt", "size": 100}],
    }
    transport._handle_event(event, say=None)
    transport._handle_files.assert_called_once_with(
        "D_DM",
        "1.0",
        "check this",
        [{"name": "test.txt", "url_private_download": "https://files.slack.com/test.txt", "size": 100}],
    )


def test_handle_files_rejects_oversize(transport, tmp_path):
    """Files over MAX_FILE_SIZE get a rejection message."""
    transport.runtime.working_dir = str(tmp_path)
    big_file = {
        "name": "huge.bin",
        "url_private_download": "https://files.slack.com/huge.bin",
        "size": MAX_FILE_SIZE + 1,
    }
    transport._handle_files("C1", "1.0", "", [big_file])
    transport._client.chat_postMessage.assert_called_once()
    msg = transport._client.chat_postMessage.call_args.kwargs["text"]
    assert "too large" in msg.lower()


def test_handle_files_downloads_and_dispatches(transport, tmp_path):
    """Valid files are downloaded and a prompt is dispatched."""
    transport.runtime.working_dir = str(tmp_path)
    file_content = b"hello world"
    file_info = {
        "name": "notes.txt",
        "url_private_download": "https://files.slack.com/notes.txt",
        "size": len(file_content),
    }

    # Mock urllib to return the file content
    with patch("enso.transports.slack.urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = file_content
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch("enso.transports.slack.asyncio.run_coroutine_threadsafe") as mock_run:
            transport._handle_files("C1", "1.0", "here's a file", [file_info])

            # File should be written to disk
            dest = os.path.join(str(tmp_path), "uploads", "notes.txt")
            assert os.path.exists(dest)
            with open(dest, "rb") as f:
                assert f.read() == file_content

            # A dispatch should have been scheduled
            mock_run.assert_called_once()


def test_handle_files_skips_no_url(transport, tmp_path):
    """Files without a download URL are skipped."""
    transport.runtime.working_dir = str(tmp_path)
    file_info = {"name": "mystery.dat", "size": 100}
    with patch("enso.transports.slack.asyncio.run_coroutine_threadsafe") as mock_run:
        transport._handle_files("C1", "1.0", "", [file_info])
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# State persistence with string chat IDs
# ---------------------------------------------------------------------------


def test_string_chat_id_state_roundtrip(tmp_enso, slack_config):
    """String chat IDs (Slack channels) survive save/load."""
    slack_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(slack_config)
    rt.active_provider_by_chat["C06ABCDEF"] = "codex"
    rt.session_by_chat_provider[("C06ABCDEF", "codex")] = "sess_456"
    rt.save_state()

    rt2 = Runtime(slack_config)
    rt2.load_state()
    assert rt2.active_provider_by_chat["C06ABCDEF"] == "codex"
    assert rt2.session_by_chat_provider[("C06ABCDEF", "codex")] == "sess_456"
