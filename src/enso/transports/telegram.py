"""Telegram transport — your phone talks to your agents here."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import sys
import uuid
from typing import TYPE_CHECKING, Any

try:
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
except ImportError:
    raise ImportError(
        "python-telegram-bot is required for the Telegram transport. "
        "Install it with: pip install enso[telegram]"
    ) from None

from .. import policy as native_policy
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
from ..core import ExecutionContext
from ..formatting import md_to_html
from ..secret_refs import resolve_config_secret
from ..teams import load_telegram
from . import BaseTransport, SecureUploadDirectory, TransportContext, safe_filename

if TYPE_CHECKING:
    from ..core import Runtime
    from ..teams import Policy, Workspace

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB Telegram bot API limit

log = logging.getLogger(__name__)

CONFIG_ERROR_REPLY = (
    "This conversation isn't fully configured for Enso — ask an admin to run "
    "`enso config check`."
)


def _is_parse_error(exc: BadRequest) -> bool:
    """Return True when Telegram rejected HTML formatting rather than delivery."""
    return "parse entities" in str(exc).lower()


def _conversation_key(chat_id: object, workspace: Workspace, policy: Policy) -> str:
    """Build a delimiter-safe state key bound to the exact Telegram execution policy."""
    payload = json.dumps(
        {
            "v": 1,
            "kind": "telegram",
            "parts": [str(chat_id), workspace.name, policy.name],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"telegram:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


# Commands registered with Telegram's menu UI.
COMMANDS = [
    BotCommand("stop", "Stop process & clear queue"),
    BotCommand("queue", "View & manage queued messages"),
    BotCommand("use", "Switch provider"),
    BotCommand("model", "Switch model"),
    BotCommand("effort", "Set the active provider's reasoning effort"),
    BotCommand("status", "Provider, model & effort info"),
    BotCommand("clear", "Clear session"),
    BotCommand("compact", "Summarise & compact the active session"),
    BotCommand("update", "Install the latest stable Enso"),
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

    def get_origin_env(self) -> dict[str, str]:
        chat = self._update.effective_chat
        user = self._update.effective_user
        name_parts: list[str] = []
        if user is not None:
            first_name = getattr(user, "first_name", None)
            last_name = getattr(user, "last_name", None)
            if first_name:
                name_parts.append(first_name)
            if last_name:
                name_parts.append(last_name)
        return {
            "ENSO_ORIGIN_TRANSPORT": "telegram",
            "ENSO_ORIGIN_CHANNEL": str(getattr(chat, "id", "")) if chat else "",
            "ENSO_ORIGIN_THREAD_TS": "",
            "ENSO_ORIGIN_USER_ID": str(getattr(user, "id", "")) if user else "",
            "ENSO_ORIGIN_USER_NAME": " ".join(name_parts),
            "ENSO_ORIGIN_CHANNEL_NAME": "dm",
        }


class TelegramTransport(BaseTransport):
    """Telegram bot transport."""

    name = "telegram"
    message_limit = 4096

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        transports = runtime.config.get("transports", {})
        tg_cfg = transports.get("telegram", {}) if isinstance(transports, dict) else {}
        if not isinstance(tg_cfg, dict):
            tg_cfg = {}
        self.bot_token = resolve_config_secret(tg_cfg, "bot_token")
        self.telegram = load_telegram(runtime.config)
        self.allowed_users = self.telegram.allowed_users
        self.notify_channel = str(tg_cfg.get("notify_channel", "") or "")
        self._bot: Any = None
        self._report_config_problems()

    def _report_config_problems(self) -> None:
        """Log every reason Telegram dispatch is disabled."""
        for error in self.telegram.errors:
            log.error("Telegram config error (dispatch disabled): %s", error)
        workspace = self.telegram.workspace
        execution_policy = self.telegram.policy
        if not self.telegram.usable or workspace is None or execution_policy is None:
            return
        for provider in execution_policy.providers:
            check = native_policy.check_provider(workspace, execution_policy, provider)
            for problem in check.problems:
                log.error(
                    "Policy %s on Telegram workspace %s cannot launch %s: %s",
                    execution_policy.name,
                    workspace.name,
                    provider,
                    problem,
                )

    def _configured_commands(self) -> list[BotCommand]:
        """Return only commands exposed by the bound workspace policy."""
        execution_policy = self.telegram.policy
        if not self.telegram.usable or execution_policy is None:
            return []
        return [command for command in COMMANDS if execution_policy.allows_command(command.command)]

    def _usable_providers(self) -> list[str]:
        """Return policy-selected providers whose native launch configuration is usable."""
        workspace = self.telegram.workspace
        execution_policy = self.telegram.policy
        if not self.telegram.usable or workspace is None or execution_policy is None:
            return []
        return [
            provider
            for provider in execution_policy.providers
            if native_policy.check_provider(workspace, execution_policy, provider).ok
        ]

    def _execution_context(self, chat_id: object) -> ExecutionContext | None:
        """Resolve one private chat to its immutable workspace/policy binding."""
        workspace = self.telegram.workspace
        execution_policy = self.telegram.policy
        if not self.telegram.usable or workspace is None or execution_policy is None:
            return None

        chat_key = _conversation_key(chat_id, workspace, execution_policy)
        provider = self.runtime.active_provider_by_chat.get(chat_key)
        if provider not in execution_policy.providers:
            provider = execution_policy.default_provider
        if provider is None or provider not in execution_policy.providers:
            log.error(
                "Telegram workspace %s has no allowed default provider %r",
                workspace.name,
                execution_policy.default_provider,
            )
            return None

        self.runtime.active_provider_by_chat[chat_key] = provider
        self.runtime.touch_session(chat_key)
        return ExecutionContext(
            chat_key=chat_key,
            path=workspace.path,
            workspace_id=workspace.name,
            workspace=workspace,
            policy=execution_policy,
            concurrency=workspace.concurrency,
            include_global_messages=True,
        )

    def _provider_usable(self, context: ExecutionContext) -> bool:
        """Revalidate the selected provider before work that can launch it."""
        provider = self.runtime.active_provider_by_chat.get(context.chat_key)
        if provider not in context.policy.providers:
            return False
        check = native_policy.check_provider(context.workspace, context.policy, provider)
        if not check.ok:
            for problem in check.problems:
                log.error(
                    "Telegram provider %s is unavailable in workspace %s: %s",
                    provider,
                    context.workspace_id,
                    problem,
                )
        return check.ok

    async def _command_context(
        self,
        update: Update,
        command: str,
    ) -> ExecutionContext | None:
        """Authorize a direct command or callback against the current policy."""
        if not self._is_authorized(update):
            return None
        context = self._execution_context(update.effective_chat.id)
        if context is None:
            await self._reply_to_command(update, CONFIG_ERROR_REPLY)
            return None
        execution_policy = self.telegram.policy
        assert execution_policy is not None
        if not execution_policy.allows_command(command):
            await self._reply_to_command(
                update,
                f"/{command} is not available in this conversation.",
            )
            return None
        return context

    @staticmethod
    async def _reply_to_command(update: Update, text: str) -> None:
        """Reply through a slash-command message or edit an inline callback message."""
        if update.callback_query is not None:
            await update.callback_query.edit_message_text(text)
        elif update.message is not None:
            await update.message.reply_text(text)

    def _is_authorized(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        if update.message is None and update.callback_query is None:
            return False
        # Telegram is private and one-to-one by design (teams.md): an
        # authorized ID must not be able to invoke Enso from a group chat
        # someone added the bot to.
        chat = update.effective_chat
        if chat is None or chat.type != "private":
            log.warning(
                "Rejected non-private Telegram chat type=%s chat=%s",
                getattr(chat, "type", None),
                getattr(chat, "id", None),
            )
            return False
        if not is_authorized(str(user.id), list(self.allowed_users)):
            log.warning("Unauthorized user: %s", user.id)
            return False
        return True

    def start(self) -> None:
        """Start polling for Telegram messages (blocking)."""
        if not self.allowed_users:
            log.warning(
                "allowed_users is empty — no one can message this bot! "
                "Run 'enso setup' or edit ~/.enso/config.json to add users."
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
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        # File uploads
        app.add_handler(
            MessageHandler(
                filters.Document.ALL
                | filters.PHOTO
                | filters.AUDIO
                | filters.VOICE
                | filters.VIDEO
                | filters.VIDEO_NOTE,
                self._handle_file_message,
            )
        )
        app.run_polling(allowed_updates=Update.ALL_TYPES)

    async def _post_init(self, app: Application) -> None:
        """Register commands with Telegram and start background tasks."""
        self._bot = app.bot
        await self._bot.set_my_commands(self._configured_commands())
        self._start_background_tasks()

    async def _send_update_confirmation(self, pending: dict, text: str) -> bool:
        await self._bot.send_message(
            chat_id=pending.get("channel", ""),
            text=text,
        )
        return True

    async def notify(self, text: str, *, destination: str | None = None) -> None:
        """Send a one-way notification to one explicit or configured target."""
        target = destination or self.notify_channel
        if not target:
            log.warning("Telegram notify dropped — no destination passed and no notify_channel set")
            return
        if not self._bot:
            log.warning("Cannot notify — bot not initialized yet")
            return
        html = md_to_html(text[:4096])
        try:
            await self._bot.send_message(
                chat_id=target,
                text=html,
                parse_mode=ParseMode.HTML,
            )
        except BadRequest as exc:
            if not _is_parse_error(exc):
                log.exception("Failed to notify Telegram destination %s", target)
                return
            try:
                await self._bot.send_message(chat_id=target, text=text[:4096])
            except Exception:
                log.exception("Failed to notify Telegram destination %s", target)
        except Exception:
            log.exception("Failed to notify Telegram destination %s", target)

    # -- Message handling --

    async def _handle_message(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return
        text = (update.message.text or "").strip()
        conv_id = str(update.effective_chat.id)
        execution = self._execution_context(conv_id)
        if execution is None or not self._provider_usable(execution):
            await update.message.reply_text(CONFIG_ERROR_REPLY)
            return
        log.info(
            "Incoming message: chat_id=%s msg_id=%s is_reply=%s len=%d",
            conv_id,
            update.message.message_id,
            update.message.reply_to_message is not None,
            len(text),
        )

        # Build reply context (Telegram-specific)
        reply_context = _build_reply_context(update.message)
        is_reply = reply_context is not None
        if reply_context:
            text = f"{reply_context}\n\n{text}"

        preview = text[:50].replace("\n", " ")
        ctx = TelegramContext(update, is_reply=is_reply)
        await self.runtime.dispatch(
            conv_id,
            text,
            ctx,
            preview=preview,
            context=execution,
        )

    async def _handle_file_message(self, update: Update, _ctx: Any) -> None:
        if not self._is_authorized(update):
            return

        msg = update.message
        conv_id = str(update.effective_chat.id)
        execution = self._execution_context(conv_id)
        if execution is None or not self._provider_usable(execution):
            await msg.reply_text(CONFIG_ERROR_REPLY)
            return
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

        try:
            upload_directory = SecureUploadDirectory.create(
                execution.path,
                uuid.uuid4().hex,
            )
        except (OSError, ValueError):
            log.exception("Failed to create Telegram upload directory in %s", execution.path)
            await msg.reply_text("Failed to prepare file upload. Please try again.")
            return

        try:
            with upload_directory:
                tg_file = await tg_file_obj.get_file()
                payload = await tg_file.download_as_bytearray()
                if len(payload) > MAX_FILE_SIZE:
                    size_mb = len(payload) / (1024 * 1024)
                    await msg.reply_text(
                        f"File too large ({size_mb:.1f}MB). "
                        "Telegram bots can only download files up to 20MB."
                    )
                    return
                with upload_directory.open_file(filename) as destination:
                    destination.write(payload)
                dest_path = upload_directory.verified_file_path(filename)
                if dest_path is None:
                    raise OSError("Telegram upload path changed after download")
                log.info("Downloaded %s to %s (%d bytes)", desc, dest_path, len(payload))
        except Exception:
            log.exception("Failed to download %s", desc)
            await msg.reply_text("Failed to download file. Please try again.")
            return

        caption = (msg.caption or "").strip()
        prompt = f"User uploaded a {desc}: {dest_path}"
        if caption:
            prompt += f"\n\n{caption}"

        ctx = TelegramContext(update)
        preview = prompt[:50].replace("\n", " ")
        await self.runtime.dispatch(
            conv_id,
            prompt,
            ctx,
            preview=preview,
            context=execution,
        )

    # -- Slash commands --

    async def _cmd_stop(self, update: Update, _ctx: Any) -> None:
        execution = await self._command_context(update, "stop")
        if execution is None:
            return
        await update.message.reply_text(await cmd_stop_async(self.runtime, execution.chat_key))

    async def _cmd_queue(self, update: Update, _ctx: Any) -> None:
        execution = await self._command_context(update, "queue")
        if execution is None:
            return
        conv_id = execution.chat_key
        args = (update.message.text or "").split()[1:]

        # Direct: /queue clear
        if args == ["clear"]:
            count = await self.runtime.clear_queue(conv_id)
            await update.message.reply_text(
                f"Cleared {count} queued message(s)." if count else "Queue is empty."
            )
            return

        await self._show_queue(update, conv_id)

    async def _show_queue(
        self,
        update_or_query: Any,
        conv_id: str,
    ) -> None:
        """Render the queue view (used by /queue command and callbacks)."""
        previews = self.runtime.get_queue(conv_id)
        if not previews:
            text = "No messages queued."
            if hasattr(update_or_query, "edit_message_text"):
                await update_or_query.edit_message_text(text)
            else:
                await update_or_query.message.reply_text(text)
            return

        lines = [f"Queued messages ({len(previews)}):"]
        for i, preview in enumerate(previews):
            label = f"{preview}\u2026" if len(preview) == 50 else preview
            lines.append(f"{i + 1}. {label}")

        remove_buttons = [
            InlineKeyboardButton(
                f"\u2715 {i + 1}",
                callback_data=f"queue:rm:{i}",
            )
            for i in range(len(previews))
        ]
        keyboard = InlineKeyboardMarkup(
            [
                remove_buttons,
                [InlineKeyboardButton("Clear all", callback_data="queue:clear")],
            ]
        )

        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(
                "\n".join(lines),
                reply_markup=keyboard,
            )
        else:
            await update_or_query.message.reply_text(
                "\n".join(lines),
                reply_markup=keyboard,
            )

    async def _cmd_use(self, update: Update, _ctx: Any) -> None:
        execution = await self._command_context(update, "use")
        if execution is None:
            return
        conv_id = execution.chat_key
        args = (update.message.text or "").split()[1:]
        choice = args[0] if args else None

        response, options = cmd_use(
            self.runtime,
            conv_id,
            choice,
            providers=self._usable_providers(),
        )
        if response:
            await update.message.reply_text(response)
            return

        buttons = [
            InlineKeyboardButton(
                f"{'● ' if active else ''}{name}",
                callback_data=f"use:{name}",
            )
            for name, active in options
        ]
        await update.message.reply_text(
            "Switch provider:",
            reply_markup=InlineKeyboardMarkup([buttons]),
        )

    async def _cmd_status(self, update: Update, _ctx: Any) -> None:
        execution = await self._command_context(update, "status")
        if execution is None:
            return
        await update.message.reply_text(cmd_status(self.runtime, execution.chat_key))

    async def _cmd_model(self, update: Update, _ctx: Any) -> None:
        execution = await self._command_context(update, "model")
        if execution is None:
            return
        conv_id = execution.chat_key
        args = (update.message.text or "").split()[1:]
        choice = args[0] if args else None

        response, options = cmd_model(self.runtime, conv_id, choice)
        if response:
            await update.message.reply_text(response)
            return

        provider = self.runtime.get_active_provider(conv_id)
        buttons = [
            InlineKeyboardButton(
                f"{'● ' if active else ''}{name}",
                callback_data=f"model:{name}",
            )
            for name, active in options
        ]
        keyboard = [[b] for b in buttons]
        await update.message.reply_text(
            f"Switch model ({provider}):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_effort(self, update: Update, _ctx: Any) -> None:
        execution = await self._command_context(update, "effort")
        if execution is None:
            return
        conv_id = execution.chat_key
        args = (update.message.text or "").split()[1:]
        choice = args[0] if args else None

        response, options = cmd_effort(self.runtime, conv_id, choice)
        if response:
            await update.message.reply_text(response)
            return

        model = self.runtime.get_active_model(
            conv_id,
            self.runtime.get_active_provider(conv_id),
        )
        buttons = [
            InlineKeyboardButton(
                f"{'● ' if active else ''}{name}",
                callback_data=f"effort:{name}",
            )
            for name, active in options
        ]
        keyboard = [[b] for b in buttons]
        keyboard.append(
            [
                InlineKeyboardButton("Use default", callback_data="effort:default"),
            ]
        )
        await update.message.reply_text(
            f"Set effort ({model}):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_clear(self, update: Update, _ctx: Any) -> None:
        execution = await self._command_context(update, "clear")
        if execution is None:
            return
        conv_id = execution.chat_key
        args = (update.message.text or "").split()[1:]

        # Direct usage: /clear all
        if args == ["all"]:
            cmd_clear(self.runtime, conv_id, context=execution, clear_all=True)
            await update.message.reply_text("Cleared all providers.")
            return

        # No args → show options
        active = self.runtime.get_active_provider(conv_id)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"Clear {active}", callback_data="clear:current"),
                    InlineKeyboardButton("Clear all", callback_data="clear:all"),
                ],
            ]
        )
        await update.message.reply_text("Clear session:", reply_markup=keyboard)

    async def _cmd_compact(self, update: Update, _ctx: Any) -> None:
        execution = await self._command_context(update, "compact")
        if execution is None:
            return
        if not self._provider_usable(execution):
            await update.message.reply_text(CONFIG_ERROR_REPLY)
            return
        await update.message.reply_text(
            "Compacting context - this can take 10-30s while the agent summarises..."
        )
        await update.effective_chat.send_action(ChatAction.TYPING)
        reply = await cmd_compact_async(
            self.runtime,
            execution.chat_key,
            context=execution,
        )
        await update.message.reply_text(reply)

    async def _cmd_update(self, update: Update, _ctx: Any) -> None:
        if await self._command_context(update, "update") is None:
            return
        status = await update.message.reply_text("Checking the latest stable Enso release…")
        result = await cmd_update_async(self.runtime)
        await status.edit_text(result.message)
        if result.restart_required:
            from ..updater import queue_update_confirmation, schedule_service_restart

            queue_update_confirmation(
                result,
                transport=self.name,
                channel=str(update.effective_chat.id),
            )
            schedule_service_restart()

    async def _cmd_restart(self, update: Update, _ctx: Any) -> None:
        if await self._command_context(update, "restart") is None:
            return
        await update.message.reply_text("Restarting...")
        asyncio.get_event_loop().call_later(1, _restart)

    async def _cmd_logs(self, update: Update, _ctx: Any) -> None:
        if await self._command_context(update, "logs") is None:
            return
        await update.message.reply_text(cmd_logs()[-4000:])

    async def _cmd_help(self, update: Update, _ctx: Any) -> None:
        if await self._command_context(update, "help") is None:
            return
        cmds = [(c.command, c.description) for c in self._configured_commands()]
        await update.message.reply_text(cmd_help(cmds))

    # -- Inline keyboard callbacks --

    async def _handle_callback(self, update: Update, _ctx: Any) -> None:
        """Route inline keyboard button taps."""
        if not self._is_authorized(update):
            return
        query = update.callback_query
        await query.answer()  # Acknowledge the tap immediately

        data = query.data or ""
        command, separator, choice = data.partition(":")
        if not separator or command not in {"use", "model", "effort", "clear", "queue"}:
            return
        execution = await self._command_context(update, command)
        if execution is None:
            return
        conv_id = execution.chat_key
        rt = self.runtime

        if command == "use":
            response, _ = cmd_use(
                rt,
                conv_id,
                choice,
                providers=self._usable_providers(),
            )
            if response:
                await query.edit_message_text(response)

        elif command == "model":
            response, _ = cmd_model(rt, conv_id, choice)
            if response:
                await query.edit_message_text(response)

        elif command == "effort":
            response, _ = cmd_effort(rt, conv_id, choice)
            if response:
                await query.edit_message_text(response)

        elif command == "clear":
            if choice not in {"current", "all"}:
                return
            is_all = choice == "all"
            parts = cmd_clear(rt, conv_id, context=execution, clear_all=is_all)
            label = "all providers" if is_all else "current provider"
            await query.edit_message_text(f"Cleared {label}.\n" + "\n".join(parts))

        elif command == "queue":
            if choice == "clear":
                count = await rt.clear_queue(conv_id)
                await query.edit_message_text(
                    f"Cleared {count} queued message(s)." if count else "Queue already empty."
                )
            elif choice.startswith("rm:") and choice[3:].isdigit():
                idx = int(choice[3:])
                await rt.remove_from_queue(conv_id, idx)
                await self._show_queue(query, conv_id)


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
        # The fallback also covers names that sanitize to empty (e.g. "...").
        name = safe_filename(msg.document.file_name or "") or f"document_{uuid.uuid4().hex[:8]}"
        return msg.document, name, f"file ({name})"
    if msg.photo:
        name = f"photo_{uuid.uuid4().hex[:8]}.jpg"
        return msg.photo[-1], name, "photo"
    if msg.audio:
        name = safe_filename(msg.audio.file_name or "") or f"audio_{uuid.uuid4().hex[:8]}.mp3"
        return msg.audio, name, f"audio file ({name})"
    if msg.voice:
        name = f"voice_{uuid.uuid4().hex[:8]}.ogg"
        return msg.voice, name, "voice message"
    if msg.video:
        name = safe_filename(msg.video.file_name or "") or f"video_{uuid.uuid4().hex[:8]}.mp4"
        return msg.video, name, f"video ({name})"
    if msg.video_note:
        name = f"videonote_{uuid.uuid4().hex[:8]}.mp4"
        return msg.video_note, name, "video note"
    return None, "", ""
