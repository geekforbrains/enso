"""Shared chat command parsing and responses."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest
from conftest import FakeReply, make_turn, session_for, write_config

from enso import commands, db
from enso.commands import Command, parse
from enso.config import Paths, parse_config
from enso.routing import UNBOUND_NOTICE
from enso.runtime import Runtime


@pytest.mark.parametrize("workspace", ["", "missing"])
async def test_commands_recheck_binding_and_pinned_workspace(runtime, monkeypatch, workspace):
    turn = replace(make_turn("!restart"), workspace=workspace)
    if not workspace:
        turn = replace(turn, user_id="unknown")

    async def unexpected(*args, **kwargs):
        pytest.fail("rejected command ran")

    monkeypatch.setattr(commands, "run", unexpected)
    reply = FakeReply()
    assert await commands.dispatch(runtime, turn, reply)
    assert reply.sent == [UNBOUND_NOTICE]


@pytest.mark.parametrize(
    ("text", "transport", "expected"),
    [
        ("!stop", "slack", Command("stop")),
        ("  !STOP now ", "slack", Command("stop", "now")),
        ("/status@enso_bot", "telegram", Command("status")),
        ("/start", "telegram", Command("help")),
        ("!use codex:sol:medium", "slack", Command("use", "codex:sol:medium")),
        ("! wow", "slack", None),
        ("/stop", "slack", None),
        ("!stop", "telegram", None),
        ("hello", "slack", None),
    ],
)
def test_parse(text: str, transport: str, expected: Command | None) -> None:
    assert parse(text, transport) == expected


async def test_status_then_clear(runtime: Runtime, enso_home: Paths) -> None:
    reply = FakeReply()
    assert await commands.dispatch(runtime, make_turn("!status"), reply)
    assert reply.sent[-1].splitlines() == [
        "Workspace: default",
        "Agent: claude · opus · xhigh (from defaults)",
        "Session: none",
        "Running: nothing",
        "Queued: 0",
    ]

    await runtime.handle(make_turn("hello"), FakeReply())
    session = session_for(enso_home, "slack:D1", "claude")
    assert session is not None
    await commands.dispatch(runtime, make_turn("!status"), reply)
    assert f"Session: claude {session.session_id[:8]} · started 0s ago" in reply.sent[-1]

    await commands.dispatch(runtime, make_turn("!clear"), reply)
    assert reply.sent[-1].startswith("Cleared.")
    assert f"claude: session {session.session_id[:8]} (no file found)" in reply.sent[-1]
    assert db.get_sessions(enso_home, "slack:D1") == []
    await commands.dispatch(runtime, make_turn("!clear"), reply)
    assert reply.sent[-1] == "No session to clear."


async def test_use_selects_exact_triples_and_reset_keeps_sessions(
    runtime: Runtime, enso_home: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = FakeReply()
    await commands.dispatch(runtime, make_turn("!use"), reply)
    assert "Using claude:opus:xhigh (configured default)." in reply.sent[-1]
    assert "!use PROVIDER:MODEL:EFFORT" in reply.sent[-1]

    db.set_session(enso_home, "slack:D1", "codex", "codex-session", "default")
    await commands.dispatch(runtime, make_turn("!use codex:sol:medium"), reply)
    assert "Resuming the earlier codex session" in reply.sent[-1]
    assert runtime.current_agent("slack:D1", "default").source == "conversation"
    await commands.dispatch(runtime, make_turn("!use model:astra"), reply)
    await commands.dispatch(runtime, make_turn("!use effort:ultra"), reply)
    assert runtime.current_agent("slack:D1", "default").model == "astra"
    assert runtime.current_agent("slack:D1", "default").effort == "ultra"
    await commands.dispatch(runtime, make_turn("!status"), reply)
    assert "astra · ultra (from conversation selection)" in reply.sent[-1]

    seen = []

    async def record_agent(conversation, workspace, turn, turn_reply, running):
        seen.append(running.agent)

    monkeypatch.setattr(runtime, "_turn", record_agent)
    await runtime.handle(make_turn("hello"), FakeReply())
    assert [(a.provider, a.model, a.effort) for a in seen] == [("codex", "astra", "ultra")]

    await commands.dispatch(runtime, make_turn("!use default"), reply)
    assert "Using claude:opus:xhigh (configured default)." in reply.sent[-1]
    assert session_for(enso_home, "slack:D1", "codex") is not None
    assert runtime.current_agent("slack:D1", "default").source == "defaults"
    await commands.dispatch(runtime, make_turn("!use codex:sol:medium"), reply)
    assert "Resuming the earlier codex session" in reply.sent[-1]
    assert Runtime(runtime.config).current_agent("slack:D1", "default").source == "defaults"


async def test_use_switches_providers_and_resumes_their_sessions(
    runtime: Runtime, enso_home: Paths, fake_opencode: str
) -> None:
    raw = copy.deepcopy(runtime.config.raw)
    raw["providers"]["opencode"] = {
        "path": fake_opencode,
        "models": ["openrouter/deepseek/deepseek-v4-flash"],
        "args": [],
    }
    config, problems, _ = parse_config(raw, enso_home)
    assert config is not None, problems
    switched = Runtime(config)

    first = FakeReply()
    await switched.handle(make_turn("first"), first)
    assert first.sent[0].startswith("new ")
    claude_session = session_for(enso_home, "slack:D1", "claude")
    assert claude_session is not None

    await commands.dispatch(
        switched,
        make_turn("!use opencode:openrouter/deepseek/deepseek-v4-flash:high"),
        FakeReply(),
    )
    second = FakeReply()
    await switched.handle(make_turn("second"), second)
    assert second.sent[0].startswith("new ses_")
    assert session_for(enso_home, "slack:D1", "opencode") is not None

    await commands.dispatch(switched, make_turn("!use default"), FakeReply())
    third = FakeReply()
    await switched.handle(make_turn("third"), third)
    assert third.sent[0].startswith(f"resumed {claude_session.session_id} ")
    assert session_for(enso_home, "slack:D1", "opencode") is not None


async def test_use_rejects_incomplete_or_invalid_choices_without_mutation(runtime: Runtime) -> None:
    reply = FakeReply()
    for spec, reason in (
        ("provider:codex", "complete triple"),
        ("codex:sol", "complete triple"),
        ("missing:sol:medium", "not configured"),
        ("codex:missing:medium", "not configured for codex"),
        ("codex:luna:ultra", "Effort 'ultra' cannot be used"),
        ("model:haiku", "Use !use claude:haiku:EFFORT"),
        ("effort:ultra", "Effort 'ultra' cannot be used"),
    ):
        await commands.dispatch(runtime, make_turn(f"!use {spec}"), reply)
        assert reason in reply.sent[-1]
        assert runtime.current_agent("slack:D1", "default").source == "defaults"


async def test_use_keeps_colons_inside_configured_model_ids(runtime: Runtime) -> None:
    config = runtime.config
    providers = {
        **config.providers,
        "codex": replace(config.providers["codex"], models=("model:free",)),
    }
    routed = Runtime(replace(config, providers=providers))
    result = await commands.run(
        routed,
        Command("use", "codex:model:free:medium"),
        conversation="slack:D1",
        binding="slack:dm:U1",
        workspace="default",
        transport="slack",
    )
    assert result.text.startswith("Using codex:model:free:medium")
    assert routed.current_agent("slack:D1", "default").model == "model:free"


async def test_clear_removes_selection_without_a_provider_session(runtime: Runtime) -> None:
    reply = FakeReply()
    await commands.dispatch(runtime, make_turn("!use model:sonnet"), reply)
    await commands.dispatch(runtime, make_turn("!clear"), reply)
    assert reply.sent[-1] == "Cleared selection. The next message uses the configured default."
    assert runtime.current_agent("slack:D1", "default").source == "defaults"


async def test_use_is_scoped_to_one_conversation(runtime: Runtime) -> None:
    selected = replace(make_turn("!use codex:sol:medium"), channel="C1", thread="10.1", is_dm=False)
    await commands.dispatch(runtime, selected, FakeReply())
    assert runtime.current_agent("slack:C1:10.1", "default").provider == "codex"
    assert runtime.current_agent("slack:C1:10.2", "default").provider == "claude"
    assert runtime.current_agent("slack:D1", "default").provider == "claude"
    assert runtime.current_agent("telegram:123", "default").provider == "claude"


async def test_use_rejects_change_while_a_turn_or_queue_is_active(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(conversation, workspace, turn, turn_reply, running):
        if turn.text == "first":
            entered.set()
            await release.wait()

    monkeypatch.setattr(runtime, "_turn", hold)
    drain = await runtime.submit(make_turn("first"), FakeReply())
    assert drain is not None
    await entered.wait()
    await runtime.submit(make_turn("second"), FakeReply())
    reply = FakeReply()
    await commands.dispatch(runtime, make_turn("!use codex:sol:medium"), reply)
    assert "running or being cleared" in reply.sent[-1]
    assert runtime.current_agent("slack:D1", "default").source == "defaults"
    release.set()
    await drain


async def test_live_config_or_rebinding_invalidates_selection(
    runtime: Runtime, enso_home: Paths
) -> None:
    await commands.dispatch(runtime, make_turn("!use codex:sol:medium"), FakeReply())
    raw = copy.deepcopy(runtime.config.raw)
    raw["providers"]["codex"]["models"].remove("sol")
    write_config(enso_home, raw)
    assert runtime.current_agent("slack:D1", "default").source == "defaults"

    await commands.dispatch(runtime, make_turn("!use codex:astra:medium"), FakeReply())
    (enso_home.workspaces / "other").mkdir()
    raw["bindings"]["slack:dm:U1"] = "other"
    write_config(enso_home, raw)
    assert runtime.current_agent("slack:D1", "default").source == "defaults"
    assert runtime.current_agent("slack:D1", "other").source == "defaults"


async def test_stop_and_clear_while_running(runtime: Runtime) -> None:
    work = FakeReply()
    running = asyncio.create_task(runtime.handle(make_turn("sleep 30"), work))
    await asyncio.sleep(0.4)
    reply = FakeReply()
    await commands.dispatch(runtime, make_turn("!clear"), reply)
    assert reply.sent[-1].startswith("A message is running.")
    await commands.dispatch(runtime, make_turn("!status"), reply)
    assert "Running: 0s · ↳ Reading a.py" in reply.sent[-1]
    await commands.dispatch(runtime, make_turn("!stop"), reply)
    assert reply.sent[-1].startswith("Stopped after")
    # The stop reply waits for the turn to unwind, so an immediate clear is not refused.
    await commands.dispatch(runtime, make_turn("!clear"), reply)
    assert reply.sent[-1].startswith("Cleared.")
    await asyncio.wait_for(running, timeout=3)
    assert work.status[-1] == "Stopped."


async def test_help_unknown_restart_and_passthrough(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    restarts: list[int] = []
    monkeypatch.setattr(commands, "restart_service", lambda: restarts.append(1))
    monkeypatch.setattr(commands, "RESTART_DELAY_SECONDS", 0)
    reply = FakeReply()

    await commands.dispatch(runtime, make_turn("/help", transport="telegram"), reply)
    assert reply.sent[-1].startswith("/stop — ") and "!" not in reply.sent[-1]
    await commands.dispatch(runtime, make_turn("!help"), reply)
    assert reply.sent[-1].startswith("!stop — ")

    await commands.dispatch(runtime, make_turn("!foo"), reply)
    assert reply.sent[-1] == "Unknown command !foo. Try !help."

    await commands.dispatch(runtime, make_turn("!restart"), reply)
    assert reply.sent[-1] == "Restarting…"
    await asyncio.sleep(0.05)
    assert restarts == [1]

    assert not await commands.dispatch(runtime, make_turn("just text"), reply)
    unbound = replace(make_turn("!status"), user_id="U9")
    assert await commands.dispatch(runtime, unbound, reply)
    assert reply.sent[-1] == UNBOUND_NOTICE
    assert len(reply.sent) == 5
