"""Telegram transport — your phone talks to your agents here."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import uuid
from typing import TYPE_CHECKING, Any

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from ..config import CONFIG_DIR
from ..providers import PROVIDER_NAMES
from . import BaseTransport, TransportContext

if TYPE_CHECKING:
    from ..core import Runtime

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB Telegram bot API limit

log = logging.getLogger(__name__)


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
        await self._update.message.reply_text(text)

    async def reply_status(self, text: str) -> Any:
        return await self._update.message.reply_text(text)

    async def edit_status(self, handle: Any, text: str) -> None:
        await handle.edit_text(text)

    async def delete_status(self, handle: Any) -> None:
        with contextlib.suppress(Exception):
            await handle.delete()


class TelegramTransport(BaseTransport):
    """Telegram bot transport."""

    name = "telegram"

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        tg_cfg = runtime.config.get("transports", {}).get("telegram", {})
        self.bot_token: str = tg_cfg.get("bot_token", "")
        self.allowed_user_ids: set[int] = set(tg_cfg.get("allowed_user_ids", []))
        self._bot: Any = None

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
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        app.add_handler(
            MessageHandler(
                (
                    filters.Document.ALL
                    | filters.PHOTO
                    | filters.AUDIO
                    | filters.VOICE
                    | filters.VIDEO
                    | filters.VIDEO_NOTE
                )
                & ~filters.COMMAND,
                self._handle_file_message,
            )
        )
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    async def _post_init(self, app: Application) -> None:
        """Called after the Application is initialized — start background tasks."""
        self._bot = app.bot
        self._scheduler_task = asyncio.create_task(
            self.runtime.run_job_scheduler()
        )

    async def notify(self, text: str) -> None:
        """Send a one-way notification to all allowed users."""
        if not self._bot:
            log.warning("Cannot notify — bot not initialized yet")
            return
        for user_id in self.allowed_user_ids:
            try:
                await self._bot.send_message(chat_id=user_id, text=text[:4096])
            except Exception:
                log.exception("Failed to notify user %s", user_id)

    # -- Message handling --

    async def _handle_message(self, update: Update, _context: Any) -> None:
        """Handle incoming text messages."""
        user = update.effective_user
        if user is None or update.message is None:
            return
        if self.allowed_user_ids and user.id not in self.allowed_user_ids:
            log.warning("Unauthorized user: %s", user.id)
            return

        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()

        # Command dispatch
        if text.startswith("!"):
            await self._handle_command(update, text, chat_id)
            return

        await self._dispatch(update, chat_id, text)

    async def _handle_file_message(self, update: Update, _context: Any) -> None:
        """Handle incoming file uploads — download and pass to agent as a prompt."""
        user = update.effective_user
        if user is None or update.message is None:
            return
        if self.allowed_user_ids and user.id not in self.allowed_user_ids:
            log.warning("Unauthorized user: %s", user.id)
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
                "A request is already running. Use !stop to cancel it."
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

    # -- Commands --

    async def _handle_command(
        self, update: Update, text: str, chat_id: int
    ) -> None:
        """Route ! commands to their handlers."""
        rt = self.runtime

        if text == "!stop":
            had, error = await rt.stop_chat(chat_id)
            if not had:
                await update.message.reply_text("No process running.")
            elif error:
                await update.message.reply_text(f"Error stopping: {error}")
            else:
                await update.message.reply_text("Process stopped.")
            return

        # Provider shortcuts: !claude, !codex, !gemini
        provider_shortcuts = {f"!{n}" for n in PROVIDER_NAMES}
        if text.startswith("!use") or text in provider_shortcuts:
            name = text.split()[-1].lstrip("!")
            if name not in PROVIDER_NAMES:
                await update.message.reply_text(
                    f"Usage: !use {'|'.join(PROVIDER_NAMES)}"
                )
                return
            rt.active_provider_by_chat[chat_id] = name
            rt.save_state()
            await update.message.reply_text(f"Provider set to {name}.")
            return

        if text == "!status":
            provider = rt.get_active_provider(chat_id)
            model = rt.get_active_model(chat_id, provider)
            await update.message.reply_text(f"Provider: {provider}\nModel: {model}")
            return

        if text in ("!clear", "!clear all"):
            await self._handle_clear(update, text, chat_id)
            return

        if text == "!models":
            await self._handle_models(update, chat_id)
            return

        if text.startswith("!model"):
            await self._handle_model(update, text, chat_id)
            return

        if text == "!help":
            await update.message.reply_text(
                "!status — active provider & model\n"
                "!use claude|codex|gemini — switch provider\n"
                "!models — list models\n"
                "!model <index|name> — switch model\n"
                "!stop — kill running process\n"
                "!clear [all] — clear session(s)\n"
                "!restart — restart the bot\n"
                "!logs — last 25 log entries"
            )
            return

        if text == "!restart":
            await update.message.reply_text("Restarting...")
            asyncio.get_event_loop().call_later(1, _restart)
            return

        if text == "!logs":
            await self._handle_logs(update)
            return

        await update.message.reply_text("Unknown command. Try !help")

    async def _handle_models(self, update: Update, chat_id: int) -> None:
        rt = self.runtime
        provider = rt.get_active_provider(chat_id)
        models = rt.models.get(provider, [])
        if not models:
            await update.message.reply_text(f"No models configured for {provider}.")
            return
        active = rt.get_active_model(chat_id, provider)
        lines = [f"Models for {provider}:"]
        for i, m in enumerate(models, 1):
            marker = " (active)" if m == active else ""
            lines.append(f"  {i}. {m}{marker}")
        await update.message.reply_text("\n".join(lines))

    async def _handle_model(self, update: Update, text: str, chat_id: int) -> None:
        rt = self.runtime
        provider = rt.get_active_provider(chat_id)
        models = rt.models.get(provider, [])
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            await update.message.reply_text(
                "Usage: !model <index|name>\nUse !models to see options."
            )
            return

        choice = parts[1].strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if not (0 <= idx < len(models)):
                await update.message.reply_text(
                    f"Invalid index. Use 1-{len(models)}."
                )
                return
            selected = models[idx]
        elif choice in models:
            selected = choice
        else:
            await update.message.reply_text(f"Unknown model '{choice}'.")
            return

        rt.active_model_by_chat_provider[(chat_id, provider)] = selected
        rt.save_state()
        await update.message.reply_text(f"{provider} model set to {selected}.")

    async def _handle_clear(self, update: Update, text: str, chat_id: int) -> None:
        rt = self.runtime
        clear_all = text.strip() == "!clear all"
        parts = []
        for prov_name in PROVIDER_NAMES:
            if clear_all or rt.get_active_provider(chat_id) == prov_name:
                sid = rt.session_by_chat_provider.pop((chat_id, prov_name), None)
                provider = rt.make_provider(prov_name)
                summary = provider.clear_session(sid, rt.working_dir)
                parts.append(f"{prov_name.capitalize()}: {summary}")
        rt.save_state()
        label = "all providers" if clear_all else "current provider"
        await update.message.reply_text(f"Cleared {label}!\n" + "\n".join(parts))

    async def _handle_logs(self, update: Update) -> None:
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
