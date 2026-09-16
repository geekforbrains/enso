"""Outbox rules: who hears a background message, and the prompt it lands in."""

from __future__ import annotations

import pytest
from conftest import FakeReply, make_turn

from enso import db, messages
from enso.config import Paths
from enso.runtime import Runtime


def send(paths: Paths, target: str, thread: str | None = None, **fields: str) -> messages.Message:
    return messages.record(
        paths,
        transport=fields.get("transport", "slack"),
        target=target,
        thread=thread,
        text=fields.get("text", "hi"),
        source=fields.get("source", "cli"),
        status=fields.get("status", "sent"),
        message_id="1.0",
    )


def test_without_identity_drops_every_caller_variable_and_keeps_the_rest() -> None:
    kept = {"ENSO_HOME": "/home", "ENSO_WORKSPACE": "default", "PATH": "/bin", "TOKEN": "s"}
    identity = (
        "ENSO_ORIGIN_TRANSPORT",
        "ENSO_ORIGIN_USER_ID",
        "ENSO_ORIGIN_USER_NAME",
        "ENSO_ORIGIN_CHANNEL",
        "ENSO_ORIGIN_CHANNEL_NAME",
        "ENSO_ORIGIN_THREAD_TS",
        "ENSO_JOB",
        "ENSO_RUN_ID",
        "ENSO_TASK",
        "ENSO_TASK_DIR",
        "ENSO_BEAT",
        "ENSO_BEAT_RUN_ID",
        "ENSO_BEAT_CHECKPOINT",
        "ENSO_RUN_STATUS",
        "ENSO_RUN_EXIT_CODE",
        "ENSO_RUN_DURATION_MS",
        "ENSO_RUN_ATTEMPT",
        "ENSO_RUN_FOLLOWUPS_REMAINING",
    )
    env = {**kept, **dict.fromkeys(identity, "inherited")}
    assert messages.without_identity(env) == kept


@pytest.mark.parametrize(
    ("env", "source"),
    [
        ({}, "cli"),
        ({"ENSO_BEAT": "HB-001"}, "beat:HB-001"),
        (
            {"ENSO_BEAT": "HB-001", "ENSO_JOB": "nightly",
             "ENSO_ORIGIN_TRANSPORT": "slack", "ENSO_ORIGIN_CHANNEL": "C1"},
            "beat:HB-001",
        ),
        ({"ENSO_JOB": "nightly", "ENSO_ORIGIN_TRANSPORT": "slack"}, "job:nightly"),
        (
            {"ENSO_ORIGIN_TRANSPORT": "slack", "ENSO_ORIGIN_CHANNEL": "C1",
             "ENSO_ORIGIN_THREAD_TS": "1.0", "ENSO_ORIGIN_CHANNEL_NAME": "#general"},
            "turn:slack:C1:1.0",
        ),
        # A DM thread reply still belongs to the DM's single conversation.
        (
            {"ENSO_ORIGIN_TRANSPORT": "slack", "ENSO_ORIGIN_CHANNEL": "D1",
             "ENSO_ORIGIN_THREAD_TS": "1.0", "ENSO_ORIGIN_CHANNEL_NAME": "dm"},
            "turn:slack:D1",
        ),
        (
            {"ENSO_ORIGIN_TRANSPORT": "telegram", "ENSO_ORIGIN_CHANNEL": "123"},
            "turn:telegram:123",
        ),
    ],
)  # fmt: skip
def test_source_from_env(env: dict[str, str], source: str) -> None:
    assert messages.source_from_env(env) == source


@pytest.mark.parametrize(
    ("row", "turn", "heard"),
    [
        # (target, thread) of the send  →  (target, thread) of the turn
        (("C1", None), ("C1", "5.0"), True),  # channel-level send reaches any thread there
        (("C1", "5.0"), ("C1", "5.0"), True),  # a threaded send reaches that thread
        (("C1", "5.0"), ("C1", "6.0"), False),  # ...and only that thread
        (("C1", "5.0"), ("C2", "5.0"), False),
        (("D1", "5.0"), ("D1", None), True),  # a DM hears everything sent to it
        (("D1", None), ("D1", None), True),
        (("D1", None), ("D2", None), False),
    ],
)
def test_take_background_matching(
    enso_home: Paths, row: tuple[str, str | None], turn: tuple[str, str | None], heard: bool
) -> None:
    db.initialize(enso_home)
    send(enso_home, *row)
    found = messages.take_background(enso_home, "slack", *turn, exclude_source="turn:x")
    assert [m.target for m in found] == ([row[0]] if heard else [])
    # Consumed on the first read: nothing accumulates.
    assert messages.take_background(enso_home, "slack", *turn, exclude_source="turn:x") == []


def test_take_background_skips_own_sends_failures_and_other_transports(enso_home: Paths) -> None:
    db.initialize(enso_home)
    send(enso_home, "C1", source="turn:slack:C1:5.0", text="mine")
    send(enso_home, "C1", status="failed", text="lost")
    send(enso_home, "C1", transport="telegram", text="elsewhere")
    send(enso_home, "C1", source="job:nightly", text="digest")
    found = messages.take_background(
        enso_home, "slack", "C1", "5.0", exclude_source="turn:slack:C1:5.0"
    )
    assert [m.text for m in found] == ["digest"]
    messages.consume_own(enso_home, "turn:slack:C1:5.0")
    unread = [m.text for m in messages.list_messages(enso_home, 10) if m.consumed_at is None]
    assert unread == ["elsewhere", "lost"]


async def test_deliver_records_success_and_failure(enso_home: Paths) -> None:
    db.initialize(enso_home)

    async def ok() -> str:
        return "9.9"

    async def boom() -> str:
        raise RuntimeError("channel_not_found")

    fields = dict(transport="slack", target="C1", thread=None, text="x", source="cli")
    sent = await messages.deliver(enso_home, ok(), **fields)
    assert (sent.status, sent.message_id) == ("sent", "9.9")
    with pytest.raises(RuntimeError):
        await messages.deliver(enso_home, boom(), **fields)
    assert [m.status for m in messages.list_messages(enso_home, 10)] == ["failed", "sent"]


async def test_background_is_injected_once_and_own_sends_retire(
    runtime: Runtime, enso_home: Paths
) -> None:
    send(enso_home, "D1", source="job:nightly", text="digest ready")
    send(enso_home, "D1", source="turn:slack:D1", text="I already said this")
    send(enso_home, "C1", source="cli", text="for a channel")
    first, second = FakeReply(), FakeReply()
    await runtime.handle(make_turn("hello"), first)
    prompt = first.sent[0]
    assert prompt.startswith("new ") and messages.HEADER in prompt
    assert "] (job:nightly) digest ready\n\nhello" in prompt
    assert "already said" not in prompt and "for a channel" not in prompt
    assert first.status[0].startswith("claude ·")
    # The turn retired its own row; the job's row was consumed; the other channel's waits.
    unread = {m.text for m in messages.list_messages(enso_home, 10) if m.consumed_at is None}
    assert unread == {"for a channel"}
    await runtime.handle(make_turn("again"), second)
    assert messages.HEADER not in second.sent[0]


def test_render_lines(enso_home: Paths) -> None:
    db.initialize(enso_home)
    row = send(enso_home, "C1", source="job:nightly", text="two\nlines")
    rendered = messages.render([row])
    assert rendered.startswith(f"{messages.HEADER}\n[") and rendered.endswith(
        "] (job:nightly) two\nlines"
    )
