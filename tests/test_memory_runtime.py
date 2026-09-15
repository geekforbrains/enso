"""Memory captures clean chat boundaries without changing provider or delivery behavior."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from conftest import FakeReply, FakeSlack, make_turn, script

from enso import db, memory
from enso.outbound import OutboundMessage
from enso.runtime import Runtime


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict], list[dict]]:
    """Observe the persistence boundary; memory core tests cover its database behavior."""
    started: list[dict] = []
    finished: list[dict] = []

    def start(_paths, **fields):
        started.append(fields)
        return len(started)

    def finish(_paths, turn_id, **fields):
        finished.append({"id": turn_id, **fields})

    monkeypatch.setattr(memory, "capture_enabled", lambda _config, _workspace: True)
    monkeypatch.setattr(memory, "start_turn", start)
    monkeypatch.setattr(memory, "finish_turn", finish)
    return started, finished


async def test_capture_keeps_original_text_origin_time_and_final_response(
    runtime, capture, tmp_path, monkeypatch
):
    script(tmp_path, monkeypatch, "I paused the deployment.")
    turn = replace(
        make_turn("[Shared message]\nprevious discussion\n\nPause deployment", thread="8.0"),
        original_text="Pause deployment",
        context="[Thread history]\nold messages",
        files=["/workspace/uploads/plan.md"],
        channel_name="dm",
        received_at="2026-09-15T12:30:00.123456+00:00",
    )
    await runtime.handle(turn, FakeReply())
    started, finished = capture
    assert started == [
        {
            "conversation": "slack:D1",
            "workspace": "default",
            "provider": "claude",
            "model": "opus",
            "effort": "xhigh",
            "transport": "slack",
            "channel": "D1",
            "channel_name": "dm",
            "thread": "8.0",
            "message_id": "1.0",
            "user_id": "U1",
            "user_name": "gavin",
            "request": "Pause deployment",
            "files": ["/workspace/uploads/plan.md"],
            "received_at": "2026-09-15T12:30:00.123456+00:00",
        }
    ]
    session = db.get_session(runtime.paths, "slack:D1", "claude")
    assert session is not None
    assert finished == [
        {
            "id": 1,
            "response": "I paused the deployment.",
            "status": "ok",
            "error": "",
            "session_id": session.session_id,
        }
    ]


async def test_rich_format_repair_is_one_clean_exchange(runtime, capture, tmp_path, monkeypatch):
    class RichReply(FakeReply):
        rich_format = True

        async def send_rich(self, message: OutboundMessage) -> str:
            return await self.send(message.fallback_text)

    script(
        tmp_path,
        monkeypatch,
        '```enso-message\n{"version":1,"fallback_text":"A: 1","blocks":[]}\n```',
        '```enso-message\n{"version":1,"fallback_text":"A: 1",'
        '"blocks":[{"type":"table","rows":[["A",1]]}]}\n```',
    )
    reply = RichReply()
    await runtime.handle(make_turn("Show a table"), reply)
    started, finished = capture
    assert len(started) == len(finished) == 1
    assert started[0]["request"] == "Show a table"
    assert finished[0]["response"] == "A: 1"
    assert finished[0]["status"] == "ok"
    assert reply.sent == ["A: 1"]


async def test_completed_turns_are_available_to_refinement_with_their_own_origin(
    runtime, tmp_path, monkeypatch
):
    script(tmp_path, monkeypatch, "Deployment paused.", "The branch is ready.")
    await runtime.handle(make_turn("Pause deployment", thread="8.0"), FakeReply())
    await runtime.handle(
        replace(make_turn("Check the branch", thread="9.0"), message_id="2.0"), FakeReply()
    )
    batch = memory.prepare_batch(runtime.paths, batch_id="capture-integration")
    assert batch is not None
    first, second = batch.turns
    assert (first.request, first.response, first.thread) == (
        "Pause deployment",
        "Deployment paused.",
        "8.0",
    )
    assert (second.request, second.response, second.thread) == (
        "Check the branch",
        "The branch is ready.",
        "9.0",
    )
    assert first.session_id == second.session_id
    assert first.session_id is not None
    assert first.status == second.status == "ok"
    assert first.received_at <= first.completed_at <= second.received_at <= second.completed_at


@pytest.mark.parametrize("launch_failure", [False, True])
async def test_provider_failure_closes_capture(runtime, capture, monkeypatch, launch_failure):
    if launch_failure:
        monkeypatch.setenv("FAKE_FAIL", "1")
    await runtime.handle(make_turn("fail please"), FakeReply())
    _, finished = capture
    assert len(finished) == 1
    assert finished[0]["status"] == "error"
    assert finished[0]["response"] == ""
    assert "fake:" in finished[0]["error"]
    assert bool(finished[0]["session_id"]) is not launch_failure


async def test_timeout_closes_capture(runtime, capture):
    runtime = Runtime(replace(runtime.config, agent_timeout=1))
    await asyncio.wait_for(runtime.handle(make_turn("sleep 30"), FakeReply()), timeout=5)
    _, finished = capture
    assert len(finished) == 1
    assert finished[0]["status"] == "timeout"
    assert finished[0]["response"] == ""
    assert "1-second timeout" in finished[0]["error"]


async def test_stop_closes_capture_and_keeps_persisted_session(runtime, capture, monkeypatch):
    provider_started = asyncio.Event()
    original = runtime._run_provider

    async def run_provider(*args, **kwargs):
        async for event in original(*args, **kwargs):
            provider_started.set()
            yield event

    monkeypatch.setattr(runtime, "_run_provider", run_provider)
    running = asyncio.create_task(runtime.handle(make_turn("sleep 30"), FakeReply()))
    await asyncio.wait_for(provider_started.wait(), timeout=5)
    await runtime.stop("slack:D1")
    await asyncio.wait_for(running, timeout=5)
    _, finished = capture
    assert len(finished) == 1
    assert finished[0]["status"] == "stopped"
    assert finished[0]["response"] == ""
    assert finished[0]["session_id"]


async def test_stop_during_capture_start_still_finalizes_inserted_source(
    runtime, capture, monkeypatch
):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def start(_paths, **_fields):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return 31

    monkeypatch.setattr(memory, "start_turn", start)
    running = asyncio.create_task(runtime.handle(make_turn("hello"), FakeReply()))
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        state = runtime.running("slack:D1")
        assert state is not None and state.task is not None
        state.stopping = True
        state.task.cancel()
        release.set()
        await asyncio.wait_for(running, timeout=5)
    finally:
        release.set()
    _, finished = capture
    assert len(finished) == 1
    assert finished[0]["id"] == 31
    assert finished[0]["status"] == "stopped"
    assert db.get_sessions(runtime.paths, "slack:D1") == []


async def test_delivery_failure_preserves_generated_response(
    runtime, capture, tmp_path, monkeypatch
):
    class FailedReply(FakeReply):
        async def send(self, text: str) -> str:
            raise OSError("delivery unavailable")

    script(tmp_path, monkeypatch, "I saved the requested file.")
    await runtime.handle(make_turn("Save it"), FailedReply())
    _, finished = capture
    assert len(finished) == 1
    assert finished[0]["response"] == "I saved the requested file."
    assert finished[0]["status"] == "error"
    assert finished[0]["error"] == "delivery unavailable"


@pytest.mark.parametrize("function", ["start_turn", "finish_turn"])
async def test_capture_write_failure_does_not_break_chat(
    runtime, capture, tmp_path, monkeypatch, caplog, function
):
    def fail(*_args, **_kwargs):
        raise OSError("database unavailable")

    script(tmp_path, monkeypatch, "Done.")
    monkeypatch.setattr(memory, function, fail)
    reply = FakeReply()
    await runtime.handle(make_turn("hello"), reply)
    assert reply.sent == ["Done."]
    assert sum("Memory" in record.message for record in caplog.records) == 1


async def test_duplicate_source_is_not_finished_again(runtime, capture, monkeypatch):
    monkeypatch.setattr(memory, "start_turn", lambda _paths, **_fields: None)
    await runtime.handle(make_turn("hello"), FakeReply())
    assert capture == ([], [])


async def test_disabled_capture_leaves_conversation_unrecorded(runtime, capture, monkeypatch):
    monkeypatch.setattr(memory, "capture_enabled", lambda _config, _workspace: False)
    await runtime.handle(make_turn("hello"), FakeReply())
    assert capture == ([], [])


@pytest.mark.parametrize("platform", ["slack", "telegram"])
async def test_transport_receipt_time_precedes_deferred_preparation(runtime, monkeypatch, platform):
    stamp = "2026-09-15T12:30:00.123456+00:00"
    received_at = stamp
    captured = []

    async def defer(_conversation, _reply, _text, prepare):
        nonlocal stamp
        stamp = "2026-09-15T12:35:00.000000+00:00"
        prepared = await prepare()
        assert prepared is not None
        captured.append(prepared[0])

    monkeypatch.setattr(db, "now", lambda: stamp)
    monkeypatch.setattr(runtime, "defer", defer)
    if platform == "slack":
        from enso.transports.slack import SlackTransport

        transport = SlackTransport(runtime.config.slack, runtime.paths)
        transport.runtime = runtime
        transport._client = FakeSlack()
        transport._users["U1"] = "Gavin"
        await transport._handle_event(
            {"channel": "D1", "ts": "1.0", "user": "U1", "text": "hello"}, mentioned=False
        )
    else:
        from telegram import Chat, Message, User

        from enso.transports.telegram import TelegramTransport

        transport = TelegramTransport(runtime.config.telegram, runtime.paths)
        transport.runtime = runtime
        transport._bot = object()  # preparation of plain text needs no Bot API call
        await transport.handle_message(
            Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=123, type=Chat.PRIVATE),
                from_user=User(id=123, first_name="Gavin", is_bot=False),
                text="hello",
            )
        )
    assert len(captured) == 1
    assert captured[0].received_at == received_at
