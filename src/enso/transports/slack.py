"""Slack transport — channel and DM support via Socket Mode."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import uuid
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
    cmd_compact_async,
    cmd_effort,
    cmd_help,
    cmd_logs,
    cmd_model,
    cmd_status,
    cmd_stop_async,
    cmd_update_async,
    cmd_use,
)
from ..formatting import md_to_mrkdwn
from . import BaseTransport, TransportContext, safe_filename

if TYPE_CHECKING:
    from ..core import Runtime

log = logging.getLogger(__name__)

# Slack message subtypes that aren't user-authored content — channel/group
# lifecycle, message lifecycle, pin/reminder noise, etc. Anything not in this
# set falls through, including the empty (plain message) case and content-
# bearing subtypes like file_share, me_message, and thread_broadcast. The
# downstream text/files guard drops anything genuinely empty.
#
# `document_mention` (canvas body @-mention) is intentionally ignored. Slack
# delivers it both as a message event and as an app_mention subtype (with a
# canvas file/section pointer rather than a chat-thread anchor), so both
# handlers consult this set. Threaded canvas comments arrive as regular
# app_mention events and still fall through.
IGNORED_SUBTYPES: frozenset[str] = frozenset({
    "bot_message",
    "message_changed", "message_deleted", "message_replied",
    "channel_join", "channel_leave",
    "channel_archive", "channel_unarchive",
    "channel_name", "channel_purpose", "channel_topic",
    "channel_convert_to_private", "channel_convert_to_public",
    "channel_posting_permissions",
    "group_join", "group_leave",
    "group_archive", "group_unarchive",
    "group_name", "group_purpose", "group_topic",
    "pinned_item", "unpinned_item",
    "reminder_add", "ekm_access_denied",
    "file_mention", "file_comment",
    "document_mention",
})

# Commands available in Slack (name, description).
SLACK_COMMANDS: list[tuple[str, str]] = [
    ("stop", "Stop process & clear queue"),
    ("use", "Switch provider"),
    ("model", "Switch model"),
    ("effort", "Set reasoning effort (Claude/Codex, or 'default' to clear)"),
    ("status", "Provider, model & effort info"),
    ("clear", "Clear session (use !clear all for all providers)"),
    ("compact", "Summarise & compact the active session"),
    ("update", "Install the latest stable Enso"),
    ("logs", "Last 25 log entries"),
    ("help", "Show commands"),
]


def _render_options(header: str, options: list[tuple[str, bool]]) -> str:
    """Render a picker as text lines with the active entry marked."""
    lines = [header]
    for label, active in options:
        prefix = "● " if active else "  "
        lines.append(f"{prefix}{label}")
    return "\n".join(lines)


def _file_download_url(file_info: dict) -> str:
    """Return the authenticated Slack download URL, if present."""
    return file_info.get("url_private_download") or file_info.get("url_private") or ""


def _download_filename(file_info: dict) -> str:
    """Build a collision-resistant local filename for a Slack file."""
    raw_name = file_info.get("name") or file_info.get("title") or "file"
    name = safe_filename(str(raw_name)) or "file"
    prefix = safe_filename(str(file_info.get("id") or "")) or uuid.uuid4().hex[:8]
    return f"{prefix}-{name}"


def _file_label(file_info: dict) -> str:
    raw_name = file_info.get("name") or file_info.get("title") or file_info.get("id")
    return safe_filename(str(raw_name)) if raw_name else "file"


def _file_prompt(downloaded: list[str], files: list[dict]) -> str:
    if downloaded:
        return "User uploaded a file: " + ", ".join(downloaded)
    if not files:
        return ""
    labels = ", ".join(_file_label(file_info) for file_info in files)
    suffix = f": {labels}" if labels else "."
    return "User uploaded a file, but it could not be downloaded" + suffix


def _is_shared_message(att: dict) -> bool:
    """True when an attachment carries a forwarded/shared message.

    Slack flags shares with ``is_msg_unfurl`` (the older ``is_share`` field is
    no longer in the schema), but we also accept any attachment carrying author
    or text content so we degrade gracefully if the flag is ever absent.
    """
    if att.get("is_msg_unfurl"):
        return True
    return bool(
        att.get("author_name") or att.get("author_id") or att.get("text")
    )


def _render_attachment(att: dict) -> str:
    """Render one shared-message attachment as prompt text."""
    author = (
        att.get("author_name")
        or att.get("author_subname")
        or att.get("author_id")
        or ""
    )
    channel = att.get("channel_name") or ""
    label_parts = [p for p in (author, f"in #{channel}" if channel else "") if p]
    label = " ".join(label_parts)
    header = f"[Shared message — {label}]" if label else "[Shared message]"

    lines = [header]
    body = (att.get("text") or att.get("fallback") or "").strip()
    if body:
        lines.append(body)
    link = att.get("from_url") or ""
    if link:
        lines.append(f"(link: {link})")
    return "\n".join(lines)


def _attachments_prompt(attachments: list[dict]) -> str:
    """Render forwarded/shared Slack messages into prompt text.

    When a user shares (forwards) a message, Slack delivers the original
    content in the event's ``attachments`` array — not in ``text``, which holds
    only the forwarder's own typed words. Each shared message arrives as an
    unfurl object carrying the author, source channel, body, and a permalink.
    """
    rendered = [
        _render_attachment(att)
        for att in attachments
        if isinstance(att, dict) and _is_shared_message(att)
    ]
    return "\n\n".join(r for r in rendered if r)


def _message_context_text(msg: dict) -> str:
    """Combine a history message's text with any forwarded-message content.

    Forwarded messages in fetched channel/thread history carry their content in
    ``attachments`` just like live events, so context rendering must surface it
    too — otherwise the agent sees a blank line where a shared message was.
    """
    text = msg.get("text", "")
    shared = _attachments_prompt(msg.get("attachments") or [])
    return "\n".join(part for part in (text, shared) if part)


def _attachment_files(attachments: list[dict]) -> list[dict]:
    """Collect files carried by shared-message attachments.

    A forwarded message's own images/files live under the attachment's
    ``files`` array, not the event's top-level ``files``, so they need
    gathering separately before download.
    """
    files: list[dict] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        for file_info in att.get("files") or []:
            if isinstance(file_info, dict):
                files.append(file_info)
    return files


class SlackContext(TransportContext):
    """Sends replies back to a Slack channel or DM."""

    def __init__(
        self,
        client: AsyncWebClient,
        channel: str,
        thread_ts: str | None = None,
        *,
        user_id: str = "",
    ):
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._user_id = user_id

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

    def get_origin_env(self) -> dict[str, str]:
        env = {
            "ENSO_ORIGIN_TRANSPORT": "slack",
            "ENSO_ORIGIN_CHANNEL": self._channel,
            "ENSO_ORIGIN_THREAD_TS": self._thread_ts or "",
            "ENSO_ORIGIN_USER_ID": self._user_id,
        }
        # Best-effort name resolution via the on-disk cache — never hits the
        # API here, since this runs on the hot path. Cache misses just leave
        # the name blank and the agent can fall back to the ID.
        try:
            cache = slack_cache.load()
            user = cache.get("users", {}).get("items", {}).get(self._user_id, {})
            name = (
                user.get("display_name")
                or user.get("real_name")
                or user.get("name")
                or ""
            )
            env["ENSO_ORIGIN_USER_NAME"] = name
            if self._channel.startswith("D"):
                env["ENSO_ORIGIN_CHANNEL_NAME"] = "dm"
            else:
                channel = (
                    cache.get("channels", {}).get("items", {}).get(self._channel, {})
                )
                cname = channel.get("name", "")
                env["ENSO_ORIGIN_CHANNEL_NAME"] = f"#{cname}" if cname else ""
        except Exception:
            log.debug("Slack cache lookup failed for origin env", exc_info=True)
            env.setdefault("ENSO_ORIGIN_USER_NAME", "")
            env.setdefault("ENSO_ORIGIN_CHANNEL_NAME", "")
        return env


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
        self._warm_directory_cache()
        app = AsyncApp(token=self.bot_token)
        self._client = app.client
        self._register_listeners(app)

        async def _run() -> None:
            handler = AsyncSocketModeHandler(app, self.app_token)
            self._start_background_tasks()
            await handler.start_async()

        asyncio.run(_run())

    async def _send_update_confirmation(self, pending: dict, text: str) -> bool:
        if not self._client:
            return False
        payload: dict[str, str] = {
            "channel": str(pending.get("channel", "")),
            "text": text,
        }
        if pending.get("thread"):
            payload["thread_ts"] = str(pending["thread"])
        await self._client.chat_postMessage(**payload)
        return True

    def _warm_directory_cache(self) -> None:
        """Populate the user+channel cache on startup so origin-env lookups
        resolve names without a per-message API hit.

        Respects the cache's own recency guard, so frequent restarts don't
        hammer the Slack API. Failures are swallowed — the transport still
        starts; lookups just fall back to IDs until the next refresh.
        """
        if not self.bot_token:
            return
        cache = slack_cache.load()
        try:
            if not slack_cache._recently_refreshed(cache["users"]):
                cache = slack_cache.refresh_users(self.bot_token, cache)
            if not slack_cache._recently_refreshed(cache["channels"]):
                slack_cache.refresh_channels(self.bot_token, cache)
        except Exception:
            log.warning("Slack directory cache warm failed", exc_info=True)

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
        if event.get("subtype") in IGNORED_SUBTYPES:
            return

        user = event.get("user", "")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        text = event.get("text", "")
        attachments = event.get("attachments") or []
        # Forwarded/shared messages bring their own files under the attachment,
        # alongside any files uploaded directly with the mention.
        files = (event.get("files") or []) + _attachment_files(attachments)

        if not is_authorized(user, self.allowed_users):
            return

        # Strip bot mention from text
        text = re.sub(r"<@\w+>\s*", "", text).strip()
        # Forwarded message content lives in `attachments`, not `text`.
        shared_prompt = _attachments_prompt(attachments)
        if not text and not files and not shared_prompt:
            return

        # Conversation scoped to channel + thread
        conv_id = f"{channel}:{thread_ts or ts}"
        # Always reply in a thread for channel mentions
        reply_thread_ts = thread_ts or ts

        # Check for !commands before dispatch
        if text.startswith("!"):
            ctx = SlackContext(client, channel, reply_thread_ts, user_id=user)
            response = await self._handle_command(text, conv_id, ctx=ctx)
            if response:
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

        # Download attachments — direct uploads and files carried by a
        # forwarded message both arrive on the app_mention event.
        downloaded = await self._download_files(files, client) if files else []

        parts: list[str] = []
        if context:
            parts.append(context)
        if shared_prompt:
            parts.append(shared_prompt)
        file_prompt = _file_prompt(downloaded, files)
        if file_prompt:
            parts.append(file_prompt)
        if text:
            parts.append(text)
        prompt = "\n\n".join(parts)

        preview_src = (
            text or shared_prompt or (downloaded[0] if downloaded else file_prompt)
        )
        preview = preview_src[:50].replace("\n", " ")
        ctx = SlackContext(client, channel, reply_thread_ts, user_id=user)
        log.info(
            "Incoming mention: channel=%s user=%s thread=%s len=%d files=%d",
            channel, user, thread_ts or ts, len(text), len(downloaded),
        )
        await self.runtime.dispatch(conv_id, prompt, ctx, preview=preview)

    async def _handle_message(
        self, event: dict, client: AsyncWebClient,
    ) -> None:
        """Handle DMs and thread continuations."""
        if event.get("subtype") in IGNORED_SUBTYPES:
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
        # Forwarded message content lives in `attachments`, not `text`, and the
        # forwarded message's own files hang off the attachment too.
        attachments = event.get("attachments") or []
        shared_prompt = _attachments_prompt(attachments)
        files = (event.get("files") or []) + _attachment_files(attachments)
        if not text and not files and not shared_prompt:
            return

        # Check for !commands before dispatch
        if text.startswith("!"):
            ctx = SlackContext(client, channel, reply_thread_ts, user_id=user)
            response = await self._handle_command(text, conv_id, ctx=ctx)
            if response:
                await ctx.reply(response)
                return

        # Handle files — direct uploads or those carried by a forwarded message.
        if files:
            await self._handle_files(
                files, text, conv_id, client, channel, reply_thread_ts,
                user=user, shared_prompt=shared_prompt,
            )
            return

        # Fetch thread context for DM threads
        thread_context = ""
        if thread_ts:
            thread_context = await self._fetch_thread_context(
                client, channel, thread_ts,
            )

        parts = [p for p in (thread_context, shared_prompt, text) if p]
        prompt = "\n\n".join(parts)

        preview = (text or shared_prompt)[:50].replace("\n", " ")
        ctx = SlackContext(client, channel, reply_thread_ts, user_id=user)
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
            body = _message_context_text(msg)
            if body:
                lines.append(f"[{role}]: {body}")

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
            body = _message_context_text(msg)
            if body:
                lines.append(f"[{role}]: {body}")

        return "[Channel context]\n" + "\n".join(lines) if lines else ""

    async def _handle_command(
        self, text: str, conv_id: str, ctx: SlackContext | None = None,
    ) -> str | None:
        """Parse and execute a !command. Returns response text or None.

        ``ctx`` is optional but commands that need to post a progress message
        before doing slow work (e.g. ``!compact``) will use it when given.
        """
        parts = text[1:].split(None, 1)
        cmd_name = parts[0].lower() if parts else ""
        cmd_args = parts[1] if len(parts) > 1 else None

        rt = self.runtime

        if cmd_name == "stop":
            return await cmd_stop_async(rt, conv_id)

        if cmd_name == "use":
            response, options = cmd_use(rt, conv_id, cmd_args)
            return response or _render_options("Switch provider:", options)

        if cmd_name == "model":
            response, options = cmd_model(rt, conv_id, cmd_args)
            provider = rt.get_active_provider(conv_id)
            return response or _render_options(f"Switch model ({provider}):", options)

        if cmd_name == "effort":
            response, options = cmd_effort(rt, conv_id, cmd_args)
            if response:
                return response
            model = rt.get_active_model(conv_id, rt.get_active_provider(conv_id))
            header = f"Set effort ({model}) — '!effort default' to clear:"
            return _render_options(header, options)

        if cmd_name == "status":
            return cmd_status(rt, conv_id)

        if cmd_name == "clear":
            clear_all = cmd_args and cmd_args.strip().lower() == "all"
            parts_list = cmd_clear(rt, conv_id, clear_all=bool(clear_all))
            return "\n".join(parts_list)

        if cmd_name == "compact":
            if ctx is not None:
                await ctx.reply(
                    "Compacting context - this can take 10-30s while the "
                    "agent summarises..."
                )
            return await cmd_compact_async(rt, conv_id)

        if cmd_name == "update":
            if ctx is not None:
                await ctx.reply("Checking the latest stable Enso release…")
            result = await cmd_update_async(rt)
            if result.restart_required:
                from ..updater import queue_update_confirmation, schedule_service_restart

                origin = ctx.get_origin_env() if ctx is not None else {}
                channel = origin.get("ENSO_ORIGIN_CHANNEL", "")
                thread = origin.get("ENSO_ORIGIN_THREAD_TS", "")
                if not channel:
                    channel, _, fallback_thread = conv_id.partition(":")
                    thread = thread or fallback_thread
                queue_update_confirmation(
                    result,
                    transport=self.name,
                    channel=channel,
                    thread=thread,
                )
                schedule_service_restart()
            return result.message

        if cmd_name == "logs":
            return cmd_logs()[-40000:]

        if cmd_name == "help":
            return cmd_help(SLACK_COMMANDS, prefix="!")

        return f"Unknown command: !{cmd_name}. Use !help for available commands."

    async def _hydrate_file_info(
        self, file_info: dict, client: AsyncWebClient,
    ) -> dict:
        """Fetch full file metadata when Slack only sends a placeholder."""
        if _file_download_url(file_info):
            return file_info
        if file_info.get("file_access") != "check_file_info":
            return file_info

        file_id = file_info.get("id")
        if not file_id:
            return file_info

        try:
            result = await client.files_info(file=file_id)
        except Exception:
            log.exception("files.info failed for Slack file %s", file_id)
            return file_info

        hydrated = result.get("file") or {}
        if not isinstance(hydrated, dict):
            return file_info
        return {**file_info, **hydrated}

    async def _download_files(
        self, files: list[dict], client: AsyncWebClient,
    ) -> list[str]:
        hydrated = await asyncio.gather(
            *(self._hydrate_file_info(file_info, client) for file_info in files)
        )
        return await asyncio.to_thread(self._download_files_sync, list(hydrated))

    def _download_files_sync(self, files: list[dict]) -> list[str]:
        """Download Slack file uploads into the workspace's uploads dir.

        Returns the local paths of files that downloaded successfully; failed
        downloads are logged and skipped so a single broken attachment doesn't
        drop the whole message.
        """
        uploads_dir = os.path.join(self.runtime.working_dir, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)

        downloaded: list[str] = []
        for file_info in files:
            url = _file_download_url(file_info)
            if not url:
                continue
            name = _download_filename(file_info)
            dest_path = os.path.join(uploads_dir, name)
            try:
                req = Request(url, headers={"Authorization": f"Bearer {self.bot_token}"})
                with urlopen(req) as resp, open(dest_path, "wb") as f:
                    f.write(resp.read())
                downloaded.append(dest_path)
                log.info("Downloaded file to %s", dest_path)
            except Exception:
                log.exception("Failed to download file %s", name)
        return downloaded

    async def _handle_files(
        self,
        files: list[dict],
        text: str,
        conv_id: str,
        client: AsyncWebClient,
        channel: str,
        reply_thread_ts: str | None,
        *,
        user: str = "",
        shared_prompt: str = "",
    ) -> None:
        """Download uploaded files and dispatch prompt (DM path).

        ``shared_prompt`` carries any forwarded-message text when the files
        came in on a shared message rather than a direct upload.
        """
        downloaded = await self._download_files(files, client)
        file_prompt = _file_prompt(downloaded, files)
        if not file_prompt and not text and not shared_prompt:
            return

        parts = [p for p in (shared_prompt, file_prompt, text) if p]
        prompt = "\n\n".join(parts)

        preview = prompt[:50].replace("\n", " ")
        ctx = SlackContext(client, channel, reply_thread_ts, user_id=user)
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
