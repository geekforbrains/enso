"""Native heartbeat sends reserve stable actions and retain receipts without live services."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3

import pytest
from conftest import FakeTransport, write_config
from typer.testing import CliRunner

from enso import db, heartbeat, messages
from enso.cli import app, messaging, slack
from enso.cli.common import deliver
from enso.config import load_config


@pytest.fixture
def active(enso_home, raw_config_both, monkeypatch):
    write_config(enso_home, raw_config_both)
    config = load_config(enso_home)
    db.initialize(enso_home)
    beat = heartbeat.create(
        config,
        {
            "title": "Dinner planning",
            "instructions": "Follow this dinner plan until everyone has agreed.",
            "completion": "Dinner agreed and final notification sent.",
            "allowed_actions": "Reply in the dinner thread and notify me.",
            "workspace": "default",
            "at": "2030-09-08T08:30:00+00:00",
            "notify": "slack:C3",
            "notify_thread": "7.0",
        },
    )
    heartbeat.resume(config, beat.ref)
    run = heartbeat.start_run(config, beat.ref, trigger="manual")
    monkeypatch.setenv("ENSO_BEAT", beat.ref)
    monkeypatch.setenv("ENSO_BEAT_RUN_ID", run.id)
    return config, beat, run


class Sender(FakeTransport):
    def __init__(
        self, config, beat, *, name="slack", connect_error=False, effect=None, connected=None
    ):
        super().__init__(name)
        self.config = config
        self.beat = beat
        self.connect_error = connect_error
        self.effect = effect
        self.connected = connected
        self.connections = 0
        self.writes = []

    @contextlib.asynccontextmanager
    async def connect(self):
        self.connections += 1
        latest = heartbeat.history(self.config.paths, self.beat.ref, limit=1)[0]
        assert latest.action_status == "pending"  # reservation precedes any connection
        if self.connect_error:
            raise RuntimeError("could not connect")
        if self.connected:
            self.connected()
        yield

    async def _write(self, target, thread, body, file):
        self.writes.append((target, thread, body, file))
        if self.effect:
            await self.effect()
        return "F123" if file else "100.1"

    async def send(self, target, text, *, thread=None):
        return await self._write(target, thread, text, False)

    async def send_file(self, target, path, *, caption="", thread=None):
        return await self._write(target, thread, caption, True)


@pytest.mark.parametrize(
    ("command", "to", "is_file"),
    [
        (("message", "send"), None, False),
        (("message", "attach"), None, True),
        (("message", "send"), "telegram:123", False),
        (("message", "attach"), "telegram:123", True),
        (("telegram", "send"), "123", False),
        (("telegram", "attach"), "123", True),
        (("slack", "send"), None, False),
        (("slack", "upload"), None, True),
    ],
)
def test_native_commands_record_receipts_and_refuse_duplicate_sends(
    active, monkeypatch, tmp_path, command, to, is_file
):
    config, beat, run = active
    name = "telegram" if to else "slack"
    sender = Sender(config, beat, name=name)
    monkeypatch.setattr(messaging, "_sender", lambda *a, **kw: sender)
    monkeypatch.setattr(slack, "transport", lambda *a, **kw: sender)
    attachment = tmp_path / "dinner.txt"
    attachment.write_text("Dinner details")
    args = [*command, str(attachment) if is_file else "Dinner is confirmed"]
    if to:
        args += ["--to", to]
    if command[0] == "slack":
        args += ["--channel", "C3", "--thread", "7.0"]
    args += ["--action-key", "dinner-final-notice", "--json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, (result.stdout, result.stderr, result.exception)
    assert json.loads(result.stdout)["ok"] is True
    latest = heartbeat.history(config.paths, beat.ref, limit=1)[0]
    assert latest.action_status == "succeeded" and latest.run_id == run.id
    receipt = json.loads(latest.receipt)
    assert receipt == {
        "transport": name,
        "target": "123" if to else "C3",
        "thread": None if to else "7.0",
        "message_id": "F123" if is_file else "100.1",
        "file": is_file,
    }
    (message,) = messages.list_messages(config.paths, 10)
    assert message.source == f"beat:{beat.ref}" and message.consumed_at is None
    refused = CliRunner().invoke(app, args)
    assert refused.exit_code == 1 and "succeeded" in json.loads(refused.stdout)["error"]
    assert "heartbeat history" in json.loads(refused.stdout)["error"]
    assert sender.connections == 1 and len(sender.writes) == 1


@pytest.mark.parametrize("change", ["cancel", "pause", "update", "finished", "disabled"])
async def test_stale_run_is_refused_before_any_network(active, change):
    config, beat, run = active
    sender = Sender(config, beat)
    if change in {"cancel", "pause"}:
        getattr(heartbeat, change)(config, beat.ref, message="User changed the plan")
    elif change == "update":
        heartbeat.update(config, beat.ref, {"instructions": "Wait for me"})
    elif change == "finished":
        heartbeat.finish_run(config, run.id, status="error", error="Interrupted")
    else:
        raw = json.loads(config.paths.config.read_text())
        raw["heartbeat"] = {"enabled": False}
        write_config(config.paths, raw)
    with pytest.raises(heartbeat.HeartbeatError):
        await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    assert sender.connections == 0 and sender.writes == []


@pytest.mark.parametrize("change", ["cancel", "pause", "update", "disabled"])
async def test_authority_changed_while_connecting_prevents_the_send(active, change):
    config, beat, _ = active

    def connected():
        if change in {"cancel", "pause"}:
            getattr(heartbeat, change)(config, beat.ref, message="Stop following this")
        elif change == "update":
            heartbeat.update(config, beat.ref, {"instructions": "Wait for me"})
        else:
            raw = json.loads(config.paths.config.read_text())
            raw["heartbeat"] = {"enabled": False}
            write_config(config.paths, raw)

    sender = Sender(config, beat, connected=connected)
    with pytest.raises(heartbeat.HeartbeatError):
        await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    assert sender.connections == 1 and sender.writes == []
    assert heartbeat.history(config.paths, beat.ref, limit=1)[0].action_status == "failed"


async def test_missing_action_key_fails_before_connecting(active):
    config, beat, _ = active
    sender = Sender(config, beat)
    with pytest.raises(heartbeat.HeartbeatError, match="require --action-key"):
        await deliver(config.paths, sender, "C3", "7.0", text="Hi")
    assert sender.connections == 0


async def test_gate_context_cannot_send_without_a_running_agent(active, monkeypatch):
    config, beat, _ = active
    monkeypatch.delenv("ENSO_BEAT_RUN_ID")
    sender = Sender(config, beat)
    with pytest.raises(heartbeat.HeartbeatError, match="gates cannot send"):
        await deliver(config.paths, sender, "C3", "7.0", text="Hi")
    assert sender.connections == 0 and sender.writes == []


async def test_action_key_uses_the_core_normalized_value_for_its_receipt(active):
    config, beat, _ = active
    sender = Sender(config, beat)
    await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key=" notice ")
    event = heartbeat.history(config.paths, beat.ref, limit=1)[0]
    assert event.action_key == "notice" and event.action_status == "succeeded"


@pytest.mark.parametrize("change", ["cancel", "update"])
async def test_known_receipt_survives_a_user_change_during_send(active, change):
    config, beat, _ = active

    async def effect():
        if change == "cancel":
            heartbeat.cancel(config, beat.ref, message="Stop following this")
        else:
            heartbeat.update(config, beat.ref, {"instructions": "Wait for further instructions"})

    sender = Sender(config, beat, effect=effect)
    result = await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    assert result["ok"] is True
    event = heartbeat.history(config.paths, beat.ref, limit=1)[0]
    assert event.action_status == "succeeded" and json.loads(event.receipt)["message_id"] == "100.1"


async def test_failure_after_send_attempt_requires_reconciliation(active):
    config, beat, _ = active

    async def ambiguous():
        raise TimeoutError("connection lost before response")

    sender = Sender(config, beat, effect=ambiguous)
    with pytest.raises(TimeoutError):
        await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    assert heartbeat.history(config.paths, beat.ref, limit=1)[0].action_status == "uncertain"
    with pytest.raises(heartbeat.HeartbeatError, match="uncertain"):
        await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    assert len(sender.writes) == 1


async def test_connection_failure_before_attempt_can_retry(active):
    config, beat, _ = active
    sender = Sender(config, beat, connect_error=True)
    with pytest.raises(RuntimeError, match="could not connect"):
        await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    assert heartbeat.history(config.paths, beat.ref, limit=1)[0].action_status == "failed"
    sender.connect_error = False
    await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    assert len(sender.writes) == 1


async def test_cancelled_inflight_send_keeps_uncertain_action(active):
    config, beat, _ = active
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    sender = Sender(config, beat, effect=blocked)
    pending = asyncio.create_task(
        deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    )
    await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert heartbeat.history(config.paths, beat.ref, limit=1)[0].action_status == "uncertain"


async def test_known_receipt_is_saved_when_outbox_write_fails(active, monkeypatch):
    config, beat, _ = active
    sender = Sender(config, beat)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("outbox write failed")

    monkeypatch.setattr(messages, "record", broken)
    with pytest.raises(sqlite3.OperationalError):
        await deliver(config.paths, sender, "C3", "7.0", text="Hi", action_key="notice")
    event = heartbeat.history(config.paths, beat.ref, limit=1)[0]
    assert event.action_status == "succeeded" and json.loads(event.receipt)["message_id"] == "100.1"


def test_saved_destination_precedes_origin_and_explicit_target_clears_thread(active, monkeypatch):
    config, _, _ = active
    monkeypatch.setenv("ENSO_ORIGIN_TRANSPORT", "telegram")
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "456")
    assert messaging.destination(config, None) == ("slack", "C3", "7.0")
    assert messaging.destination(config, "slack:C9") == ("slack", "C9", None)
    assert messaging.destination(config, "telegram:456") == ("telegram", "456", None)
    with pytest.raises(ValueError, match="saved destination is not on telegram"):
        messaging.destination(config, None, transport="telegram")


def test_invalid_saved_destination_never_falls_back(active, monkeypatch):
    config, beat, _ = active
    raw = json.loads(config.paths.config.read_text())
    raw["transports"].pop("slack")
    raw["bindings"] = {"telegram:123": "default"}
    write_config(config.paths, raw)
    current = load_config(config.paths)
    monkeypatch.setenv("ENSO_ORIGIN_TRANSPORT", "telegram")
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "456")
    with pytest.raises(ValueError, match="slack is not configured"):
        messaging.destination(current, None)
    assert heartbeat.get(config.paths, beat.ref).notify == "slack:C3"
