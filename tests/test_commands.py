"""Command parsing and the five command responses on both prefixes."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest
from conftest import FakeReply, make_turn, session_for

from enso import commands, db
from enso.commands import Command, parse
from enso.config import Agent, Paths
from enso.routing import UNBOUND_NOTICE
from enso.runtime import Runtime


@pytest.mark.parametrize("transport", ["slack", "telegram"])
@pytest.mark.parametrize("workspace", ["", "missing"])
async def test_commands_recheck_binding_and_pinned_workspace(
    runtime, monkeypatch, transport, workspace
):
    turn = make_turn("!restart" if transport == "slack" else "/restart", transport=transport)
    turn = replace(turn, workspace=workspace)
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
        ("!foo bar", "slack", Command("foo", "bar")),
        ("!!!", "slack", None),
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


async def test_status_compacts_a_routed_model_name(runtime: Runtime) -> None:
    routed = Runtime(
        replace(
            runtime.config,
            defaults=Agent("opencode", "openrouter/deepseek/deepseek-v4-flash-vision-exp", "low"),
        )
    )

    text = await commands.status_text(routed, "slack:D1", "default")

    assert "Agent: opencode · deepseek-v4-fla…sion-exp · low (from defaults)" in text


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


async def test_status_reads_sessions_off_loop(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session read runs in a worker thread (Slack admission shares this path)."""
    threads: list[threading.Thread] = []
    real = db.get_sessions

    def record(paths: Paths, conversation: str) -> list[db.Session]:
        threads.append(threading.current_thread())
        return real(paths, conversation)

    monkeypatch.setattr(db, "get_sessions", record)
    assert await commands.dispatch(runtime, make_turn("!status"), FakeReply())
    assert threads
    assert threading.current_thread() not in threads


async def test_commands_in_a_dm_thread_target_the_dm(runtime: Runtime, enso_home: Paths) -> None:
    """``!status``/``!clear`` typed inside a DM thread act on the DM's one conversation."""
    await runtime.handle(make_turn("hello"), FakeReply())
    session = session_for(enso_home, "slack:D1", "claude")
    assert session is not None
    reply = FakeReply()
    await commands.dispatch(runtime, make_turn("!status", thread="9.9"), reply)
    assert f"Session: claude {session.session_id[:8]}" in reply.sent[-1]
    await commands.dispatch(runtime, make_turn("!clear", thread="9.9"), reply)
    assert reply.sent[-1].startswith("Cleared.")
    assert db.get_sessions(enso_home, "slack:D1") == []
