"""Tests for the runtime core."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from enso import messages
from enso.core import Runtime, split_text
from enso.jobs import Job

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


def test_runtime_provider_switch(sample_config):
    rt = Runtime(sample_config)
    rt.active_provider_by_chat["1"] = "gemini"
    assert rt.get_active_provider("1") == "gemini"


def test_runtime_model_switch(sample_config):
    rt = Runtime(sample_config)
    rt.active_model_by_chat_provider[("1", "claude")] = "sonnet"
    assert rt.get_active_model("1", "claude") == "sonnet"


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


def test_should_run_job_not_due(sample_config):
    """Job should not run when it was just executed."""
    rt = Runtime(sample_config)
    job = Job(dir_name="test", name="Test", schedule="0 9 * * *", provider="claude", model="sonnet")
    rt._job_last_run["test"] = datetime.now()
    assert rt._should_run_job(job, datetime.now()) is False


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

    async def fake_run(provider, prompt, chat_id, model, *, effort=None):
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
