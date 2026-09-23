"""The bundled memory job logs notable chat turns in shared ``Memory/`` daily notes."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest
from conftest import load_job, script

from enso import db, outbound, workspaces
from enso.jobs import validate
from enso.jobs.runner import JobRunner
from enso.messages import HEADER, Message, render
from enso.runtime import ORIGIN_HEADER, Runtime
from enso.transports import Turn
from enso.transports.slack import THREAD_CONTEXT_HEADER, channel_access

JOB = Path(workspaces.__file__).parent / "bundled/jobs/enso-memory"
ZONE = "America/Vancouver"


def bundled() -> ModuleType:
    spec = importlib.util.spec_from_file_location("enso_memory_job", JOB / "memory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode, previous = True, sys.dont_write_bytecode
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


memory = bundled()


def chat_prompt(text: str, *, background: bool = False, **fields) -> str:
    turn = Turn(
        transport="slack",
        channel="C1",
        thread="1.2",
        message_id="1.3",
        user_id="U1",
        user_name="Ada",
        text=text,
        **fields,
    )
    sent = Message(
        1,
        "2026-09-23T15:00:00+00:00",
        "default",
        "slack",
        "C1",
        None,
        "Backup done.\n\nAll good.",
        "job:default:x",
        "sent",
        None,
        None,
    )
    block = render([sent]) if background else ""
    return Runtime.assemble_prompt(turn, background=block, rich=True)


def claude_turn(session: str, text: str, reply: str, at: datetime) -> list[dict]:
    """A user prompt, a tool call, and the final answer, as Claude stores them."""
    user, tool, answer = (f"{session}-{n}" for n in ("u", "t", "a"))
    stamp = at.isoformat().replace("+00:00", "Z")
    later = (at + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    return [
        {"uuid": user, "type": "user", "timestamp": stamp, "message": {"content": text}},
        {
            "uuid": tool,
            "parentUuid": user,
            "type": "assistant",
            "timestamp": stamp,
            "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use"}]},
        },
        {
            "uuid": answer,
            "parentUuid": tool,
            "type": "assistant",
            "timestamp": later,
            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": reply}]},
        },
    ]


def test_template_installs_with_the_default_agent(enso_home, config):
    workspaces.seed_jobs(enso_home, config.defaults)
    job = load_job(enso_home, config, "enso-memory")
    assert validate(job, config) == []
    assert (job.agent.provider, job.agent.model, job.agent.effort) == (
        config.defaults.provider,
        config.defaults.model,
        config.defaults.effort,
    )
    assert (job.schedule, job.enabled, job.catch_up) == ("0 * * * *", True, True)
    assert job.gate.command == "python3 memory.py gate"
    assert job.postrun.command == "python3 memory.py postrun"
    assert "{{gate_output}}" in job.prompt
    installed = job.job_dir / "memory.py"
    assert installed.read_bytes() == (JOB / "memory.py").read_bytes()


def test_upgrades_install_the_job_in_an_existing_home(enso_home, config, monkeypatch):
    with monkeypatch.context() as older:
        older.setattr(workspaces, "BUNDLED_JOBS", ("enso-audit", "enso-update"))
        workspaces.seed_jobs(enso_home, config.defaults)
        workspaces.reconcile_bundles(enso_home, config.defaults)

    changed = workspaces.reconcile_bundles(enso_home, config.defaults)

    assert set(changed) >= {
        "workspaces/default/jobs/enso-memory/JOB.md",
        "workspaces/default/jobs/enso-memory/memory.py",
    }
    job = load_job(enso_home, config, "enso-memory")
    assert job.agent.provider == config.defaults.provider and validate(job, config) == []
    assert workspaces.reconcile_bundles(enso_home, config.defaults) == []


def test_chat_markers_match_the_prompts_enso_writes():
    assert memory.ORIGIN == ORIGIN_HEADER
    assert HEADER.startswith(memory.BACKGROUND)
    assert outbound.CONTRACT.startswith(memory.RICH_FORMAT + "\n")
    assert THREAD_CONTEXT_HEADER.startswith(memory.THREAD_CONTEXT)
    assert channel_access("C1", "general", "1.2").startswith(memory.CHANNEL_ACCESS)

    prompt = chat_prompt("Ship it Friday.", background=True, files=["/tmp/plan.pdf"])
    assert memory.clean_user(prompt) == "Ship it Friday."
    # Background sends have no end marker of their own, so without a following Enso
    # block they stay labelled for the reviewer instead of being guessed away.
    unfenced = memory.clean_user(chat_prompt("Ship it Friday.", background=True))
    assert unfenced.startswith(HEADER) and unfenced.endswith("\n\nShip it Friday.")
    assert memory.sender(prompt).startswith('"Ada"') and "U1" in memory.sender(prompt)
    context = f"{THREAD_CONTEXT_HEADER}\nBob: earlier"
    kept = memory.clean_user(chat_prompt("Agreed.", context=context))
    assert kept == f"{context}\n\nAgreed."  # labelled history stays for the reviewer
    assert memory.clean_user("A job prompt, not a chat.") == ""


def test_claude_history_follows_the_active_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    at = datetime(2026, 9, 23, 16, 0, tzinfo=UTC)
    first = claude_turn("one", chat_prompt("Pick a name."), "Named it Atlas.", at)
    side = {"uuid": "side", "parentUuid": "one-a", "isSidechain": True, "type": "user"}
    boundary = {
        "uuid": "compact",
        "type": "system",
        "subtype": "compact_boundary",
        "logicalParentUuid": "one-a",
    }
    second = claude_turn("two", chat_prompt("Ship it."), "Shipped.", at + timedelta(hours=1))
    second[0]["parentUuid"] = "compact"
    open_turn = claude_turn("three", chat_prompt("And docs?"), "", at + timedelta(hours=2))[:1]
    open_turn[0]["parentUuid"] = "two-a"
    lines = [json.dumps(r) for r in [*first, side, boundary, *second, *open_turn]]
    transcript = tmp_path / "projects" / "workspace" / "abc.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("\n".join(lines) + "\n" + '{"partial": ')

    units = memory.read_claude("abc")

    assert [(u["user"], u["assistant"]) for u in units] == [
        ("Pick a name.", "Named it Atlas."),
        ("Ship it.", "Shipped."),
    ]
    assert units[1]["completed_at"] == "2026-09-23T17:00:30.000000+00:00"
    with pytest.raises(memory.MissingHistoryError):
        memory.read_claude("gone")


def test_merge_sorts_entries_and_keeps_edits():
    early, late = "- **08:05 AM**: Early.", "- **01:10 PM**: Late."
    assert memory.merge(f"{late}\n", [early, late]) == f"{early}\n{late}\n"
    edited = f"## Notes\n\nKept by hand.\n\n{late}\n"
    assert memory.merge(edited, [early]) == f"{edited}{early}\n"


def run_hook(mode: str, env: dict, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(env["JOB_DIR"] / "memory.py"), mode],
        input=stdin,
        capture_output=True,
        text=True,
        env={k: str(v) for k, v in env.items()},
        timeout=60,
    )


@pytest.fixture
def chat_home(enso_home, fake_config, tmp_path, monkeypatch):
    """One Slack thread bound to a Claude session with a turn completed a minute ago."""
    monkeypatch.setenv("TZ", ZONE)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    bin_dir = Path(sys.executable).parent  # the checkout's `enso` for knowledge writes
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    workspaces.seed_jobs(enso_home, fake_config.defaults)
    db.set_session(enso_home, "slack:C1:1.2", "claude", "abc", "default")
    db.set_session(enso_home, "slack:C2:3.4", "opencode", "other", "default")
    note = enso_home.knowledge / "Projects" / "Apollo launch.md"
    note.parent.mkdir(parents=True)
    note.write_text("The launch plan.\n")
    at = datetime.now(UTC) - timedelta(minutes=2)
    turn = claude_turn("t", chat_prompt("Move the Apollo launch to Friday."), "Moved it.", at)
    history = tmp_path / "claude" / "projects" / "workspace" / "abc.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("".join(json.dumps(r) + "\n" for r in turn))
    return at


async def test_job_publishes_a_corrected_review_once(
    enso_home, fake_config, chat_home, tmp_path, monkeypatch
):
    job = load_job(enso_home, fake_config, "enso-memory")
    env = {**os.environ, "JOB_DIR": job.job_dir}
    preview = run_hook("gate", env)
    assert preview.returncode == 0, preview.stderr
    batch = json.loads(preview.stdout)
    [record] = batch["records"]
    assert record["user"] == "Move the Apollo launch to Friday."
    assert record["related_notes"] == ["Projects/Apollo launch.md"]
    assert batch["existing_daily_entries"] == {
        chat_home.astimezone(ZoneInfo(ZONE)).date().isoformat(): ""
    }
    entry = {
        "source_id": record["source_id"],
        "moment": "user",
        "text": "Ada moved the Apollo launch to Friday.",
        "notes": ["Projects/Apollo launch.md"],
    }
    bad = {"version": 1, "batch_id": batch["batch_id"], "entries": [{**entry, "notes": ["X.md"]}]}
    good = {"version": 1, "batch_id": batch["batch_id"], "entries": [entry]}
    script(tmp_path, monkeypatch, json.dumps(bad), json.dumps(good))

    result = await JobRunner(fake_config).run(job, trigger="manual")

    assert result.status == "ok", result
    local = chat_home.astimezone(ZoneInfo(ZONE))
    note = enso_home.knowledge / "Memory" / f"{local.date().isoformat()}.md"
    body = note.read_text().split("---\n", 2)[2].lstrip("\n")
    hour = local.hour % 12 or 12
    time = f"{hour:02d}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    assert body == (
        f"- **{time}**: Ada moved the Apollo launch to Friday."
        " ([[shared:Projects/Apollo launch|Apollo launch]])\n"
    )
    again = run_hook("gate", env)
    assert again.returncode == 1 and again.stdout == "" and again.stderr == ""
    state = json.loads((job.job_dir / "runtime" / "state.json").read_text())
    assert set(state["sessions"]) == {"claude:abc", "opencode:other"}
    assert not (job.job_dir / "runtime" / "pending.json").exists()


def test_gate_waits_without_history_and_reports_unreadable_sessions(
    enso_home, fake_config, chat_home, tmp_path
):
    job_dir = enso_home.workspace_jobs("default") / "enso-memory"
    env = {**os.environ, "JOB_DIR": job_dir}
    history = tmp_path / "claude" / "projects" / "workspace" / "abc.jsonl"
    history.unlink()
    assert run_hook("gate", env).returncode == 1  # nothing left to read is not a failure

    history.write_text("not json\n")
    db.touch_session(enso_home, "slack:C1:1.2", "claude")  # new activity rereads it
    failed = run_hook("gate", env)
    assert failed.returncode == 2
    assert "ENSO_ERROR: memory could not read 1 chat session(s)" in failed.stderr
    assert "not json" not in failed.stderr
