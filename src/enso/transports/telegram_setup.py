"""Verify a new Telegram bot and pair exactly one private, challenged sender.

This receiver never starts the ordinary transport or deletes a webhook. It owns
one polling loop until completion, timeout or cancellation, then closes it.
"""

from __future__ import annotations

import secrets
from typing import Any

from .connection import Claim, PairedIdentity, PairingError, PairingRequest, Ready


def matching_message(message: Any, request: PairingRequest) -> bool:
    """Old, forwarded, bot, group and unrelated messages cannot establish ownership."""
    return bool(
        message
        and message.from_user
        and not message.from_user.is_bot
        and message.chat.type == "private"
        and str(message.chat.id) == str(message.from_user.id)
        and not message.forward_origin
        and message.date.timestamp() >= int(request.created_at)
        and isinstance(message.text, str)
        and message.text.isascii()
        and secrets.compare_digest(message.text.strip(), f"/start {request.nonce}")
    )


async def pair(request: PairingRequest, ready: Ready, claim: Claim) -> PairedIdentity:
    from telegram import Bot
    from telegram.error import BadRequest, Conflict, Forbidden, InvalidToken, NetworkError

    try:
        async with Bot(request.bot_token) as bot:
            me = await bot.get_me(read_timeout=10, connect_timeout=5)
            if not me.username:
                raise PairingError("invalid_token", "This bot has no Telegram username.")
            webhook = await bot.get_webhook_info(read_timeout=10, connect_timeout=5)
            if webhook.url:
                raise PairingError(
                    "webhook_conflict", "This bot already uses a webhook. Connect a new bot."
                )
            await ready(
                {
                    "bot_name": me.username,
                    "workspace_name": "",
                    "open_url": f"https://t.me/{me.username}?start={request.nonce}",
                    "instruction": "Open your bot and tap Start to connect your account.",
                }
            )
            offset: int | None = None
            while True:
                updates = await bot.get_updates(
                    offset=offset,
                    timeout=10,
                    read_timeout=15,
                    connect_timeout=5,
                    allowed_updates=["message"],
                )
                for update in updates:
                    offset = update.update_id + 1
                    message = update.message
                    if not matching_message(message, request):
                        continue
                    assert message is not None and message.from_user is not None
                    await claim()
                    await bot.send_message(
                        chat_id=message.chat.id,
                        text="Connected. Finish signing in to your AI provider, then start Enso.",
                        read_timeout=10,
                        connect_timeout=5,
                    )
                    # Confirm the matching update, so normal polling cannot replay the code.
                    await bot.get_updates(
                        offset=offset, timeout=0, read_timeout=5, connect_timeout=5
                    )
                    return PairedIdentity(str(message.from_user.id), str(message.chat.id))
    except Conflict as exc:
        raise PairingError(
            "polling_conflict", "Another service is using this bot. Connect a new bot."
        ) from exc
    except InvalidToken, Forbidden, BadRequest:
        raise PairingError(
            "invalid_token", "Telegram could not use this token. Copy it again from BotFather."
        ) from None
    except TimeoutError, NetworkError, OSError:
        raise PairingError(
            "connection_failed", "Telegram could not be reached. Try connecting again."
        ) from None
