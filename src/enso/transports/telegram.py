"""Telegram transport — your phone talks to your agents here."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import uuid
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

log = logging.getLogger(__name__)


def _is_parse_error(exc: BadRequest) -> bool:
    """Return True when Telegram rejected HTML formatting rather than delivery."""
    return "parse entities" in str(exc).lower()


# Commands registered with Telegram's menu UI.
COMMANDS = [
    BotCommand("stop", "Kill running process"),
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

    def __init__(self, update: Update):
        self._update = update

    async def reply(self, text: str) -> None:
        try:
            await self._update.message.reply_text(
                md_to_html(text), parse_mode=ParseMode.HTML,
            )
        except BadRequest as exc:
            if not _is_parse_error(exc):
                raise
            # Fallback to plain text if HTML parsing fails
            await self._update.message.reply_text(text)

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
        """Send a prompt to the active provider, guarding against concurrent requests."""
        rt = self.runtime
        provider = rt.get_active_provider(chat_id)
        lock = rt.get_chat_lock(chat_id)

        if lock.locked():
            log.info("Rejected (lock held) chat_id=%s provider=%s", chat_id, provider)
            await update.message.reply_text(
                "A request is already running. Use /stop to cancel it."
            )
            return

        ctx = TelegramContext(update)
        async with lock:
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

    # -- Slash commands --

    async def _cmd_stop(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        had, error = await self.runtime.stop_chat(update.effective_chat.id)
        if not had:
            await update.message.reply_text("No process running.")
        elif error:
            await update.message.reply_text(f"Error stopping: {error}")
        else:
            await update.message.reply_text("Process stopped.")

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
