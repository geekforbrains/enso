"""Telegram transport: long polling in, HTML replies and status edits out."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from telegram import (
        Bot,
        BotCommand,
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
        ReplyParameters,
        Update,
    )
    from telegram.constants import ChatAction, ChatType, ParseMode
    from telegram.error import BadRequest, Forbidden, RetryAfter
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        f"Telegram transport dependencies are missing ({exc.name}); install enso[telegram]"
    ) from exc

from .. import captures, commands, routing, workspaces
from ..capture_runtime import CaptureWriter
from ..config import Config, Paths, TelegramConfig
from ..formatting import md_to_html
from ..maintenance import paused
from . import Reply, Transport, Turn

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
FILE_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API ceiling for getFile
QUOTE_LIMIT = 500


@dataclass
class _UsePicker:
    token: str
    chat_id: int
    message_id: int
    workspace: str
    provider: str
    model: str
    config: Config
    original: routing.ResolvedAgent
    stage: str = "main"
    revision: int = 0
    options: tuple[str, ...] = ()
    history: list[tuple[str, str, str]] = field(default_factory=list)


def _button_label(value: str) -> str:
    return value if len(value) <= 60 else f"{value[:44]}…{value[-15:]}"


def _is_parse_error(exc: BadRequest) -> bool:
    """True when Telegram rejected the HTML markup rather than the delivery."""
    return "parse entities" in str(exc).lower()


def _chat(target: str) -> int | str:
    return int(target) if target.lstrip("-").isdigit() else target


def safe_filename(name: str) -> str:
    """Basename only, no leading dots, no characters outside ``[\\w.-]``."""
    return re.sub(r"[^\w.\-]+", "_", os.path.basename(name)).lstrip(".")


def resolve_file(message: Message) -> tuple[Any, str, str] | None:
    """(file object, local name, description) for the attachment a message carries."""
    short = uuid.uuid4().hex[:8]
    if message.document:
        name = safe_filename(message.document.file_name or "") or f"document_{short}"
        return message.document, name, "document"
    if message.photo:
        return message.photo[-1], f"photo_{short}.jpg", "photo"
    if message.audio:
        name = safe_filename(message.audio.file_name or "") or f"audio_{short}.mp3"
        return message.audio, name, "audio file"
    if message.voice:
        return message.voice, f"voice_{short}.ogg", "voice message"
    if message.video:
        name = safe_filename(message.video.file_name or "") or f"video_{short}.mp4"
        return message.video, name, "video"
    if message.video_note:
        return message.video_note, f"videonote_{short}.mp4", "video note"
    return None


def build_turn(
    message: Message,
    text: str,
    *,
    files: list[str] | None = None,
    context: str = "",
    workspace: str = "",
) -> Turn:
    """A private-chat ``Turn``: the chat id is the user's id and there is no thread."""
    user = message.from_user
    assert user is not None
    return Turn(
        transport="telegram",
        channel=str(message.chat.id),
        thread=None,
        message_id=str(message.message_id),
        user_id=str(user.id),
        user_name=user.full_name,
        text=text,
        files=files or [],
        is_dm=True,
        context=context,
        channel_name="dm",
        workspace=workspace,
    )


def reply_context(message: Message) -> str:
    """The quoted message when the user replied to one; empty otherwise."""
    replied = message.reply_to_message
    if replied is None:
        return ""
    quote = message.quote
    if quote is not None and quote.text:
        text = quote.text  # the user's highlighted selection beats the whole message
    else:
        text = replied.text or replied.caption or "(media or deleted message)"
    if len(text) > QUOTE_LIMIT:
        text = text[:QUOTE_LIMIT] + "…"
    sender = "assistant" if replied.from_user is not None and replied.from_user.is_bot else "user"
    return f"[Replying to {sender}: {text}]"


