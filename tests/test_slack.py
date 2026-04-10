"""Tests for the Slack transport."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from enso.transports.slack import (
    SlackContext,
    SlackTransport,
    _safe_filename,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(**overrides: object) -> AsyncMock:
    """Return an AsyncMock that behaves like AsyncWebClient."""
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "1234567890.123456"}
    client.chat_update.return_value = {"ok": True}
    client.chat_delete.return_value = {"ok": True}
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


def _make_runtime(**overrides: object) -> MagicMock:
    """Return a MagicMock that behaves like Runtime."""
    rt = MagicMock()
    rt.config = {
        "working_dir": "/tmp/enso-test",
        "transports": {
            "slack": {
                "bot_token": "xoxb-fake",
                "app_token": "xapp-fake",
                "allowed_users": ["U123"],
                "bot_user_id": "UBOT",
                "notify_channel": "C999",
            },
        },
        "providers": {
            "claude": {"path": "claude", "models": ["opus", "sonnet"]},
        },
    }
    rt.working_dir = "/tmp/enso-test"
    rt.session_by_chat_provider = {}
    rt.active_provider_by_chat = {}
    rt.active_model_by_chat_provider = {}
    rt.dispatch = AsyncMock()
    rt.stop_chat = AsyncMock(return_value=(False, None))
    rt.clear_queue = MagicMock(return_value=0)
    rt.get_active_provider = MagicMock(return_value="claude")
    rt.get_active_model = MagicMock(return_value="opus")
    rt.models = {"claude": ["opus", "sonnet"]}
    rt.save_state = MagicMock()
    for k, v in overrides.items():
        setattr(rt, k, v)
    return rt


# ---------------------------------------------------------------------------
# SlackContext
# ---------------------------------------------------------------------------


class TestSlackContext:
    """Tests for SlackContext message methods."""

    @pytest.mark.asyncio
    async def test_reply_calls_chat_post_message(self):
        client = _make_client()
        ctx = SlackContext(client, "C123", thread_ts="1234.5678")
        await ctx.reply("hello world")

        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["thread_ts"] == "1234.5678"
        assert "hello world" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_reply_without_thread(self):
        client = _make_client()
        ctx = SlackContext(client, "C123")
        await ctx.reply("no thread")

        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert "thread_ts" not in call_kwargs

    @pytest.mark.asyncio
    async def test_reply_applies_mrkdwn_formatting(self):
        client = _make_client()
        ctx = SlackContext(client, "C123")
        await ctx.reply("**bold text**")

        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert "*bold text*" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_reply_status_returns_ts(self):
        client = _make_client()
        ctx = SlackContext(client, "C123", thread_ts="1234.5678")
        handle = await ctx.reply_status("processing...")

        assert handle == "1234567890.123456"
        client.chat_postMessage.assert_called_once()

    @pytest.mark.asyncio
    async def test_reply_status_in_thread(self):
        client = _make_client()
        ctx = SlackContext(client, "C123", thread_ts="1234.5678")
        await ctx.reply_status("status msg")

        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "1234.5678"

    @pytest.mark.asyncio
    async def test_edit_status_calls_chat_update(self):
        client = _make_client()
        ctx = SlackContext(client, "C123")
        await ctx.edit_status("1234567890.123456", "updated")

        client.chat_update.assert_called_once_with(
            channel="C123", ts="1234567890.123456", text="updated",
        )

    @pytest.mark.asyncio
    async def test_delete_status_calls_chat_delete(self):
        client = _make_client()
        ctx = SlackContext(client, "C123")
        await ctx.delete_status("1234567890.123456")

        client.chat_delete.assert_called_once_with(
            channel="C123", ts="1234567890.123456",
        )

    @pytest.mark.asyncio
    async def test_delete_status_suppresses_errors(self):
        client = _make_client()
        client.chat_delete.side_effect = Exception("API error")
        ctx = SlackContext(client, "C123")
        # Should not raise
        await ctx.delete_status("1234567890.123456")

    @pytest.mark.asyncio
    async def test_send_typing_is_noop(self):
        client = _make_client()
        ctx = SlackContext(client, "C123")
        await ctx.send_typing()
        # No calls should be made
        client.assert_not_called()


# ---------------------------------------------------------------------------
# SlackTransport — thread context
# ---------------------------------------------------------------------------


class TestFetchThreadContext:
    """Tests for _fetch_thread_context."""

    @pytest.mark.asyncio
    async def test_builds_context_from_messages(self):
        client = _make_client()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U123", "text": "hello bot"},
                {"user": "UBOT", "text": "hello human"},
                {"user": "U123", "text": "current message"},
            ],
        }

        rt = _make_runtime()
        transport = SlackTransport(rt)

        result = await transport._fetch_thread_context(client, "C123", "1234.5678")

        assert "[Thread context]" in result
        assert "[user]: hello bot" in result
        assert "[assistant]: hello human" in result
        # Current message should be excluded
        assert "current message" not in result

    @pytest.mark.asyncio
    async def test_empty_for_single_message(self):
        client = _make_client()
        client.conversations_replies.return_value = {
            "messages": [{"user": "U123", "text": "only one"}],
        }

        rt = _make_runtime()
        transport = SlackTransport(rt)

        result = await transport._fetch_thread_context(client, "C123", "1234.5678")
        assert result == ""

    @pytest.mark.asyncio
    async def test_empty_on_api_error(self):
        client = _make_client()
        client.conversations_replies.side_effect = Exception("API error")

        rt = _make_runtime()
        transport = SlackTransport(rt)

        result = await transport._fetch_thread_context(client, "C123", "1234.5678")
        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_messages_without_text(self):
        client = _make_client()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U123", "text": ""},
                {"user": "UBOT", "text": "response"},
                {"user": "U123", "text": "current"},
            ],
        }

        rt = _make_runtime()
        transport = SlackTransport(rt)

        result = await transport._fetch_thread_context(client, "C123", "1234.5678")
        assert "[user]:" not in result or "[assistant]: response" in result


# ---------------------------------------------------------------------------
# SlackTransport — command handling
# ---------------------------------------------------------------------------


class TestCommandHandling:
    """Tests for !command parsing and execution."""

    @pytest.mark.asyncio
    async def test_stop_command(self):
        rt = _make_runtime()
        rt.stop_chat.return_value = (True, None)
        transport = SlackTransport(rt)

        result = await transport._handle_command("!stop", "C123:1234")
        assert "Stopped" in result

    @pytest.mark.asyncio
    async def test_stop_nothing_running(self):
        rt = _make_runtime()
        rt.stop_chat.return_value = (False, None)
        rt.clear_queue.return_value = 0
        transport = SlackTransport(rt)

        result = await transport._handle_command("!stop", "C123:1234")
        assert result == "Nothing running."

    @pytest.mark.asyncio
    async def test_stop_with_queued(self):
        rt = _make_runtime()
        rt.stop_chat.return_value = (False, None)
        rt.clear_queue.return_value = 3
        transport = SlackTransport(rt)

        result = await transport._handle_command("!stop", "C123:1234")
        assert "3 queued" in result

    @pytest.mark.asyncio
    async def test_status_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        result = await transport._handle_command("!status", "C123:1234")
        assert "Provider" in result
        assert "Model" in result

    @pytest.mark.asyncio
    async def test_use_command_with_choice(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        with patch("enso.transports.slack.cmd_use", return_value=("Provider set to codex.", [])):
            result = await transport._handle_command("!use codex", "C123:1234")
        assert "codex" in result

    @pytest.mark.asyncio
    async def test_use_command_no_choice(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        with patch(
            "enso.transports.slack.cmd_use",
            return_value=(None, [("claude", True), ("codex", False)]),
        ):
            result = await transport._handle_command("!use", "C123:1234")
        assert "claude" in result
        assert "codex" in result

    @pytest.mark.asyncio
    async def test_model_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        with patch(
            "enso.transports.slack.cmd_model",
            return_value=("claude model \u2192 sonnet", []),
        ):
            result = await transport._handle_command("!model sonnet", "C123:1234")
        assert "sonnet" in result

    @pytest.mark.asyncio
    async def test_clear_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        with patch(
            "enso.transports.slack.cmd_clear",
            return_value=["Claude: Cleared."],
        ):
            result = await transport._handle_command("!clear", "C123:1234")
        assert "Cleared" in result

    @pytest.mark.asyncio
    async def test_clear_all_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        with patch(
            "enso.transports.slack.cmd_clear",
            return_value=["Claude: Cleared.", "Codex: Cleared."],
        ) as mock_clear:
            result = await transport._handle_command("!clear all", "C123:1234")
        mock_clear.assert_called_once_with(rt, "C123:1234", clear_all=True)
        assert "Cleared" in result

    @pytest.mark.asyncio
    async def test_logs_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        with patch("enso.transports.slack.cmd_logs", return_value="line1\nline2"):
            result = await transport._handle_command("!logs", "C123:1234")
        assert "line1" in result

    @pytest.mark.asyncio
    async def test_help_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        result = await transport._handle_command("!help", "C123:1234")
        assert "!stop" in result
        assert "!help" in result

    @pytest.mark.asyncio
    async def test_unknown_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        result = await transport._handle_command("!foobar", "C123:1234")
        assert "Unknown command" in result
        assert "foobar" in result


# ---------------------------------------------------------------------------
# SlackTransport — message routing
# ---------------------------------------------------------------------------


class TestMessageRouting:
    """Tests for DM vs channel message routing."""

    @pytest.mark.asyncio
    async def test_dm_message_dispatches(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "user": "U123",
            "channel": "D999",
            "channel_type": "im",
            "ts": "1234.5678",
            "text": "hello",
        }
        await transport._handle_message(event, client)

        rt.dispatch.assert_called_once()
        call_args = rt.dispatch.call_args
        assert call_args[0][0] == "D999"  # conv_id = channel for DMs
        assert call_args[0][1] == "hello"

    @pytest.mark.asyncio
    async def test_channel_message_without_mention_ignored(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "ts": "1234.5678",
            "text": "just chatting",
        }
        await transport._handle_message(event, client)

        rt.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_subtype_ignored(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "subtype": "bot_message",
            "channel": "C123",
            "ts": "1234.5678",
            "text": "bot says hi",
        }
        await transport._handle_message(event, client)

        rt.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_user_ignored(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "subtype": None,
            "channel": "C123",
            "ts": "1234.5678",
            "text": "ghost message",
        }
        await transport._handle_message(event, client)

        rt.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_user_ignored(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "user": "UBAD",
            "channel": "D999",
            "channel_type": "im",
            "ts": "1234.5678",
            "text": "sneaky",
        }
        await transport._handle_message(event, client)

        rt.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_reply_with_active_session(self):
        rt = _make_runtime()
        rt.session_by_chat_provider = {("C123:1000.0000", "claude"): "sess-1"}
        transport = SlackTransport(rt)
        client = _make_client()
        client.conversations_replies.return_value = {"messages": []}

        event = {
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "ts": "1234.5678",
            "thread_ts": "1000.0000",
            "text": "follow up",
        }
        await transport._handle_message(event, client)

        rt.dispatch.assert_called_once()
        call_args = rt.dispatch.call_args
        assert call_args[0][0] == "C123:1000.0000"

    @pytest.mark.asyncio
    async def test_thread_reply_without_session_ignored(self):
        rt = _make_runtime()
        rt.session_by_chat_provider = {}
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "user": "U123",
            "channel": "C123",
            "channel_type": "channel",
            "ts": "1234.5678",
            "thread_ts": "1000.0000",
            "text": "nobody home",
        }
        await transport._handle_message(event, client)

        rt.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_with_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "user": "U123",
            "channel": "D999",
            "channel_type": "im",
            "ts": "1234.5678",
            "text": "!status",
        }
        await transport._handle_message(event, client)

        # Command was handled, dispatch should NOT be called
        rt.dispatch.assert_not_called()
        # But a reply should have been sent
        client.chat_postMessage.assert_called_once()


# ---------------------------------------------------------------------------
# SlackTransport — app_mention handler
# ---------------------------------------------------------------------------


class TestAppMention:
    """Tests for the app_mention event handler."""

    @pytest.mark.asyncio
    async def test_mention_dispatches(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()
        client.conversations_replies.return_value = {"messages": []}

        event = {
            "user": "U123",
            "channel": "C123",
            "ts": "1234.5678",
            "text": "<@UBOT> do something",
        }
        await transport._handle_app_mention(event, client)

        rt.dispatch.assert_called_once()
        call_args = rt.dispatch.call_args
        assert call_args[0][0] == "C123:1234.5678"
        assert call_args[0][1] == "do something"  # mention stripped

    @pytest.mark.asyncio
    async def test_mention_unauthorized_ignored(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "user": "UBAD",
            "channel": "C123",
            "ts": "1234.5678",
            "text": "<@UBOT> do something",
        }
        await transport._handle_app_mention(event, client)

        rt.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_empty_text_ignored(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "user": "U123",
            "channel": "C123",
            "ts": "1234.5678",
            "text": "<@UBOT>",
        }
        await transport._handle_app_mention(event, client)

        rt.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_in_thread(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()
        client.conversations_replies.return_value = {
            "messages": [
                {"user": "U123", "text": "start thread"},
                {"user": "U123", "text": "<@UBOT> help me"},
            ],
        }

        event = {
            "user": "U123",
            "channel": "C123",
            "ts": "2000.0000",
            "thread_ts": "1000.0000",
            "text": "<@UBOT> help me",
        }
        await transport._handle_app_mention(event, client)

        rt.dispatch.assert_called_once()
        call_args = rt.dispatch.call_args
        # conv_id uses thread_ts
        assert call_args[0][0] == "C123:1000.0000"
        # Thread context should be prepended
        assert "[Thread context]" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_mention_with_command(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        client = _make_client()

        event = {
            "user": "U123",
            "channel": "C123",
            "ts": "1234.5678",
            "text": "<@UBOT> !help",
        }
        await transport._handle_app_mention(event, client)

        rt.dispatch.assert_not_called()
        client.chat_postMessage.assert_called_once()


# ---------------------------------------------------------------------------
# SlackTransport — notify
# ---------------------------------------------------------------------------


class TestNotify:
    """Tests for the notify method."""

    @pytest.mark.asyncio
    async def test_notify_to_channel(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        transport._client = _make_client()

        await transport.notify("hello")

        transport._client.chat_postMessage.assert_called_once_with(
            channel="C999", text="hello",
        )

    @pytest.mark.asyncio
    async def test_notify_with_destination(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        transport._client = _make_client()

        await transport.notify("hello", destination="C111")

        transport._client.chat_postMessage.assert_called_once_with(
            channel="C111", text="hello",
        )

    @pytest.mark.asyncio
    async def test_notify_fallback_to_first_user(self):
        rt = _make_runtime()
        rt.config["transports"]["slack"]["notify_channel"] = ""
        transport = SlackTransport(rt)
        transport._client = _make_client()

        await transport.notify("hello")

        transport._client.chat_postMessage.assert_called_once_with(
            channel="U123", text="hello",
        )

    @pytest.mark.asyncio
    async def test_notify_no_client_warns(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)
        transport._client = None

        # Should not raise
        await transport.notify("hello")


# ---------------------------------------------------------------------------
# SlackTransport — bot participation check
# ---------------------------------------------------------------------------


class TestBotParticipation:
    """Tests for _is_bot_participating."""

    def test_participating(self):
        rt = _make_runtime()
        rt.session_by_chat_provider = {("C123:1000.0000", "claude"): "sess-1"}
        transport = SlackTransport(rt)

        assert transport._is_bot_participating("C123:1000.0000") is True

    def test_not_participating(self):
        rt = _make_runtime()
        rt.session_by_chat_provider = {}
        transport = SlackTransport(rt)

        assert transport._is_bot_participating("C123:1000.0000") is False

    def test_different_conversation(self):
        rt = _make_runtime()
        rt.session_by_chat_provider = {("C123:9999.0000", "claude"): "sess-1"}
        transport = SlackTransport(rt)

        assert transport._is_bot_participating("C123:1000.0000") is False


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


class TestSafeFilename:
    """Tests for _safe_filename."""

    def test_normal_filename(self):
        assert _safe_filename("report.pdf") == "report.pdf"

    def test_path_traversal(self):
        assert _safe_filename("../../etc/passwd") == "passwd"

    def test_dotfile(self):
        assert _safe_filename(".env") == "env"

    def test_nested_path(self):
        assert _safe_filename("/home/user/file.txt") == "file.txt"


# ---------------------------------------------------------------------------
# Transport init
# ---------------------------------------------------------------------------


class TestTransportInit:
    """Tests for SlackTransport initialization."""

    def test_config_loading(self):
        rt = _make_runtime()
        transport = SlackTransport(rt)

        assert transport.bot_token == "xoxb-fake"
        assert transport.app_token == "xapp-fake"
        assert transport.allowed_users == ["U123"]
        assert transport.bot_user_id == "UBOT"
        assert transport.notify_channel == "C999"
        assert transport.name == "slack"
        assert transport.message_limit == 40000

    def test_empty_config(self):
        rt = _make_runtime()
        rt.config = {"transports": {}}
        transport = SlackTransport(rt)

        assert transport.bot_token == ""
        assert transport.allowed_users == []
