"""Live capture through admission, FIFO preparation, provider outcomes, and delivery."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace

import pytest
from conftest import FakeReply, ImmediateIngress, make_turn, script
from test_slack import ENVELOPE, RecordingClient
from test_telegram import FakeBot
from test_telegram import message as telegram_message

from enso import captures, outbound
from enso.capture_runtime import CaptureWriter
from enso.runtime import Runtime
from enso.transports.slack import SlackReply, SlackTransport
from enso.transports.telegram import TelegramTransport


def source(text="original request", ident="1", **fields):
    return replace(
        captures.Message(
            "slack",
            "default",
            "slack:D1",
            "D1",
            None,
            ident,
            "U1",
            "Gavin",
            "2026-09-16T12:00:00Z",
            text,
        ),
        **fields,
    )


async def enqueue(runtime, text="original request", ident="1", *, reply=None, prepare=None):
    writer = CaptureWriter.start(runtime.paths, source(text, ident))
    reply = reply or FakeReply()

    async def normal():
        return replace(make_turn(text), message_id=ident, context="Injected history"), reply

    await runtime.defer("slack:D1", reply, text, prepare or normal, capture=writer)
    return writer, reply


async def drain(runtime):
    while tasks := [
        *(s.task for s in runtime._ingress.values() if s.task),
        *runtime._drains.values(),
    ]:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)


async def test_original_request_and_final_reply_exclude_internal_context(
    runtime, tmp_path, monkeypatch
):
    script(tmp_path, monkeypatch, "The final answer")
    sink = FakeReply()
    sink.sender_id, sink.sender_name = "B1", "Assistant"
    await enqueue(runtime, "  actual request  ", reply=sink)
    await drain(runtime)
    original, reply = captures.query(runtime.paths, "default")
    assert original.text == "  actual request  " and original.finalized
    assert reply.text == "The final answer" and reply.parent_id == original.id
    assert (reply.sender_id, reply.sender_name) == ("B1", "Assistant")
    assert (reply.outcome, reply.delivery) == ("completed", "complete")
    assert reply.parts == (captures.Part(0, len(reply.text), "sent", "1"),)
    await runtime.clear("slack:D1")
    assert captures.query(runtime.paths, "default") == (original, reply)


@pytest.mark.parametrize(
    ("prompt", "answer", "outcome"),
    [
        ("hello", "", "empty"),
        ("fail please", None, "failed"),
        ("sleep 30", None, "timed_out"),
    ],
)
async def test_unsuccessful_generation_never_captures_progress_or_diagnostics(
    runtime,
    tmp_path,
    monkeypatch,
    prompt,
    answer,
    outcome,
):
    runtime = Runtime(replace(runtime.config, agent_timeout=1))
    if answer is not None:
        script(tmp_path, monkeypatch, answer)
    await enqueue(runtime, prompt)
    await drain(runtime)
    original, reply = captures.query(runtime.paths, "default")
    assert original.outcome == reply.outcome == outcome
    assert reply.text == "" and reply.parts == () and reply.delivery == "unattempted"


@pytest.mark.parametrize(
    ("exception", "delivery"),
    [(ValueError("rejected"), "partial"), (TimeoutError("unknown"), "uncertain")],
)
async def test_delivery_records_acknowledged_parts_and_never_resends(
    runtime, tmp_path, monkeypatch, exception, delivery
):
    class PartialReply(FakeReply):
        limit = 5

        def delivery_rejected(self, error):
            return isinstance(error, ValueError)

        async def send(self, text):
            if text == "world":
                raise exception
            return await super().send(text)

    script(tmp_path, monkeypatch, "hello\nworld")
    writer, sink = await enqueue(runtime, reply=PartialReply())
    await drain(runtime)
    original, reply = captures.query(runtime.paths, "default")
    assert reply.outcome == "completed" and original.outcome == "failed"
    assert reply.delivery == delivery and reply.text == "hello\n\nworld"
    assert reply.parts[0] == captures.Part(0, 5, "sent", "1")
    assert reply.parts[1].status == ("failed" if delivery == "partial" else "uncertain")
    assert sink.sent.count("hello") == 1 and "world" not in sink.sent
    assert writer.finalized


@pytest.mark.parametrize("refuse", ["", "invalid_blocks"])
async def test_rich_reply_captures_readable_content_or_the_actual_fallback(runtime, refuse):
    writer = CaptureWriter.start(runtime.paths, source())
    assert await writer.ready()
    message = outbound.parse_outbound_message(ENVELOPE)
    sink = SlackReply(RecordingClient(refuse), "D1", None)
    writer.rejected = sink.delivery_rejected
    await writer.begin(sink.representation("", message), "completed")
    await sink.deliver("", message, writer)
    await writer.finish()
    _, reply = captures.query(runtime.paths, "default")
    assert reply.delivery == "complete" and len(reply.parts) == 1
    if refuse:
        assert reply.text == message.fallback_text
        assert reply.parts[0].message_id == "2.0"
    else:
        assert reply.text.startswith("# Report")
        assert "| Widgets | 42 |" in reply.text and "Trend (USD)" in reply.text
        assert "| Jan | 10 |" in reply.text and "| Feb | 20 |" in reply.text
    assert "enso-message" not in reply.text


@pytest.mark.parametrize("repaired", [False, True])
async def test_format_repair_does_not_become_an_input(runtime, tmp_path, monkeypatch, repaired):
    from test_runtime import RichReply

    script(
        tmp_path,
        monkeypatch,
        "```enso-message\n{\n```",
        ENVELOPE if repaired else "```enso-message\n{\n```",
    )
    await enqueue(runtime, reply=RichReply())
    await drain(runtime)
    original, reply = captures.query(runtime.paths, "default")
    assert original.text == "original request"
    if repaired:
        assert reply.text == outbound.markdown(outbound.parse_outbound_message(ENVELOPE))
        assert reply.outcome == "completed" and reply.delivery == "complete"
    else:
        assert reply.text == "" and reply.outcome == "failed"
        assert reply.delivery == "unattempted"


async def test_pending_messages_are_captured_before_preparation_and_stop_accounts_for_them(runtime):
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    first, _ = await enqueue(runtime, "first", prepare=blocked)
    await started.wait()
    second, _ = await enqueue(runtime, "second", "2")
    await second.ready()
    assert [row.text for row in captures.query(runtime.paths, "default")] == ["first", "second"]
    await runtime.stop("slack:D1")
    inputs = [row for row in captures.query(runtime.paths, "default") if row.kind == "addressed"]
    assert [(row.text, row.outcome) for row in inputs] == [
        ("first", "cancelled"),
        ("second", "dropped"),
    ]
    assert first.finalized and second.finalized


async def test_slow_capture_cannot_reorder_fifo_and_duplicate_never_prepares(runtime, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_record = captures.record

    def delayed(paths, message):
        if message.message_id == "1":
            entered.set()
            assert release.wait(timeout=5)
        return original_record(paths, message)

    order = []
    original_turn = runtime._run_turn

    async def run(conversation, turn, reply):
        order.append(turn.text)
        await original_turn(conversation, turn, reply)

    monkeypatch.setattr(captures, "record", delayed)
    monkeypatch.setattr(runtime, "_run_turn", run)
    try:
        await enqueue(runtime, "first")
        assert await asyncio.to_thread(entered.wait, 2)
        later, _ = await enqueue(runtime, "second", "2")
        await later.ready()
        assert order == []
    finally:
        release.set()
    await drain(runtime)
    assert order == ["first", "second"]
    await enqueue(runtime, "changed event snapshot", "1")
    await drain(runtime)
    assert order == ["first", "second"]
    assert len(captures.query(runtime.paths, "default")) == 4


@pytest.mark.parametrize("operation", ["record", "reply"])
async def test_capture_failure_logs_no_body_and_does_not_block_or_repeat_reply(
    runtime,
    tmp_path,
    monkeypatch,
    caplog,
    operation,
):
    script(tmp_path, monkeypatch, "Answer once")

    def fail(*args, **kwargs):
        raise OSError("PRIVATE body and xoxb-secret")

    monkeypatch.setattr(captures, operation, fail)
    with caplog.at_level(logging.WARNING):
        _, sink = await enqueue(runtime, "PRIVATE original")
        await drain(runtime)
    assert sink.sent == ["Answer once"]
    warnings = [r.getMessage() for r in caplog.records if r.name == "enso.capture_runtime"]
    assert len(warnings) == 1 and "PRIVATE" not in warnings[0] and "secret" not in warnings[0]
    rows = captures.query(runtime.paths, "default")
    assert len(rows) == (0 if operation == "record" else 1)
    if rows:
        assert not rows[0].finalized
        assert captures.recover(runtime.paths) == 1


class Admitted(ImmediateIngress):
    def __init__(self, config):
        self.config = config
        self.turns = []

    async def sessions(self, conversation):
        return []

    def busy(self, conversation):
        return False

    async def submit(self, turn, reply):
        self.turns.append(turn)


async def test_slack_ambient_capture_requires_no_history_lookup_download_or_provider(
    runtime, monkeypatch
):
    transport = SlackTransport(replace(runtime.config.slack, mention_required=True), runtime.paths)
    transport.runtime = Admitted(runtime.config)
    transport.bot_user_id = "UBOT"
    transport._users["U1"] = "Gavin"

    async def forbidden(*args, **kwargs):
        pytest.fail("ambient capture tried to fetch history, prepare files, or invoke a provider")

    monkeypatch.setattr(transport, "fetch_thread", forbidden)
    monkeypatch.setattr(transport, "download_files", forbidden)
    event = {
        "channel": "C1",
        "ts": "100.001",
        "user": "U1",
        "text": "Team discussion",
        "files": [{"id": "F1", "name": "notes.pdf", "url_private": "https://secret"}],
    }
    await transport._handle_event(event, mentioned=False)
    # A new transport instance/event-wrapper replay must still deduplicate durably.
    transport._seen.clear()
    await transport._handle_event(event, mentioned=False)
    (record,) = captures.query(runtime.paths, "default")
    assert record.kind == "ambient" and record.text == "Team discussion" and record.finalized
    assert record.sender_name == "Gavin" and record.attachments[0].status == "not_downloaded"
    assert not (runtime.paths.workspace("default") / "uploads").exists()
    assert transport.runtime.turns == []


async def test_telegram_records_caption_and_download_reference_but_not_quoted_context(runtime):
    from telegram import Document

    transport = TelegramTransport(runtime.config.telegram, runtime.paths)
    transport.runtime = Admitted(runtime.config)
    transport._bot = FakeBot()
    transport.bot.files["F1"] = b"document bytes"
    await transport.handle_message(
        telegram_message(
            caption="Read this",
            document=Document("F1", "stable1", file_name="notes.txt"),
            reply_to_message=telegram_message(message_id=9, text="Earlier discussion"),
        )
    )
    (record,) = captures.query(runtime.paths, "default")
    assert record.text == "Read this" and record.kind == "addressed"
    (attachment,) = record.attachments
    assert attachment.id == "stable1" and attachment.status == "downloaded"
    assert attachment.path.startswith("uploads/")
    assert (runtime.paths.workspace("default") / attachment.path).read_bytes() == b"document bytes"
    (turn,) = transport.runtime.turns
    assert "Earlier discussion" in turn.context and "Earlier discussion" not in record.text
    await transport.handle_message(telegram_message(text="edited replay"))
    assert len(transport.runtime.turns) == 1


async def test_stop_during_provider_execution_keeps_request_and_drops_waiting_turn(
    runtime, monkeypatch
):
    started = asyncio.Event()

    async def collecting(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_collect", collecting)
    await enqueue(runtime, "running")
    await started.wait()
    waiting, _ = await enqueue(runtime, "waiting", "2")
    await waiting.ready()
    if state := runtime._ingress.get("slack:D1"):
        await state.task
    await runtime.stop("slack:D1")
    await drain(runtime)
    rows = captures.query(runtime.paths, "default")
    assert {(r.text, r.outcome) for r in rows if r.kind == "addressed"} == {
        ("running", "cancelled"),
        ("waiting", "dropped"),
    }
    assert all(r.text == "" and r.delivery == "unattempted" for r in rows if r.kind == "reply")


@pytest.mark.parametrize("removed", [False, True])
async def test_binding_change_preserves_capture_owner_and_removal_drops_turn(runtime, removed):
    from enso.config import LiveConfig

    started, release = asyncio.Event(), asyncio.Event()

    async def preparing():
        started.set()
        await release.wait()
        return make_turn("original"), FakeReply()

    await enqueue(runtime, "original", prepare=preparing)
    await started.wait()
    runtime.paths.workspace("team").mkdir()
    runtime._live = LiveConfig(
        replace(runtime.config, bindings={} if removed else {"slack:dm:U1": "team"})
    )
    release.set()
    await drain(runtime)
    original, reply = captures.query(runtime.paths, "default")
    assert original.outcome == reply.outcome == ("dropped" if removed else "completed")
    assert captures.query(runtime.paths, "team") == ()
    # Retry in the new binding remains owned by the first workspace and does not prepare.
    writer = CaptureWriter.start(runtime.paths, source(workspace="team"))
    assert not await writer.ready()


async def test_provider_retry_yields_one_answer_and_one_capture(runtime, monkeypatch):
    from enso.providers import StreamEvent
    from enso.providers.claude import ClaudeProvider

    attempts = []

    async def run(*args, **kwargs):
        attempts.append(1)
        yield (
            StreamEvent(kind="error", text="temporary")
            if len(attempts) == 1
            else StreamEvent(kind="response", text="Recovered answer")
        )

    monkeypatch.setattr(ClaudeProvider, "retryable_error", lambda self, text: text == "temporary")
    monkeypatch.setattr(runtime, "_run_provider", run)
    await enqueue(runtime)
    await drain(runtime)
    original, reply = captures.query(runtime.paths, "default")
    assert len(attempts) == 2 and reply.text == "Recovered answer"
    assert original.outcome == reply.outcome == "completed"


async def test_interruption_between_delivery_parts_is_partial(runtime):
    writer = CaptureWriter.start(runtime.paths, source())
    await writer.ready()
    await writer.begin("first\n\nsecond", "completed")

    async def sent():
        return "known-id"

    await writer.send("first", sent)
    await writer.finish("cancelled")
    original, reply = captures.query(runtime.paths, "default")
    assert original.outcome == "cancelled"
    assert reply.outcome == "completed" and reply.delivery == "partial"
    assert reply.parts == (captures.Part(0, 5, "sent", "known-id"),)


async def test_rejected_messages_commands_and_edits_leave_no_captures(runtime):
    from telegram import User

    slack = SlackTransport(runtime.config.slack, runtime.paths)
    slack.runtime = runtime
    slack.bot_user_id = "UBOT"
    slack._client = RecordingClient()
    for index, fields in enumerate(
        (
            {"channel": "C9", "text": "<@UBOT> hello"},
            {"text": "<@UBOT> hello", "bot_id": "B1"},
            {"text": "!help"},
            {"text": "edited", "subtype": "message_changed"},
            {"text": "deleted", "subtype": "message_deleted"},
        )
    ):
        await slack._handle_event(
            {"channel": "D1", "user": "U1", "ts": f"100.{index}", **fields}, mentioned=False
        )
    telegram = TelegramTransport(runtime.config.telegram, runtime.paths)
    telegram.runtime = runtime
    telegram._bot = FakeBot()
    for msg in (
        telegram_message(text="/help"),
        telegram_message(text="/start pairing-challenge"),
        telegram_message(text="bot", user=User(123, "Bot", is_bot=True)),
    ):
        await telegram.handle_message(msg)
    assert captures.query(runtime.paths, "default") == ()
    assert not runtime.paths.workspace_memory("default").exists()


async def test_preparation_failure_and_queue_overflow_keep_explicit_outcomes(runtime):
    from enso.runtime import MAX_QUEUE

    async def fail():
        raise RuntimeError("preparation failed")

    await enqueue(runtime, "cannot prepare", prepare=fail)
    await drain(runtime)
    original, reply = captures.query(runtime.paths, "default")
    assert original.outcome == reply.outcome == "failed"

    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    await enqueue(runtime, "blocked", "2", prepare=blocked)
    await started.wait()
    for i in range(MAX_QUEUE + 1):
        writer, _ = await enqueue(runtime, f"queued {i}", str(i + 3))
    assert writer.finalized
    overflow = captures.get(runtime.paths, "default", writer.id)
    assert overflow.outcome == "dropped"
    await runtime.stop("slack:D1")


async def test_telegram_failed_attachment_is_recorded_without_copying_content(runtime):
    from telegram import Document

    transport = TelegramTransport(runtime.config.telegram, runtime.paths)
    transport.runtime = Admitted(runtime.config)
    transport._bot = FakeBot()  # missing remote file makes its ordinary download fail
    await transport.handle_message(telegram_message(document=Document("missing", "stable")))
    original, reply = captures.query(runtime.paths, "default")
    assert original.text == "" and original.attachments[0].status == "failed"
    assert original.attachments[0].path is None and reply.outcome == "dropped"
    assert transport.runtime.turns == []


async def test_capture_limits_do_not_shorten_live_input_or_output(runtime, tmp_path, monkeypatch):
    answer = "€" * 24_000
    script(tmp_path, monkeypatch, answer)
    _, sink = await enqueue(runtime, "a" * 70_000)
    await drain(runtime)
    original, reply = captures.query(runtime.paths, "default")
    assert original.truncated and len(original.text) == captures.MAX_TEXT_BYTES
    assert reply.truncated and len(reply.text.encode()) <= captures.MAX_TEXT_BYTES
    assert "".join(sink.sent) == answer
    assert reply.parts[-1].end > len(reply.text)
