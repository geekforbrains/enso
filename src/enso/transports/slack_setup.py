"""Pair a Slack owner on one bounded Socket Mode connection before normal service.

Both credentials must identify the same app. The receiver handles only challenged
private messages; it cannot dispatch a provider or bind an unrelated sender.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

from .connection import Claim, PairedIdentity, PairingError, PairingRequest, Ready


def matching_event(event: dict[str, Any], request: PairingRequest) -> bool:
    try:
        fresh = float(event.get("ts", "0")) >= request.created_at
    except TypeError, ValueError:
        return False
    return bool(
        fresh
        and event.get("type") == "message"
        and event.get("channel_type") == "im"
        and not event.get("subtype")
        and not event.get("bot_id")
        and not event.get("thread_ts")
        and re.fullmatch(r"[UW][A-Z0-9]+", str(event.get("user", "")))
        and re.fullmatch(r"D[A-Z0-9]+", str(event.get("channel", "")))
        and isinstance(event.get("text"), str)
        and event["text"].isascii()
        and secrets.compare_digest(event["text"].strip(), f"ENSO-{request.nonce}")
    )


def socket_url(value: object) -> str:
    """Socket credentials are sent only to Slack's authenticated connection endpoint."""
    if not isinstance(value, str) or len(value) > 4096:
        raise PairingError("connection_failed", "Slack returned an invalid connection.")
    parts = urlsplit(value)
    if (
        parts.scheme != "wss"
        or not parts.hostname
        or not parts.hostname.endswith(".slack.com")
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise PairingError("connection_failed", "Slack returned an invalid connection.")
    return value


async def pair(request: PairingRequest, ready: Ready, claim: Claim) -> PairedIdentity:
    import aiohttp
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            bot = AsyncWebClient(token=request.bot_token, session=session, retry_handlers=[])
            app = AsyncWebClient(token=request.app_token, session=session, retry_handlers=[])
            identity = await bot.auth_test()
            bot_id, team_id = identity.get("bot_id"), identity.get("team_id")
            if not bot_id or not re.fullmatch(r"T[A-Z0-9]+", str(team_id)):
                raise PairingError("invalid_token", "Use the Bot User OAuth Token from Slack.")
            bot_info = await bot.bots_info(bot=bot_id)
            bot_record: dict[str, Any] = bot_info.get("bot") or {}
            app_id = bot_record.get("app_id")
            if not isinstance(app_id, str) or not re.fullmatch(r"A[A-Z0-9]+", app_id):
                raise PairingError("invalid_token", "Slack could not identify this bot's app.")
            opened = await app.api_call("apps.connections.open", http_verb="POST")
            async with session.ws_connect(
                socket_url(opened.get("url")), heartbeat=10, max_msg_size=65536
            ) as socket:
                hello = await asyncio.wait_for(socket.receive_json(), timeout=10)
                if hello.get("type") != "hello":
                    raise PairingError("connection_failed", "Slack did not open the connection.")
                if hello.get("connection_info", {}).get("app_id") != app_id:
                    raise PairingError(
                        "mismatched_app", "The bot and app tokens belong to different Slack apps."
                    )
                await ready(
                    {
                        "bot_name": str(identity.get("user", "Enso"))[:100],
                        "workspace_name": str(identity.get("team", ""))[:100],
                        "open_url": "https://slack.com/app_redirect?"
                        + urlencode({"app": app_id, "team": team_id}),
                        "instruction": f"ENSO-{request.nonce}",
                    }
                )
                async for message in socket:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(message.data)
                    envelope = data.get("envelope_id")
                    if isinstance(envelope, str) and len(envelope) <= 128:
                        await socket.send_json({"envelope_id": envelope})
                    payload = data.get("payload", {})
                    event = payload.get("event", {})
                    if (
                        data.get("type") != "events_api"
                        or payload.get("team_id") != team_id
                        or not isinstance(event, dict)
                        or not matching_event(event, request)
                    ):
                        continue
                    user = await bot.users_info(user=event["user"])
                    user_record: dict[str, Any] = user.get("user") or {}
                    if user_record.get("is_bot"):
                        continue
                    await claim()
                    await bot.chat_postMessage(
                        channel=event["channel"],
                        text="Connected. Finish signing in to your AI provider, then start Enso.",
                    )
                    return PairedIdentity(event["user"], event["channel"])
            raise PairingError("connection_failed", "Slack disconnected. Try connecting again.")
    except SlackApiError as exc:
        code = exc.response.get("error")
        if code in ("missing_scope", "not_allowed_token_type"):
            raise PairingError(
                "missing_scope",
                "Check the bot token and the app token's connections:write permission.",
            ) from None
        raise PairingError(
            "invalid_token", "Slack could not use these tokens. Copy them again from your app."
        ) from None
    except TimeoutError, aiohttp.ClientError, OSError, ValueError:
        raise PairingError(
            "connection_failed", "Slack could not be reached. Try connecting again."
        ) from None