async def send_html(bot: Bot, chat_id: int | str, text: str, *, reply_to: int | None = None) -> int:
    """Send markdown as HTML, falling back to plain text when Telegram rejects the markup."""
    params = (
        ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    )
    try:
        message = await bot.send_message(
            chat_id, md_to_html(text), parse_mode=ParseMode.HTML, reply_parameters=params
        )
    except BadRequest as exc:
        if not _is_parse_error(exc):
            raise
        log.debug("html rejected, sending plain text: %s", exc)
        message = await bot.send_message(chat_id, text, reply_parameters=params)
    return message.message_id


class TelegramReply(Reply):
    """Replies for one Telegram turn; quotes the user's message when they replied to one."""

    limit = TELEGRAM_TEXT_LIMIT

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        reply_to: int | None = None,
        user_id: str = "",
        user_name: str = "",
    ):
        self._bot = bot
        self._chat_id = chat_id
        self._reply_to = reply_to
        self._user_id = user_id
        self._user_name = user_name

    async def send(self, text: str) -> str:
        return str(await send_html(self._bot, self._chat_id, text, reply_to=self._reply_to))

    def delivery_rejected(self, error: Exception) -> bool:
        return isinstance(error, (BadRequest, Forbidden, RetryAfter))

    async def send_file(self, path: str, caption: str = "") -> str:
        with open(path, "rb") as handle:
            message = await self._bot.send_document(self._chat_id, handle, caption=caption or None)
        return str(message.message_id)

    async def status_post(self, text: str) -> str:
        message = await self._bot.send_message(self._chat_id, text)
        return str(message.message_id)

    async def status_edit(self, message_id: str, text: str) -> None:
        await self._bot.edit_message_text(text, chat_id=self._chat_id, message_id=int(message_id))

    async def status_delete(self, message_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._bot.delete_message(self._chat_id, int(message_id))

    async def typing(self) -> None:
        await self._bot.send_chat_action(self._chat_id, ChatAction.TYPING)

    def origin_env(self) -> dict[str, str]:
        return {
            "ENSO_ORIGIN_TRANSPORT": "telegram",
            "ENSO_ORIGIN_USER_ID": self._user_id,
            "ENSO_ORIGIN_USER_NAME": self._user_name,
            "ENSO_ORIGIN_CHANNEL": str(self._chat_id),
            "ENSO_ORIGIN_CHANNEL_NAME": "dm",
            "ENSO_ORIGIN_THREAD_TS": "",
        }


class TelegramTransport(Transport):
    """Telegram bot over long polling; bindings admit private human conversations."""

    name = "telegram"

    def __init__(self, config: TelegramConfig, paths: Paths):
        self.config = config
        self.paths = paths
        self.runtime: Runtime | None = None
        self.bot_user_id = ""
        self.bot_name = "Enso"
        self._bot: Bot | None = None
        self._use_pickers: dict[int, _UsePicker] = {}

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            raise RuntimeError("telegram transport is not started")
        return self._bot

    # -- Transport API --

    async def start(self, runtime: Runtime) -> None:
        self.runtime = runtime
        app = Application.builder().token(self.config.bot_token).concurrent_updates(True).build()
        app.add_handler(MessageHandler(filters.UpdateType.MESSAGE, self._on_update))
        app.add_handler(CallbackQueryHandler(self._on_callback, pattern=r"^u:"))
        async with app:
            self._bot = app.bot
            me = await app.bot.get_me()
            self.bot_user_id, self.bot_name = str(me.id), me.full_name
            await app.bot.set_my_commands([BotCommand(n, d) for n, d in commands.COMMANDS])
            log.info("telegram connected as @%s (%s)", me.username, me.id)
            await app.start()
            assert app.updater is not None
            await app.updater.start_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
            runtime.transport_ready(self.name)
            try:
                await asyncio.Event().wait()
            finally:
                # The polling stop makes one last API call; even if that is cut
                # short, the application must be stopped or ``async with`` cannot
                # shut it down.
                try:
                    await app.updater.stop()
                finally:
                    await app.stop()

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[None]:
        async with Bot(self.config.bot_token) as bot:
            self._bot = bot
            try:
                yield
            finally:
                self._bot = None

    async def send(self, target: str, text: str, *, thread: str | None = None) -> str:
        return str(await send_html(self.bot, _chat(target), text))

    async def send_file(
        self, target: str, path: str, *, caption: str = "", thread: str | None = None
    ) -> str:
        with open(path, "rb") as handle:
            message = await self.bot.send_document(_chat(target), handle, caption=caption or None)
        return str(message.message_id)

    async def receipt(
        self, target: str, message_id: str | None, thread: str | None, *, file: bool
    ) -> dict:
        return {"ok": True, "transport": self.name, "chat_id": target, "message_id": message_id}

    async def edit(self, target: str, message_id: str, text: str) -> None:
        chat_id, mid = _chat(target), int(message_id)
        try:
            await self.bot.edit_message_text(
                md_to_html(text), chat_id=chat_id, message_id=mid, parse_mode=ParseMode.HTML
            )
        except BadRequest as exc:
            if not _is_parse_error(exc):
                raise
            log.debug("html rejected, editing to plain text: %s", exc)
            await self.bot.edit_message_text(text, chat_id=chat_id, message_id=mid)

    async def delete(self, target: str, message_id: str) -> None:
        await self.bot.delete_message(_chat(target), int(message_id))

    async def fetch_thread(self, target: str, thread: str) -> list[dict]:
        return []  # the Bot API has no history endpoint

    # -- Inbound --

    async def _on_update(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        try:
            await self.handle_message(update.message)
        except Exception:
            log.exception("telegram message handling failed")

    async def _on_callback(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.callback_query is None:
            return
        try:
            await self.handle_callback(update.callback_query)
        except Exception:
            log.exception("telegram picker handling failed")

    def _picker_view(self, picker: _UsePicker) -> tuple[str, InlineKeyboardMarkup]:
        config = picker.config
        if picker.stage == "main":
            prompt = (
                f"Current: {picker.provider}:{picker.model}:{picker.original.effort}"
                "\nChoose what to change."
            )
            options: tuple[str, ...] = ("Provider", "Model", "Effort", "Default")
        elif picker.stage == "provider":
            prompt = "Choose a configured provider."
            options = tuple(config.providers)
        elif picker.stage == "model":
            prompt = f"Choose a model for {picker.provider}."
            provider = config.providers.get(picker.provider)
            options = provider.models if provider is not None else ()
        else:
            prompt = f"Choose effort for {picker.provider}:{picker.model}."
            options = tuple(routing.exact_efforts(picker.provider, picker.model))
        picker.revision += 1
        picker.options = options
        prefix = f"u:{picker.token}:{picker.revision}:"
        rows = [
            [InlineKeyboardButton(_button_label(option), callback_data=f"{prefix}{index}")]
            for index, option in enumerate(options)
        ]
        if picker.history:
            rows.append([InlineKeyboardButton("Back", callback_data=f"{prefix}b")])
        rows.append([InlineKeyboardButton("Cancel", callback_data=f"{prefix}c")])
        return prompt, InlineKeyboardMarkup(rows)

    async def _open_use_picker(self, chat_id: int, workspace: str) -> None:
        assert self.runtime is not None
        conversation = routing.conversation_key("telegram", str(chat_id), None)
        agent = self.runtime.current_agent(conversation, workspace)
        picker = _UsePicker(
            uuid.uuid4().hex[:10],
            chat_id,
            0,
            workspace,
            agent.provider,
            agent.model,
            self.runtime.config,
            agent,
        )
        text, markup = self._picker_view(picker)
        self._use_pickers[chat_id] = picker
        sent = await self.bot.send_message(chat_id, text, reply_markup=markup)
        picker.message_id = sent.message_id

    async def handle_callback(self, query: CallbackQuery) -> None:
        assert self.runtime is not None
        message = query.message
        user = query.from_user
        if (
            message is None
            or user is None
            or user.is_bot
            or message.chat.type != ChatType.PRIVATE
            or message.chat.id != user.id
        ):
            await query.answer()
            return
        chat_id = message.chat.id
        picker = self._use_pickers.get(chat_id)
        parts = (query.data or "").split(":")
        if (
            picker is None
            or len(parts) != 4
            or parts[1] != picker.token
            or parts[2] != str(picker.revision)
            or message.message_id != picker.message_id
        ):
            await query.answer("This picker expired. Send /use again.", show_alert=True)
            return
        config = self.runtime.config
        bound = routing.workspace_for(
            config, routing.binding_key("telegram", str(chat_id), user_id=str(user.id))
        )
        if bound != picker.workspace:
            self._use_pickers.pop(chat_id, None)
            await query.answer("This chat changed workspace. Send /use again.", show_alert=True)
            return
        if config is not picker.config:
            self._use_pickers.pop(chat_id, None)
            await query.answer("Configuration changed. Send /use again.", show_alert=True)
            return
        choice = parts[3]
        if choice == "c":
            self._use_pickers.pop(chat_id, None)
            await query.answer()
            await self.bot.edit_message_text(
                "Selection cancelled.", chat_id=chat_id, message_id=picker.message_id
            )
            return
        if choice == "b" and picker.history:
            picker.stage, picker.provider, picker.model = picker.history.pop()
        elif choice.isdecimal() and int(choice) < len(picker.options):
            selected = picker.options[int(choice)]
            if picker.stage == "main" and selected == "Default":
                await self._finish_picker(picker, query, "default")
                return
            if picker.stage == "effort":
                await self._finish_picker(
                    picker, query, f"{picker.provider}:{picker.model}:{selected}"
                )
                return
            picker.history.append((picker.stage, picker.provider, picker.model))
            if picker.stage == "main":
                picker.stage = selected.lower()
            elif picker.stage == "provider":
                picker.provider, picker.stage = selected, "model"
            else:
                picker.model, picker.stage = selected, "effort"
        else:
            await query.answer("This menu changed. Use the latest buttons.", show_alert=True)
            return
        text, markup = self._picker_view(picker)
        await query.answer()
        await self.bot.edit_message_text(
            text, chat_id=chat_id, message_id=picker.message_id, reply_markup=markup
        )

    async def _finish_picker(self, picker: _UsePicker, query: CallbackQuery, spec: str) -> None:
        assert self.runtime is not None
        self._use_pickers.pop(picker.chat_id, None)
        await query.answer()
        if paused(self.runtime.config.paths):
            text = "Enso is updating; wait for it to report ready before changing its state."
        else:
            conversation = routing.conversation_key("telegram", str(picker.chat_id), None)
            if self.runtime.current_agent(conversation, picker.workspace) != picker.original:
                text = "Selection changed. Send /use again."
            else:
                result = await commands.run(
                    self.runtime,
                    commands.Command("use", spec),
                    conversation=conversation,
                    binding=routing.binding_key(
                        "telegram", str(picker.chat_id), user_id=str(query.from_user.id)
                    ),
                    workspace=picker.workspace,
                    transport="telegram",
                )
                text = result.text
        await self.bot.edit_message_text(text, chat_id=picker.chat_id, message_id=picker.message_id)

    async def handle_message(self, message: Message) -> None:
        assert self.runtime is not None
        user = message.from_user
        if user is None or user.is_bot:
            return
        if message.chat.type != ChatType.PRIVATE or message.chat.id != user.id:
            return
        chat_id = str(message.chat.id)
        conversation = routing.conversation_key("telegram", chat_id, None)
        reply = TelegramReply(
            self.bot,
            message.chat.id,
            reply_to=message.message_id if message.reply_to_message is not None else None,
            user_id=str(user.id),
            user_name=user.full_name,
        )
        reply.sender_id, reply.sender_name = self.bot_user_id, self.bot_name
        workspace = routing.workspace_for(
            self.runtime.config, routing.binding_key("telegram", chat_id, user_id=str(user.id))
        )
        if workspace is None:
            await reply.send(routing.UNBOUND_NOTICE)
            return

        text = (message.text or message.caption or "").strip()
        parsed = commands.parse(text, "telegram")
        if (
            parsed is not None
            and parsed.name == "use"
            and not parsed.args
            and not paused(self.runtime.config.paths)
        ):
            await self._open_use_picker(message.chat.id, workspace)
            return
        if parsed is not None and await commands.dispatch(
            self.runtime, build_turn(message, text, workspace=workspace), reply
        ):
            return

        resolved = resolve_file(message)
        attachments: tuple[captures.Attachment, ...] = ()
        if resolved is not None:
            file_obj = resolved[0]
            attachments = (
                captures.Attachment(
                    file_obj.file_unique_id,
                    getattr(file_obj, "file_name", None) or "",
                    getattr(file_obj, "mime_type", None),
                    getattr(file_obj, "file_size", None),
                ),
            )
        capture = CaptureWriter.start(
            self.paths,
            captures.Message(
                transport="telegram",
                workspace=workspace,
                conversation=conversation,
                channel=chat_id,
                thread=None,
                message_id=str(message.message_id),
                sender_id=str(user.id),
                sender_name=user.full_name,
                occurred_at=message.date.isoformat(),
                text=message.text or message.caption or "",
                attachments=attachments,
            ),
        )
        await self.runtime.defer(
            conversation,
            reply,
            text,
            lambda: self._prepare_message(message, reply, workspace, capture),
            capture=capture,
        )

    async def _prepare_message(
        self, message: Message, reply: Reply, workspace: str, capture: CaptureWriter
    ) -> tuple[Turn, Reply] | None:
        """Resolve files and context for one message after its FIFO reservation."""
        assert self.runtime is not None
        assert message.from_user is not None
        key = routing.binding_key(
            "telegram", str(message.chat.id), user_id=str(message.from_user.id)
        )
        if routing.workspace_for(self.runtime.config, key, workspace=workspace) is None:
            await reply.send(routing.UNBOUND_NOTICE)
            return None
        files = None
        try:
            files = await self.download(message, workspace, reply)
        finally:
            if capture.message and capture.message.attachments:
                reference = capture.message.attachments[0]
                if files:
                    reference = replace(
                        reference,
                        status="downloaded",
                        path=Path(files[0]).relative_to(self.paths.workspace(workspace)).as_posix(),
                    )
                else:
                    reference = replace(reference, status="failed")
                await capture.attachments((reference,))
        if files is None:
            return None
        text = (message.text or message.caption or "").strip()
        if not text and not files:
            return None
        turn = build_turn(
            message, text, files=files, context=reply_context(message), workspace=workspace
        )
        return turn, reply

    async def download(self, message: Message, workspace: str, reply: Reply) -> list[str] | None:
        """Save the message's attachment under ``<workspace>/uploads/<8 hex>/``; None if refused."""
        resolved = resolve_file(message)
        if resolved is None:
            return []
        file_obj, name, description = resolved
        size = getattr(file_obj, "file_size", None) or 0
        if size > FILE_DOWNLOAD_LIMIT:
            await reply.send(
                f"File too large ({size / (1 << 20):.1f} MiB); Telegram bots can download "
                f"up to {FILE_DOWNLOAD_LIMIT >> 20} MiB."
            )
            return None
        directory = workspaces.new_uploads_dir(self.paths, workspace)
        destination = directory / name
        try:
            tg_file = await self.bot.get_file(file_obj.file_id)
            await tg_file.download_to_drive(destination)
        except asyncio.CancelledError:
            destination.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                directory.rmdir()
            raise
        except Exception as exc:
            log.warning("could not download %s: %s", description, exc)
            destination.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                directory.rmdir()
            await reply.send(f"Could not download the {description}: {exc}")
            return None
        log.info("downloaded %s to %s", description, destination)
        return [str(destination)]
