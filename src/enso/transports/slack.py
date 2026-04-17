"""Slack transport — channel and DM support via Socket Mode."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from typing import TYPE_CHECKING, Any
from urllib.request import Request, urlopen

try:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp
    from slack_sdk.web.async_client import AsyncWebClient
except ImportError as e:
    raise ImportError(
        f"Slack transport dependencies are missing ({e.name}). "
        "Install them with: pip install enso[slack]"
    ) from e

from .. import slack_cache
from ..auth import is_authorized
from ..commands import (
    cmd_clear,
    cmd_effort,
    cmd_help,
    cmd_logs,
    cmd_model,
    cmd_status,
    cmd_stop_async,
    cmd_use,
)
from ..formatting import md_to_mrkdwn
from . import BaseTransport, TransportContext

if TYPE_CHECKING:
    from ..core import Runtime

log = logging.getLogger(__name__)

# Commands available in Slack (name, description).
SLACK_COMMANDS: list[tuple[str, str]] = [
    ("stop", "Stop process & clear queue"),
    ("use", "Switch provider"),
    ("model", "Switch model"),
    ("effort", "Set reasoning effort (Claude, or 'default' to clear)"),
    ("status", "Provider, model & effort info"),
    ("clear", "Clear session (use !clear all for all providers)"),
    ("logs", "Last 25 log entries"),
    ("help", "Show commands"),
]


def _safe_filename(name: str) -> str:
    """Sanitise a filename to prevent path traversal."""
    return os.path.basename(name).lstrip(".")


class SlackContext(TransportContext):
    """Sends replies back to a Slack channel or DM."""

    def __init__(
        self,
        client: AsyncWebClient,
        channel: str,
        thread_ts: str | None = None,
    ):
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts

    async def reply(self, text: str) -> None:
        kwargs: dict[str, Any] = {
            "channel": self._channel,
            "text": md_to_mrkdwn(text),
        }
        if self._thread_ts:
            kwargs["thread_ts"] = self._thread_ts
        await self._client.chat_postMessage(**kwargs)

    async def reply_status(self, text: str) -> Any:
        kwargs: dict[str, Any] = {
            "channel": self._channel,
            "text": text,
        }
        if self._thread_ts:
            kwargs["thread_ts"] = self._thread_ts
        result = await self._client.chat_postMessage(**kwargs)
        return result["ts"]

    async def edit_status(self, handle: Any, text: str) -> None:
        await self._client.chat_update(
            channel=self._channel, ts=handle, text=text,
        )

    async def delete_status(self, handle: Any) -> None:
        with contextlib.suppress(Exception):
            await self._client.chat_delete(
                channel=self._channel, ts=handle,
            )

    async def send_typing(self) -> None:
        """No-op — Slack bots cannot send typing indicators."""


class SlackTransport(BaseTransport):
    """Slack bot transport using Socket Mode."""

    name = "slack"
    message_limit = 40000

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        slack_cfg = runtime.config.get("transports", {}).get("slack", {})
        self.bot_token: str = slack_cfg.get("bot_token", "")
        self.app_token: str = slack_cfg.get("app_token", "")
        raw = slack_cfg.get("allowed_users", [])
        self.allowed_users: list[str] = [str(u) for u in raw]
        self.bot_user_id: str = slack_cfg.get("bot_user_id", "")
        self.notify_channel: str = slack_cfg.get("notify_channel", "")
        self.channel_context_messages: int = int(
            slack_cfg.get("channel_context_messages", 20)
        )
        self._client: AsyncWebClient | None = None

    def start(self) -> None:
        """Start listening for Slack events via Socket Mode (blocking)."""
        if not self.allowed_users:
            log.warning(
                "allowed_users is empty — no one can message this bot! "
                "Run 'enso setup' or edit ~/.enso/config.json to add users."
            )
        log.info("Starting Slack transport")
        app = AsyncApp(token=self.bot_token)
        self._client = app.client
        self._register_listeners(app)

        async def _run() -> None:
            handler = AsyncSocketModeHandler(app, self.app_token)
            self._scheduler_task = asyncio.create_task(
                self.runtime.run_job_scheduler()
            )
            await handler.start_async()

        asyncio.run(_run())

    def _register_listeners(self, app: AsyncApp) -> None:
        """Register event listeners on the Slack app."""

        @app.event("app_mention")
        async def handle_app_mention(event: dict, client: AsyncWebClient) -> None:
            await self._handle_app_mention(event, client)

        @app.event("message")
        async def handle_message(event: dict, client: AsyncWebClient) -> None:
            await self._handle_message(event, client)

        self._register_directory_listeners(app)

    def _register_directory_listeners(self, app: AsyncApp) -> None:
        """Register event handlers that keep the Slack directory cache fresh.

        These only fire if the Slack app has the corresponding event
        subscriptions enabled (see README). When they don't fire the cache
        falls back to refresh-on-miss via the ``enso slack`` CLI, so missing
        subscriptions just make the cache less immediate — not broken.
        """

        async def _apply_user(event: dict) -> None:
            user = event.get("user") or {}
            if user.get("id"):
                await asyncio.to_thread(slack_cache.apply_user_change, user)

        @app.event("user_change")
        async def on_user_change(event: dict) -> None:
            await _apply_user(event)

        @app.event("team_join")
        async def on_team_join(event: dict) -> None:
            await _apply_user(event)

        async def _apply_channel_upsert(event: dict) -> None:
            channel = event.get("channel")
            # Slack is inconsistent — channel_created sends a dict, but
            # channel_rename / channel_archived send just the ID at the top
            # level (or a minimal dict). Fetch fresh info to be safe.
            if isinstance(channel, dict) and channel.get("id"):
                await asyncio.to_thread(slack_cache.apply_channel_upsert, channel)
                return
            channel_id = channel if isinstance(channel, str) else event.get("channel", "")
            if not channel_id or not self._client:
                return
            try:
                info = await self._client.conversations_info(channel=channel_id)
            except Exception:
                log.exception("conversations.info failed for %s", channel_id)
                return
            ch = info.get("channel")
            if ch:
                await asyncio.to_thread(slack_cache.apply_channel_upsert, ch)

        @app.event("channel_created")
        async def on_channel_created(event: dict) -> None:
            await _apply_channel_upsert(event)

        @app.event("channel_rename")
        async def on_channel_rename(event: dict) -> None:
            await _apply_channel_upsert(event)

        @app.event("channel_archive")
        async def on_channel_archive(event: dict) -> None:
            await _apply_channel_upsert(event)

        @app.event("channel_unarchive")
        async def on_channel_unarchive(event: dict) -> None:
            await _apply_channel_upsert(event)

        @app.event("channel_deleted")
        async def on_channel_deleted(event: dict) -> None:
            channel_id = event.get("channel", "")
            if channel_id:
                await asyncio.to_thread(slack_cache.apply_channel_delete, channel_id)

        async def _on_membership(event: dict, *, joined: bool) -> None:
            if event.get("user") != self.bot_user_id:
                return  # Only care when the bot itself is the subject.
            channel_id = event.get("channel", "")
            if channel_id:
                await asyncio.to_thread(
                    slack_cache.set_channel_is_member, channel_id, joined,
                )

        @app.event("member_joined_channel")
        async def on_member_joined(event: dict) -> None:
            await _on_membership(event, joined=True)

        @app.event("member_left_channel")
        async def on_member_left(event: dict) -> None:
            await _on_membership(event, joined=False)

    # -- Event handlers --

    async def _handle_app_mention(
        self, event: dict, client: AsyncWebClient,
    ) -> None:
        """Handle @bot mentions in channels."""
        user = event.get("user", "")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        text = event.get("text", "")

        if not is_authorized(user, self.allowed_users):
            return

        # Strip bot mention from text
        text = re.sub(r"<@\w+>\s*", "", text).strip()
        if not text:
            return

        # Conversation scoped to channel + thread
        conv_id = f"{channel}:{thread_ts or ts}"
        # Always reply in a thread for channel mentions
        reply_thread_ts = thread_ts or ts

        # Check for !commands before dispatch
        if text.startswith("!"):
            response = await self._handle_command(text, conv_id)
            if response:
                ctx = SlackContext(client, channel, reply_thread_ts)
                await ctx.reply(response)
                return

        # Inject context: channel history for top-level, thread history for threads
        context = ""
        if thread_ts:
            context = await self._fetch_thread_context(
                client, channel, thread_ts,
            )
        else:
            context = await self._fetch_channel_context(
                client, channel, ts,
            )

        prompt = f"{context}\n\n{text}".strip() if context else text

        preview = text[:50].replace("\n", " ")
        ctx = SlackContext(client, channel, reply_thread_ts)
        log.info(
            "Incoming mention: channel=%s user=%s thread=%s len=%d",
            channel, user, thread_ts or ts, len(text),
        )
        await self.runtime.dispatch(conv_id, prompt, ctx, preview=preview)

    async def _handle_message(
        self, event: dict, client: AsyncWebClient,
    ) -> None:
        """Handle DMs and thread continuations."""
        # Skip bot messages, subtypes (joins, edits, etc.)
        if event.get("subtype") is not None:
            return
        if event.get("user") is None:
            return

        user = event["user"]
        channel = event.get("channel", "")
        channel_type = event.get("channel_type", "")
        thread_ts = event.get("thread_ts")
        text = event.get("text", "")

        if channel_type == "im":
            # Direct message — always respond
            conv_id = channel
            reply_thread_ts = thread_ts  # None for inline, set for threaded
        else:
            # Channel messages (top-level or thread) without mention — ignore.
            # Bot only responds in channels via app_mention.
            return

        if not is_authorized(user, self.allowed_users):
            return

        # Strip bot mention if present (can happen in DMs too)
        text = re.sub(r"<@\w+>\s*", "", text).strip()
        if not text and not event.get("files"):
            return

        # Check for !commands before dispatch
        if text.startswith("!"):
            response = await self._handle_command(text, conv_id)
            if response:
                ctx = SlackContext(client, channel, reply_thread_ts)
                await ctx.reply(response)
                return

        # Handle file uploads
        files = event.get("files", [])
        if files:
            await self._handle_files(files, text, conv_id, client, channel, reply_thread_ts)
            return

        # Fetch thread context for DM threads
        thread_context = ""
        if thread_ts:
            thread_context = await self._fetch_thread_context(
                client, channel, thread_ts,
            )

        prompt = f"{thread_context}\n\n{text}".strip() if thread_context else text

        preview = text[:50].replace("\n", " ")
        ctx = SlackContext(client, channel, reply_thread_ts)
        log.info(
            "Incoming message: channel=%s user=%s type=%s len=%d",
            channel, user, channel_type, len(text),
        )
        await self.runtime.dispatch(conv_id, prompt, ctx, preview=preview)

    # -- Helpers --

    async def _fetch_thread_context(
        self,
        client: AsyncWebClient,
        channel: str,
        thread_ts: str,
    ) -> str:
        """Fetch thread messages since the bot's last reply.

        This gives the agent context for what the team discussed since
        it last spoke, rather than the entire thread history.
        """
        try:
            result = await client.conversations_replies(
                channel=channel, ts=thread_ts, limit=100,
            )
        except Exception:
            log.exception("Failed to fetch thread context")
            return ""

        messages = result.get("messages", [])
        if len(messages) <= 1:
            return ""

        # Find the bot's last message index
        bot_last_idx = -1
        for i, msg in enumerate(messages):
            if msg.get("user") == self.bot_user_id:
                bot_last_idx = i

        # Messages after bot's last reply, excluding current message
        context_msgs = (
            messages[bot_last_idx + 1 : -1]
            if bot_last_idx >= 0
            else messages[:-1]
        )

        if not context_msgs:
            return ""

        lines = []
        for msg in context_msgs:
            role = "assistant" if msg.get("user") == self.bot_user_id else "user"
            text = msg.get("text", "")
            if text:
                lines.append(f"[{role}]: {text}")

        return "[Thread context]\n" + "\n".join(lines) if lines else ""

    async def _fetch_channel_context(
        self,
        client: AsyncWebClient,
        channel: str,
        before_ts: str,
    ) -> str:
        """Fetch recent channel messages before a top-level mention.

        Gives the agent awareness of what was said in the channel
        leading up to the mention.
        """
        try:
            result = await client.conversations_history(
                channel=channel,
                latest=before_ts,
                limit=self.channel_context_messages,
                inclusive=False,
            )
        except Exception:
            log.exception("Failed to fetch channel context")
            return ""

        messages = result.get("messages", [])
        if not messages:
            return ""

        # API returns newest-first, reverse for chronological
        messages.reverse()

        lines = []
        for msg in messages:
            role = "assistant" if msg.get("user") == self.bot_user_id else "user"
            text = msg.get("text", "")
            if text:
                lines.append(f"[{role}]: {text}")

        return "[Channel context]\n" + "\n".join(lines) if lines else ""

    async def _handle_command(self, text: str, conv_id: str) -> str | None:
        """Parse and execute a !command. Returns response text or None."""
        parts = text[1:].split(None, 1)
        cmd_name = parts[0].lower() if parts else ""
        cmd_args = parts[1] if len(parts) > 1 else None

        rt = self.runtime

        if cmd_name == "stop":
            queued_count = rt.clear_queue(conv_id)
            had, error = await cmd_stop_async(rt, conv_id)
            if not had and not queued_count:
                return "Nothing running."
            if error:
                return f"Error stopping: {error}"
            msg_parts = []
            if had:
                msg_parts.append("Stopped.")
            if queued_count:
                msg_parts.append(f"Cleared {queued_count} queued message(s).")
            return " ".join(msg_parts)

        if cmd_name == "use":
            response, options = cmd_use(rt, conv_id, cmd_args)
            if response:
                return response
            lines = ["Switch provider:"]
            for name, active in options:
                prefix = "\u25cf " if active else "  "
                lines.append(f"{prefix}{name}")
            return "\n".join(lines)

        if cmd_name == "model":
            response, options = cmd_model(rt, conv_id, cmd_args)
            if response:
                return response
            provider = rt.get_active_provider(conv_id)
            lines = [f"Switch model ({provider}):"]
            for name, active in options:
                prefix = "\u25cf " if active else "  "
                lines.append(f"{prefix}{name}")
            return "\n".join(lines)

        if cmd_name == "effort":
            response, options = cmd_effort(rt, conv_id, cmd_args)
            if response:
                return response
            model = rt.get_active_model(conv_id, rt.get_active_provider(conv_id))
            lines = [f"Set effort ({model}) — '!effort default' to clear:"]
            for name, active in options:
                prefix = "\u25cf " if active else "  "
                lines.append(f"{prefix}{name}")
            return "\n".join(lines)

        if cmd_name == "status":
            return cmd_status(rt, conv_id)

        if cmd_name == "clear":
            clear_all = cmd_args and cmd_args.strip().lower() == "all"
            parts_list = cmd_clear(rt, conv_id, clear_all=bool(clear_all))
            return "\n".join(parts_list)

        if cmd_name == "logs":
            return cmd_logs()[-40000:]

        if cmd_name == "help":
            return cmd_help(SLACK_COMMANDS, prefix="!")

        return f"Unknown command: !{cmd_name}. Use !help for available commands."

    async def _handle_files(
        self,
        files: list[dict],
        text: str,
        conv_id: str,
        client: AsyncWebClient,
        channel: str,
        reply_thread_ts: str | None,
    ) -> None:
        """Download uploaded files and dispatch prompt."""
        uploads_dir = os.path.join(self.runtime.working_dir, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)

        downloaded: list[str] = []
        for file_info in files:
            url = file_info.get("url_private_download") or file_info.get("url_private")
            if not url:
                continue
            name = _safe_filename(file_info.get("name", "file"))
            dest_path = os.path.join(uploads_dir, name)
            try:
                req = Request(url, headers={"Authorization": f"Bearer {self.bot_token}"})
                with urlopen(req) as resp, open(dest_path, "wb") as f:
                    f.write(resp.read())
                downloaded.append(dest_path)
                log.info("Downloaded file to %s", dest_path)
            except Exception:
                log.exception("Failed to download file %s", name)

        if not downloaded:
            return

        prompt = "User uploaded a file: " + ", ".join(downloaded)
        if text:
            prompt += f"\n\n{text}"

        preview = prompt[:50].replace("\n", " ")
        ctx = SlackContext(client, channel, reply_thread_ts)
        await self.runtime.dispatch(conv_id, prompt, ctx, preview=preview)

    async def notify(self, text: str, *, destination: str | None = None) -> None:
        """Send a one-way notification. Requires an explicit destination.

        Resolves to ``destination`` or ``notify_channel``; never falls back to
        the allowed_users list (Slack must always target a single, explicit
        channel or DM to avoid accidental broadcast).
        """
        channel = destination or self.notify_channel
        if not channel:
            log.warning(
                "Slack notify dropped — no destination passed and no"
                " notify_channel set"
            )
            return
        if not self._client:
            log.warning("Cannot notify — client not initialized")
            return
        try:
            await self._client.chat_postMessage(
                channel=channel, text=text[:40000],
            )
        except Exception:
            log.exception("Failed to notify channel %s", channel)
