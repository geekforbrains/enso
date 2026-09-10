"""Completion means a delivered provider answer from the paired, loaded configuration."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FakeReply, make_turn, script

from enso import connection_setup
from enso.config import Config, Paths, config_fingerprint, load_config, save_config
from enso.outbound import FAILURE_NOTICE, OutboundMessage
from enso.runtime import Runtime


class RichReply(FakeReply):
    rich_format = True

    async def send_rich(self, message: OutboundMessage) -> str:
        return await self.send(message.fallback_text)


@pytest.fixture
def onboarded(fake_config: Config) -> Runtime:
    paths = fake_config.paths
    save_config(paths, fake_config.raw)
    config = load_config(paths)
    connection_setup._prepare(paths)
    connection_setup._write(
        paths.connection_dir / "state.json",
        {
            "attempt_id": "onboarding-test",
            "state": "applied",
            "transport": "slack",
            "user_id": "U1",
            "channel": "D1",
            "defaults": config.raw["defaults"],
            "config_hash": config.source_hash,
        },
    )
    return Runtime(config)


async def test_delivered_answer_records_only_receipt(onboarded: Runtime, enso_home: Paths) -> None:
    reply = FakeReply()
    await onboarded.handle(make_turn("a private question"), reply)
    assert reply.sent
    receipt = json.loads((enso_home.connection_dir / "reply.json").read_text())
    assert receipt == {
        "attempt_id": "onboarding-test",
        "provider": "claude",
        "model": "opus",
        "effort": "xhigh",
        "transport": "slack",
        "user_id": "U1",
        "channel": "D1",
        "at": receipt["at"],
        "config_hash": config_fingerprint(enso_home),
    }
    assert "private question" not in json.dumps(receipt)


@pytest.mark.parametrize("answer", ["", "   ", "fail please"])
async def test_empty_or_failed_provider_does_not_complete(
    onboarded: Runtime,
    enso_home: Paths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    if answer != "fail please":
        script(tmp_path, monkeypatch, answer)
    await onboarded.handle(make_turn(answer or "hello"), FakeReply())
    assert not (enso_home.connection_dir / "reply.json").exists()


async def test_failed_rich_correction_does_not_complete(
    onboarded: Runtime,
    enso_home: Paths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(tmp_path, monkeypatch, "```enso-message\n{\n```", "```enso-message\n{\n```")
    reply = RichReply()
    await onboarded.handle(make_turn("hello"), reply)
    assert reply.sent == [FAILURE_NOTICE]
    assert not (enso_home.connection_dir / "reply.json").exists()


async def test_successful_rich_correction_completes(
    onboarded: Runtime,
    enso_home: Paths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(tmp_path, monkeypatch, "```enso-message\n{\n```", "Your bot is working.")
    await onboarded.handle(make_turn("hello"), RichReply())
    assert (enso_home.connection_dir / "reply.json").exists()


async def test_failed_delivery_does_not_complete(onboarded: Runtime, enso_home: Paths) -> None:
    class BrokenReply(FakeReply):
        async def send(self, text: str) -> str:
            if not self.sent:
                self.sent.append("delivery failed")
                raise OSError("chat unavailable")
            return await super().send(text)

    await onboarded.handle(make_turn("hello"), BrokenReply())
    assert not (enso_home.connection_dir / "reply.json").exists()


async def test_other_sender_does_not_complete(onboarded: Runtime, enso_home: Paths) -> None:
    await onboarded.handle(replace(make_turn("hello"), user_id="U2"), FakeReply())
    assert not (enso_home.connection_dir / "reply.json").exists()


def _reconfigure(enso_home: Paths, *, finished: bool, **sections: object) -> None:
    """Write a changed config; ``finished`` means ``connect finish`` recorded its hash."""
    save_config(enso_home, {**load_config(enso_home).raw, **sections})
    if finished:
        state_path = enso_home.connection_dir / "state.json"
        state = json.loads(state_path.read_text())
        state["config_hash"] = config_fingerprint(enso_home)
        connection_setup._write(state_path, state)


async def test_receipt_names_the_config_the_turn_ran_with(
    onboarded: Runtime, enso_home: Paths
) -> None:
    _reconfigure(enso_home, finished=True, agent={"timeout": 10})
    await onboarded.handle(make_turn("hello"), FakeReply())
    receipt = json.loads((enso_home.connection_dir / "reply.json").read_text())
    assert receipt["config_hash"] == config_fingerprint(enso_home)


async def test_a_config_change_without_finish_does_not_complete(
    onboarded: Runtime, enso_home: Paths
) -> None:
    _reconfigure(enso_home, finished=False, agent={"timeout": 10})
    await onboarded.handle(make_turn("hello"), FakeReply())
    assert not (enso_home.connection_dir / "reply.json").exists()


async def test_timeout_does_not_complete(onboarded: Runtime, enso_home: Paths) -> None:
    _reconfigure(enso_home, finished=True, agent={"timeout": 1})
    reply = FakeReply()
    await onboarded.handle(make_turn("sleep 30"), reply)
    assert "timeout" in reply.status[-1]
    assert not (enso_home.connection_dir / "reply.json").exists()
