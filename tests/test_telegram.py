"""Tests for Telegram transport helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from enso.transports.telegram import TelegramContext, TelegramTransport, _resolve_file


def _msg(**kwargs):
    fields = {
        "document": None, "photo": None, "audio": None,
        "voice": None, "video": None, "video_note": None,
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
