"""Tests for Telegram transport helpers."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from enso.transports.telegram import (
    COMMANDS,
    CONFIG_ERROR_REPLY,
    TelegramContext,
    TelegramTransport,
    _conversation_key,
    _resolve_file,
)


def _msg(**kwargs):
    fields = {
        "document": None,
        "photo": None,
        "audio": None,
        "voice": None,
        "video": None,
        "video_note": None,
        "caption": None,
        "message_id": 1,
        "quote": None,
        "reply_to_message": None,
        "reply_text": AsyncMock(),
        "text": "hello",
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _config(
    workspace_path: str,
    *,
    allowed_users: object = None,
    commands: object = "*",
    providers: list[str] | None = None,
    default_provider: str | None = None,
    workspace_name: str = "phone",
    policy_name: str = "mobile",
) -> dict:
    providers = providers or ["claude", "codex"]
    default_provider = default_provider or providers[0]
    users = ["123"] if allowed_users is None else allowed_users
    return {
        "transports": {
            "telegram": {
                "bot_token": "t",
                "allowed_users": users,
                "workspace": workspace_name,
            },
        },
        "workspaces": {
            workspace_name: {
                "path": workspace_path,
                "policy": policy_name,
                "concurrency": 2,
            },
        },
        "policies": {
            policy_name: {
                "unrestricted": True,
                "providers": providers,
                "default_provider": default_provider,
                "chat_commands": commands,
            },
        },
    }


def _runtime(config: dict) -> SimpleNamespace:
    active: dict[str, str] = {}
    runtime = SimpleNamespace(
        config=config,
        active_provider_by_chat=active,
        active_model_by_chat_provider={},
        effort_by_chat_provider_model={},
        models={"claude": ["sonnet"], "codex": ["gpt-5"]},
        dispatch=AsyncMock(),
        touch_session=Mock(),
        save_state=Mock(),
        clear_queue=AsyncMock(return_value=0),
        remove_from_queue=AsyncMock(return_value=True),
        get_queue=Mock(return_value=[]),
    )
    runtime.get_active_provider = Mock(
        side_effect=lambda chat_key: active.get(chat_key, "claude")
    )
    runtime.get_active_model = Mock(
        side_effect=lambda _chat_key, provider: runtime.models[provider][0]
    )
    runtime.get_active_effort = Mock(return_value=None)
    return runtime


def _bound_transport(tmp_path, **kwargs) -> TelegramTransport:
    return TelegramTransport(_runtime(_config(str(tmp_path / "workspace"), **kwargs)))


def _update(
    chat_type: str = "private",
    user_id: int = 123,
    *,
    chat_id: int = 999,
    message: object | None = None,
    callback_data: str | None = None,
):
    chat = SimpleNamespace(type=chat_type, id=chat_id, send_action=AsyncMock())
    callback = None
    if callback_data is not None:
        callback = SimpleNamespace(
            data=callback_data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        message = None
    elif message is None:
        message = _msg()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=chat,
        message=message,
        callback_query=callback,
    )


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
    assert transport.allowed_users == ("123",)


def test_legacy_allowed_user_ids_is_ignored_and_fails_closed():
    runtime = SimpleNamespace(
        config={
            "transports": {
                "telegram": {"bot_token": "t", "allowed_user_ids": [123]},
            },
        },
    )

    transport = TelegramTransport(runtime)

    assert transport.allowed_users == ()
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

    assert transport.allowed_users == ()
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

    assert transport.allowed_users == ()
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

    assert transport.allowed_users == ()
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

    assert transport.allowed_users == ()


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

    assert transport.allowed_users == ()


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

    assert transport.allowed_users == ()


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


# -- Workspace and policy binding --


def test_execution_context_is_complete_and_uses_an_opaque_bound_key(tmp_path):
    transport = _bound_transport(tmp_path)

    context = transport._execution_context(999)

    assert context is not None
    assert context.path == str(tmp_path / "workspace")
    assert context.workspace_id == "phone"
    assert context.workspace is transport.telegram.workspace
    assert context.policy is transport.telegram.policy
    assert context.concurrency == 2
    assert context.include_global_messages is True
    assert context.chat_key.startswith("telegram:")
    assert "999" not in context.chat_key
    assert "phone" not in context.chat_key
    assert "mobile" not in context.chat_key
    assert transport.runtime.active_provider_by_chat == {context.chat_key: "claude"}
    transport.runtime.touch_session.assert_called_once_with(context.chat_key)


def test_conversation_key_separates_chat_workspace_and_policy(tmp_path):
    transport = _bound_transport(tmp_path)
    workspace = transport.telegram.workspace
    execution_policy = transport.telegram.policy
    assert workspace is not None
    assert execution_policy is not None

    keys = {
        _conversation_key("1", workspace, execution_policy),
        _conversation_key("2", workspace, execution_policy),
        _conversation_key("1", replace(workspace, name="other"), execution_policy),
        _conversation_key("1", workspace, replace(execution_policy, name="other")),
    }

    assert len(keys) == 4


async def test_message_dispatch_uses_workspace_policy_context(tmp_path):
    transport = _bound_transport(tmp_path)
    message = _msg(text="ship it")

    await transport._handle_message(_update(message=message), None)

    transport.runtime.dispatch.assert_awaited_once()
    call = transport.runtime.dispatch.await_args
    assert call.args[0:2] == ("999", "ship it")
    context = call.kwargs["context"]
    assert context.workspace_id == "phone"
    assert context.workspace is transport.telegram.workspace
    assert context.policy is transport.telegram.policy
    assert context.include_global_messages is True


async def test_message_with_invalid_workspace_binding_fails_closed(tmp_path):
    config = _config(str(tmp_path / "workspace"))
    config["transports"]["telegram"]["workspace"] = "missing"
    runtime = _runtime(config)
    transport = TelegramTransport(runtime)
    message = _msg(text="ship it")

    await transport._handle_message(_update(message=message), None)

    message.reply_text.assert_awaited_once_with(CONFIG_ERROR_REPLY)
    runtime.dispatch.assert_not_awaited()


def test_unusable_default_provider_keeps_config_binding_for_repair_commands(
    tmp_path,
    monkeypatch,
):
    def check_provider(_workspace, _policy, provider):
        return SimpleNamespace(ok=provider == "codex", problems=())

    monkeypatch.setattr(
        "enso.transports.telegram.native_policy.check_provider",
        check_provider,
    )
    transport = _bound_transport(
        tmp_path,
        providers=["claude", "codex"],
        default_provider="claude",
    )

    context = transport._execution_context(999)

    assert context is not None
    assert transport.runtime.active_provider_by_chat == {context.chat_key: "claude"}
    assert transport._provider_usable(context) is False


async def test_message_refuses_native_unusable_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "enso.transports.telegram.native_policy.check_provider",
        lambda _workspace, _policy, provider: SimpleNamespace(
            ok=provider != "claude",
            problems=(),
        ),
    )
    transport = _bound_transport(tmp_path)
    message = _msg(text="ship it")

    await transport._handle_message(_update(message=message), None)

    message.reply_text.assert_awaited_once_with(CONFIG_ERROR_REPLY)
    transport.runtime.dispatch.assert_not_awaited()


def test_stored_provider_must_be_policy_allowed_and_usable(tmp_path):
    transport = _bound_transport(
        tmp_path,
        providers=["codex"],
        default_provider="codex",
    )
    workspace = transport.telegram.workspace
    execution_policy = transport.telegram.policy
    assert workspace is not None
    assert execution_policy is not None
    key = _conversation_key(999, workspace, execution_policy)
    transport.runtime.active_provider_by_chat[key] = "claude"

    context = transport._execution_context(999)

    assert context is not None
    assert transport.runtime.active_provider_by_chat[key] == "codex"


# -- Policy-controlled commands and callbacks --


@pytest.mark.parametrize("command", [item.command for item in COMMANDS])
async def test_every_direct_command_is_denied_when_policy_disallows_it(
    tmp_path,
    command,
):
    transport = _bound_transport(tmp_path, commands=[])
    message = _msg(text=f"/{command}")

    await getattr(transport, f"_cmd_{command}")(_update(message=message), None)

    message.reply_text.assert_awaited_once_with(
        f"/{command} is not available in this conversation."
    )


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("use", "use:codex"),
        ("model", "model:gpt-5"),
        ("effort", "effort:high"),
        ("clear", "clear:all"),
        ("queue", "queue:clear"),
    ],
)
async def test_stale_or_forged_callback_is_denied_by_current_policy(
    tmp_path,
    command,
    data,
):
    transport = _bound_transport(tmp_path, commands=[])
    update = _update(callback_data=data)

    await transport._handle_callback(update, None)

    update.callback_query.answer.assert_awaited_once_with()
    update.callback_query.edit_message_text.assert_awaited_once_with(
        f"/{command} is not available in this conversation."
    )


async def test_use_filters_policy_disallowed_or_native_unusable_providers(
    tmp_path,
    monkeypatch,
):
    def check_provider(_workspace, _policy, provider):
        return SimpleNamespace(ok=provider == "claude", problems=())

    monkeypatch.setattr(
        "enso.transports.telegram.native_policy.check_provider",
        check_provider,
    )
    transport = _bound_transport(
        tmp_path,
        commands=["use"],
        providers=["claude", "codex"],
    )
    message = _msg(text="/use")

    await transport._cmd_use(_update(message=message), None)

    markup = message.reply_text.await_args.kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["● claude"]

    callback_update = _update(callback_data="use:codex")
    await transport._handle_callback(callback_update, None)
    callback_update.callback_query.edit_message_text.assert_awaited_once_with(
        "Provider codex is not available here."
    )


async def test_menu_and_help_only_show_policy_allowed_commands(tmp_path):
    transport = _bound_transport(tmp_path, commands=["status", "help"])
    transport._start_background_tasks = Mock()
    bot = SimpleNamespace(set_my_commands=AsyncMock())

    await transport._post_init(SimpleNamespace(bot=bot))

    menu = bot.set_my_commands.await_args.args[0]
    assert [command.command for command in menu] == ["status", "help"]

    message = _msg(text="/help")
    await transport._cmd_help(_update(message=message), None)
    help_text = message.reply_text.await_args.args[0]
    assert "/status" in help_text
    assert "/help" in help_text
    assert "/use" not in help_text


async def test_help_and_logs_work_while_native_provider_is_unusable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "enso.transports.telegram.native_policy.check_provider",
        lambda _workspace, _policy, _provider: SimpleNamespace(ok=False, problems=()),
    )
    monkeypatch.setattr("enso.transports.telegram.cmd_logs", lambda: "recent logs")
    transport = _bound_transport(tmp_path, commands=["help", "logs"])
    help_message = _msg(text="/help")
    logs_message = _msg(text="/logs")

    await transport._cmd_help(_update(message=help_message), None)
    await transport._cmd_logs(_update(message=logs_message), None)

    assert "/help" in help_message.reply_text.await_args.args[0]
    assert "/logs" in help_message.reply_text.await_args.args[0]
    logs_message.reply_text.assert_awaited_once_with("recent logs")


async def test_forged_clear_callback_scope_does_not_clear(tmp_path, monkeypatch):
    clear = Mock(return_value=[])
    monkeypatch.setattr("enso.transports.telegram.cmd_clear", clear)
    transport = _bound_transport(tmp_path)
    update = _update(callback_data="clear:forged")

    await transport._handle_callback(update, None)

    update.callback_query.answer.assert_awaited_once_with()
    clear.assert_not_called()


async def test_direct_clear_passes_complete_context(tmp_path, monkeypatch):
    clear = Mock(return_value=[])
    monkeypatch.setattr("enso.transports.telegram.cmd_clear", clear)
    transport = _bound_transport(tmp_path)
    message = _msg(text="/clear all")

    await transport._cmd_clear(_update(message=message), None)

    clear.assert_called_once()
    call = clear.call_args
    context = call.kwargs["context"]
    assert call.args == (transport.runtime, context.chat_key)
    assert call.kwargs["clear_all"] is True
    assert context.workspace_id == "phone"
    assert context.include_global_messages is True


async def test_clear_callback_passes_complete_context(tmp_path, monkeypatch):
    clear = Mock(return_value=["Claude: cleared"])
    monkeypatch.setattr("enso.transports.telegram.cmd_clear", clear)
    transport = _bound_transport(tmp_path)
    update = _update(callback_data="clear:current")

    await transport._handle_callback(update, None)

    context = clear.call_args.kwargs["context"]
    assert clear.call_args.args == (transport.runtime, context.chat_key)
    assert context.workspace_id == "phone"
    assert context.include_global_messages is True


async def test_compact_passes_complete_context(tmp_path, monkeypatch):
    compact = AsyncMock(return_value="Compacted.")
    monkeypatch.setattr("enso.transports.telegram.cmd_compact_async", compact)
    transport = _bound_transport(tmp_path)
    message = _msg(text="/compact")

    await transport._cmd_compact(_update(message=message), None)

    context = compact.await_args.kwargs["context"]
    assert compact.await_args.args == (transport.runtime, context.chat_key)
    assert context.workspace_id == "phone"
    assert context.include_global_messages is True


async def test_compact_refuses_native_unusable_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "enso.transports.telegram.native_policy.check_provider",
        lambda _workspace, _policy, _provider: SimpleNamespace(ok=False, problems=()),
    )
    compact = AsyncMock(return_value="Compacted.")
    monkeypatch.setattr("enso.transports.telegram.cmd_compact_async", compact)
    transport = _bound_transport(tmp_path)
    message = _msg(text="/compact")

    await transport._cmd_compact(_update(message=message), None)

    message.reply_text.assert_awaited_once_with(CONFIG_ERROR_REPLY)
    compact.assert_not_awaited()


# -- Workspace-scoped retained uploads --


async def test_uploads_use_unique_retained_directories_in_workspace(tmp_path):
    transport = _bound_transport(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for caption in ("first", "second"):
        download = AsyncMock(return_value=bytearray(b"hello"))
        document = SimpleNamespace(
            file_name="notes.txt",
            file_size=5,
            get_file=AsyncMock(
                return_value=SimpleNamespace(download_as_bytearray=download),
            ),
        )
        message = _msg(document=document, caption=caption)
        await transport._handle_file_message(_update(message=message), None)

    destinations = sorted(
        path for path in (workspace / "uploads").rglob("*") if path.is_file()
    )
    assert len(destinations) == 2
    assert destinations[0].parent != destinations[1].parent
    assert all(path.read_bytes() == b"hello" for path in destinations)
    assert transport.runtime.dispatch.await_count == 2
    for call in transport.runtime.dispatch.await_args_list:
        context = call.kwargs["context"]
        assert context.workspace_id == "phone"
        assert context.include_global_messages is True


async def test_uploads_parent_symlink_is_rejected(tmp_path):
    transport = _bound_transport(tmp_path)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "uploads").symlink_to(outside, target_is_directory=True)
    download = AsyncMock(return_value=bytearray(b"secret"))
    document = SimpleNamespace(
        file_name="notes.txt",
        file_size=6,
        get_file=AsyncMock(
            return_value=SimpleNamespace(download_as_bytearray=download),
        ),
    )
    message = _msg(document=document)

    await transport._handle_file_message(_update(message=message), None)

    document.get_file.assert_not_awaited()
    download.assert_not_awaited()
    transport.runtime.dispatch.assert_not_awaited()
    assert list(outside.iterdir()) == []
    message.reply_text.assert_awaited_once_with(
        "Failed to prepare file upload. Please try again."
    )


async def test_upload_filename_symlink_is_not_followed(tmp_path, monkeypatch):
    transport = _bound_transport(tmp_path)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    outside_file = outside / "target.txt"
    outside_file.write_bytes(b"original")
    monkeypatch.setattr(
        "enso.transports.telegram.uuid.uuid4",
        lambda: SimpleNamespace(hex="fixed-turn"),
    )

    async def poison_destination() -> bytearray:
        destination = workspace / "uploads" / "fixed-turn" / "notes.txt"
        destination.symlink_to(outside_file)
        return bytearray(b"replacement")

    document = SimpleNamespace(
        file_name="notes.txt",
        file_size=11,
        get_file=AsyncMock(
            return_value=SimpleNamespace(
                download_as_bytearray=AsyncMock(side_effect=poison_destination)
            ),
        ),
    )
    message = _msg(document=document)

    await transport._handle_file_message(_update(message=message), None)

    assert outside_file.read_bytes() == b"original"
    transport.runtime.dispatch.assert_not_awaited()
    message.reply_text.assert_awaited_once_with(
        "Failed to download file. Please try again."
    )


async def test_swapped_upload_turn_directory_is_not_trusted(tmp_path, monkeypatch):
    transport = _bound_transport(tmp_path)
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setattr(
        "enso.transports.telegram.uuid.uuid4",
        lambda: SimpleNamespace(hex="fixed-turn"),
    )

    async def swap_turn_directory() -> bytearray:
        turn = workspace / "uploads" / "fixed-turn"
        moved = workspace / "uploads" / "moved-turn"
        turn.rename(moved)
        turn.symlink_to(outside, target_is_directory=True)
        return bytearray(b"secret")

    document = SimpleNamespace(
        file_name="notes.txt",
        file_size=6,
        get_file=AsyncMock(
            return_value=SimpleNamespace(
                download_as_bytearray=AsyncMock(side_effect=swap_turn_directory)
            ),
        ),
    )
    message = _msg(document=document)

    await transport._handle_file_message(_update(message=message), None)

    assert list(outside.iterdir()) == []
    assert not (workspace / "uploads" / "moved-turn" / "notes.txt").exists()
    transport.runtime.dispatch.assert_not_awaited()
    message.reply_text.assert_awaited_once_with(
        "Failed to download file. Please try again."
    )


async def test_downloaded_payload_size_is_capped(tmp_path, monkeypatch):
    transport = _bound_transport(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr("enso.transports.telegram.MAX_FILE_SIZE", 4)
    download = AsyncMock(return_value=bytearray(b"12345"))
    document = SimpleNamespace(
        file_name="notes.txt",
        file_size=1,
        get_file=AsyncMock(
            return_value=SimpleNamespace(download_as_bytearray=download),
        ),
    )
    message = _msg(document=document)

    await transport._handle_file_message(_update(message=message), None)

    transport.runtime.dispatch.assert_not_awaited()
    assert not any(path.is_file() for path in (workspace / "uploads").rglob("*"))
    assert message.reply_text.await_args.args[0].startswith("File too large")


async def test_invalid_binding_downloads_nothing(tmp_path):
    config = _config(str(tmp_path / "workspace"))
    config["transports"]["telegram"]["workspace"] = "missing"
    runtime = _runtime(config)
    transport = TelegramTransport(runtime)
    document = SimpleNamespace(
        file_name="notes.txt",
        file_size=5,
        get_file=AsyncMock(),
    )
    message = _msg(document=document)

    await transport._handle_file_message(_update(message=message), None)

    document.get_file.assert_not_awaited()
    runtime.dispatch.assert_not_awaited()
    assert not (tmp_path / "workspace" / "uploads").exists()
    message.reply_text.assert_awaited_once_with(CONFIG_ERROR_REPLY)


async def test_native_unusable_provider_downloads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "enso.transports.telegram.native_policy.check_provider",
        lambda _workspace, _policy, _provider: SimpleNamespace(ok=False, problems=()),
    )
    transport = _bound_transport(tmp_path)
    document = SimpleNamespace(
        file_name="notes.txt",
        file_size=5,
        get_file=AsyncMock(),
    )
    message = _msg(document=document)

    await transport._handle_file_message(_update(message=message), None)

    document.get_file.assert_not_awaited()
    transport.runtime.dispatch.assert_not_awaited()
    assert not (tmp_path / "workspace" / "uploads").exists()
    message.reply_text.assert_awaited_once_with(CONFIG_ERROR_REPLY)
