"""Tests for the runtime core."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

import pytest

from enso import messages
from enso.core import Runtime, split_text
from enso.jobs import Job
from enso.providers.claude import KageClaudeProvider

# -- split_text --


def test_split_text_short():
    assert split_text("hello", limit=100) == ["hello"]


def test_split_text_at_line_boundaries():
    text = "line1\nline2\nline3"
    chunks = split_text(text, limit=12)
    assert all(len(c) <= 12 for c in chunks)
    assert "\n".join(chunks) == text


def test_split_text_long_line():
    text = "a" * 200
    chunks = split_text(text, limit=50)
    assert all(len(c) <= 50 for c in chunks)
    assert "".join(chunks) == text


# -- Runtime state --


def test_runtime_defaults(sample_config):
    rt = Runtime(sample_config)
    assert rt.get_active_provider("1") == "claude"
    assert rt.get_active_model("1", "claude") == "opus"
    assert rt.debug_prompts is False
    assert rt.debug_events is False


def test_runtime_reads_debug_logging_flags(sample_config):
    sample_config["logging"] = {"debug_prompts": True, "debug_events": True}
    rt = Runtime(sample_config)
    assert rt.debug_prompts is True
    assert rt.debug_events is True


def test_runtime_provider_switch(sample_config):
    rt = Runtime(sample_config)
    rt.active_provider_by_chat["1"] = "gemini"
    assert rt.get_active_provider("1") == "gemini"


def test_runtime_model_switch(sample_config):
    rt = Runtime(sample_config)
    rt.active_model_by_chat_provider[("1", "claude")] = "sonnet"
    assert rt.get_active_model("1", "claude") == "sonnet"


def test_runtime_make_provider_uses_kage_runner(sample_config):
    sample_config["providers"]["claude"].update({
        "runner": "kage",
        "kage_path": "kage",
        "kage_timeout": 900,
    })
    rt = Runtime(sample_config)
    provider = rt.make_provider("claude")
    assert isinstance(provider, KageClaudeProvider)
    assert provider.path == "kage"
    assert provider.timeout == 900


def test_make_provider_overrides_runner(sample_config):
    """overrides win over stored config without mutating it."""
    sample_config["providers"]["claude"].update({"runner": "print", "kage_path": "kage"})
    rt = Runtime(sample_config)
    provider = rt.make_provider("claude", overrides={"runner": "kage"})
    assert isinstance(provider, KageClaudeProvider)
    # Stored config is untouched by the override.
    assert rt.config["providers"]["claude"]["runner"] == "print"


# -- Job runner resolution --


def test_resolve_job_runner_defaults_to_print(sample_config):
    rt = Runtime(sample_config)
    assert rt.resolve_job_runner("claude") == "print"


def test_resolve_job_runner_reads_job_runner_key(sample_config):
    sample_config["providers"]["claude"]["job_runner"] = "kage"
    rt = Runtime(sample_config)
    assert rt.resolve_job_runner("claude") == "kage"


def test_resolve_job_runner_independent_of_interactive(sample_config):
    """Interactive runner=kage must NOT make jobs use kage."""
    sample_config["providers"]["claude"]["runner"] = "kage"
    rt = Runtime(sample_config)
    assert rt.resolve_job_runner("claude") == "print"


def test_resolve_job_runner_non_claude_is_none(sample_config):
    rt = Runtime(sample_config)
    assert rt.resolve_job_runner("codex") is None
    assert rt.resolve_job_runner("gemini") is None


def test_make_job_provider_selects_kage_and_threads_timeout(sample_config):
    sample_config["providers"]["claude"].update({
        "job_runner": "kage",
        "kage_path": "kage",
        "kage_timeout": 1800,
    })
    rt = Runtime(sample_config)
    job = Job(
        dir_name="j", name="J", schedule="* * * * *",
        provider="claude", model="sonnet", timeout=300,
    )
    provider = rt.make_job_provider(job)
    assert isinstance(provider, KageClaudeProvider)
    # kage's --timeout is threaded from job.timeout, not the global kage_timeout.
    assert provider.timeout == 300
    cmd = provider.build_batch_command("hi", "sonnet")
    assert cmd[cmd.index("--timeout") + 1] == "300"
    assert "--stop-on-signal" in cmd


def test_make_job_provider_uses_print_when_job_runner_unset(sample_config):
    sample_config["providers"]["claude"]["runner"] = "kage"  # interactive only
    rt = Runtime(sample_config)
    job = Job(
        dir_name="j", name="J", schedule="* * * * *",
        provider="claude", model="sonnet",
    )
    provider = rt.make_job_provider(job)
    assert not isinstance(provider, KageClaudeProvider)
    cmd = provider.build_batch_command("hi", "sonnet")
    assert cmd[:2] == [provider.path, "-p"]


# -- Kage smart restart (warm-session reuse) --


def test_interactive_overrides_empty_for_non_kage(sample_config):
    rt = Runtime(sample_config)  # runner unset -> print
    assert rt._interactive_overrides("claude", "1", "opus", None) == {}
    assert rt._interactive_overrides("codex", "1", "gpt", None) == {}


def test_interactive_overrides_first_turn_no_restart(sample_config):
    sample_config["providers"]["claude"]["runner"] = "kage"
    rt = Runtime(sample_config)
    # First turn for the chat: kage starts fresh, no restart needed.
    assert rt._interactive_overrides("claude", "1", "opus", None) == {"kage_restart": False}


def test_interactive_overrides_reuses_warm_session(sample_config):
    sample_config["providers"]["claude"]["runner"] = "kage"
    rt = Runtime(sample_config)
    rt._interactive_overrides("claude", "1", "opus", "high")
    # Same model+effort -> warm reuse, still no restart.
    assert rt._interactive_overrides("claude", "1", "opus", "high") == {"kage_restart": False}


def test_interactive_overrides_restarts_on_model_change(sample_config):
    sample_config["providers"]["claude"]["runner"] = "kage"
    rt = Runtime(sample_config)
    rt._interactive_overrides("claude", "1", "opus", None)
    assert rt._interactive_overrides("claude", "1", "sonnet", None) == {"kage_restart": True}


def test_interactive_overrides_restarts_on_effort_change(sample_config):
    sample_config["providers"]["claude"]["runner"] = "kage"
    rt = Runtime(sample_config)
    rt._interactive_overrides("claude", "1", "opus", "high")
    assert rt._interactive_overrides("claude", "1", "opus", "max") == {"kage_restart": True}


def test_interactive_overrides_per_chat_isolation(sample_config):
    sample_config["providers"]["claude"]["runner"] = "kage"
    rt = Runtime(sample_config)
    rt._interactive_overrides("claude", "chatA", "opus", None)
    # A different chat is independent -> its first turn never restarts.
    assert rt._interactive_overrides("claude", "chatB", "sonnet", None) == {"kage_restart": False}


def test_interactive_overrides_respects_disabled_restart(sample_config):
    sample_config["providers"]["claude"].update({"runner": "kage", "kage_restart": False})
    rt = Runtime(sample_config)
    rt._interactive_overrides("claude", "1", "opus", None)
    # Even on a config change, an explicit kage_restart=False never restarts.
    assert rt._interactive_overrides("claude", "1", "sonnet", None) == {"kage_restart": False}


def test_runtime_state_persistence(tmp_enso, sample_config):
    """State survives save/load roundtrip."""

    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    rt.active_provider_by_chat["42"] = "codex"
    rt.session_by_chat_provider[("42", "codex")] = "sess_123"
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()
    assert rt2.active_provider_by_chat["42"] == "codex"
    assert rt2.session_by_chat_provider[("42", "codex")] == "sess_123"


def test_compact_seed_persistence(tmp_enso, sample_config):
    """Compact seeds survive save/load roundtrip."""
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    rt.compact_seed_by_chat["42"] = "summary text"
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()
    assert rt2.compact_seed_by_chat["42"] == "summary text"


def test_consume_compact_seed_wraps_and_clears(sample_config):
    """Seed is prepended to prompt then removed from runtime state."""
    rt = Runtime(sample_config)
    rt.compact_seed_by_chat["42"] = "prior summary"

    wrapped = rt._consume_compact_seed("42", "user message", "claude")

    assert "prior summary" in wrapped
    assert wrapped.endswith("user message")
    assert "Continuing from a previous session" in wrapped
    assert "42" not in rt.compact_seed_by_chat


def test_consume_compact_seed_noop_when_absent(sample_config):
    """With no seed, prompt is returned unchanged."""
    rt = Runtime(sample_config)
    out = rt._consume_compact_seed("42", "user message", "claude")
    assert out == "user message"


def test_prune_clears_compact_seed(tmp_enso, sample_config):
    """Stale chats lose their compact seed too."""
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    rt.compact_seed_by_chat["42"] = "old summary"
    rt._last_active["42"] = datetime.now() - timedelta(days=999)
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()  # triggers prune
    assert "42" not in rt2.compact_seed_by_chat


# -- Effort --


def test_get_active_effort_none_by_default(sample_config):
    rt = Runtime(sample_config)
    assert rt.get_active_effort("1", "claude", "opus") is None


def test_get_active_effort_claude(sample_config):
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "claude", "opus")] = "xhigh"
    assert rt.get_active_effort("1", "claude", "opus") == "xhigh"


def test_get_active_effort_clamps_to_model_cap(sample_config):
    """Requesting max on a model that caps at high returns high."""
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "claude", "sonnet")] = "max"
    assert rt.get_active_effort("1", "claude", "sonnet") == "high"


def test_get_active_effort_non_claude_returns_none(sample_config):
    rt = Runtime(sample_config)
    # Shouldn't happen in practice, but defends the invariant.
    rt.effort_by_chat_provider_model[("1", "codex", "gpt-5.4")] = "high"
    assert rt.get_active_effort("1", "codex", "gpt-5.4") is None


def test_effort_state_persistence(tmp_enso, sample_config):
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("42", "claude", "opus")] = "xhigh"
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()
    assert rt2.effort_by_chat_provider_model[("42", "claude", "opus")] == "xhigh"


@pytest.mark.asyncio
async def test_run_provider_injects_extra_env(tmp_enso, sample_config, monkeypatch):
    """extra_env reaches create_subprocess_exec merged on top of os.environ."""
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)

    captured: dict = {}

    class FakeProcess:
        pid = 42
        returncode = 0
        stdout = None
        stderr = None

        async def wait(self):
            return 0

    async def fake_spawn(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    provider = rt.make_provider("claude")
    gen = rt.run_provider(
        provider, "hi", "1", "opus",
        extra_env={"ENSO_ORIGIN_CHANNEL": "C012345"},
    )
    # Drain — the fake stdout is None, so the loop exits immediately.
    try:
        async for _ in gen:
            pass
    except (TypeError, AssertionError):
        # FakeProcess.stdout is None; the `async for` will blow up on the
        # assert or the iteration. Either way we only care that env was
        # captured before that happens.
        pass

    env = captured["env"]
    assert env is not None, "env= must be passed when extra_env is set"
    assert env["ENSO_ORIGIN_CHANNEL"] == "C012345"
    # Parent env is preserved (PATH always exists on Unix / Windows).
    assert "PATH" in env


@pytest.mark.asyncio
async def test_run_provider_omits_env_when_not_requested(tmp_enso, sample_config, monkeypatch):
    """Without extra_env the child inherits the parent env implicitly."""
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)

    captured: dict = {}

    class FakeProcess:
        pid = 42
        returncode = 0
        stdout = None
        stderr = None

        async def wait(self):
            return 0

    async def fake_spawn(*args, **kwargs):
        captured["env"] = kwargs.get("env", "SENTINEL_UNSET")
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    provider = rt.make_provider("claude")
    gen = rt.run_provider(provider, "hi", "1", "opus")
    try:
        async for _ in gen:
            pass
    except (TypeError, AssertionError):
        pass

    assert captured["env"] == "SENTINEL_UNSET"


def test_prune_clears_effort(tmp_enso, sample_config):
    """Stale conversations drop their effort settings too."""
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    rt.active_provider_by_chat["old_chat"] = "claude"
    rt.effort_by_chat_provider_model[("old_chat", "claude", "opus")] = "xhigh"
    rt._last_active["old_chat"] = datetime.now() - timedelta(days=60)
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()
    assert ("old_chat", "claude", "opus") not in rt2.effort_by_chat_provider_model


# -- Job scheduling --


def test_get_or_create_session_claude(sample_config):
    """Claude gets a pre-generated session ID with new: prefix."""
    rt = Runtime(sample_config)
    sid = rt._get_or_create_session("1", "claude")
    assert sid is not None
    assert sid.startswith("new:")
    # Second call returns the same ID
    assert rt._get_or_create_session("1", "claude") == sid


def test_get_or_create_session_codex(sample_config):
    """Codex does not get a pre-generated session — it creates its own."""
    rt = Runtime(sample_config)
    assert rt._get_or_create_session("1", "codex") is None


def test_should_run_job_first_time(sample_config):
    """First encounter with a job should not fire immediately."""
    rt = Runtime(sample_config)
    job = Job(dir_name="test", name="Test", schedule="* * * * *", provider="claude", model="sonnet")
    assert rt._should_run_job(job, datetime.now()) is False
    assert "test" in rt._job_last_run


def test_should_run_job_due(sample_config):
    """Job should run when next cron time has passed."""
    rt = Runtime(sample_config)
    job = Job(dir_name="test", name="Test", schedule="* * * * *", provider="claude", model="sonnet")
    rt._job_last_run["test"] = datetime.now() - timedelta(minutes=2)
    assert rt._should_run_job(job, datetime.now()) is True


def test_should_run_job_skips_stale_misfire(sample_config):
    """Missed daily jobs should not run hours late by default."""
    rt = Runtime(sample_config)
    job = Job(
        dir_name="today",
        name="Today",
        schedule="30 6 * * *",
        provider="claude",
        model="opus",
    )
    now = datetime(2026, 5, 14, 21, 0)
    rt._job_last_run["today"] = datetime(2026, 5, 13, 6, 30)

    assert rt._should_run_job(job, now) is False
    assert rt._job_last_run["today"] == now


def test_should_run_job_allows_explicit_catch_up(sample_config):
    """Jobs can opt into stale catch-up when that is intentional."""
    rt = Runtime(sample_config)
    job = Job(
        dir_name="catch-up",
        name="Catch Up",
        schedule="30 6 * * *",
        provider="claude",
        model="opus",
        catch_up=True,
    )
    now = datetime(2026, 5, 14, 21, 0)
    rt._job_last_run["catch-up"] = datetime(2026, 5, 13, 6, 30)

    assert rt._should_run_job(job, now) is True


def test_should_run_job_not_due(sample_config):
    """Job should not run when it was just executed."""
    rt = Runtime(sample_config)
    job = Job(dir_name="test", name="Test", schedule="0 9 * * *", provider="claude", model="sonnet")
    rt._job_last_run["test"] = datetime.now()
    assert rt._should_run_job(job, datetime.now()) is False


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_communicate_timeout_kills_process_group(tmp_path, sample_config):
    """Timeout cleanup kills child processes spawned by a CLI wrapper."""
    if os.name == "nt":
        pytest.skip("process group semantics differ on Windows")

    rt = Runtime(sample_config)
    child_pid_file = tmp_path / "child.pid"
    proc = await rt._spawn_process(
        "bash",
        "-c",
        f"sleep 30 & echo $! > {child_pid_file}; wait",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    for _ in range(20):
        if child_pid_file.exists():
            break
        await asyncio.sleep(0.05)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text().strip())
    assert _pid_exists(child_pid)

    _, _, timed_out = await rt._communicate_with_timeout(proc, "test job", 1)

    assert timed_out is True
    assert proc.returncode is not None
    for _ in range(20):
        if not _pid_exists(child_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_exists(child_pid)


# -- Session pruning --


def test_prune_stale_sessions(tmp_enso, sample_config):
    """Stale sessions are pruned on load_state."""
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)

    # Create an old conversation and a fresh one
    rt.active_provider_by_chat["old_chat"] = "claude"
    rt.session_by_chat_provider[("old_chat", "claude")] = "old_session"
    rt._last_active["old_chat"] = datetime.now() - timedelta(days=60)

    rt.active_provider_by_chat["fresh_chat"] = "gemini"
    rt.session_by_chat_provider[("fresh_chat", "gemini")] = "fresh_session"
    rt._last_active["fresh_chat"] = datetime.now()

    rt.save_state()

    # Load into a new runtime — pruning should remove old_chat
    rt2 = Runtime(sample_config)
    rt2.load_state()

    assert "old_chat" not in rt2.active_provider_by_chat
    assert ("old_chat", "claude") not in rt2.session_by_chat_provider
    assert "old_chat" not in rt2._last_active
    # Fresh one survives
    assert rt2.active_provider_by_chat["fresh_chat"] == "gemini"
    assert rt2.session_by_chat_provider[("fresh_chat", "gemini")] == "fresh_session"


# -- Message injection --


@pytest.mark.asyncio
async def test_process_request_injects_messages(tmp_enso, sample_config):
    """Background messages are consumed and injected into the prompt."""
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")

    messages.send("background info", source="test")
    assert len(messages.pending()) == 1

    rt = Runtime(sample_config)
    prompts_received: list[str] = []

    # Mock the provider and run_provider to capture the prompt
    class FakeCtx:
        async def reply(self, text): pass
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def fake_run(
        provider, prompt, chat_id, model, *, effort=None, extra_env=None,
    ):
        prompts_received.append(prompt)
        if False:
            yield  # make this an async generator

    rt.run_provider = fake_run
    await rt.process_request("claude", "user message", "1", FakeCtx())

    # Messages should have been consumed
    assert messages.pending() == []
    assert len(prompts_received) == 1
    assert "background info" in prompts_received[0]
    assert "user message" in prompts_received[0]
