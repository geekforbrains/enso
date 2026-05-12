"""Tests for shared command handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from enso.commands import cmd_compact_async, cmd_effort, cmd_status
from enso.core import Runtime


def test_cmd_effort_non_claude_rejects(sample_config):
    rt = Runtime(sample_config)
    rt.active_provider_by_chat["1"] = "gemini"
    response, options = cmd_effort(rt, "1", "high")
    assert response is not None
    assert "only supported for Claude" in response
    assert options == []


def test_cmd_effort_set_level(sample_config):
    rt = Runtime(sample_config)
    response, options = cmd_effort(rt, "1", "high")
    assert options == []
    assert response == "Effort \u2192 high"
    assert rt.effort_by_chat_provider_model[("1", "claude", "opus")] == "high"


def test_cmd_effort_set_by_index(sample_config):
    """1-based index picks from levels supported by the current model."""
    rt = Runtime(sample_config)
    # Opus supports [low, medium, high, xhigh, max] — index 4 → xhigh
    response, _ = cmd_effort(rt, "1", "4")
    assert response == "Effort \u2192 xhigh"
    assert rt.effort_by_chat_provider_model[("1", "claude", "opus")] == "xhigh"


def test_cmd_effort_default_clears(sample_config):
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "claude", "opus")] = "xhigh"
    response, _ = cmd_effort(rt, "1", "default")
    assert response is not None
    assert "cleared" in response.lower()
    assert ("1", "claude", "opus") not in rt.effort_by_chat_provider_model


def test_cmd_effort_unknown_level(sample_config):
    rt = Runtime(sample_config)
    response, options = cmd_effort(rt, "1", "ludicrous")
    assert response is not None
    assert "Unknown effort" in response
    assert options == []


def test_cmd_effort_list_options_filters_by_model(sample_config):
    """Sonnet tops out at high — xhigh/max shouldn't appear in the picker."""
    rt = Runtime(sample_config)
    rt.active_model_by_chat_provider[("1", "claude")] = "sonnet"
    response, options = cmd_effort(rt, "1", None)
    assert response is None
    levels = [name for name, _ in options]
    assert levels == ["low", "medium", "high"]
    # Nothing selected yet
    assert not any(active for _, active in options)


def test_cmd_effort_list_options_marks_active(sample_config):
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "claude", "opus")] = "xhigh"
    response, options = cmd_effort(rt, "1", None)
    assert response is None
    assert ("xhigh", True) in options


def test_cmd_effort_clamp_warning_on_set(sample_config):
    """Setting max on a capped model reports the clamped value."""
    rt = Runtime(sample_config)
    rt.active_model_by_chat_provider[("1", "claude")] = "sonnet"
    response, _ = cmd_effort(rt, "1", "max")
    assert response is not None
    assert "clamped to high" in response
    # Raw intent is preserved; accessor does the clamping at read time.
    assert rt.effort_by_chat_provider_model[("1", "claude", "sonnet")] == "max"


def test_cmd_status_includes_effort(sample_config):
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "claude", "opus")] = "xhigh"
    out = cmd_status(rt, "1")
    assert "Effort: xhigh" in out


def test_cmd_status_omits_effort_when_unset(sample_config):
    rt = Runtime(sample_config)
    out = cmd_status(rt, "1")
    assert "Effort" not in out


# -- cmd_compact_async --


@pytest.mark.asyncio
async def test_compact_happy_path(tmp_enso, sample_config, monkeypatch):
    """Successful compaction stashes summary as seed and clears the session."""
    import os as _os

    sample_config["working_dir"] = _os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("42", "claude")] = "sess_existing"
    rt.run_compaction = AsyncMock(return_value="distilled context")

    # Stub provider.clear_session so cmd_clear doesn't try to touch disk.
    captured: dict = {}

    class _FakeProvider:
        def clear_session(self, sid, working_dir):
            captured["cleared"] = (sid, working_dir)
            return "deleted"

    monkeypatch.setattr(rt, "make_provider", lambda _name: _FakeProvider())

    reply = await cmd_compact_async(rt, "42")

    assert "Compacted" in reply
    rt.run_compaction.assert_awaited_once_with("42", "claude")
    assert rt.compact_seed_by_chat["42"] == "distilled context"
    # cmd_clear should have removed the active provider's session.
    assert ("42", "claude") not in rt.session_by_chat_provider
    assert captured["cleared"][0] == "sess_existing"


@pytest.mark.asyncio
async def test_compact_no_session_refuses(sample_config):
    """No session for this chat → return a 'nothing to compact' message."""
    rt = Runtime(sample_config)
    rt.run_compaction = AsyncMock()  # should never run

    reply = await cmd_compact_async(rt, "42")

    assert "Nothing to compact" in reply
    rt.run_compaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_refuses_while_busy(sample_config):
    """A locked chat (request in flight) gets a 'wait or stop' message."""
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("42", "claude")] = "sess_existing"
    rt.run_compaction = AsyncMock()
    lock = rt.get_chat_lock("42")
    await lock.acquire()
    try:
        reply = await cmd_compact_async(rt, "42")
    finally:
        lock.release()

    assert "Stop it" in reply
    rt.run_compaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_summary_empty_leaves_session(tmp_enso, sample_config):
    """If run_compaction returns empty, we don't clear or stash."""
    import os as _os

    sample_config["working_dir"] = _os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("42", "claude")] = "sess_existing"
    rt.run_compaction = AsyncMock(return_value="")

    reply = await cmd_compact_async(rt, "42")

    assert "failed" in reply.lower()
    assert rt.session_by_chat_provider[("42", "claude")] == "sess_existing"
    assert "42" not in rt.compact_seed_by_chat
