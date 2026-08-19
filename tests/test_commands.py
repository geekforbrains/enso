"""Tests for shared command handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from enso.commands import (
    cmd_clear,
    cmd_compact_async,
    cmd_effort,
    cmd_model,
    cmd_status,
    cmd_stop_async,
    cmd_use,
)
from enso.config import managed_workspace_path
from enso.core import ExecutionContext, Runtime
from enso.providers import PROVIDER_NAMES
from enso.providers.agy import AgyProvider
from enso.teams import Policy, Workspace


def _execution_context(
    sample_config: dict,
    chat_key: str,
    *,
    providers: tuple[str, ...] = tuple(PROVIDER_NAMES),
    include_global_messages: bool = False,
    **kwargs,
) -> ExecutionContext:
    path = managed_workspace_path("default")
    workspace = Workspace("test", path, "test", 1)
    policy = Policy("test", None, True, providers, providers[0], "*")
    settings_key = kwargs.pop("settings_key", chat_key)
    provider = kwargs.pop("provider", policy.default_provider)
    model = kwargs.pop(
        "model",
        sample_config["providers"][provider]["models"][0],
    )
    effort = kwargs.pop("effort", None)
    return ExecutionContext(
        chat_key=chat_key,
        settings_key=settings_key,
        path=path,
        workspace_id=workspace.name,
        workspace=workspace,
        policy=policy,
        include_global_messages=include_global_messages,
        provider=provider,
        model=model,
        effort=effort,
        provider_source=kwargs.pop("provider_source", "policy_default"),
        model_source=kwargs.pop("model_source", "provider_default"),
        effort_source=kwargs.pop("effort_source", "cli_default"),
        **kwargs,
    )


def test_cmd_model_selects_codex_alias(sample_config):
    sample_config["providers"]["codex"]["models"] = ["sol", "terra", "luna"]
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_provider("1", "codex")

    response, options = cmd_model(rt, "1", "terra", policy=policy)

    assert response == "codex model → terra"
    assert options == []
    assert rt.resolve_route_settings("1", policy).model == "terra"


def test_cmd_use_default_restores_policy_default(sample_config):
    rt = Runtime(sample_config)
    settings_key = "slack:account-1:channel-C1"
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_provider(settings_key, "codex")

    response, options = cmd_use(
        rt,
        settings_key,
        "default",
        policy=policy,
    )

    assert options == []
    assert response is not None and "default" in response.lower()
    resolved = rt.resolve_route_settings(settings_key, policy)
    assert (resolved.provider, resolved.provider_source) == (
        "claude",
        "policy_default",
    )


def test_cmd_model_default_restores_provider_default(sample_config):
    rt = Runtime(sample_config)
    settings_key = "slack:account-1:channel-C1"
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_model(settings_key, "claude", "sonnet")

    response, options = cmd_model(
        rt,
        settings_key,
        "default",
        policy=policy,
    )

    assert options == []
    assert response is not None and "default" in response.lower()
    resolved = rt.resolve_route_settings(settings_key, policy)
    assert (resolved.model, resolved.model_source) == (
        "opus",
        "provider_default",
    )


def test_provider_and_model_switching_follow_registry(sample_config):
    sample_config["providers"]["agy"]["models"] = list(AgyProvider.default_models)
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy

    response, providers = cmd_use(rt, "1", None, policy=policy)
    assert response is None
    assert [name for name, _active in providers] == PROVIDER_NAMES

    response, providers = cmd_use(rt, "1", "agy", policy=policy)
    assert response == "Provider set to agy."
    assert providers == []

    response, models = cmd_model(rt, "1", None, policy=policy)
    assert response is None
    assert [name for name, _active in models] == AgyProvider.default_models


def test_cmd_use_provider_list_cannot_exceed_policy(sample_config):
    rt = Runtime(sample_config)
    policy = _execution_context(
        sample_config,
        "conversation-1",
        providers=("claude",),
    ).policy

    response, options = cmd_use(
        rt,
        "route-1",
        "codex",
        policy=policy,
        providers=["claude", "codex"],
    )

    assert response == "Provider codex is not available here."
    assert options == []
    assert "route-1" not in rt.route_preferences


def test_cmd_effort_set_level(sample_config):
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    response, options = cmd_effort(rt, "1", "high", policy=policy)
    assert options == []
    assert response == "Effort \u2192 high"
    assert rt.resolve_route_settings("1", policy).effort == "high"


def test_cmd_effort_set_by_index(sample_config):
    """1-based index picks from levels supported by the current model."""
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    # Opus supports [low, medium, high, xhigh, max] — index 4 → xhigh
    response, _ = cmd_effort(rt, "1", "4", policy=policy)
    assert response == "Effort \u2192 xhigh"
    assert rt.resolve_route_settings("1", policy).effort == "xhigh"


def test_cmd_effort_default_clears(sample_config):
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_effort("1", "claude", "opus", "xhigh")
    response, _ = cmd_effort(rt, "1", "default", policy=policy)
    assert response is not None
    assert "cleared" in response.lower()
    resolved = rt.resolve_route_settings("1", policy)
    assert (resolved.effort, resolved.effort_source) == (None, "cli_default")


def test_cmd_effort_unknown_level(sample_config):
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    response, options = cmd_effort(rt, "1", "ludicrous", policy=policy)
    assert response is not None
    assert "Unknown effort" in response
    assert options == []


def test_cmd_effort_list_options_for_sonnet(sample_config):
    """Current Sonnet supports the full Claude effort range."""
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_model("1", "claude", "sonnet")
    response, options = cmd_effort(rt, "1", None, policy=policy)
    assert response is None
    levels = [name for name, _ in options]
    assert levels == ["low", "medium", "high", "xhigh", "max"]
    # Nothing selected yet
    assert not any(active for _, active in options)


def test_cmd_effort_list_options_marks_active(sample_config):
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_effort("1", "claude", "opus", "xhigh")
    response, options = cmd_effort(rt, "1", None, policy=policy)
    assert response is None
    assert ("xhigh", True) in options


def test_cmd_effort_clamp_warning_on_set(sample_config):
    """Setting max on a capped model reports the clamped value."""
    sample_config["providers"]["claude"]["models"].append("haiku")
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_model("1", "claude", "haiku")
    response, _ = cmd_effort(rt, "1", "max", policy=policy)
    assert response is not None
    assert "clamped to high" in response
    # Raw intent is preserved; the resolver clamps it at read time.
    assert rt.route_preferences["1"].efforts["claude"]["haiku"] == "max"
    assert rt.resolve_route_settings("1", policy).effort == "high"


def test_cmd_effort_codex_set_ultra(sample_config):
    sample_config["providers"]["codex"]["models"] = ["sol", "terra", "luna"]
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_provider("1", "codex")

    response, options = cmd_effort(rt, "1", "ultra", policy=policy)

    assert response == "Effort → ultra"
    assert options == []
    assert rt.resolve_route_settings("1", policy).effort == "ultra"


def test_cmd_effort_codex_lists_model_specific_levels(sample_config):
    sample_config["providers"]["codex"]["models"] = ["sol", "terra", "luna"]
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_provider("1", "codex")

    _, sol_options = cmd_effort(rt, "1", None, policy=policy)
    rt.set_route_model("1", "codex", "luna")
    _, luna_options = cmd_effort(rt, "1", None, policy=policy)

    assert [level for level, _ in sol_options] == [
        "low", "medium", "high", "xhigh", "max", "ultra",
    ]
    assert [level for level, _ in luna_options] == [
        "low", "medium", "high", "xhigh", "max",
    ]


def test_cmd_effort_codex_clamps_ultra_for_luna(sample_config):
    sample_config["providers"]["codex"]["models"] = ["luna"]
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_provider("1", "codex")

    response, _ = cmd_effort(rt, "1", "ultra", policy=policy)

    assert response is not None
    assert "clamped to max" in response
    assert rt.route_preferences["1"].efforts["codex"]["luna"] == "ultra"
    assert rt.resolve_route_settings("1", policy).effort == "max"


def test_cmd_effort_agy_uses_model_variants(sample_config):
    sample_config["providers"]["agy"]["models"] = list(AgyProvider.default_models)
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_provider("1", "agy")

    response, options = cmd_effort(rt, "1", "low", policy=policy)

    assert response == (
        "Agy effort is selected through /model; choose an "
        "effort-qualified model variant."
    )
    assert options == []
    assert rt.route_preferences["1"].efforts == {}


def test_cmd_status_includes_effort(sample_config):
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_effort("1", "claude", "opus", "xhigh")
    out = cmd_status(rt, "1", policy=policy)
    assert "Effort: xhigh (route selection)" in out


def test_cmd_status_includes_codex_effort(sample_config):
    sample_config["providers"]["codex"]["models"] = ["terra"]
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    rt.set_route_provider("1", "codex")
    rt.set_route_effort("1", "codex", "terra", "max")
    assert "Effort: max (route selection)" in cmd_status(rt, "1", policy=policy)


def test_cmd_status_reports_default_provenance(sample_config):
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config, "conversation-1").policy
    out = cmd_status(rt, "1", policy=policy)
    assert "Provider: claude (policy default)" in out
    assert "Model: opus (provider default)" in out
    assert "Effort: CLI default" in out
    assert "Runner" not in out


def test_cmd_clear_only_touches_policy_allowed_providers(sample_config, monkeypatch):
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("42", "claude")] = "claude-session"
    rt.session_by_chat_provider[("42", "codex")] = "codex-session"
    context = _execution_context(sample_config, "42", providers=("claude",))
    cleared: list[tuple[str, str | None, str]] = []

    class _FakeProvider:
        def __init__(self, name: str):
            self.name = name

        def clear_session(self, session_id, working_dir, *, policy_dir=None):
            cleared.append((self.name, session_id, working_dir))
            return "deleted"

    monkeypatch.setattr(
        rt,
        "make_provider",
        lambda name, *, context: _FakeProvider(name),
    )

    parts = cmd_clear(rt, "42", context=context, clear_all=True)

    assert parts == ["Claude: deleted"]
    assert cleared == [("claude", "claude-session", context.path)]
    assert rt.session_by_chat_provider[("42", "codex")] == "codex-session"


def test_cmd_clear_uses_provider_snapshotted_in_context(sample_config, monkeypatch):
    rt = Runtime(sample_config)
    conversation_key = "conversation-1"
    model = sample_config["providers"]["codex"]["models"][0]
    context = _execution_context(
        sample_config,
        conversation_key,
        settings_key="route-1",
        provider="codex",
        model=model,
        effort="high",
        provider_source="route",
        model_source="route",
        effort_source="route",
    )
    rt.session_by_chat_provider[(conversation_key, "claude")] = "claude-session"
    rt.session_by_chat_provider[(conversation_key, "codex")] = "codex-session"
    cleared: list[tuple[str, str | None]] = []

    class _FakeProvider:
        def __init__(self, name: str):
            self.name = name

        def clear_session(self, session_id, _working_dir, *, policy_dir=None):
            cleared.append((self.name, session_id))
            return "deleted"

    monkeypatch.setattr(
        rt,
        "make_provider",
        lambda name, *, context: _FakeProvider(name),
    )
    rt.resolve_route_settings = Mock(
        side_effect=AssertionError("clear reread mutable route preferences")
    )

    parts = cmd_clear(rt, conversation_key, context=context)

    assert parts == ["Codex: deleted"]
    assert cleared == [("codex", "codex-session")]
    assert (conversation_key, "claude") in rt.session_by_chat_provider
    assert (conversation_key, "codex") not in rt.session_by_chat_provider


@pytest.mark.asyncio
async def test_cmd_stop_finalizes_cleared_queue_items(sample_config):
    rt = Runtime(sample_config)
    chat_key = "teams:stable"
    completed = []
    context = _execution_context(
        sample_config,
        chat_key,
        on_complete=lambda outcome, reason: completed.append((outcome, reason)),
    )
    lock = rt.get_chat_lock(chat_key)
    await lock.acquire()
    try:
        await rt.dispatch("C1:thread", "queued", AsyncMock(), context=context)
        rt.stop_chat = AsyncMock(return_value=(False, None))

        response = await cmd_stop_async(rt, chat_key)
    finally:
        lock.release()

    assert response == "Cleared 1 queued message(s)."
    assert completed == [("blocked", "queue_cleared")]


# -- cmd_compact_async --


@pytest.mark.asyncio
async def test_compact_happy_path(tmp_enso, sample_config, monkeypatch):
    """Successful compaction stashes summary as seed and clears the session."""
    rt = Runtime(sample_config)
    context = _execution_context(sample_config, "42")
    rt.session_by_chat_provider[("42", "claude")] = "sess_existing"
    rt.run_compaction = AsyncMock(return_value="distilled context")

    # Stub provider.clear_session so cmd_clear doesn't try to touch disk.
    captured: dict = {}

    class _FakeProvider:
        def clear_session(self, sid, working_dir, *, policy_dir=None):
            captured["cleared"] = (sid, working_dir)
            return "deleted"

    monkeypatch.setattr(rt, "make_provider", lambda _name, *, context: _FakeProvider())

    reply = await cmd_compact_async(rt, "42", context=context)

    assert "Compacted" in reply
    rt.run_compaction.assert_awaited_once_with("42", "claude", context=context)
    assert rt.compact_seed_by_chat["42"] == "distilled context"
    # cmd_clear should have removed the active provider's session.
    assert ("42", "claude") not in rt.session_by_chat_provider
    assert captured["cleared"][0] == "sess_existing"


@pytest.mark.asyncio
async def test_compact_uses_provider_snapshotted_in_context(
    tmp_enso,
    sample_config,
    monkeypatch,
):
    rt = Runtime(sample_config)
    conversation_key = "conversation-1"
    model = sample_config["providers"]["codex"]["models"][0]
    context = _execution_context(
        sample_config,
        conversation_key,
        settings_key="route-1",
        provider="codex",
        model=model,
        effort="high",
        provider_source="route",
        model_source="route",
        effort_source="route",
    )
    rt.session_by_chat_provider[(conversation_key, "codex")] = "codex-session"
    rt.run_compaction = AsyncMock(return_value="distilled context")

    class _FakeProvider:
        def clear_session(self, session_id, _working_dir, *, policy_dir=None):
            assert session_id == "codex-session"
            return "deleted"

    monkeypatch.setattr(
        rt,
        "make_provider",
        lambda _name, *, context: _FakeProvider(),
    )
    rt.resolve_route_settings = Mock(
        side_effect=AssertionError("compact reread mutable route preferences")
    )

    reply = await cmd_compact_async(rt, conversation_key, context=context)

    assert "Compacted" in reply
    rt.run_compaction.assert_awaited_once_with(
        conversation_key,
        "codex",
        context=context,
    )
    assert rt.compact_seed_by_chat[conversation_key] == "distilled context"


@pytest.mark.asyncio
async def test_compact_no_session_refuses(sample_config):
    """No session for this chat → return a 'nothing to compact' message."""
    rt = Runtime(sample_config)
    rt.run_compaction = AsyncMock()  # should never run

    reply = await cmd_compact_async(rt, "42", context=_execution_context(sample_config, "42"))

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
        reply = await cmd_compact_async(
            rt, "42", context=_execution_context(sample_config, "42")
        )
    finally:
        lock.release()

    assert "Stop it" in reply
    rt.run_compaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_compact_summary_empty_leaves_session(tmp_enso, sample_config):
    """If run_compaction returns empty, we don't clear or stash."""
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("42", "claude")] = "sess_existing"
    rt.run_compaction = AsyncMock(return_value="")

    reply = await cmd_compact_async(rt, "42", context=_execution_context(sample_config, "42"))

    assert "failed" in reply.lower()
    assert rt.session_by_chat_provider[("42", "claude")] == "sess_existing"
    assert "42" not in rt.compact_seed_by_chat
