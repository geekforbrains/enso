"""Tests for Telegram transport helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from enso.transports.telegram import TelegramContext, TelegramTransport, _resolve_file


def _msg(**kwargs):
    fields = {
        "document": None,
        "photo": None,
        "audio": None,
        "voice": None,
        "video": None,
        "video_note": None,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def test_resolve_file_sanitizes_document_name():
    doc = SimpleNamespace(file_name="../../etc/passwd")
    _obj, name, desc = _resolve_file(_msg(document=doc))
    assert name == "passwd"
    assert desc == "file (passwd)"


def test_resolve_file_dot_only_name_falls_back_to_generated():
    """A name that sanitizes to empty (e.g. '...') must not yield an empty path."""
    doc = SimpleNamespace(file_name="...")
    _obj, name, _desc = _resolve_file(_msg(document=doc))
    assert name.startswith("document_")
    assert len(name) > len("document_")


def test_resolve_file_missing_audio_name_falls_back_to_generated():
    audio = SimpleNamespace(file_name=None)
    _obj, name, _desc = _resolve_file(_msg(audio=audio))
    assert name.startswith("audio_")
    assert name.endswith(".mp3")


def test_transport_resolves_1password_token_reference(monkeypatch):
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {
                    "bot_token_1password": {
                        "item": "Telegram",
                        "field": "TOKEN",
                    },
                    "allowed_users": ["123"],
                },
            },
        },
    )
    monkeypatch.setattr(
        "enso.transports.telegram.resolve_config_secret",
        lambda cfg, key: "resolved-telegram-token",
    )

    transport = TelegramTransport(runtime)

    assert transport.bot_token == "resolved-telegram-token"
    assert transport.allowed_users == ["123"]


def test_legacy_allowed_user_ids_is_ignored_and_fails_closed():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {"bot_token": "t", "allowed_user_ids": [123]},
            },
        },
    )

    transport = TelegramTransport(runtime)

    assert transport.allowed_users == []
    assert transport._is_authorized(_update()) is False


def test_wildcard_allowed_user_is_rejected():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {"bot_token": "t", "allowed_users": ["*"]},
            },
        },
    )

    transport = TelegramTransport(runtime)

    assert transport.allowed_users == []
    assert transport._is_authorized(_update()) is False


def test_invalid_allowed_users_value_fails_closed():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {"bot_token": "t", "allowed_users": "123"},
            },
        },
    )

    transport = TelegramTransport(runtime)

    assert transport.allowed_users == []
    assert transport._is_authorized(_update()) is False


def test_one_invalid_allowed_user_fails_closed_for_the_whole_list():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {
                    "bot_token": "t",
                    "allowed_users": ["123", 456],
                },
            },
        },
    )

    transport = TelegramTransport(runtime)

    assert transport.allowed_users == []
    assert transport._is_authorized(_update()) is False


def test_duplicate_allowed_users_fail_closed():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {
                    "bot_token": "t",
                    "allowed_users": ["123", "123"],
                },
            },
        },
    )

    transport = TelegramTransport(runtime)

    assert transport.allowed_users == []


def test_non_positive_allowed_user_fails_closed():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {
                    "bot_token": "t",
                    "allowed_users": ["123", "0"],
                },
            },
        },
    )

    transport = TelegramTransport(runtime)

    assert transport.allowed_users == []


def test_legacy_alias_fails_closed_even_with_valid_allowed_users():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {
                    "bot_token": "t",
                    "allowed_users": ["123"],
                    "allowed_user_ids": [123],
                },
            },
        },
    )

    transport = TelegramTransport(runtime)

    assert transport.allowed_users == []


async def test_notify_uses_configured_notify_channel():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {
                    "bot_token": "t",
                    "allowed_users": ["123", "456"],
                    "notify_channel": "789",
                },
            },
        },
    )
    transport = TelegramTransport(runtime)
    transport._bot = SimpleNamespace(send_message=AsyncMock())

    await transport.notify("Hello")

    transport._bot.send_message.assert_awaited_once()
    assert transport._bot.send_message.await_args.kwargs["chat_id"] == "789"


async def test_notify_explicit_destination_wins_over_notify_channel():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {
                    "bot_token": "t",
                    "allowed_users": ["123"],
                    "notify_channel": "789",
                },
            },
        },
    )
    transport = TelegramTransport(runtime)
    transport._bot = SimpleNamespace(send_message=AsyncMock())

    await transport.notify("Hello", destination="999")

    transport._bot.send_message.assert_awaited_once()
    assert transport._bot.send_message.await_args.kwargs["chat_id"] == "999"


async def test_notify_without_destination_is_dropped_instead_of_broadcast():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {
                    "bot_token": "t",
                    "allowed_users": ["123", "456"],
                },
            },
        },
    )
    transport = TelegramTransport(runtime)
    transport._bot = SimpleNamespace(send_message=AsyncMock())

    await transport.notify("Hello")

    transport._bot.send_message.assert_not_awaited()


async def test_reply_status_returns_message_handle():
    handle = object()
    reply_text = AsyncMock(return_value=handle)
    context = TelegramContext(SimpleNamespace(message=SimpleNamespace(reply_text=reply_text)))

    result = await context.reply_status("Working…")

    assert result is handle
    reply_text.assert_awaited_once_with("Working…")


async def test_edit_status_updates_message_handle():
    handle = SimpleNamespace(edit_text=AsyncMock())
    context = TelegramContext(SimpleNamespace())

    await context.edit_status(handle, "Still working…")

    handle.edit_text.assert_awaited_once_with("Still working…")


async def test_delete_status_deletes_message_handle():
    handle = SimpleNamespace(delete=AsyncMock())
    context = TelegramContext(SimpleNamespace())

    await context.delete_status(handle)

    handle.delete.assert_awaited_once_with()


async def test_delete_status_ignores_telegram_errors():
    handle = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("message is gone")))
    context = TelegramContext(SimpleNamespace())

    await context.delete_status(handle)

    handle.delete.assert_awaited_once_with()


# -- Non-private chat rejection --


def _auth_transport():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {"bot_token": "t", "allowed_users": ["123"]},
            },
        },
    )
    return TelegramTransport(runtime)


def _update(chat_type="private", user_id=123):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type=chat_type, id=999),
        message=SimpleNamespace(),
        callback_query=None,
    )


def test_private_chat_from_allowed_user_is_authorized():
    assert _auth_transport()._is_authorized(_update()) is True


def test_group_chat_is_rejected_even_for_allowed_user():
    """An authorized ID must not be able to invoke Enso from a group chat."""
    for chat_type in ("group", "supergroup", "channel"):
        assert _auth_transport()._is_authorized(_update(chat_type=chat_type)) is False


def test_missing_chat_is_rejected():
    update = _update()
    update.effective_chat = None
    assert _auth_transport()._is_authorized(update) is False


def test_unknown_user_is_rejected_in_private_chat():
    assert _auth_transport()._is_authorized(_update(user_id=666)) is False
