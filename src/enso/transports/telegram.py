"""Telegram transport — your phone talks to your agents here."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import uuid
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from ..config import CONFIG_DIR
from ..formatting import md_to_html
from ..providers import PROVIDER_NAMES
from . import BaseTransport, TransportContext

if TYPE_CHECKING:
    from ..core import Runtime

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB Telegram bot API limit
MAX_QUEUE_SIZE = 5

log = logging.getLogger(__name__)


@dataclass
class _QueuedMessage:
    """A message waiting to be dispatched while another request is running."""

    update: Update
    prompt: str  # with reply context already prepended
    is_reply: bool
    preview: str  # short preview of user's original text for display


def _is_parse_error(exc: BadRequest) -> bool:
    """Return True when Telegram rejected HTML formatting rather than delivery."""
    return "parse entities" in str(exc).lower()


# Commands registered with Telegram's menu UI.
COMMANDS = [
    BotCommand("stop", "Stop process & clear queue"),
    BotCommand("queue", "View & manage queued messages"),
    BotCommand("use", "Switch provider"),
    BotCommand("model", "Switch model"),
    BotCommand("status", "Provider & model info"),
    BotCommand("clear", "Clear session"),
    BotCommand("restart", "Restart the bot"),
    BotCommand("logs", "Last 25 log entries"),
    BotCommand("help", "Show commands"),
]


def _restart() -> None:
    """Restart the enso service (platform-aware) or re-exec the process."""
    if sys.platform == "darwin":
        plist = os.path.expanduser("~/Library/LaunchAgents/com.enso.agent.plist")
        if os.path.exists(plist):
            uid = str(os.getuid())
            os.execvp(
                "launchctl",
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.enso.agent"],
            )
    elif sys.platform == "linux":
        os.execvp("systemctl", ["systemctl", "--user", "restart", "enso.service"])
    os.execvp(sys.executable, [sys.executable, "-m", "enso.cli", "serve"])


def _safe_filename(name: str) -> str:
    """Sanitise a filename from Telegram to prevent path traversal."""
    return os.path.basename(name).lstrip(".")


class TelegramContext(TransportContext):
    """Sends replies back to a Telegram chat."""

    def __init__(self, update: Update, *, is_reply: bool = False):
        self._update = update
        self._is_reply = is_reply

    async def reply(self, text: str) -> None:
        # When the user sent a reply-message, visually thread the bot's
        # response back to that message so the link is clear in chat.
        kwargs: dict[str, Any] = {"parse_mode": ParseMode.HTML}
        if self._is_reply:
            kwargs["do_quote"] = True
        try:
            await self._update.message.reply_text(md_to_html(text), **kwargs)
        except BadRequest as exc:
            if not _is_parse_error(exc):
                raise
            # Fallback to plain text if HTML parsing fails
            plain_kwargs: dict[str, Any] = {}
            if self._is_reply:
                plain_kwargs["do_quote"] = True
            await self._update.message.reply_text(text, **plain_kwargs)

    async def reply_status(self, text: str) -> Any:
        return await self._update.message.reply_text(text)

    async def edit_status(self, handle: Any, text: str) -> None:
        await handle.edit_text(text)

    async def delete_status(self, handle: Any) -> None:
        with contextlib.suppress(Exception):
            await handle.delete()

    async def send_typing(self) -> None:
        await self._update.effective_chat.send_action(ChatAction.TYPING)


class TelegramTransport(BaseTransport):
    """Telegram bot transport."""

    name = "telegram"

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        tg_cfg = runtime.config.get("transports", {}).get("telegram", {})
        self.bot_token: str = tg_cfg.get("bot_token", "")
        self.allowed_user_ids: set[int] = set(tg_cfg.get("allowed_user_ids", []))
        self._bot: Any = None
        self._queue_by_chat: dict[int, deque[_QueuedMessage]] = {}

    def _is_authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        # Allow callback queries (inline keyboard taps) — they have no .message
        if update.message is None and update.callback_query is None:
            return False
        if self.allowed_user_ids and user.id not in self.allowed_user_ids:
            log.warning("Unauthorized user: %s", user.id)
            return False
        return True

    def start(self) -> None:
        """Start polling for Telegram messages (blocking)."""
        if not self.allowed_user_ids:
            log.warning(
                "allowed_user_ids is empty — anyone can message this bot! "
                "Run 'enso setup' or edit ~/.enso/config.json to restrict access."
            )
        log.info("Starting Telegram transport")
        app = (
            Application.builder()
            .token(self.bot_token)
            .post_init(self._post_init)
            .concurrent_updates(True)
            .build()
        )

        # Slash commands
        for cmd in COMMANDS:
            handler = getattr(self, f"_cmd_{cmd.command}", None)
            if handler:
                app.add_handler(CommandHandler(cmd.command, handler))

        # Inline keyboard callbacks
        app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Plain text → agent prompt
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        # File uploads
        app.add_handler(
            MessageHandler(
                filters.Document.ALL | filters.PHOTO | filters.AUDIO
                | filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE,
                self._handle_file_message,
            )
        )
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    async def _post_init(self, app: Application) -> None:
        """Register commands with Telegram and start background tasks."""
        self._bot = app.bot
        await self._bot.set_my_commands(COMMANDS)
        self._scheduler_task = asyncio.create_task(
            self.runtime.run_job_scheduler()
        )

    async def notify(self, text: str) -> None:
        """Send a one-way notification to all allowed users."""
        if not self._bot:
            log.warning("Cannot notify — bot not initialized yet")
            return
        html = md_to_html(text[:4096])
        for user_id in self.allowed_user_ids:
            try:
                await self._bot.send_message(
                    chat_id=user_id, text=html, parse_mode=ParseMode.HTML,
                )
            except BadRequest as exc:
                if not _is_parse_error(exc):
                    log.exception("Failed to notify user %s", user_id)
                    continue
                try:
                    await self._bot.send_message(chat_id=user_id, text=text[:4096])
                except Exception:
                    log.exception("Failed to notify user %s", user_id)
            except Exception:
                log.exception("Failed to notify user %s", user_id)

    # -- Message handling --

    async def _handle_message(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        text = (update.message.text or "").strip()
        log.info(
            "Incoming message: chat_id=%s msg_id=%s is_reply=%s len=%d",
            update.effective_chat.id,
            update.message.message_id,
            update.message.reply_to_message is not None,
            len(text),
        )
        await self._dispatch(update, update.effective_chat.id, text)

    async def _handle_file_message(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return

        msg = update.message
        chat_id = update.effective_chat.id
        tg_file_obj, filename, desc = _resolve_file(msg)
        if tg_file_obj is None:
            return

        file_size = getattr(tg_file_obj, "file_size", None) or 0
        if file_size > MAX_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            await msg.reply_text(
                f"File too large ({size_mb:.1f}MB). "
                "Telegram bots can only download files up to 20MB."
            )
            return

        uploads_dir = os.path.join(self.runtime.working_dir, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        dest_path = os.path.join(uploads_dir, filename)

        try:
            tg_file = await tg_file_obj.get_file()
            await tg_file.download_to_drive(dest_path)
            log.info("Downloaded %s to %s (%d bytes)", desc, dest_path, file_size)
        except Exception:
            log.exception("Failed to download %s", desc)
            await msg.reply_text("Failed to download file. Please try again.")
            return

        caption = (msg.caption or "").strip()
        prompt = f"User uploaded a {desc}: {dest_path}"
        if caption:
            prompt += f"\n\n{caption}"
        await self._dispatch(update, chat_id, prompt)

    # -- Dispatch --

    async def _dispatch(self, update: Update, chat_id: int, prompt: str) -> None:
        """Send a prompt to the active provider, or queue it if one is already running."""
        rt = self.runtime
        lock = rt.get_chat_lock(chat_id)

        # Build reply context early — needed whether we run now or queue
        reply_context = _build_reply_context(update.message)
        is_reply = reply_context is not None
        preview = prompt[:50].replace("\n", " ")
        if reply_context:
            prompt = f"{reply_context}\n\n{prompt}"

        if lock.locked():
            queue = self._queue_by_chat.setdefault(chat_id, deque())
            if len(queue) >= MAX_QUEUE_SIZE:
                await update.message.reply_text(
                    f"Queue full ({MAX_QUEUE_SIZE}). Use /queue to manage."
                )
                return
            queue.append(_QueuedMessage(
                update=update, prompt=prompt,
                is_reply=is_reply, preview=preview,
            ))
            pos = len(queue)
            label = f"{preview}…" if len(preview) == 50 else preview
            await update.message.reply_text(f"Queued (#{pos}): {label}")
            log.info("Queued #%d for chat_id=%s: %s", pos, chat_id, preview)
            return

        provider = rt.get_active_provider(chat_id)
        log.info(
            "Dispatch: chat_id=%s provider=%s is_reply=%s prompt_len=%d",
            chat_id, provider, is_reply, len(prompt),
        )
        log.debug("Dispatch prompt:\n%s", prompt)

        ctx = TelegramContext(update, is_reply=is_reply)
        async with lock:
            await self._run_request(provider, prompt, chat_id, ctx)
            await self._drain_queue(chat_id)

    async def _run_request(
        self, provider: str, prompt: str, chat_id: int, ctx: TelegramContext,
    ) -> None:
        """Run a single provider request, tracking the task for cancellation."""
        rt = self.runtime
        task = asyncio.create_task(
            rt.process_request(provider, prompt, chat_id, ctx)
        )
        rt.running_task_by_chat[chat_id] = task
        try:
            await task
        except asyncio.CancelledError:
            log.info("Task cancelled by user for chat_id=%s", chat_id)
        finally:
            if rt.running_task_by_chat.get(chat_id) is task:
                rt.running_task_by_chat.pop(chat_id, None)

    async def _drain_queue(self, chat_id: int) -> None:
        """Process queued messages one by one until the queue is empty."""
        queue = self._queue_by_chat.get(chat_id)
        if not queue:
            return
        while queue:
            queued = queue.popleft()
            provider = self.runtime.get_active_provider(chat_id)
            ctx = TelegramContext(queued.update, is_reply=queued.is_reply)
            log.info(
                "Dequeuing for chat_id=%s (%d remaining): %s",
                chat_id, len(queue), queued.preview,
            )
            await self._run_request(provider, queued.prompt, chat_id, ctx)

    # -- Slash commands --

    async def _cmd_stop(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        chat_id = update.effective_chat.id
        queue = self._queue_by_chat.get(chat_id)
        queued_count = len(queue) if queue else 0
        if queue:
            queue.clear()
        had, error = await self.runtime.stop_chat(chat_id)
        if not had and not queued_count:
            await update.message.reply_text("Nothing running.")
        elif error:
            await update.message.reply_text(f"Error stopping: {error}")
        else:
            parts = []
            if had:
                parts.append("Stopped.")
            if queued_count:
                parts.append(f"Cleared {queued_count} queued message(s).")
            await update.message.reply_text(" ".join(parts))

    async def _cmd_queue(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        chat_id = update.effective_chat.id
        args = (update.message.text or "").split()[1:]

        # Direct: /queue clear
        if args == ["clear"]:
            queue = self._queue_by_chat.get(chat_id)
            count = len(queue) if queue else 0
            if queue:
                queue.clear()
            await update.message.reply_text(
                f"Cleared {count} queued message(s)." if count else "Queue is empty."
            )
            return

        await self._show_queue(update, chat_id)

    async def _show_queue(
        self, update_or_query: Any, chat_id: int,
    ) -> None:
        """Render the queue view (used by /queue command and callbacks)."""
        queue = self._queue_by_chat.get(chat_id)
        if not queue:
            text = "No messages queued."
            if hasattr(update_or_query, "edit_message_text"):
                await update_or_query.edit_message_text(text)
            else:
                await update_or_query.message.reply_text(text)
            return

        lines = [f"Queued messages ({len(queue)}):"]
        for i, item in enumerate(queue):
            label = f"{item.preview}…" if len(item.preview) == 50 else item.preview
            lines.append(f"{i + 1}. {label}")

        remove_buttons = [
            InlineKeyboardButton(
                f"✕ {i + 1}", callback_data=f"queue:rm:{i}",
            )
            for i in range(len(queue))
        ]
        keyboard = InlineKeyboardMarkup([
            remove_buttons,
            [InlineKeyboardButton("Clear all", callback_data="queue:clear")],
        ])

        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(
                "\n".join(lines), reply_markup=keyboard,
            )
        else:
            await update_or_query.message.reply_text(
                "\n".join(lines), reply_markup=keyboard,
            )

    async def _cmd_use(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        rt = self.runtime
        chat_id = update.effective_chat.id
        args = (update.message.text or "").split()[1:]

        # Direct usage: /use claude
        if args and args[0] in PROVIDER_NAMES:
            rt.active_provider_by_chat[chat_id] = args[0]
            rt.save_state()
            await update.message.reply_text(f"Provider set to {args[0]}.")
            return

        # No args → show inline keyboard
        active = rt.get_active_provider(chat_id)
        buttons = [
            InlineKeyboardButton(
                f"{'● ' if p == active else ''}{p}",
                callback_data=f"use:{p}",
            )
            for p in PROVIDER_NAMES
        ]
        await update.message.reply_text(
            "Switch provider:",
            reply_markup=InlineKeyboardMarkup([buttons]),
        )

    async def _cmd_status(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        rt = self.runtime
        chat_id = update.effective_chat.id
        provider = rt.get_active_provider(chat_id)
        model = rt.get_active_model(chat_id, provider)
        await update.message.reply_text(f"Provider: {provider}\nModel: {model}")

    async def _cmd_model(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        rt = self.runtime
        chat_id = update.effective_chat.id
        provider = rt.get_active_provider(chat_id)
        models = rt.models.get(provider, [])
        args = (update.message.text or "").split()[1:]

        # Direct usage: /model sonnet
        if args:
            choice = args[0]
            if choice.isdigit():
                idx = int(choice) - 1
                if not (0 <= idx < len(models)):
                    await update.message.reply_text(f"Invalid index. Use 1-{len(models)}.")
                    return
                selected = models[idx]
            elif choice in models:
                selected = choice
            else:
                await update.message.reply_text(f"Unknown model '{choice}'.")
                return
            rt.active_model_by_chat_provider[(chat_id, provider)] = selected
            rt.save_state()
            await update.message.reply_text(f"{provider} model → {selected}")
            return

        # No args → show inline keyboard
        if not models:
            await update.message.reply_text(f"No models configured for {provider}.")
            return
        active = rt.get_active_model(chat_id, provider)
        buttons = [
            InlineKeyboardButton(
                f"{'● ' if m == active else ''}{m}",
                callback_data=f"model:{m}",
            )
            for m in models
        ]
        # Stack vertically — one model per row for readability
        keyboard = [[b] for b in buttons]
        await update.message.reply_text(
            f"Switch model ({provider}):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_clear(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        rt = self.runtime
        chat_id = update.effective_chat.id
        args = (update.message.text or "").split()[1:]

        # Direct usage: /clear all
        if args == ["all"]:
            self._do_clear(chat_id, clear_all=True)
            await update.message.reply_text("Cleared all providers.")
            return

        # No args → show options
        active = rt.get_active_provider(chat_id)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"Clear {active}", callback_data="clear:current"),
                InlineKeyboardButton("Clear all", callback_data="clear:all"),
            ],
        ])
        await update.message.reply_text("Clear session:", reply_markup=keyboard)

    def _do_clear(self, chat_id: int, *, clear_all: bool = False) -> list[str]:
        """Execute session clear and return summary lines."""
        rt = self.runtime
        parts = []
        for prov_name in PROVIDER_NAMES:
            if clear_all or rt.get_active_provider(chat_id) == prov_name:
                sid = rt.session_by_chat_provider.pop((chat_id, prov_name), None)
                provider = rt.make_provider(prov_name)
                summary = provider.clear_session(sid, rt.working_dir)
                parts.append(f"{prov_name.capitalize()}: {summary}")
        rt.save_state()
        return parts

    async def _cmd_restart(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        await update.message.reply_text("Restarting...")
        asyncio.get_event_loop().call_later(1, _restart)

    async def _cmd_logs(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        log_path = os.path.join(CONFIG_DIR, "enso.log")
        if not os.path.exists(log_path):
            await update.message.reply_text("No log file found.")
            return
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 32768))
                tail = f.read().decode(errors="replace")
            lines = tail.splitlines()[-25:]
            text = "\n".join(lines) if lines else "(empty)"
            await update.message.reply_text(text[-4000:])
        except Exception as exc:
            await update.message.reply_text(f"Error reading logs: {exc}")

    async def _cmd_help(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        lines = [f"/{c.command} — {c.description}" for c in COMMANDS]
        await update.message.reply_text("\n".join(lines))

    # -- Inline keyboard callbacks --

    async def _handle_callback(self, update: Update, _ctx: Any) -> None:
        """Route inline keyboard button taps."""
        if not self._is_authorized(update):
            return
        query = update.callback_query
        await query.answer()  # Acknowledge the tap immediately

        data = query.data or ""
        chat_id = update.effective_chat.id
        rt = self.runtime

        if data.startswith("use:"):
            name = data.split(":", 1)[1]
            if name in PROVIDER_NAMES:
                rt.active_provider_by_chat[chat_id] = name
                rt.save_state()
                await query.edit_message_text(f"Provider → {name}")

        elif data.startswith("model:"):
            model = data.split(":", 1)[1]
            provider = rt.get_active_provider(chat_id)
            models = rt.models.get(provider, [])
            if model in models:
                rt.active_model_by_chat_provider[(chat_id, provider)] = model
                rt.save_state()
                await query.edit_message_text(f"{provider} model → {model}")

        elif data.startswith("clear:"):
            scope = data.split(":", 1)[1]
            clear_all = scope == "all"
            parts = self._do_clear(chat_id, clear_all=clear_all)
            label = "all providers" if clear_all else "current provider"
            await query.edit_message_text(f"Cleared {label}.\n" + "\n".join(parts))

        elif data.startswith("queue:"):
            action = data.split(":", 1)[1]
            queue = self._queue_by_chat.get(chat_id)
            if action == "clear":
                count = len(queue) if queue else 0
                if queue:
                    queue.clear()
                await query.edit_message_text(
                    f"Cleared {count} queued message(s)."
                    if count else "Queue already empty."
                )
            elif action.startswith("rm:"):
                idx = int(action.split(":")[1])
                if queue and 0 <= idx < len(queue):
                    del queue[idx]
                    log.info("Removed queue item %d for chat_id=%s", idx, chat_id)
                await self._show_queue(query, chat_id)


def _build_reply_context(msg: Any) -> str | None:
    """Build a reply-context prefix when the user replies to a specific message.

    Returns a bracketed context string to prepend to the prompt, or None if
    the message is not a reply.
    """
    reply = msg.reply_to_message
    if reply is None:
        return None

    # Prefer the user's partial quote selection (highlighted text) over
    # the full original message.
    quote = getattr(msg, "quote", None)
    if quote and getattr(quote, "text", None):
        quoted_text = quote.text
        quote_source = "partial_quote"
    elif getattr(reply, "text", None):
        quoted_text = reply.text
        quote_source = "full_text"
    elif getattr(reply, "caption", None):
        quoted_text = reply.caption
        quote_source = "caption"
    else:
        quoted_text = "(media or deleted message)"
        quote_source = "fallback"

    # Truncate very long quotes to keep the prompt manageable
    if len(quoted_text) > 500:
        quoted_text = quoted_text[:500] + "…"

    # In a 1:1 chat the replied-to message is either from the bot or the user
    from_user = getattr(reply, "from_user", None)
    sender = "assistant" if from_user and from_user.is_bot else "user"

    log.info(
        "Reply context: replying_to_msg_id=%s sender=%s source=%s quoted_len=%d",
        getattr(reply, "message_id", "?"),
        sender,
        quote_source,
        len(quoted_text),
    )
    log.debug("Reply quoted text: %s", quoted_text)

    return f"[Replying to {sender}: {quoted_text}]"


def _resolve_file(msg: Any) -> tuple[Any, str, str]:
    """Extract the file object, filename, and description from a Telegram message."""
    if msg.document:
        name = _safe_filename(msg.document.file_name or f"document_{uuid.uuid4().hex[:8]}")
        return msg.document, name, f"file ({name})"
    if msg.photo:
        name = f"photo_{uuid.uuid4().hex[:8]}.jpg"
        return msg.photo[-1], name, "photo"
    if msg.audio:
        name = _safe_filename(msg.audio.file_name or f"audio_{uuid.uuid4().hex[:8]}.mp3")
        return msg.audio, name, f"audio file ({name})"
    if msg.voice:
        name = f"voice_{uuid.uuid4().hex[:8]}.ogg"
        return msg.voice, name, "voice message"
    if msg.video:
        name = _safe_filename(msg.video.file_name or f"video_{uuid.uuid4().hex[:8]}.mp4")
        return msg.video, name, f"video ({name})"
    if msg.video_note:
        name = f"videonote_{uuid.uuid4().hex[:8]}.mp4"
        return msg.video_note, name, "video note"
    return None, "", ""
