"""Slack transport — chat with your agents from Slack."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import os
import re
import threading
import time
import urllib.request
import uuid
from typing import TYPE_CHECKING, Any

from ..core import ChatId, split_text
from ..formatting import md_to_slack
from ..providers import PROVIDER_NAMES
from . import BaseTransport, TransportContext

if TYPE_CHECKING:
    from ..core import Runtime

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    from slack_sdk import WebClient
except ImportError:
    raise ImportError(
        "Slack support requires slack-bolt. Install with: pip install enso[slack]"
    )

MAX_SLACK_MSG = 4000  # Slack's message length limit
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB limit (matches Telegram transport)

log = logging.getLogger(__name__)

# Command prefix for message-based commands (no Slack app manifest changes needed).
_CMD_RE = re.compile(r"^!(\w+)(?:\s+(.*))?$", re.DOTALL)

# Commands available via !command syntax in Slack.
COMMANDS = {
    "use": "Switch provider",
    "model": "Switch model",
    "stop": "Kill running process",
    "status": "Provider & model info",
    "clear": "Clear session",
    "help": "Show commands",
}

# Max threads to remember for participation tracking (bounded LRU).
_MAX_PARTICIPATED_THREADS = 10_000


def _chat_id(channel: str, thread_ts: str | None) -> ChatId:
    """Derive a session-scoped chat ID.

    Threaded messages get their own session so conversations in different
    threads don't bleed into each other (mirrors openclaw behaviour).
    """
    if thread_ts:
        return f"{channel}:{thread_ts}"
    return channel


class SlackContext(TransportContext):
    """Sends replies back to a Slack channel/thread."""

    include_prefix = False

    def __init__(self, client: WebClient, channel: str, thread_ts: str | None = None):
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts

    async def reply(self, text: str) -> None:
        formatted = md_to_slack(text)
        for chunk in split_text(formatted, MAX_SLACK_MSG):
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda t=chunk: self._client.chat_postMessage(
                    channel=self._channel,
                    text=t,
                    thread_ts=self._thread_ts,
                    # Send as mrkdwn block so Slack renders formatting.
                    blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": t}}],
                ),
            )

    async def reply_status(self, text: str) -> Any:
        # Use Slack's assistant thread status API for a native "thinking" indicator.
        # Falls back to posting a regular message if the API isn't available.
        if self._thread_ts:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._client.api_call(
                        "assistant.threads.setStatus",
                        json={
                            "channel_id": self._channel,
                            "thread_ts": self._thread_ts,
                            "status": text,
                        },
                    ),
                )
                return "thread_status"  # sentinel handle
            except Exception:
                pass  # fall through to regular message

        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._client.chat_postMessage(
                channel=self._channel,
                text=text,
                thread_ts=self._thread_ts,
            ),
        )
        return resp["ts"]

    async def edit_status(self, handle: Any, text: str) -> None:
        if handle == "thread_status":
            with contextlib.suppress(Exception):
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._client.api_call(
                        "assistant.threads.setStatus",
                        json={
                            "channel_id": self._channel,
                            "thread_ts": self._thread_ts,
                            "status": text,
                        },
                    ),
                )
            return

        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._client.chat_update(
                channel=self._channel,
                ts=handle,
                text=text,
            ),
        )

    async def delete_status(self, handle: Any) -> None:
        if handle == "thread_status":
            with contextlib.suppress(Exception):
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._client.api_call(
                        "assistant.threads.setStatus",
                        json={
                            "channel_id": self._channel,
                            "thread_ts": self._thread_ts,
                            "status": "",
                        },
                    ),
                )
            return

        with contextlib.suppress(Exception):
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._client.chat_delete(
                    channel=self._channel,
                    ts=handle,
                ),
            )


class SlackTransport(BaseTransport):
    """Slack bot transport using Socket Mode."""

    name = "slack"

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        slack_cfg = runtime.config.get("transports", {}).get("slack", {})
        self.bot_token: str = slack_cfg.get("bot_token", "")
        self.app_token: str = slack_cfg.get("app_token", "")
        self.allowed_user_ids: set[str] = set(slack_cfg.get("allowed_user_ids", []))
        self._client: WebClient | None = None
        self._bot_user_id: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Threads the bot has replied in — auto-respond without @mention.
        self._participated_threads: collections.OrderedDict[str, None] = (
            collections.OrderedDict()
        )

    def _is_authorized(self, user_id: str) -> bool:
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            log.warning("Unauthorized Slack user: %s", user_id)
            return False
        return True

    def start(self) -> None:
        """Start the Slack Socket Mode connection (blocking)."""
        if not self.allowed_user_ids:
            log.warning(
                "allowed_user_ids is empty — anyone can message this bot! "
                "Run 'enso setup' or edit ~/.enso/config.json to restrict access."
            )
        log.info("Starting Slack transport")

        app = App(token=self.bot_token)
        self._client = app.client

        # Identify the bot's own user ID so we can ignore our own messages.
        auth = app.client.auth_test()
        self._bot_user_id = auth.get("user_id")
        log.info("Slack bot user: %s", self._bot_user_id)

        # Register event handlers.
        app.event("message")(self._handle_event)
        app.event("app_mention")(self._handle_mention_event)
        app.event("assistant_thread_started")(lambda event, say: None)
        app.event("assistant_thread_context_changed")(lambda event, say: None)

        # Start asyncio event loop in a background thread for the runtime.
        self._loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(
            target=self._run_async_loop, daemon=True
        )
        loop_thread.start()

        # Start job scheduler in the async loop.
        asyncio.run_coroutine_threadsafe(
            self.runtime.run_job_scheduler(), self._loop
        )

        # Socket Mode blocks on the main thread with auto-reconnect.
        backoff = 1
        max_backoff = 300  # 5 minutes
        while True:
            try:
                handler = SocketModeHandler(app, self.app_token)
                connected_at = time.monotonic()
                handler.start()  # blocks until disconnect
                break  # clean exit
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # If we stayed connected for >60s, reset backoff — the
                # disconnect was likely transient, not a config error.
                if time.monotonic() - connected_at > 60:
                    backoff = 1
                # Non-recoverable auth errors — don't retry forever.
                err_str = str(exc).lower()
                if any(
                    tok in err_str
                    for tok in ("invalid_auth", "account_inactive", "token_revoked")
                ):
                    log.error("Non-recoverable Slack auth error: %s", exc)
                    raise
                log.error(
                    "Socket Mode disconnected: %s — reconnecting in %ds",
                    exc,
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    def _run_async_loop(self) -> None:
        """Run the asyncio event loop in a background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def notify(self, text: str) -> None:
        """Send a one-way notification to all allowed users via DM."""
        if not self._client:
            log.warning("Cannot notify — Slack client not initialized yet")
            return
        for user_id in self.allowed_user_ids:
            try:
                # Open a DM channel with the user.
                resp = self._client.conversations_open(users=[user_id])
                dm_channel = resp["channel"]["id"]
                self._client.chat_postMessage(
                    channel=dm_channel, text=text[:MAX_SLACK_MSG],
                )
            except Exception:
                log.exception("Failed to notify Slack user %s", user_id)

    # -- Event handlers (called by Bolt in its own thread) --

    def _handle_event(self, event: dict, say: Any) -> None:
        """Handle incoming Slack messages (DMs and channels)."""
        # Ignore bot's own messages and message edits/deletions.
        subtype = event.get("subtype")
        if subtype in ("bot_message", "message_changed", "message_deleted"):
            return
        user_id = event.get("user", "")
        if user_id == self._bot_user_id:
            return
        if not self._is_authorized(user_id):
            return

        # In channels, only respond if the bot is mentioned or if the bot
        # is already participating in this thread (openclaw-style tracking).
        channel_type = event.get("channel_type", "")
        text = (event.get("text") or "").strip()
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        msg_ts = event.get("ts", "")

        if channel_type != "im":
            mentioned = (
                self._bot_user_id and f"<@{self._bot_user_id}>" in text
            )
            in_participated_thread = (
                thread_ts in self._participated_threads
            )
            if not mentioned and not in_participated_thread:
                return
            # Strip the mention from the prompt if present.
            if mentioned:
                text = re.sub(rf"<@{self._bot_user_id}>\s*", "", text).strip()

        files = event.get("files") or []

        if files:
            self._handle_files(channel, thread_ts, msg_ts, text, files)
            return

        if not text:
            return

        self._dispatch_to_runtime(channel, thread_ts, msg_ts, text, user_id)

    def _handle_mention_event(self, event: dict, say: Any) -> None:
        """Handle @mention events in channels.

        DMs are skipped here — they're already handled by ``_handle_event``
        which prevents duplicate processing (matches openclaw behaviour).
        """
        # Skip DMs — already handled by the message event handler.
        if event.get("channel_type") == "im":
            return

        user_id = event.get("user", "")
        if not self._is_authorized(user_id):
            return

        text = (event.get("text") or "").strip()
        # Strip the mention.
        if self._bot_user_id:
            text = re.sub(rf"<@{self._bot_user_id}>\s*", "", text).strip()

        if not text:
            return

        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        msg_ts = event.get("ts", "")
        self._dispatch_to_runtime(channel, thread_ts, msg_ts, text, user_id)

    def _handle_files(
        self,
        channel: str,
        thread_ts: str | None,
        msg_ts: str,
        caption: str,
        files: list[dict],
    ) -> None:
        """Download files from Slack and dispatch them to the agent."""
        if not self._client or not self._loop:
            return

        uploads_dir = os.path.join(self.runtime.working_dir, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)

        downloaded: list[str] = []
        for file_info in files:
            url = file_info.get("url_private_download") or file_info.get("url_private")
            if not url:
                continue

            size = file_info.get("size", 0)
            if size > MAX_FILE_SIZE:
                size_mb = size / (1024 * 1024)
                log.warning("File too large (%.1fMB), skipping: %s", size_mb, file_info.get("name"))
                if self._client:
                    self._client.chat_postMessage(
                        channel=channel,
                        text=f"File too large ({size_mb:.1f}MB). Max is 20MB.",
                        thread_ts=thread_ts,
                    )
                continue

            raw_name = file_info.get("name") or f"file_{uuid.uuid4().hex[:8]}"
            filename = _safe_filename(raw_name)
            dest_path = os.path.join(uploads_dir, filename)

            try:
                req = urllib.request.Request(
                    url, headers={"Authorization": f"Bearer {self.bot_token}"}
                )
                with urllib.request.urlopen(req) as resp:
                    with open(dest_path, "wb") as f:
                        f.write(resp.read())
                log.info("Downloaded %s to %s (%d bytes)", filename, dest_path, size)
                downloaded.append(dest_path)
            except Exception:
                log.exception("Failed to download file %s", filename)
                if self._client:
                    self._client.chat_postMessage(
                        channel=channel,
                        text=f"Failed to download `{filename}`. Please try again.",
                        thread_ts=thread_ts,
                    )

        if not downloaded:
            return

        file_list = "\n".join(f"- {p}" for p in downloaded)
        desc = "file" if len(downloaded) == 1 else f"{len(downloaded)} files"
        prompt = f"User uploaded {desc}:\n{file_list}"
        if caption:
            prompt += f"\n\n{caption}"

        chat_id = _chat_id(channel, thread_ts)
        asyncio.run_coroutine_threadsafe(
            self._dispatch(channel, thread_ts, msg_ts, chat_id, prompt), self._loop
        )

    def _dispatch_to_runtime(
        self,
        channel: str,
        thread_ts: str | None,
        msg_ts: str,
        text: str,
        user_id: str,
    ) -> None:
        """Parse commands or dispatch a prompt to the runtime."""
        if not self._loop:
            return

        # Check for !command syntax.
        cmd_match = _CMD_RE.match(text)
        if cmd_match:
            cmd_name = cmd_match.group(1).lower()
            cmd_args = (cmd_match.group(2) or "").strip()
            handler = getattr(self, f"_cmd_{cmd_name}", None)
            if handler:
                asyncio.run_coroutine_threadsafe(
                    handler(channel, thread_ts, cmd_args), self._loop
                )
                return

        # Regular message — dispatch to agent.
        chat_id = _chat_id(channel, thread_ts)
        asyncio.run_coroutine_threadsafe(
            self._dispatch(channel, thread_ts, msg_ts, chat_id, text), self._loop
        )

    def _track_thread(self, thread_ts: str | None) -> None:
        """Record that the bot participated in a thread (bounded LRU)."""
        if not thread_ts:
            return
        # Move to end (most recent) if already present.
        self._participated_threads.pop(thread_ts, None)
        self._participated_threads[thread_ts] = None
        while len(self._participated_threads) > _MAX_PARTICIPATED_THREADS:
            self._participated_threads.popitem(last=False)

    def _add_reaction(self, channel: str, ts: str, name: str = "eyes") -> None:
        """Add an emoji reaction to a message (best-effort)."""
        if self._client:
            with contextlib.suppress(Exception):
                self._client.reactions_add(channel=channel, timestamp=ts, name=name)

    def _remove_reaction(self, channel: str, ts: str, name: str = "eyes") -> None:
        """Remove an emoji reaction from a message (best-effort)."""
        if self._client:
            with contextlib.suppress(Exception):
                self._client.reactions_remove(channel=channel, timestamp=ts, name=name)

    async def _dispatch(
        self,
        channel: str,
        thread_ts: str | None,
        msg_ts: str,
        chat_id: ChatId,
        prompt: str,
    ) -> None:
        """Send a prompt to the active provider, guarding against concurrent requests."""
        rt = self.runtime
        provider = rt.get_active_provider(chat_id)
        lock = rt.get_chat_lock(chat_id)

        if lock.locked():
            log.info("Waiting for lock chat_id=%s provider=%s", chat_id, provider)

        # Ack reaction — immediate visual feedback that the message was received.
        if msg_ts:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._add_reaction(channel, msg_ts),
            )

        ctx = SlackContext(self._client, channel, thread_ts)
        async with lock:
            task = asyncio.create_task(
                rt.process_request(provider, prompt, chat_id, ctx)
            )
            rt.running_task_by_chat[chat_id] = task
            try:
                await task
                # Track thread participation on success so future messages
                # in this thread don't require an @mention.
                self._track_thread(thread_ts)
            except asyncio.CancelledError:
                log.info("Task cancelled by user for chat_id=%s", chat_id)
            finally:
                if rt.running_task_by_chat.get(chat_id) is task:
                    rt.running_task_by_chat.pop(chat_id, None)
                # Remove the ack reaction now that we've replied.
                if msg_ts:
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda: self._remove_reaction(channel, msg_ts),
                    )

    # -- Commands --

    async def _cmd_stop(self, channel: str, thread_ts: str | None, args: str) -> None:
        chat_id = _chat_id(channel, thread_ts)
        had, error = await self.runtime.stop_chat(chat_id)
        msg = "Process stopped." if had and not error else (
            f"Error stopping: {error}" if error else "No process running."
        )
        if self._client:
            self._client.chat_postMessage(
                channel=channel, text=msg, thread_ts=thread_ts,
            )

    async def _cmd_use(self, channel: str, thread_ts: str | None, args: str) -> None:
        chat_id = _chat_id(channel, thread_ts)
        rt = self.runtime
        if args and args in PROVIDER_NAMES:
            rt.active_provider_by_chat[chat_id] = args
            rt.save_state()
            msg = f"Provider set to {args}."
        else:
            active = rt.get_active_provider(chat_id)
            providers = [f"{'* ' if p == active else ''}{p}" for p in PROVIDER_NAMES]
            msg = "Switch provider with `!use <name>`:\n" + "\n".join(providers)
        if self._client:
            self._client.chat_postMessage(
                channel=channel, text=msg, thread_ts=thread_ts,
            )

    async def _cmd_model(self, channel: str, thread_ts: str | None, args: str) -> None:
        chat_id = _chat_id(channel, thread_ts)
        rt = self.runtime
        provider = rt.get_active_provider(chat_id)
        models = rt.models.get(provider, [])

        if args:
            if args.isdigit():
                idx = int(args) - 1
                if not (0 <= idx < len(models)):
                    msg = f"Invalid index. Use 1-{len(models)}."
                else:
                    selected = models[idx]
                    rt.active_model_by_chat_provider[(chat_id, provider)] = selected
                    rt.save_state()
                    msg = f"{provider} model -> {selected}"
            elif args in models:
                rt.active_model_by_chat_provider[(chat_id, provider)] = args
                rt.save_state()
                msg = f"{provider} model -> {args}"
            else:
                msg = f"Unknown model '{args}'."
        else:
            if not models:
                msg = f"No models configured for {provider}."
            else:
                active = rt.get_active_model(chat_id, provider)
                items = [f"{'* ' if m == active else ''}{m}" for m in models]
                msg = (
                    f"Switch model ({provider}) with `!model <name>`:\n"
                    + "\n".join(items)
                )
        if self._client:
            self._client.chat_postMessage(
                channel=channel, text=msg, thread_ts=thread_ts,
            )

    async def _cmd_status(self, channel: str, thread_ts: str | None, args: str) -> None:
        chat_id = _chat_id(channel, thread_ts)
        rt = self.runtime
        provider = rt.get_active_provider(chat_id)
        model = rt.get_active_model(chat_id, provider)
        msg = f"Provider: {provider}\nModel: {model}"
        if self._client:
            self._client.chat_postMessage(
                channel=channel, text=msg, thread_ts=thread_ts,
            )

    async def _cmd_clear(self, channel: str, thread_ts: str | None, args: str) -> None:
        chat_id = _chat_id(channel, thread_ts)
        rt = self.runtime
        clear_all = args == "all"
        parts = []
        for prov_name in PROVIDER_NAMES:
            if clear_all or rt.get_active_provider(chat_id) == prov_name:
                sid = rt.session_by_chat_provider.pop((chat_id, prov_name), None)
                provider = rt.make_provider(prov_name)
                summary = provider.clear_session(sid, rt.working_dir)
                parts.append(f"{prov_name.capitalize()}: {summary}")
        rt.save_state()
        label = "all providers" if clear_all else "current provider"
        msg = f"Cleared {label}.\n" + "\n".join(parts)
        if self._client:
            self._client.chat_postMessage(
                channel=channel, text=msg, thread_ts=thread_ts,
            )

    async def _cmd_help(self, channel: str, thread_ts: str | None, args: str) -> None:
        lines = [f"`!{cmd}` — {desc}" for cmd, desc in COMMANDS.items()]
        msg = "\n".join(lines)
        if self._client:
            self._client.chat_postMessage(
                channel=channel, text=msg, thread_ts=thread_ts,
            )


def _safe_filename(name: str) -> str:
    """Sanitise a filename from Slack to prevent path traversal."""
    return os.path.basename(name).lstrip(".")
