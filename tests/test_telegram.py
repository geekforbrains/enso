"""Telegram authorization (private chats only), binding, files, and HTML fallback."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import ImmediateIngress

pytest.importorskip("telegram")

from telegram import Chat, Document, Message, TextQuote, User
from telegram.error import BadRequest

from enso import commands
from enso.config import Config
from enso.routing import UNBOUND_NOTICE
from enso.transports import Reply, Turn
from enso.transports.telegram import (
    TelegramReply,
    TelegramTransport,
    safe_filename,
)

OWNER = User(id=123, first_name="Gavin", last_name="V", is_bot=False)
OTHER = User(id=456, first_name="Other", is_bot=False)  # chat not bound by default
STRANGER = User(id=999, first_name="Someone", is_bot=False)
BOT = User(id=1, first_name="enso", is_bot=True)
PRIVATE = Chat(id=123, type=Chat.PRIVATE)
OTHER_PRIVATE = Chat(id=456, type=Chat.PRIVATE)
GROUP = Chat(id=-5, type=Chat.GROUP, title="Ops")


class FakeFile:
    def __init__(self, data: bytes):
        self.data = data

    async def download_to_drive(self, custom_path: Path) -> Path:
        Path(custom_path).write_bytes(self.data)
        return Path(custom_path)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.reject_html = False
        self.edit_error: BadRequest | None = None

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        if kwargs.get("parse_mode") and self.reject_html:
            raise BadRequest("Can't parse entities: unsupported start tag")
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any:
        if kwargs.get("parse_mode") and self.reject_html:
            raise self.edit_error or BadRequest("Can't parse entities: unsupported start tag")
        self.edited.append({"text": text, **kwargs})
        return SimpleNamespace(message_id=kwargs.get("message_id"))

    async def get_file(self, file_id: str) -> FakeFile:
        return FakeFile(self.files[file_id])


class FakeRuntime(ImmediateIngress):
    def __init__(self, config: Config):
        self.config = config
        self.handled: list[tuple[Turn, Reply]] = []

    async def submit(self, turn: Turn, reply: Reply) -> None:
        self.handled.append((turn, reply))


class FakeQuery:
    def __init__(self, data: str, message_id: int, user: User = OWNER, chat: Chat = PRIVATE):
        self.data = data
        self.message = SimpleNamespace(chat=chat, message_id=message_id)
        self.from_user = user
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str = "", **kwargs: Any) -> None:
        self.answers.append({"text": text, **kwargs})


def message(
    user: User = OWNER, chat: Chat = PRIVATE, message_id: int = 10, **fields: Any
) -> Message:
    return Message(
        message_id=message_id, date=datetime.now(UTC), chat=chat, from_user=user, **fields
    )


@pytest.fixture
def transport(config_both: Config) -> TelegramTransport:
    assert config_both.telegram is not None
    transport = TelegramTransport(config_both.telegram, config_both.paths)
    transport.runtime = FakeRuntime(config_both)  # type: ignore[assignment]
    transport._bot = FakeBot()  # type: ignore[assignment]
    return transport


def _runtime(transport: TelegramTransport) -> FakeRuntime:
    return transport.runtime  # type: ignore[return-value]


def _bot(transport: TelegramTransport) -> FakeBot:
    return transport._bot  # type: ignore[return-value]


def _picker_markup(transport: TelegramTransport) -> Any:
    bot = _bot(transport)
    return (bot.edited[-1] if bot.edited else bot.sent[-1])["reply_markup"]


async def _tap(transport: TelegramTransport, label: str) -> FakeQuery:
    markup = _picker_markup(transport)
    button = next(
        button for row in markup.inline_keyboard for button in row if button.text == label
    )
    query = FakeQuery(button.callback_data, transport._use_pickers[123].message_id)
    await transport.handle_callback(query)  # type: ignore[arg-type]
    return query


@pytest.mark.parametrize(
    ("msg", "reply_text"),
    [
        (message(user=STRANGER, text="hi"), None),
        (message(user=STRANGER, chat=Chat(id=999, type=Chat.PRIVATE), text="hi"), UNBOUND_NOTICE),
        (message(user=BOT, text="hi"), None),
        (message(user=None, text="hi"), None),
        # A bound user in a group is rejected outright: not even a command runs.
        (message(chat=GROUP, text="/clear"), None),
        (message(chat=Chat(id=-1005, type=Chat.SUPERGROUP), text="hi"), None),
        (message(chat=Chat(id=-1005, type=Chat.CHANNEL), text="hi"), None),
        (message(user=OTHER, chat=OTHER_PRIVATE, text="hi"), UNBOUND_NOTICE),
    ],
)
async def test_unsupported_or_unbound_never_prepares_or_dispatches(
    transport: TelegramTransport, msg: Message, reply_text: str | None, monkeypatch
) -> None:
    async def unexpected(*args, **kwargs):
        pytest.fail("rejected input reached preparation or command dispatch")

    monkeypatch.setattr(transport, "download", unexpected)
    monkeypatch.setattr(commands, "dispatch", unexpected)
    await transport.handle_message(msg)
    assert _runtime(transport).handled == []
    sent = _bot(transport).sent
    assert [m["text"] for m in sent] == ([reply_text] if reply_text else [])


@pytest.mark.parametrize("text", ["/restart", ""])
@pytest.mark.parametrize("missing_workspace", [False, True])
async def test_unavailable_telegram_binding_rejects_commands_and_files(
    transport, monkeypatch, text, missing_workspace
):
    runtime = _runtime(transport)
    if missing_workspace:
        runtime.config.paths.workspace("default").rename(runtime.config.paths.workspace("retired"))
    else:
        runtime.config = replace(runtime.config, bindings={})

    async def unexpected(*args, **kwargs):
        pytest.fail("rejected input downloaded an attachment or ran a command")

    monkeypatch.setattr(transport, "download", unexpected)
    monkeypatch.setattr(commands, "dispatch", unexpected)
    await transport.handle_message(
        message(caption=text, document=Document("f1", "u1", file_name="notes.txt"))
    )
    assert [m["text"] for m in _bot(transport).sent] == [UNBOUND_NOTICE]
    assert runtime.handled == []
    if missing_workspace:
        assert not runtime.config.paths.workspace("default").exists()


@pytest.mark.parametrize("workspace", ["default", "personal"])
async def test_telegram_user_binding_selects_shared_or_personal_context(transport, workspace):
    runtime = _runtime(transport)
    runtime.config.paths.workspace(workspace).mkdir(exist_ok=True)
    runtime.config = replace(
        runtime.config, bindings={**runtime.config.bindings, "telegram:456": workspace}
    )
    await transport.handle_message(message(text="first"))
    await transport.handle_message(message(user=OTHER, chat=OTHER_PRIVATE, text="second"))
    assert [(turn.user_id, turn.channel, turn.workspace) for turn, _ in runtime.handled] == [
        ("123", "123", "default"),
        ("456", "456", workspace),
    ]


async def test_removed_binding_drops_deferred_telegram_attachment(transport, monkeypatch):
    runtime = _runtime(transport)

    async def defer(conversation, reply, text, prepare, *, message_id=None):
        runtime.config = replace(runtime.config, bindings={})
        assert await prepare() is None

    async def unexpected(*args, **kwargs):
        pytest.fail("removed binding reached attachment download")

    monkeypatch.setattr(runtime, "defer", defer)
    monkeypatch.setattr(transport, "download", unexpected)
    await transport.handle_message(message(document=Document("f1", "u1", file_name="notes.txt")))
    assert [m["text"] for m in _bot(transport).sent] == [UNBOUND_NOTICE]
    assert runtime.handled == []


async def test_text_turn_with_reply_context(transport: TelegramTransport) -> None:
    quoted = message(user=BOT, message_id=9, text="The tests pass now.")
    await transport.handle_message(message(text="  ship it  ", reply_to_message=quoted))
    (turn, reply), *_ = _runtime(transport).handled
    assert (turn.transport, turn.channel, turn.thread) == ("telegram", "123", None)
    assert turn.is_dm
    assert (turn.user_id, turn.user_name, turn.text) == ("123", "Gavin V", "ship it")
    assert turn.context == "[Replying to assistant: The tests pass now.]"
    partial = message(text="why?", reply_to_message=quoted, quote=TextQuote("pass", 10))
    await transport.handle_message(partial)
    assert _runtime(transport).handled[-1][0].context == "[Replying to assistant: pass]"
    assert reply.origin_env()["ENSO_ORIGIN_CHANNEL"] == "123"
    assert reply.origin_env()["ENSO_ORIGIN_CHANNEL_NAME"] == "dm"
    assert reply.origin_env()["ENSO_ORIGIN_USER_NAME"] == "Gavin V"

    await reply.send("**done**")
    sent = _bot(transport).sent[-1]
    assert sent["text"] == "<b>done</b>" and sent["parse_mode"] == "HTML"
    assert sent["reply_parameters"].message_id == 10


async def test_document_is_downloaded_into_workspace_uploads(
    transport: TelegramTransport, config_both: Config
) -> None:
    _bot(transport).files["f1"] = b"hello"
    document = Document("f1", "u1", file_name="../notes.txt", file_size=5)
    await transport.handle_message(message(document=document, caption="read this"))
    (turn, _), *_ = _runtime(transport).handled
    assert turn.text == "read this"
    (path,) = turn.files
    assert Path(path).read_bytes() == b"hello"
    assert Path(path).parent.parent == config_both.paths.workspace("default") / "uploads"
    assert Path(path).name == "notes.txt"
    # The turn runs where its upload landed, whatever the binding becomes meanwhile.
    assert turn.workspace == "default"


@pytest.mark.parametrize(
    ("buttons", "spec"),
    [
        (("Provider", "codex", "sol", "medium"), "codex:sol:medium"),
        (("Model", "sonnet", "high"), "claude:sonnet:high"),
        (("Effort", "low"), "claude:opus:low"),
        (("Default",), "default"),
    ],
)
async def test_use_picker_applies_selected_triple(
    transport: TelegramTransport,
    monkeypatch: pytest.MonkeyPatch,
    buttons: tuple[str, ...],
    spec: str,
) -> None:
    selections: list[str] = []

    async def apply(rt, command, *, conversation, binding, workspace, transport):
        assert conversation == "telegram:123"
        assert binding == "telegram:123"
        assert workspace == "default"
        assert transport == "telegram"
        selections.append(command.args)
        return commands.Result("Selection applied.")

    monkeypatch.setattr(commands, "run", apply)
    await transport.handle_message(message(text="/use"))
    assert _bot(transport).sent[-1]["text"].startswith("Current: claude:opus:xhigh")
    assert _runtime(transport).handled == []
    for label in buttons:
        query = await _tap(transport, label)
        assert query.answers == [{"text": ""}]
    assert selections == [spec]
    assert _bot(transport).edited[-1]["text"] == "Selection applied."
    assert 123 not in transport._use_pickers


async def test_use_picker_back_cancel_and_old_buttons(
    transport: TelegramTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected(*args, **kwargs):
        pytest.fail("cancelled picker changed the agent")

    monkeypatch.setattr(commands, "run", unexpected)
    await transport.handle_message(message(text="/use"))
    old_data = _picker_markup(transport).inline_keyboard[0][0].callback_data
    await _tap(transport, "Provider")
    await _tap(transport, "Back")
    stale = FakeQuery(old_data, transport._use_pickers[123].message_id)
    await transport.handle_callback(stale)  # type: ignore[arg-type]
    assert "expired" in stale.answers[0]["text"]
    await _tap(transport, "Cancel")
    assert _bot(transport).edited[-1]["text"] == "Selection cancelled."
    assert 123 not in transport._use_pickers


async def test_use_picker_rechecks_binding_and_expiry(
    transport: TelegramTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected(*args, **kwargs):
        pytest.fail("invalid picker changed the agent")

    monkeypatch.setattr(commands, "run", unexpected)
    await transport.handle_message(message(text="/use"))
    data = _picker_markup(transport).inline_keyboard[0][0].callback_data
    message_id = transport._use_pickers[123].message_id
    forged = FakeQuery(data, message_id, user=OTHER)
    await transport.handle_callback(forged)  # type: ignore[arg-type]
    assert forged.answers == [{"text": ""}]
    assert _bot(transport).edited == []

    runtime = _runtime(transport)
    runtime.config = replace(runtime.config, bindings={})
    unbound = FakeQuery(data, message_id)
    await transport.handle_callback(unbound)  # type: ignore[arg-type]
    assert "changed workspace" in unbound.answers[0]["text"]
    assert 123 not in transport._use_pickers

    expired = FakeQuery(data, message_id)
    await transport.handle_callback(expired)  # type: ignore[arg-type]
    assert "expired" in expired.answers[0]["text"]


async def test_use_picker_uses_configured_models_and_effective_efforts(
    transport: TelegramTransport,
) -> None:
    await transport.handle_message(message(text="/use"))
    await _tap(transport, "Model")
    model_buttons = [
        button.text for row in _picker_markup(transport).inline_keyboard for button in row
    ]
    assert model_buttons == ["opus", "sonnet", "haiku", "Back", "Cancel"]
    await _tap(transport, "haiku")
    effort_buttons = [
        button.text for row in _picker_markup(transport).inline_keyboard for button in row
    ]
    assert effort_buttons == ["low", "medium", "high", "Back", "Cancel"]
    for row in _picker_markup(transport).inline_keyboard:
        for button in row:
            assert len(button.callback_data) <= 64


async def test_use_picker_keeps_long_model_id_out_of_callback(transport: TelegramTransport) -> None:
    runtime = _runtime(transport)
    model = "openrouter/" + "long-model-name/" * 7 + ":free"
    claude = runtime.config.providers["claude"]
    runtime.config = replace(
        runtime.config,
        providers={**runtime.config.providers, "claude": replace(claude, models=("opus", model))},
    )
    await transport.handle_message(message(text="/use"))
    await _tap(transport, "Model")
    button = _picker_markup(transport).inline_keyboard[1][0]
    assert model not in button.callback_data
    assert len(button.callback_data) <= 64
    query = FakeQuery(button.callback_data, transport._use_pickers[123].message_id)
    await transport.handle_callback(query)  # type: ignore[arg-type]
    assert transport._use_pickers[123].model == model


async def test_oversized_file_is_refused(transport: TelegramTransport) -> None:
    document = Document("f2", "u2", file_name="big.bin", file_size=21 * 1024 * 1024)
    await transport.handle_message(message(document=document))
    assert _runtime(transport).handled == []
    assert _bot(transport).sent[-1]["text"].startswith("File too large")


async def test_html_rejection_falls_back_to_plain_text() -> None:
    bot = FakeBot()
    bot.reject_html = True
    reply = TelegramReply(bot, 123)  # type: ignore[arg-type]
    assert await reply.send("*hi*") == "1"
    assert bot.sent == [{"chat_id": 123, "text": "*hi*", "reply_parameters": None}]


async def test_edit_html_rejection_falls_back_to_plain_text(
    transport: TelegramTransport,
) -> None:
    bot = _bot(transport)
    bot.reject_html = True
    await transport.edit("123", "7", "*hi*")
    assert bot.edited == [{"text": "*hi*", "chat_id": 123, "message_id": 7}]


async def test_edit_non_parse_bad_request_propagates(transport: TelegramTransport) -> None:
    bot = _bot(transport)
    bot.reject_html = True
    bot.edit_error = BadRequest("Message to edit not found")
    with pytest.raises(BadRequest):
        await transport.edit("123", "7", "*hi*")
    assert bot.edited == []


def test_safe_filename() -> None:
    assert safe_filename("../.hidden/report final.pdf") == "report_final.pdf"
    assert safe_filename("...") == ""
