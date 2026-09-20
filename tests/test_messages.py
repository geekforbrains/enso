"""Outbox rules: who hears a background message, and the prompt it lands in."""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeReply, make_turn, write_config

from enso import db, messages
from enso.config import Paths
from enso.runtime import Runtime


def send(paths: Paths, target: str, thread: str | None = None, **fields: str) -> messages.Message:
    return messages.record(
        paths,
        workspace=fields.get("workspace", "default"),
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
        "ENSO_ORIGIN_CHANNEL",
        "ENSO_BEAT_RUN_ID",
        "ENSO_RUN_ID",
        "ENSO_JOB",
        "ENSO_TASK",
        "ENSO_TASK_DIR",
    )
    env = {**kept, **dict.fromkeys(identity, "inherited")}
    assert messages.without_identity(env) == kept


@pytest.mark.parametrize(
    ("env", "source"),
    [
        ({}, "cli"),
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
    ],
)
def test_take_background_matching(
    enso_home: Paths, row: tuple[str, str | None], turn: tuple[str, str | None], heard: bool
) -> None:
    db.initialize(enso_home)
    send(enso_home, *row)
    found = messages.take_background(enso_home, "slack", *turn, workspace="default")
    assert [m.target for m in found] == ([row[0]] if heard else [])
    # Consumed on the first read: nothing accumulates.
    assert messages.take_background(enso_home, "slack", *turn, workspace="default") == []


def test_take_background_skips_retired_sends_failures_and_other_transports(
    enso_home: Paths,
) -> None:
    db.initialize(enso_home)
    send(enso_home, "C1", source="turn:slack:C1:5.0", text="mine")
    send(enso_home, "C1", status="failed", text="lost")
    send(enso_home, "C1", transport="telegram", text="elsewhere")
    send(enso_home, "C1", source="job:nightly", text="digest")
    messages.consume_own(enso_home, "turn:slack:C1:5.0", workspace="default")
    found = messages.take_background(enso_home, "slack", "C1", "5.0", workspace="default")
    assert [m.text for m in found] == ["digest"]
    unread = [m.text for m in messages.list_messages(enso_home, 10) if m.consumed_at is None]
    assert unread == ["elsewhere", "lost"]


async def test_deliver_records_success_and_failure(enso_home: Paths) -> None:
    db.initialize(enso_home)

    async def ok() -> str:
        return "9.9"

    async def boom() -> str:
        raise RuntimeError("channel_not_found")

    fields = dict(
        workspace="default", transport="slack", target="C1", thread=None, text="x", source="cli"
    )
    sent = await messages.deliver(enso_home, ok(), **fields)
    assert (sent.status, sent.message_id) == ("sent", "9.9")
    with pytest.raises(RuntimeError):
        await messages.deliver(enso_home, boom(), **fields)
    assert [m.status for m in messages.list_messages(enso_home, 10)] == ["failed", "sent"]


async def test_background_is_injected_once_and_own_sends_retire(
    runtime: Runtime, enso_home: Paths
) -> None:
    send(enso_home, "D1", source="job:nightly", text="digest ready")
    send(enso_home, "C1", source="cli", text="for a channel")

    class SendingReply(FakeReply):
        async def send(self, text):
            send(enso_home, "D1", source="turn:slack:D1", text="I already said this")
            return await super().send(text)

    first, second = SendingReply(), FakeReply()
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


async def test_a_cross_workspace_send_is_heard_by_the_workspace_it_names(
    runtime, enso_home, monkeypatch
):
    enso_home.workspace("team").mkdir()
    entered, release = asyncio.Event(), asyncio.Event()
    collect = runtime._collect

    async def held(*args):
        result = await collect(*args)
        if not entered.is_set():
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(runtime, "_collect", held)
    first, queued, rebound = FakeReply(), FakeReply(), FakeReply()
    drain = await runtime.submit(make_turn("first"), first)
    await asyncio.wait_for(entered.wait(), 5)
    assert await runtime.submit(make_turn("queued"), queued) is None
    raw = {**runtime.config.raw, "bindings": {"slack:dm:U1": "team"}}
    write_config(enso_home, raw)
    assert await runtime.submit(make_turn("rebound"), rebound) is None
    # A running default turn deliberately sends context to team, even to the same DM.
    cross = send(enso_home, "D1", workspace="team", source="turn:slack:D1", text="team result")
    send(enso_home, "D1", text="default result")
    release.set()
    await asyncio.wait_for(drain, 5)
    assert "default result" in queued.sent[-1] and "team result" not in queued.sent[-1]
    assert "team result" in rebound.sent[-1] and "default result" not in rebound.sent[-1]
    stored = next(m for m in messages.list_messages(enso_home, 20) if m.id == cross.id)
    assert stored.workspace == "team" and stored.consumed_at is not None
