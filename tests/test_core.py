"""Tests for the runtime core."""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from enso import core as core_module
from enso import messages
from enso.config import managed_workspace_path
from enso.core import (
    ExecutionContext,
    Runtime,
    _redacted_command,
    format_elapsed,
    split_text,
    status_header,
    status_text,
)
from enso.instructions import InstructionError
from enso.jobs import Job
from enso.outbound import (
    CanvasPublication,
    ChartAxis,
    ChartPoint,
    ChartSeries,
    DataVisualizationBlock,
    MarkdownBlock,
    OutboundMessage,
    SeriesChart,
)
from enso.providers import BaseProvider, StreamEvent
from enso.providers.claude import ClaudeProvider
from enso.teams import Policy, Workspace
from enso.transports import TransportContext


def _execution_context(
    sample_config: dict,
    chat_key: str = "1",
    *,
    include_global_messages: bool = True,
    providers: tuple[str, ...] = ("claude", "codex", "agy"),
    concurrency: int = 1,
    **kwargs,
) -> ExecutionContext:
    """Build a complete unrestricted workspace binding for runtime tests."""
    path = managed_workspace_path()
    workspace = Workspace("default", path, "test", concurrency)
    policy = Policy("test", None, True, providers, providers[0], "*")
    provider = kwargs.pop("provider", providers[0])
    models = sample_config["providers"][provider]["models"]
    model = kwargs.pop("model", models[0] if models else "default")
    effort = kwargs.pop("effort", None)
    return ExecutionContext(
        chat_key=chat_key,
        settings_key=kwargs.pop("settings_key", chat_key),
        path=path,
        workspace_id=workspace.name,
        workspace=workspace,
        policy=policy,
        include_global_messages=include_global_messages,
        provider=provider,
        concurrency=concurrency,
        model=model,
        effort=effort,
        provider_source=kwargs.pop(
            "provider_source",
            "policy_default" if provider == policy.default_provider else "route",
        ),
        model_source=kwargs.pop("model_source", "provider_default"),
        effort_source=kwargs.pop(
            "effort_source",
            "route" if effort is not None else "cli_default",
        ),
        **kwargs,
    )


async def _process_request(
    runtime: Runtime,
    sample_config: dict,
    provider_name: str,
    prompt: str,
    chat_id: str,
    ctx: TransportContext,
    **kwargs,
):
    """Exercise request handling with a complete personal workspace binding."""
    context = await _prepared_context(
        runtime,
        sample_config,
        provider_name,
        chat_id,
        **kwargs,
    )
    return await runtime.process_request(
        provider_name,
        prompt,
        chat_id,
        ctx,
        context=context,
    )


async def _prepared_context(
    runtime: Runtime,
    sample_config: dict,
    provider_name: str,
    chat_key: str = "1",
    **kwargs,
) -> ExecutionContext:
    context = _execution_context(
        sample_config,
        chat_key,
        provider=provider_name,
        **kwargs,
    )
    _ensure_launch_discovery_fixture(context)
    return await runtime._prepare_execution_context(provider_name, context)


def _ensure_launch_discovery_fixture(context: ExecutionContext) -> None:
    """Complete the shared lightweight fixture only for real spawn tests."""
    from enso.repository import EnsoRepository
    from enso.scaffolding import ScaffoldService

    root = Path(context.path).parent.parent
    scaffold = ScaffoldService(root)
    scaffold.repair_global()
    scaffold.repair_workspace(context.workspace.name)
    workspace_agents = Path(context.path, "AGENTS.md")
    if not workspace_agents.exists():
        workspace_agents.write_text("# Test workspace instructions\n", encoding="utf-8")
    scaffold.repair_workspace(context.workspace.name)
    EnsoRepository(str(root)).ensure()

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


def test_format_elapsed_switches_units_as_a_run_lengthens():
    assert format_elapsed(0) == "0s"
    assert format_elapsed(59) == "59s"
    assert format_elapsed(60) == "1m 00s"
    assert format_elapsed(125) == "2m 05s"
    assert format_elapsed(3600) == "1h 00m"
    assert format_elapsed(4320) == "1h 12m"


def test_status_header_omits_effort_when_the_provider_has_none():
    assert status_header("claude", "opus", "high") == "claude · opus · high"
    assert status_header("agy", "gemini-3.6-flash-low") == "agy · gemini-3.6-flash-low"


def test_status_text_appends_the_current_action_when_one_is_known():
    header = "claude · opus · high"
    assert status_text(header, 12) == "claude · opus · high · 12s"
    assert status_text(header, 12, "Reading core.py") == (
        "claude · opus · high · 12s\n↳ Reading core.py"
    )


def test_redacted_command_hides_agy_prompt():
    rendered = _redacted_command(["agy", "--model", "model", "--prompt", "secret prompt"])
    assert "secret prompt" not in rendered
    assert "<prompt chars=13>" in rendered


def test_redacted_command_hides_codex_user_prompt():
    rendered = _redacted_command(["codex", "exec", "--", "user secret"])

    assert "user secret" not in rendered
    assert "<prompt chars=11>" in rendered


def test_redacted_command_hides_grok_single_prompt():
    """Grok's prompt rides attached to its flag, not behind a separator."""
    rendered = _redacted_command(
        ["grok", "--output-format", "streaming-messages-json", "--single=secret prompt"]
    )
    assert "secret prompt" not in rendered
    assert "--single=<prompt chars=13>" in rendered


def test_redacted_command_hides_grok_rules_instructions():
    """Grok's explicit shared instructions ride attached and must be redacted."""
    rendered = _redacted_command(
        ["grok", "--rules=SECRET OPERATOR GUIDANCE", "--single=hi"]
    )
    assert "SECRET" not in rendered
    assert "--rules=<instructions chars=24>" in rendered


def test_runtime_has_no_global_execution_directory_or_context(sample_config):
    runtime = Runtime(sample_config)

    assert not hasattr(runtime, "working_dir")
    assert not hasattr(runtime, "global_context")


def test_execution_context_requires_workspace_policy_and_message_scope(sample_config):
    path = managed_workspace_path()

    with pytest.raises(TypeError):
        ExecutionContext(chat_key="chat", path=path, workspace_id="test")  # type: ignore[call-arg]


def test_has_session_memory_reports_only_used_sessions(sample_config):
    """A reserved `new:` ID has sent the provider nothing, so it holds no memory."""
    runtime = Runtime(sample_config)

    assert not runtime.has_session_memory("chat", "claude")

    runtime.session_by_chat_provider[("chat", "claude")] = "new:abc-123"
    assert not runtime.has_session_memory("chat", "claude")

    runtime.session_by_chat_provider[("chat", "claude")] = "abc-123"
    assert runtime.has_session_memory("chat", "claude")
    # Sessions are per provider, never shared across them.
    assert not runtime.has_session_memory("chat", "codex")


# -- Bundled guidance --


def test_bundled_prompts_are_transport_neutral():
    prompts = importlib.resources.files("enso").joinpath("prompts")
    for filename in ("AGENTS.md", "WORKSPACE_AGENTS.md"):
        content = prompts.joinpath(filename).read_text(encoding="utf-8").lower()
        assert "slack" not in content
        assert "telegram" not in content


def test_runtime_has_no_content_installer_api():
    assert not hasattr(Runtime, "install_system_prompts")
    assert not hasattr(Runtime, "install_workspaces")
    assert not hasattr(Runtime, "_install_bundled_skills")
    assert not hasattr(Runtime, "_install_skill_tools")


# -- Runtime state --


def test_runtime_defaults(sample_config):
    rt = Runtime(sample_config)
    resolved = rt.resolve_route_settings(
        "route-1",
        _execution_context(sample_config).policy,
    )
    assert (resolved.provider, resolved.model, resolved.effort) == (
        "claude",
        "opus",
        None,
    )
    assert rt.agent_timeout == 30 * 60
    assert rt.debug_prompts is False
    assert rt.debug_events is False


def test_runtime_reads_configured_agent_timeout(sample_config):
    sample_config["agent"] = {"timeout": 75}

    assert Runtime(sample_config).agent_timeout == 75


def test_runtime_reads_debug_logging_flags(sample_config):
    sample_config["logging"] = {"debug_prompts": True, "debug_events": True}
    rt = Runtime(sample_config)
    assert rt.debug_prompts is True
    assert rt.debug_events is True


def test_route_preferences_use_one_durable_namespace(tmp_enso, sample_config):
    """Provider, model, and effort choices persist together by route."""
    rt = Runtime(sample_config)
    route_key = "slack:account-1:channel-C1"
    model = sample_config["providers"]["codex"]["models"][0]

    rt.set_route_provider(route_key, "codex")
    rt.set_route_model(route_key, "codex", model)
    rt.set_route_effort(route_key, "codex", model, "high")
    rt.save_state()

    persisted = json.loads(Path(tmp_enso, "state.json").read_text())
    assert persisted["version"] == 3
    assert persisted["route_preferences"] == {
        route_key: {
            "provider": "codex",
            "models": {"codex": model},
            "efforts": {"codex": {model: "high"}},
        }
    }
    assert "active_provider_by_chat" not in persisted
    assert "active_model_by_chat_provider" not in persisted
    assert "effort_by_chat_provider_model" not in persisted

    loaded = Runtime(sample_config)
    loaded.load_state()
    resolved = loaded.resolve_route_settings(
        route_key,
        _execution_context(sample_config).policy,
    )
    assert (resolved.provider, resolved.model, resolved.effort) == (
        "codex",
        model,
        "high",
    )


def test_resolve_route_settings_reports_default_provenance(sample_config):
    rt = Runtime(sample_config)

    resolved = rt.resolve_route_settings(
        "slack:account-1:channel-C1",
        _execution_context(sample_config).policy,
    )

    assert (resolved.provider, resolved.provider_source) == (
        "claude",
        "policy_default",
    )
    assert (resolved.model, resolved.model_source) == (
        "opus",
        "provider_default",
    )
    assert (resolved.effort, resolved.effort_source) == (None, "cli_default")
    assert rt.route_preferences == {}


def test_resolve_route_settings_reports_selected_provenance(sample_config):
    rt = Runtime(sample_config)
    route_key = "slack:account-1:channel-C1"
    model = sample_config["providers"]["codex"]["models"][0]
    rt.set_route_provider(route_key, "codex")
    rt.set_route_model(route_key, "codex", model)
    rt.set_route_effort(route_key, "codex", model, "high")

    resolved = rt.resolve_route_settings(
        route_key,
        _execution_context(sample_config).policy,
    )

    assert (resolved.provider, resolved.provider_source) == ("codex", "route")
    assert (resolved.model, resolved.model_source) == (model, "route")
    assert (resolved.effort, resolved.effort_source) == ("high", "route")


def test_resolve_route_settings_falls_back_without_erasing_disallowed_choice(
    sample_config,
):
    rt = Runtime(sample_config)
    route_key = "slack:account-1:channel-C1"
    rt.set_route_provider(route_key, "codex")
    restricted = _execution_context(
        sample_config,
        providers=("claude",),
    ).policy

    resolved = rt.resolve_route_settings(route_key, restricted)

    assert (resolved.provider, resolved.provider_source) == (
        "claude",
        "policy_default",
    )
    assert rt.route_preferences[route_key].provider == "codex"


@pytest.mark.parametrize(
    ("version", "model_state", "effort_state", "session_state"),
    [
        (
            1,
            {"conversation-1:claude": "sonnet"},
            {"conversation-1:claude:sonnet": "high"},
            {"conversation-1:claude": "session-1"},
        ),
        (
            2,
            [
                {
                    "chat": "conversation-1",
                    "provider": "claude",
                    "model": "sonnet",
                }
            ],
            [
                {
                    "chat": "conversation-1",
                    "provider": "claude",
                    "model": "sonnet",
                    "effort": "high",
                }
            ],
            [
                {
                    "chat": "conversation-1",
                    "provider": "claude",
                    "session": "session-1",
                }
            ],
        ),
    ],
)
def test_load_legacy_state_drops_selections_but_preserves_conversation_and_job_state(
    tmp_enso,
    sample_config,
    version,
    model_state,
    effort_state,
    session_state,
):
    """The v3 boundary discards ambiguous conversation-scoped preferences only."""
    timestamp = datetime.now().isoformat()
    state_file = Path(tmp_enso, "state.json")
    state_file.write_text(
        json.dumps(
            {
                "version": version,
                "active_provider_by_chat": {"conversation-1": "claude"},
                "active_model_by_chat_provider": model_state,
                "effort_by_chat_provider_model": effort_state,
                "session_by_chat_provider": session_state,
                "compact_seed_by_chat": {"conversation-1": "summary"},
                "last_active": {"conversation-1": timestamp},
                "job_last_run": {"daily": timestamp},
                "job_failure_alerts": {"daily": {"failure_count": 2}},
            }
        )
    )

    rt = Runtime(sample_config)
    rt.load_state()

    assert rt.route_preferences == {}
    assert rt.session_by_chat_provider[("conversation-1", "claude")] == "session-1"
    assert rt.compact_seed_by_chat["conversation-1"] == "summary"
    assert rt._last_active["conversation-1"] == datetime.fromisoformat(timestamp)
    assert rt.conversation_is_active("conversation-1")
    assert rt.jobs.last_run["daily"] == datetime.fromisoformat(timestamp)
    assert rt.jobs.failure_alerts["daily"] == {"failure_count": 2}

    migrated = json.loads(state_file.read_text())
    assert migrated["version"] == 3
    assert "active_provider_by_chat" not in migrated
    assert "active_model_by_chat_provider" not in migrated
    assert "effort_by_chat_provider_model" not in migrated


def test_make_provider_uses_configured_path(sample_config):
    sample_config["providers"]["claude"]["path"] = "/custom/claude"
    rt = Runtime(sample_config)
    provider = rt.make_provider("claude", context=_execution_context(sample_config))
    assert isinstance(provider, ClaudeProvider)
    assert provider.path == "/custom/claude"


def test_make_provider_binds_working_dir(sample_config):
    """Providers see the directory their process will run in (agy needs it
    to pin conversations to the workspace project)."""
    rt = Runtime(sample_config)
    context = _execution_context(sample_config)
    provider = rt.make_provider("agy", context=context)
    assert provider.working_dir == context.path
    assert provider.timeout == rt.agent_timeout


def test_make_provider_unknown_provider_raises(sample_config):
    rt = Runtime(sample_config)
    with pytest.raises(ValueError, match="Unknown provider"):
        rt.make_provider("retired", context=_execution_context(sample_config))


def test_session_state_persistence(tmp_enso, sample_config):
    """Conversation sessions survive a save/load roundtrip."""
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("42", "codex")] = "sess_123"
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()
    assert rt2.session_by_chat_provider[("42", "codex")] == "sess_123"


def test_runtime_state_roundtrip_preserves_opaque_team_keys(tmp_enso, sample_config):
    key = "teams:0123456789abcdef"
    rt = Runtime(sample_config)
    model = rt.models["codex"][0]
    rt.set_route_provider(key, "codex")
    rt.set_route_model(key, "codex", model)
    rt.set_route_effort(key, "codex", model, "high")
    rt.session_by_chat_provider[(key, "codex")] = "session-1"
    rt.save_state()

    loaded = Runtime(sample_config)
    loaded.load_state()

    resolved = loaded.resolve_route_settings(
        key,
        _execution_context(sample_config).policy,
    )
    assert (resolved.provider, resolved.model, resolved.effort) == (
        "codex",
        model,
        "high",
    )
    assert loaded.session_by_chat_provider[(key, "codex")] == "session-1"


def test_save_state_failure_preserves_existing_file_and_removes_temp(
    tmp_enso, sample_config, monkeypatch, caplog
):
    state_file = Path(tmp_enso, "state.json")
    original = b'{"existing": "state"}\n'
    state_file.write_bytes(original)
    rt = Runtime(sample_config)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("enso.core.os.replace", fail_replace)

    rt.save_state()

    assert state_file.read_bytes() == original
    assert list(Path(tmp_enso).glob("*.tmp")) == []
    assert "Failed to save state" in caplog.text


def test_load_state_retires_legacy_task_runner_key(tmp_enso, sample_config):
    """The removed scheduler's reserved state does not linger after upgrade."""
    timestamp = datetime.now().isoformat()
    state_file = Path(tmp_enso, "state.json")
    state_file.write_text(json.dumps({
        "job_last_run": {
            "__task_runner__": "obsolete-value-need-not-be-a-timestamp",
            "real-job": timestamp,
        },
    }))

    rt = Runtime(sample_config)
    rt.load_state()

    assert "__task_runner__" not in rt.jobs.last_run
    assert rt.jobs.last_run["real-job"] == datetime.fromisoformat(timestamp)
    persisted = json.loads(state_file.read_text())
    assert "__task_runner__" not in persisted["job_last_run"]
    assert persisted["job_last_run"]["real-job"] == timestamp


def test_compact_seed_persistence(tmp_enso, sample_config):
    """Compact seeds survive save/load roundtrip."""
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


@pytest.mark.asyncio
async def test_run_compaction_honors_context_effort_without_model_override(
    sample_config,
):
    rt = Runtime(sample_config)
    context = _execution_context(sample_config, "42", effort="high")
    captured: dict[str, str | None] = {}

    class _FakeProvider:
        def format_response(self, parts):
            return "".join(parts)

    async def fake_run_provider(
        _provider, _prompt, _chat_id, _model, *, effort, context
    ):
        captured["effort"] = effort
        yield StreamEvent(kind="response", text="summary")

    rt._prepare_execution_context = AsyncMock(return_value=context)
    rt.make_provider = lambda _name, *, timeout, context: _FakeProvider()
    rt.run_provider = fake_run_provider

    summary = await rt.run_compaction("42", "claude", context=context)

    assert summary == "summary"
    assert captured["effort"] == "high"


def test_prune_clears_compact_seed(tmp_enso, sample_config):
    """Stale chats lose their compact seed too."""
    rt = Runtime(sample_config)
    rt.compact_seed_by_chat["42"] = "old summary"
    rt._last_active["42"] = datetime.now() - timedelta(days=999)
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()  # triggers prune
    assert "42" not in rt2.compact_seed_by_chat


def test_session_ttl_pruning_leaves_route_preferences(
    tmp_enso,
    sample_config,
):
    rt = Runtime(sample_config)
    route_key = "slack:account-1:channel-C1"
    conversation_key = "slack:account-1:channel-C1:thread-T1"
    model = sample_config["providers"]["codex"]["models"][0]
    rt.set_route_provider(route_key, "codex")
    rt.set_route_model(route_key, "codex", model)
    rt.set_route_effort(route_key, "codex", model, "high")
    rt.session_by_chat_provider[(conversation_key, "codex")] = "old-session"
    rt.compact_seed_by_chat[conversation_key] = "old summary"
    rt._last_active[conversation_key] = datetime.now() - timedelta(days=999)
    rt.save_state()

    loaded = Runtime(sample_config)
    loaded.load_state()

    assert route_key in loaded.route_preferences
    assert (conversation_key, "codex") not in loaded.session_by_chat_provider
    assert conversation_key not in loaded.compact_seed_by_chat
    assert conversation_key not in loaded._last_active


# -- Effort resolution --


def test_resolve_route_effort_clamps_to_model_cap(sample_config):
    """Requesting max on a model that caps at high returns high."""
    sample_config["providers"]["claude"]["models"].append("haiku")
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config).policy
    rt.set_route_model("route-1", "claude", "haiku")
    rt.set_route_effort("route-1", "claude", "haiku", "max")

    resolved = rt.resolve_route_settings("route-1", policy)

    assert (resolved.effort, resolved.effort_source) == ("high", "route")


def test_resolve_route_effort_codex_clamps_to_model_cap(sample_config):
    sample_config["providers"]["codex"]["models"] = ["luna"]
    rt = Runtime(sample_config)
    policy = _execution_context(sample_config).policy
    rt.set_route_provider("route-1", "codex")
    rt.set_route_effort("route-1", "codex", "luna", "ultra")

    resolved = rt.resolve_route_settings("route-1", policy)

    assert (resolved.effort, resolved.effort_source) == ("max", "route")


class _EmptyAsyncStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeSpawnedProcess:
    pid = 42
    returncode = 0
    stdout = _EmptyAsyncStream()
    stderr = None

    async def wait(self):
        return 0


class _FakePlainProcess:
    pid = 43
    returncode = 0
    stdout = object()
    stderr = object()

    async def communicate(self):
        return b"First paragraph.\n\nSecond paragraph.\n", b""


class _CapturingProvider(BaseProvider):
    """Minimal provider that records the launch-boundary instruction revision."""

    name = "agy"

    def __init__(self):
        super().__init__("fake")
        self.instruction_contents: list[str] = []

    def build_command(
        self,
        prompt,
        model,
        session_id=None,
        *,
        effort=None,
        launch=None,
        instructions=None,
    ):
        assert instructions is not None
        self.instruction_contents.append(instructions.content)
        return ["fake"]

    def build_batch_command(
        self, prompt, model, *, effort=None, launch=None, instructions=None
    ):
        raise AssertionError("interactive test must not build a batch command")

    def parse_event(self, event):
        return []


class _RetryingCapturingProvider(_CapturingProvider):
    def parse_event(self, event):
        return [StreamEvent(kind="error", text="transient startup failure")]

    def retryable_error(self, text):
        return text == "transient startup failure"


class _OneLineStream:
    def __init__(self, line: bytes):
        self._line = line

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._line:
            raise StopAsyncIteration
        line, self._line = self._line, b""
        return line


class _RetryProcess:
    pid = 44
    returncode = 1
    stderr = None

    def __init__(self):
        self.stdout = _OneLineStream(b'{"error": true}\n')

    async def wait(self):
        return 1


@pytest.mark.asyncio
async def test_run_provider_rejects_unprepared_execution_context(sample_config):
    rt = Runtime(sample_config)
    context = _execution_context(sample_config)
    provider = rt.make_provider("claude", context=context)
    rt._spawn_process = AsyncMock(side_effect=AssertionError("must not spawn"))

    with pytest.raises(RuntimeError, match="prepared"):
        async for _event in rt.run_provider(
            provider, "hi", "1", "opus", context=context
        ):
            pass

    rt._spawn_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_preparation_does_not_cache_shared_instructions(sample_config):
    rt = Runtime(sample_config)

    prepared = await rt._prepare_execution_context("claude", _execution_context(sample_config))

    assert prepared.launch is not None
    assert not hasattr(prepared, "instructions")


@pytest.mark.asyncio
async def test_run_provider_revalidates_instructions_after_context_preparation(
    tmp_enso, sample_config
):
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "agy")
    provider = _CapturingProvider()
    rt._spawn_process = AsyncMock(return_value=_FakeSpawnedProcess())

    async for _event in rt.run_provider(provider, "first", "1", "model", context=context):
        pass

    Path(tmp_enso, "AGENTS.md").write_text(
        "# Revised shared instructions\n", encoding="utf-8"
    )
    async for _event in rt.run_provider(provider, "second", "1", "model", context=context):
        pass

    assert provider.instruction_contents == [
        "# Test shared instructions\n",
        "# Revised shared instructions\n",
    ]


@pytest.mark.asyncio
async def test_run_provider_rejects_invalid_discovery_created_after_preparation(
    tmp_enso, sample_config
):
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "agy")
    Path(context.path, ".git").touch()
    rt._spawn_process = AsyncMock(side_effect=AssertionError("must not spawn"))

    with pytest.raises(InstructionError, match=r"forbidden \.git entry"):
        async for _event in rt.run_provider(
            _CapturingProvider(), "hi", "1", "model", context=context
        ):
            pass

    rt._spawn_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_provider_rejects_deleted_instructions_after_preparation(
    tmp_enso, sample_config
):
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "agy")
    Path(tmp_enso, "AGENTS.md").unlink()
    rt._spawn_process = AsyncMock(side_effect=AssertionError("must not spawn"))

    with pytest.raises(InstructionError, match="instruction source is missing"):
        async for _event in rt.run_provider(
            _CapturingProvider(), "hi", "1", "model", context=context
        ):
            pass

    rt._spawn_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_revalidates_current_instructions_before_second_spawn(
    tmp_enso, sample_config
):
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "agy")
    provider = _RetryingCapturingProvider()
    spawn_count = 0

    async def fake_spawn(*_args, **_kwargs):
        nonlocal spawn_count
        spawn_count += 1
        if spawn_count == 1:
            Path(tmp_enso, "AGENTS.md").write_text(
                "# Revised before retry\n", encoding="utf-8"
            )
        return _RetryProcess()

    rt._spawn_process = fake_spawn

    _parts, error, timed_out = await rt._collect_provider_output(
        provider,
        "hello",
        "1",
        "model",
        effort=None,
        origin_env={},
        context=context,
        state={},
    )

    assert not timed_out
    assert error == "transient startup failure"
    assert spawn_count == 2
    assert provider.instruction_contents == [
        "# Test shared instructions\n",
        "# Revised before retry\n",
    ]


@pytest.mark.asyncio
async def test_run_provider_injects_extra_env(tmp_enso, sample_config, monkeypatch):
    """extra_env reaches create_subprocess_exec merged on top of os.environ."""
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "claude")

    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["command"] = args
        captured["env"] = kwargs.get("env")
        captured["stdin"] = kwargs.get("stdin")
        return _FakeSpawnedProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    provider = rt.make_provider("claude", context=context)
    gen = rt.run_provider(
        provider, "hi", "1", "opus",
        context=context,
        extra_env={"ENSO_ORIGIN_CHANNEL": "C012345"},
    )
    async for _ in gen:
        pass

    env = captured["env"]
    assert env is not None, "env= must be passed when extra_env is set"
    assert env["ENSO_ORIGIN_CHANNEL"] == "C012345"
    # Parent env is preserved (PATH always exists on Unix / Windows).
    assert "PATH" in env
    assert captured["stdin"] == asyncio.subprocess.DEVNULL
    assert "--append-system-prompt-file" not in captured["command"]


@pytest.mark.asyncio
async def test_run_provider_omits_env_when_not_requested(tmp_enso, sample_config, monkeypatch):
    """Without extra_env the child inherits the parent env implicitly."""
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "claude")

    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["env"] = kwargs.get("env", "SENTINEL_UNSET")
        captured["stdin"] = kwargs.get("stdin")
        return _FakeSpawnedProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    provider = rt.make_provider("claude", context=context)
    gen = rt.run_provider(provider, "hi", "1", "opus", context=context)
    async for _ in gen:
        pass

    assert captured["env"] == "SENTINEL_UNSET"
    assert captured["stdin"] == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_run_provider_handles_agy_plain_output_and_captures_session(
    tmp_enso, sample_config, monkeypatch,
):
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "agy")
    session_id = "44444444-4444-4444-8444-444444444444"

    async def fake_spawn(*args, **kwargs):
        log_path = args[args.index("--log-file") + 1]
        Path(log_path).write_text(f"Print mode: conversation={session_id}, sending message\n")
        return _FakePlainProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    events = [
        event
        async for event in rt.run_provider(
            rt.make_provider("agy", context=context),
            "hello",
            "1",
            "gemini-3.6-flash-high",
            context=context,
        )
    ]

    assert [(event.kind, event.text) for event in events] == [
        ("response", "First paragraph.\n\nSecond paragraph."),
        ("session", ""),
    ]
    assert rt.session_by_chat_provider[("1", "agy")] == session_id


@pytest.mark.asyncio
async def test_run_provider_cleans_agy_log_when_spawn_fails(
    tmp_enso, sample_config, monkeypatch,
):
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "agy")
    provider = rt.make_provider("agy", context=context)
    captured: dict[str, str] = {}

    async def fake_spawn(*args, **kwargs):
        captured["log_path"] = args[args.index("--log-file") + 1]
        raise OSError("spawn failed")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    with pytest.raises(OSError, match="spawn failed"):
        async for _event in rt.run_provider(
            provider, "hello", "1", "gemini-3.6-flash-high", context=context,
        ):
            pass

    assert not Path(captured["log_path"]).exists()
    assert provider._log_path is None


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


def test_get_or_create_session_agy(sample_config):
    """Agy creates its own session ID, captured from its private run log."""
    rt = Runtime(sample_config)
    assert rt._get_or_create_session("1", "agy") is None


def test_should_run_job_first_time(sample_config):
    """First encounter with a job should not fire immediately."""
    rt = Runtime(sample_config)
    job = Job(
        dir_name="test", name="Test", schedule="* * * * *",
        provider="claude", model="sonnet", workspace="unused",
    )
    assert rt.jobs._should_run_job(job, datetime.now()) is False
    assert "test" in rt.jobs.last_run


def test_should_run_job_due(sample_config):
    """Job should run when next cron time has passed."""
    rt = Runtime(sample_config)
    job = Job(
        dir_name="test", name="Test", schedule="* * * * *",
        provider="claude", model="sonnet", workspace="unused",
    )
    rt.jobs.last_run["test"] = datetime.now() - timedelta(minutes=2)
    assert rt.jobs._should_run_job(job, datetime.now()) is True


def test_should_run_job_skips_stale_misfire(sample_config):
    """Missed daily jobs should not run hours late by default."""
    rt = Runtime(sample_config)
    job = Job(
        dir_name="today",
        name="Today",
        schedule="30 6 * * *",
        provider="claude",
        model="opus",
        workspace="unused",
    )
    now = datetime(2026, 5, 14, 21, 0)
    rt.jobs.last_run["today"] = datetime(2026, 5, 13, 6, 30)

    assert rt.jobs._should_run_job(job, now) is False
    assert rt.jobs.last_run["today"] == now


def test_should_run_job_allows_explicit_catch_up(sample_config):
    """Jobs can opt into stale catch-up when that is intentional."""
    rt = Runtime(sample_config)
    job = Job(
        dir_name="catch-up",
        name="Catch Up",
        schedule="30 6 * * *",
        provider="claude",
        model="opus",
        workspace="unused",
        catch_up=True,
    )
    now = datetime(2026, 5, 14, 21, 0)
    rt.jobs.last_run["catch-up"] = datetime(2026, 5, 13, 6, 30)

    assert rt.jobs._should_run_job(job, now) is True


def test_should_run_job_not_due(sample_config):
    """Job should not run when it was just executed."""
    rt = Runtime(sample_config)
    job = Job(
        dir_name="test", name="Test", schedule="0 9 * * *",
        provider="claude", model="sonnet", workspace="unused",
    )
    rt.jobs.last_run["test"] = datetime.now()
    assert rt.jobs._should_run_job(job, datetime.now()) is False


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
    rt = Runtime(sample_config)

    # Create an old conversation and a fresh one
    rt.session_by_chat_provider[("old_chat", "claude")] = "old_session"
    rt._last_active["old_chat"] = datetime.now() - timedelta(days=60)

    rt.session_by_chat_provider[("fresh_chat", "codex")] = "fresh_session"
    rt._last_active["fresh_chat"] = datetime.now()

    rt.save_state()

    # Load into a new runtime — pruning should remove old_chat
    rt2 = Runtime(sample_config)
    rt2.load_state()

    assert ("old_chat", "claude") not in rt2.session_by_chat_provider
    assert "old_chat" not in rt2._last_active
    # Fresh one survives
    assert rt2.session_by_chat_provider[("fresh_chat", "codex")] == "fresh_session"


# -- Message injection --


@pytest.mark.asyncio
async def test_process_request_injects_messages(tmp_enso, sample_config):
    """Background messages are consumed and injected into the prompt."""
    messages.send("background info", source="test")
    assert len(messages.pending()) == 1

    rt = Runtime(sample_config)
    prompts_received: list[str] = []

    # Mock the provider and run_provider to capture the prompt
    class FakeCtx(TransportContext):
        async def reply(self, text): pass
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def fake_run(
        provider, prompt, chat_id, model, *, effort=None, extra_env=None, context=None,
    ):
        prompts_received.append(prompt)
        if False:
            yield  # make this an async generator

    rt.run_provider = fake_run
    context = await _prepared_context(rt, sample_config, "claude", "1")
    await rt.process_request(
        "claude",
        "user message",
        "1",
        FakeCtx(),
        context=context,
    )

    # Messages should have been consumed
    assert messages.pending() == []
    assert len(prompts_received) == 1
    assert "background info" in prompts_received[0]
    assert "user message" in prompts_received[0]


@pytest.mark.asyncio
async def test_invalid_discovery_preserves_messages_and_compact_seed_before_provider_work(
    tmp_enso, sample_config
):
    chat_id = "conversation-1"
    messages.send("global background", source="test")
    messages.send("scoped background", source="test", conversation_id=chat_id)
    runtime = Runtime(sample_config)
    runtime.compact_seed_by_chat[chat_id] = "prior compacted context"
    context = await _prepared_context(runtime, sample_config, "claude", chat_id)
    Path(context.path, ".git").touch()
    provider_factory = runtime.make_provider
    runtime.make_provider = Mock(wraps=provider_factory)
    runtime._spawn_process = AsyncMock(side_effect=AssertionError("must not spawn"))
    transport = _OutcomeCtx()

    result = await runtime.process_request(
        "claude",
        "user message",
        chat_id,
        transport,
        context=context,
    )

    assert result == ("blocked", "execution_unavailable")
    assert [message["text"] for message in messages.pending()] == [
        "global background",
        "scoped background",
    ]
    assert runtime.compact_seed_by_chat[chat_id] == "prior compacted context"
    runtime.make_provider.assert_not_called()
    runtime._spawn_process.assert_not_awaited()


def test_prompt_assembly_does_not_consume_global_messages_without_opt_in(sample_config):
    messages.send("global background", source="test")
    rt = Runtime(sample_config)
    context = _execution_context(
        sample_config, "shared", include_global_messages=False
    )

    prompt, _, _ = rt._assemble_prompt(
        "user message", "shared", "claude", _OutcomeCtx(), context
    )

    assert prompt == "user message"
    assert [message["text"] for message in messages.pending()] == ["global background"]


class _OutcomeCtx(TransportContext):
    def __init__(self): self.replies = []
    async def reply(self, text): self.replies.append(text)
    async def reply_status(self, text): return "h"
    async def edit_status(self, handle, text): pass
    async def delete_status(self, handle): pass
    async def send_typing(self): pass
    def get_origin_env(self): return {}


@pytest.mark.asyncio
async def test_dispatch_uses_selection_snapshotted_in_execution_context(sample_config):
    """An intaken message cannot change provider when preferences change."""
    rt = Runtime(sample_config)
    model = sample_config["providers"]["codex"]["models"][0]
    context = _execution_context(
        sample_config,
        "conversation-1",
        settings_key="route-1",
        provider="codex",
        model=model,
        effort="high",
        provider_source="route",
        model_source="route",
        effort_source="route",
    )
    rt.resolve_route_settings = Mock(
        side_effect=AssertionError("dispatch reread mutable route preferences")
    )
    rt._run_request = AsyncMock()
    transport = _OutcomeCtx()

    await rt.dispatch("C1:thread", "hello", transport, context=context)

    rt._run_request.assert_awaited_once_with(
        "codex",
        "hello",
        transport,
        context,
    )


@pytest.mark.asyncio
async def test_queued_dispatch_keeps_selection_snapshotted_at_intake(sample_config):
    rt = Runtime(sample_config)
    model = sample_config["providers"]["codex"]["models"][0]
    context = _execution_context(
        sample_config,
        "conversation-1",
        settings_key="route-1",
        provider="codex",
        model=model,
        effort="high",
        provider_source="route",
        model_source="route",
        effort_source="route",
    )
    transport = _OutcomeCtx()
    lock = rt.get_chat_lock(context.chat_key)
    await lock.acquire()
    try:
        await rt.dispatch("C1:thread", "queued", transport, context=context)
    finally:
        lock.release()

    rt.set_route_provider("route-1", "claude")
    rt._run_request = AsyncMock()
    await rt._drain_queue(context.chat_key)

    rt._run_request.assert_awaited_once_with(
        "codex",
        "queued",
        transport,
        context,
    )


@pytest.mark.asyncio
async def test_workspace_semaphore_serializes_concurrent_runs(sample_config):
    """concurrency=1 caps a workspace to one active provider run across chats."""
    rt = Runtime(sample_config)
    active = 0
    peak = 0

    async def slow_run(*a, **k):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        yield StreamEvent(kind="response", text="ok")
    rt.run_provider = slow_run

    def ctx_for(conv):
        return _execution_context(sample_config, conv, concurrency=1)
    first_context = ctx_for("k1")
    second_context = ctx_for("k2")
    _ensure_launch_discovery_fixture(first_context)
    # Two distinct conversations, same workspace — must not overlap.
    await asyncio.gather(
        rt._run_request("claude", "a", _OutcomeCtx(), first_context),
        rt._run_request("claude", "b", _OutcomeCtx(), second_context),
    )
    assert peak == 1


@pytest.mark.asyncio
async def test_personal_context_is_still_workspace_bounded(sample_config):
    """Opting into global messages never bypasses workspace concurrency."""
    rt = Runtime(sample_config)
    active = 0
    peak = 0

    async def slow_run(*a, **k):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        yield StreamEvent(kind="response", text="ok")
    rt.run_provider = slow_run
    first_context = _execution_context(
        sample_config, "k1", include_global_messages=True
    )
    second_context = _execution_context(
        sample_config, "k2", include_global_messages=True
    )
    _ensure_launch_discovery_fixture(first_context)
    await asyncio.gather(
        rt._run_request(
            "claude",
            "a",
            _OutcomeCtx(),
            first_context,
        ),
        rt._run_request(
            "claude",
            "b",
            _OutcomeCtx(),
            second_context,
        ),
    )
    assert peak == 1


@pytest.mark.asyncio
async def test_process_request_returns_terminal_outcome(sample_config):
    rt = Runtime(sample_config)

    async def ok_run(*a, **k):
        yield StreamEvent(kind="response", text="hi")
    rt.run_provider = ok_run
    context = await _prepared_context(rt, sample_config, "claude")
    assert await rt.process_request(
        "claude", "x", "1", _OutcomeCtx(), context=context
    ) == ("completed", None)

    async def err_run(*a, **k):
        yield StreamEvent(kind="error", text="boom")
    rt.run_provider = err_run
    outcome, _reason = await rt.process_request(
        "claude", "x", "1", _OutcomeCtx(), context=context
    )
    assert outcome == "error"


@pytest.mark.asyncio
async def test_run_request_reports_outcome_to_on_complete(sample_config):
    rt = Runtime(sample_config)
    seen = []

    async def ok_run(*a, **k):
        yield StreamEvent(kind="response", text="hi")
    rt.run_provider = ok_run
    ctx_obj = _execution_context(
        sample_config,
        "k",
        on_complete=lambda outcome, reason: seen.append((outcome, reason)),
    )
    _ensure_launch_discovery_fixture(ctx_obj)
    await rt._run_request("claude", "x", _OutcomeCtx(), ctx_obj)
    assert seen == [("completed", None)]


@pytest.mark.asyncio
async def test_early_returns_finalize_teams_turn(sample_config):
    """Queue-full and update-in-progress must not leak an audited turn."""
    rt = Runtime(sample_config)
    finalized = []
    ctx_obj = _execution_context(
        sample_config,
        "k",
        on_complete=lambda outcome, reason: finalized.append((outcome, reason)),
    )

    # update-in-progress
    rt.update_in_progress = True
    await rt.dispatch("conv", "hi", _OutcomeCtx(), context=ctx_obj)
    assert finalized == [("blocked", "update_in_progress")]
    rt.update_in_progress = False

    # queue-full: hold the lock and fill the queue
    finalized.clear()
    lock = rt.get_chat_lock("k")
    await lock.acquire()
    try:
        for _ in range(rt._queue_by_conversation.get("k", []).__len__(), 5):
            await rt.dispatch("conv", "q", _OutcomeCtx(), context=ctx_obj)
        finalized.clear()
        await rt.dispatch("conv", "overflow", _OutcomeCtx(), context=ctx_obj)
    finally:
        lock.release()
    assert finalized == [("blocked", "queue_full")]


@pytest.mark.asyncio
async def test_process_request_uses_normalized_status_and_plain_final_response(sample_config):
    rt = Runtime(sample_config)

    class FakeCtx(TransportContext):
        def __init__(self):
            self.statuses = []
            self.replies = []
            self.deleted = []

        async def reply(self, text): self.replies.append(text)
        async def reply_status(self, text):
            self.statuses.append(text)
            return "handle"
        async def edit_status(self, handle, text): self.statuses.append(text)
        async def delete_status(self, handle): self.deleted.append(handle)
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text="Done")

    ctx = FakeCtx()
    rt.run_provider = fake_run
    await _process_request(
        rt,
        sample_config,
        "claude",
        "hello",
        "1",
        ctx,
        effort="high",
    )

    assert ctx.statuses == ["claude · opus · high · 0s\n↳ Processing"]
    assert ctx.deleted == ["handle"]
    assert ctx.replies == ["Done"]


@pytest.mark.asyncio
async def test_process_request_sends_rich_markdown_before_legacy_splitting(sample_config):
    rt = Runtime(sample_config)
    response = "```text\n" + ("a" * 100 + "\n") * 410 + "```"

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert len(response) > 40000
    assert ctx.rich_replies == [response]
    assert ctx.replies == []


@pytest.mark.asyncio
async def test_process_request_keeps_provider_errors_on_plain_reply_path(sample_config):
    rt = Runtime(sample_config)

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="error", text="Error: provider failed")

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.replies == ["Error: provider failed"]
    assert ctx.rich_replies == []


@pytest.mark.asyncio
async def test_process_request_delivers_explicit_structured_response(sample_config):
    rt = Runtime(sample_config)
    response = (
        "```enso-message\n"
        '{"version":1,"fallback_text":"Accessible summary","blocks":'
        '[{"type":"markdown","text":"# Rich summary"}]}\n'
        "```"
    )

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []
            self.messages = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def reply_message(self, message): self.messages.append(message)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."

    received_prompts = []

    async def fake_run(provider, prompt, *args, **kwargs):
        received_prompts.append(prompt)
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.messages == [
        OutboundMessage(
            fallback_text="Accessible summary",
            blocks=(MarkdownBlock(text="# Rich summary"),),
        )
    ]
    assert ctx.replies == []
    assert ctx.rich_replies == []
    assert received_prompts == ["hello\n\nStructured output instructions."]


@pytest.mark.asyncio
async def test_process_request_offers_persistent_surface_draft_without_publishing(
    sample_config,
):
    rt = Runtime(sample_config)
    response = (
        "```enso-surface\n"
        '{"version":1,"surface":"canvas","fallback_text":"Report published.",'
        '"title":"Quarterly report","markdown":"# Quarterly report",'
        '"placement":"standalone"}\n'
        "```"
    )

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []
            self.drafts = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def offer_surface_draft(self, publication, source_text):
            self.drafts.append((publication, source_text))
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."
        def get_surface_instructions(self): return "Persistent surface instructions."

    received_prompts = []

    async def fake_run(provider, prompt, *args, **kwargs):
        received_prompts.append(prompt)
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(
        rt,
        sample_config,
        "claude",
        "Create a standalone Canvas for this quarterly report",
        "1",
        ctx,
    )

    assert ctx.drafts == [
        (
            CanvasPublication(
                fallback_text="Report published.",
                title="Quarterly report",
                markdown="# Quarterly report",
                placement="standalone",
            ),
            response,
        )
    ]
    assert ctx.replies == []
    assert ctx.rich_replies == []
    assert received_prompts == [
        "Create a standalone Canvas for this quarterly report\n\n"
        "Structured output instructions.\n\nPersistent surface instructions."
    ]


@pytest.mark.asyncio
async def test_process_request_surface_falls_back_for_legacy_context(sample_config):
    rt = Runtime(sample_config)
    response = (
        "```enso-surface\n"
        '{"version":1,"surface":"app_home","fallback_text":"Dashboard updated.",'
        '"blocks":[{"type":"header","text":"Dashboard"}]}\n'
        "```"
    )

    class FakeCtx(TransportContext):
        def __init__(self):
            self.replies = []

        async def reply(self, text): self.replies.append(text)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return ""
        def get_surface_instructions(self): return "Persistent surface instructions."

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.replies == ["Dashboard updated."]


@pytest.mark.asyncio
async def test_process_request_preserves_surface_fence_without_surface_capability(
    sample_config,
):
    rt = Runtime(sample_config)
    response = (
        "```enso-surface\n"
        '{"version":1,"surface":"canvas","fallback_text":"Must stay literal",'
        '"title":"Report","markdown":"# Report","placement":"standalone"}\n'
        "```"
    )

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []
            self.publications = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def offer_surface_draft(self, publication, source_text):
            self.publications.append((publication, source_text))
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."
        def get_surface_instructions(self): return ""

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.publications == []
    assert ctx.replies == []
    assert ctx.rich_replies == [response]


@pytest.mark.asyncio
async def test_process_request_retries_invalid_surface_in_same_request(sample_config):
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("1", "claude")] = "session-1"
    invalid_response = "```enso-surface\n{not json}\n```"
    responses = iter([invalid_response, "Recovered as ordinary Markdown."])

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []
            self.publications = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def offer_surface_draft(self, publication, source_text):
            self.publications.append((publication, source_text))
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return ""
        def get_surface_instructions(self): return "Persistent surface instructions."

    ctx = FakeCtx()
    calls = []

    async def fake_run(provider, prompt, chat_id, *args, **kwargs):
        calls.append((provider, prompt, chat_id, kwargs["context"]))
        assert ctx.replies == []
        assert ctx.rich_replies == []
        assert ctx.publications == []
        yield StreamEvent(kind="response", text=next(responses))

    rt.run_provider = fake_run

    outcome = await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert outcome == ("completed", None)
    assert len(calls) == 2
    assert calls[0][0] is calls[1][0]
    assert calls[0][2:] == calls[1][2:]
    assert "enso-surface" in calls[1][1]
    assert "did not deliver" in calls[1][1]
    assert invalid_response not in calls[1][1]
    assert ctx.publications == []
    assert ctx.replies == []
    assert ctx.rich_replies == ["Recovered as ordinary Markdown."]


@pytest.mark.asyncio
async def test_process_request_uses_safe_fallback_for_over_limit_app_home(sample_config):
    rt = Runtime(sample_config)
    response = (
        "```enso-surface\n"
        + json.dumps(
            {
                "version": 1,
                "surface": "app_home",
                "fallback_text": "Compact complete dashboard fallback.",
                "blocks": [{"type": "divider"}] * 101,
            }
        )
        + "\n```"
    )

    class FakeCtx(TransportContext):
        def __init__(self):
            self.replies = []
            self.publications = []

        async def reply(self, text): self.replies.append(text)
        async def offer_surface_draft(self, publication, source_text):
            self.publications.append((publication, source_text))
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return ""
        def get_surface_instructions(self): return "Persistent surface instructions."

    calls = 0

    async def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert calls == 1
    assert ctx.replies == ["Compact complete dashboard fallback."]
    assert ctx.publications == []


@pytest.mark.asyncio
async def test_process_request_delivers_explicit_chart_response(sample_config):
    rt = Runtime(sample_config)
    response = (
        "```enso-message\n"
        + json.dumps(
            {
                "version": 1,
                "fallback_text": "Revenue rose from 10 to 12.",
                "blocks": [
                    {
                        "type": "data_visualization",
                        "title": "Monthly revenue",
                        "chart": {
                            "type": "line",
                            "series": [
                                {
                                    "name": "Revenue",
                                    "data": [
                                        {"label": "Jan", "value": 10},
                                        {"label": "Feb", "value": 12},
                                    ],
                                }
                            ],
                            "axis_config": {
                                "categories": ["Jan", "Feb"],
                                "x_label": "Month",
                            },
                        },
                    }
                ],
            }
        )
        + "\n```"
    )

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.messages = []

        async def reply(self, text): self.replies.append(text)
        async def reply_message(self, message): self.messages.append(message)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.messages == [
        OutboundMessage(
            fallback_text="Revenue rose from 10 to 12.",
            blocks=(
                DataVisualizationBlock(
                    title="Monthly revenue",
                    chart=SeriesChart(
                        chart_type="line",
                        series=(
                            ChartSeries(
                                name="Revenue",
                                data=(
                                    ChartPoint(label="Jan", value=10),
                                    ChartPoint(label="Feb", value=12),
                                ),
                            ),
                        ),
                        axis_config=ChartAxis(
                            categories=("Jan", "Feb"),
                            x_label="Month",
                        ),
                    ),
                ),
            ),
        )
    ]
    assert ctx.replies == []


@pytest.mark.asyncio
async def test_process_request_structured_response_falls_back_for_legacy_context(sample_config):
    rt = Runtime(sample_config)
    response = (
        "```enso-message\n"
        '{"version":1,"fallback_text":"Accessible summary","blocks":'
        '[{"type":"markdown","text":"# Rich summary"}]}\n'
        "```"
    )

    class FakeCtx(TransportContext):
        def __init__(self):
            self.replies = []

        async def reply(self, text): self.replies.append(text)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.replies == ["Accessible summary"]


@pytest.mark.asyncio
async def test_process_request_preserves_structured_fence_without_capability(sample_config):
    rt = Runtime(sample_config)
    response = (
        "```enso-message\n"
        '{"version":1,"fallback_text":"Must stay literal","blocks":'
        '[{"type":"markdown","text":"# Presentation"}]}\n'
        "```"
    )

    class FakeCtx(TransportContext):
        rich_markdown_enabled = False

        def __init__(self):
            self.replies = []
            self.messages = []

        async def reply(self, text): self.replies.append(text)
        async def reply_message(self, message): self.messages.append(message)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return ""

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.replies == [response]
    assert ctx.messages == []


@pytest.mark.asyncio
async def test_process_request_retries_invalid_structured_response(
    sample_config, monkeypatch
):
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("1", "claude")] = "session-1"
    invalid_response = "```enso-message\n{not json}\n```"
    valid_response = (
        "```enso-message\n"
        '{"version":1,"fallback_text":"Recovered summary","blocks":'
        '[{"type":"markdown","text":"# Recovered"}]}\n'
        "```"
    )
    responses = iter([invalid_response, valid_response])

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []
            self.messages = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def reply_message(self, message): self.messages.append(message)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."

    ctx = FakeCtx()
    prompts = []
    wait_timeouts = []
    real_wait = asyncio.wait

    async def recording_wait(tasks, **kwargs):
        wait_timeouts.append(kwargs.get("timeout"))
        return await real_wait(tasks, **kwargs)

    async def fake_run(provider, prompt, *args, **kwargs):
        prompts.append(prompt)
        assert ctx.replies == []
        assert ctx.rich_replies == []
        assert ctx.messages == []
        yield StreamEvent(kind="response", text=next(responses))

    rt.run_provider = fake_run
    monkeypatch.setattr(asyncio, "wait", recording_wait)

    outcome = await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert outcome == ("completed", None)
    assert len(prompts) == 2
    assert prompts[0] == "hello\n\nStructured output instructions."
    assert "enso-message" in prompts[1]
    assert "did not deliver" in prompts[1]
    assert invalid_response not in prompts[1]
    assert len(wait_timeouts) == 2
    assert wait_timeouts[1] < wait_timeouts[0]
    assert ctx.messages == [
        OutboundMessage(
            fallback_text="Recovered summary",
            blocks=(MarkdownBlock(text="# Recovered"),),
        )
    ]
    assert ctx.replies == []
    assert ctx.rich_replies == []


@pytest.mark.asyncio
async def test_process_request_stops_after_second_invalid_structured_response(sample_config):
    rt = Runtime(sample_config)
    rt.session_by_chat_provider[("1", "claude")] = "session-1"
    response = "```enso-message\n{not json}\n```"

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []
            self.messages = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def reply_message(self, message): self.messages.append(message)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."

    calls = 0

    async def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    outcome = await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert outcome == ("error", "invalid_structured_output")
    assert calls == 2
    assert ctx.replies == ["I couldn't format that response correctly. Please try again."]
    assert ctx.rich_replies == []
    assert ctx.messages == []


@pytest.mark.asyncio
async def test_process_request_does_not_retry_without_a_resumable_session(sample_config):
    rt = Runtime(sample_config)
    response = "```enso-message\n{not json}\n```"

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."

    calls = 0

    async def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    outcome = await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert outcome == ("error", "invalid_structured_output")
    assert calls == 1
    assert ctx.replies == ["I couldn't format that response correctly. Please try again."]
    assert ctx.rich_replies == []


@pytest.mark.asyncio
async def test_process_request_uses_safe_fallback_for_over_limit_native_table(
    sample_config,
):
    rt = Runtime(sample_config)
    response = (
        "```enso-message\n"
        + json.dumps(
            {
                "version": 1,
                "fallback_text": "Compact complete fallback",
                "blocks": [
                    {
                        "type": "data_table",
                        "caption": "Too much data",
                        "rows": [
                            [{"type": "text", "text": "Value"}],
                            [{"type": "text", "text": "x" * 20_000}],
                        ],
                    }
                ],
            }
        )
        + "\n```"
    )

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.replies = []
            self.rich_replies = []
            self.messages = []

        async def reply(self, text): self.replies.append(text)
        async def reply_markdown(self, text): self.rich_replies.append(text)
        async def reply_message(self, message): self.messages.append(message)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}
        def get_output_instructions(self): return "Structured output instructions."

    calls = 0

    async def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert calls == 1
    assert ctx.replies == ["Compact complete fallback"]
    assert ctx.rich_replies == []
    assert ctx.messages == []


@pytest.mark.asyncio
async def test_process_request_error_wins_over_structured_looking_response(sample_config):
    rt = Runtime(sample_config)
    response = (
        "```enso-message\n"
        '{"version":1,"fallback_text":"Do not deliver","blocks":'
        '[{"type":"markdown","text":"# Partial"}]}\n'
        "```"
    )

    class FakeCtx(TransportContext):
        def __init__(self):
            self.replies = []
            self.messages = []

        async def reply(self, text): self.replies.append(text)
        async def reply_message(self, message): self.messages.append(message)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)
        yield StreamEvent(kind="error", text="Error: provider failed")

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.replies == ["Error: provider failed"]
    assert ctx.messages == []


@pytest.mark.asyncio
async def test_process_request_terminal_error_wins_over_partial_response(sample_config):
    rt = Runtime(sample_config)

    class FakeCtx(TransportContext):
        def __init__(self):
            self.replies = []

        async def reply(self, text): self.replies.append(text)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def failed_run(*args, **kwargs):
        yield StreamEvent(kind="response", text="partial output")
        yield StreamEvent(kind="error", text="Error: provider failed")

    ctx = FakeCtx()
    rt.run_provider = failed_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.replies == ["Error: provider failed"]


@pytest.mark.asyncio
async def test_process_request_collapses_repeated_case_insensitive_error_prefixes(
    sample_config,
):
    rt = Runtime(sample_config)

    class FakeCtx(TransportContext):
        def __init__(self): self.replies = []
        async def reply(self, text): self.replies.append(text)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def failed_run(*args, **kwargs):
        yield StreamEvent(kind="error", text="error: ERROR: quota reached")

    ctx = FakeCtx()
    rt.run_provider = failed_run

    await _process_request(rt, sample_config, "agy", "hello", "1", ctx)

    assert ctx.replies == ["Error: quota reached"]


@pytest.mark.asyncio
async def test_process_request_timeout_stops_provider_and_queues_scoped_notice(
    tmp_enso, sample_config,
):
    rt = Runtime(sample_config)
    rt.agent_timeout = 0.01
    provider_cancelled = asyncio.Event()

    class FakeCtx(TransportContext):
        def __init__(self):
            self.statuses = []
            self.edits = []
            self.replies = []
            self.deleted = []

        async def reply(self, text): self.replies.append(text)
        async def reply_status(self, text):
            self.statuses.append(text)
            return "handle"
        async def edit_status(self, handle, text): self.edits.append(text)
        async def delete_status(self, handle): self.deleted.append(handle)
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def hanging_run(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            provider_cancelled.set()
        if False:
            yield

    rt.run_provider = hanging_run
    ctx = FakeCtx()

    await asyncio.wait_for(
        _process_request(rt, sample_config, "claude", "hello", "chat-a", ctx),
        timeout=0.5,
    )

    assert provider_cancelled.is_set()
    assert ctx.statuses == ["claude · opus · 0s\n↳ Processing"]
    assert len(ctx.edits) == 1
    assert "timeout" in ctx.edits[0].lower()
    assert ctx.edits[0] != "Stopped."
    assert ctx.deleted == []
    assert ctx.replies == []
    pending = messages.pending()
    assert len(pending) == 1
    assert pending[0]["conversation_id"] == "chat-a"
    assert pending[0]["source"] == "enso:timeout"
    assert "Partial work may remain" in pending[0]["text"]


@pytest.mark.asyncio
async def test_provider_timeout_error_is_not_mislabeled_as_configured_timeout(
    tmp_enso, sample_config,
):
    rt = Runtime(sample_config)

    class FakeCtx(TransportContext):
        def __init__(self):
            self.replies = []
            self.deleted = []

        async def reply(self, text): self.replies.append(text)
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): self.deleted.append(handle)
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def failing_run(*args, **kwargs):
        raise asyncio.TimeoutError("provider read failed")
        if False:
            yield

    rt.run_provider = failing_run
    ctx = FakeCtx()

    await _process_request(rt, sample_config, "claude", "hello", "chat-a", ctx)

    assert ctx.deleted == ["handle"]
    assert ctx.replies == ["Error: provider read failed"]
    assert messages.pending() == []


@pytest.mark.asyncio
async def test_timeout_notice_is_injected_once_after_provider_switch(
    tmp_enso, sample_config,
):
    messages.send(
        "The previous agent turn timed out. Partial work may remain.",
        source="enso:timeout",
        conversation_id="chat-a",
    )
    rt = Runtime(sample_config)
    prompts_received: list[tuple[str, str]] = []

    class FakeCtx(TransportContext):
        async def reply(self, text): pass
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def fake_run(provider, prompt, chat_id, *args, **kwargs):
        prompts_received.append((chat_id, prompt))
        yield StreamEvent(kind="response", text="Done")

    rt.run_provider = fake_run

    await _process_request(
        rt, sample_config, "claude", "other chat", "chat-b", FakeCtx()
    )
    assert "timed out" not in prompts_received[-1][1]
    assert len(messages.pending()) == 1

    await _process_request(
        rt, sample_config, "agy", "what happened?", "chat-a", FakeCtx()
    )
    assert "The previous agent turn timed out" in prompts_received[-1][1]
    assert prompts_received[-1][1].endswith("what happened?")
    assert messages.pending() == []


@pytest.mark.asyncio
async def test_manual_cancellation_does_not_queue_timeout_notice(
    tmp_enso, sample_config,
):
    rt = Runtime(sample_config)
    started = asyncio.Event()

    class FakeCtx(TransportContext):
        def __init__(self): self.edits = []
        async def reply(self, text): pass
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): self.edits.append(text)
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def hanging_run(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
        if False:
            yield

    rt.run_provider = hanging_run
    ctx = FakeCtx()
    execution = _execution_context(sample_config, "chat-a")
    _ensure_launch_discovery_fixture(execution)
    request = asyncio.create_task(
        rt._run_request(
            "claude", "hello", ctx, execution
        ),
    )
    await started.wait()

    stopped, error = await rt.stop_chat("chat-a")
    await request

    assert stopped is True
    assert error is None
    assert ctx.edits == ["Stopped."]
    assert messages.pending() == []


@pytest.mark.asyncio
async def test_agy_timeout_captures_session_and_removes_private_log(
    tmp_enso, sample_config,
):
    rt = Runtime(sample_config)
    # Leave enough time for the launch-boundary filesystem validation to run
    # before exercising cancellation of the already-spawned provider.
    rt.agent_timeout = 0.1
    session_id = "55555555-5555-4555-8555-555555555555"
    captured: dict[str, str] = {}

    class HangingProcess:
        pid = 45
        returncode = None
        stdout = object()
        stderr = object()

        async def communicate(self):
            await asyncio.Event().wait()

    async def fake_spawn(*args, **kwargs):
        log_path = args[args.index("--log-file") + 1]
        captured["log_path"] = log_path
        captured["print_timeout"] = args[args.index("--print-timeout") + 1]
        Path(log_path).write_text(
            f"Print mode: conversation={session_id}, sending message\n",
        )
        return HangingProcess()

    async def fake_terminate(process, label, *, grace=1.0):
        process.returncode = -15

    class FakeCtx(TransportContext):
        async def reply(self, text): pass
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    rt._spawn_process = fake_spawn
    rt._terminate_process_tree = fake_terminate

    await asyncio.wait_for(
        _process_request(rt, sample_config, "agy", "hello", "chat-a", FakeCtx()),
        timeout=1.0,
    )

    assert rt.session_by_chat_provider[("chat-a", "agy")] == session_id
    assert captured["print_timeout"] == "6s"
    assert not Path(captured["log_path"]).exists()
    assert "chat-a" not in rt.running_process_by_chat


@pytest.mark.asyncio
async def test_timeout_notice_wins_over_in_flight_ticker_edit(
    tmp_enso, sample_config,
):
    rt = Runtime(sample_config)
    rt.agent_timeout = 0.01
    edit_started = asyncio.Event()
    release_edit = asyncio.Event()

    class FakeCtx(TransportContext):
        def __init__(self): self.edits = []
        async def reply(self, text): pass
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text):
            if text == "old progress":
                edit_started.set()
                try:
                    await release_edit.wait()
                except asyncio.CancelledError:
                    await release_edit.wait()
            self.edits.append(text)
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def hanging_run(*args, **kwargs):
        await asyncio.Event().wait()
        if False:
            yield

    async def in_flight_ticker(ctx, status_msg, state, stop):
        await ctx.edit_status(status_msg, "old progress")

    rt.run_provider = hanging_run
    rt._run_ticker = in_flight_ticker
    ctx = FakeCtx()
    task = asyncio.create_task(
        _process_request(rt, sample_config, "claude", "hello", "chat-a", ctx),
    )
    await edit_started.wait()
    await asyncio.sleep(0.03)
    release_edit.set()
    await asyncio.wait_for(task, timeout=0.5)

    assert ctx.edits[0] == "old progress"
    assert "timeout" in ctx.edits[-1].lower()


@pytest.mark.asyncio
async def test_manual_cancellation_wins_race_with_timeout_cleanup(
    tmp_enso, sample_config,
):
    rt = Runtime(sample_config)
    rt.agent_timeout = 0.01
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class FakeCtx(TransportContext):
        def __init__(self): self.edits = []
        async def reply(self, text): pass
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): self.edits.append(text)
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    async def hanging_run(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
        if False:
            yield

    rt.run_provider = hanging_run
    ctx = FakeCtx()
    task = asyncio.create_task(
        _process_request(rt, sample_config, "claude", "hello", "chat-a", ctx),
    )
    await cleanup_started.wait()
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert ctx.edits == ["Stopped."]
    assert messages.pending() == []


@pytest.mark.asyncio
async def test_run_provider_streams_progress_while_a_batch_provider_runs(sample_config):
    """A provider whose stdout lands only at exit still reports live activity."""
    rt = Runtime(sample_config)
    finish = asyncio.Event()
    progress_exhausted = asyncio.Event()

    class FakeProcess:
        pid = 99
        returncode = None
        stdout = object()
        stderr = object()

        async def communicate(self):
            await finish.wait()
            self.returncode = 0
            return b"the answer", b""

    class BatchProvider(BaseProvider):
        name = "agy"
        streaming_output = False

        def build_command(
            self,
            prompt,
            model,
            session_id=None,
            *,
            effort=None,
            launch=None,
            instructions=None,
        ):
            return ["fake"]

        def build_batch_command(
            self, prompt, model, *, effort=None, launch=None, instructions=None
        ):
            return ["fake"]

        def parse_event(self, event):
            return []

        async def poll_progress(self):
            for action in ("Reading notes file", "Listing files"):
                yield StreamEvent(kind="status", text=action)
            progress_exhausted.set()
            await asyncio.Event().wait()  # a real poller runs until cancelled

    async def fake_spawn(*args, **kwargs):
        return FakeProcess()

    rt._spawn_process = fake_spawn
    collected = []
    context = await _prepared_context(rt, sample_config, "agy", "chat-a")

    async def drain():
        async for event in rt.run_provider(
            BatchProvider("fake"), "hi", "chat-a", "m", context=context
        ):
            collected.append(event)
            if len(collected) == 2:
                # Progress must arrive before the process has produced stdout.
                await progress_exhausted.wait()
                finish.set()

    await asyncio.wait_for(drain(), timeout=2)

    assert [(e.kind, e.text) for e in collected] == [
        ("status", "Reading notes file"),
        ("status", "Listing files"),
        ("response", "the answer"),
    ]


@pytest.mark.asyncio
async def test_run_provider_survives_a_failing_progress_poller(sample_config):
    """Best-effort progress must never break the actual request."""
    rt = Runtime(sample_config)

    class FakeProcess:
        pid = 100
        returncode = 0
        stdout = object()
        stderr = object()

        async def communicate(self):
            return b"still fine", b""

    class BrokenProgressProvider(BaseProvider):
        name = "agy"
        streaming_output = False

        def build_command(
            self,
            prompt,
            model,
            session_id=None,
            *,
            effort=None,
            launch=None,
            instructions=None,
        ):
            return ["fake"]

        def build_batch_command(
            self, prompt, model, *, effort=None, launch=None, instructions=None
        ):
            return ["fake"]

        def parse_event(self, event):
            return []

        async def poll_progress(self):
            raise sqlite3.DatabaseError("trajectory schema moved")
            yield  # pragma: no cover

    async def fake_spawn(*args, **kwargs):
        return FakeProcess()

    rt._spawn_process = fake_spawn
    context = await _prepared_context(rt, sample_config, "agy", "chat-a")
    collected = [
        event
        async for event in rt.run_provider(
            BrokenProgressProvider("fake"), "hi", "chat-a", "m",
            context=context,
        )
    ]

    assert [(e.kind, e.text) for e in collected] == [("response", "still fine")]


class _EditRecorder:
    """Ticker context that stops the loop once it has seen enough edits."""

    def __init__(self, stop, stop_after):
        self._stop = stop
        self._stop_after = stop_after
        self.edits = []

    async def edit_status(self, handle, text):
        self.edits.append(text)
        if len(self.edits) >= self._stop_after:
            self._stop.set()

    async def send_typing(self): pass


@pytest.mark.asyncio
async def test_ticker_updates_every_second_with_latest_action(sample_config, monkeypatch):
    rt = Runtime(sample_config)
    actions = ["Reading core.py", "Running pytest", "Writing report.md"]
    state = {"elapsed": 0, "header": "claude · opus · high", "action": None}
    stop = asyncio.Event()
    ticks = {"n": 0}

    async def no_wait(_seconds):
        # Stand in for the provider reporting a new action every second.
        if ticks["n"] < len(actions):
            state["action"] = actions[ticks["n"]]
        ticks["n"] += 1
        return None

    monkeypatch.setattr(core_module.asyncio, "sleep", no_wait)
    ctx = _EditRecorder(stop, stop_after=len(actions))
    await rt._run_ticker(ctx, "handle", state, stop)

    assert ctx.edits == [
        "claude · opus · high · 1s\n↳ Reading core.py",
        "claude · opus · high · 2s\n↳ Running pytest",
        "claude · opus · high · 3s\n↳ Writing report.md",
    ]


@pytest.mark.asyncio
async def test_ticker_switches_from_one_second_to_five_second_updates(
    sample_config, monkeypatch,
):
    rt = Runtime(sample_config)
    state = {"elapsed": 0, "header": "agy · gemini-3.6-flash", "action": None}
    stop = asyncio.Event()
    ticks = {"n": 0}

    async def no_wait(_seconds):
        ticks["n"] += 1
        if ticks["n"] == 33:
            state["action"] = "Reading trajectory"
        return None

    monkeypatch.setattr(core_module.asyncio, "sleep", no_wait)
    ctx = _EditRecorder(stop, stop_after=32)
    await rt._run_ticker(ctx, "handle", state, stop)

    assert ctx.edits[:30] == [
        f"agy · gemini-3.6-flash · {second}s" for second in range(1, 31)
    ]
    assert ctx.edits[30:] == [
        "agy · gemini-3.6-flash · 35s\n↳ Reading trajectory",
        "agy · gemini-3.6-flash · 40s\n↳ Reading trajectory",
    ]


@pytest.mark.asyncio
async def test_ticker_survives_a_transient_edit_failure(sample_config, monkeypatch):
    """One failed edit must not silence status for the rest of the request."""
    rt = Runtime(sample_config)
    state = {"elapsed": 0, "header": "codex · terra", "action": "Running ls"}
    stop = asyncio.Event()

    class FlakyCtx:
        def __init__(self):
            self.edits = []
            self.attempts = 0

        async def edit_status(self, handle, text):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("429 slow down")
            self.edits.append(text)
            state["action"] = f"step {len(self.edits)}"
            if len(self.edits) == 2:
                stop.set()

        async def send_typing(self): pass

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(core_module.asyncio, "sleep", no_wait)
    ctx = FlakyCtx()
    await rt._run_ticker(ctx, "handle", state, stop)

    assert ctx.attempts == 3
    assert len(ctx.edits) == 2


def test_should_run_job_invalid_schedule_is_skipped(sample_config):
    """A malformed schedule must not raise — it would kill the scheduler."""
    rt = Runtime(sample_config)
    job = Job(
        dir_name="bad", name="Bad", schedule="0 9 * *",
        provider="claude", model="sonnet", workspace="unused",
    )
    rt.jobs.last_run["bad"] = datetime.now() - timedelta(days=1)
    assert rt.jobs._should_run_job(job, datetime.now()) is False


class _SilentStream:
    """An empty async stdout/stderr that also supports read()."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def read(self):
        return b""


class _ImmediateExitProcess:
    pid = 45
    returncode = 1

    def __init__(self):
        self.stdout = _SilentStream()
        self.stderr = _SilentStream()

    async def wait(self):
        return 1


@pytest.mark.asyncio
async def test_run_provider_reverts_session_on_spawn_failure(
    tmp_enso, sample_config, monkeypatch,
):
    """A first turn that never spawns must not leave a --resume-able id behind."""
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "claude")

    async def fake_spawn(*args, **kwargs):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    with pytest.raises(FileNotFoundError):
        async for _event in rt.run_provider(
            rt.make_provider("claude", context=context),
            "hi",
            "1",
            "opus",
            context=context,
        ):
            pass

    stored = rt.session_by_chat_provider[("1", "claude")]
    assert stored.startswith("new:"), "failed first turn must stay a fresh session"


@pytest.mark.asyncio
async def test_run_provider_reverts_session_on_eventless_failure(
    tmp_enso, sample_config, monkeypatch,
):
    """An immediate nonzero exit with no events must not promote the session."""
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "claude")

    async def fake_spawn(*args, **kwargs):
        return _ImmediateExitProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    async for _event in rt.run_provider(
        rt.make_provider("claude", context=context),
        "hi",
        "1",
        "opus",
        context=context,
    ):
        pass

    stored = rt.session_by_chat_provider[("1", "claude")]
    assert stored.startswith("new:")


# -- Grok sessions and auth retry --


def _grok_result_line(text, session_id="77777777-7777-4777-8777-777777777777"):
    """One grok streaming-messages-json result line (Claude wire format)."""
    return (json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": session_id,
        "total_cost_usd": 0.0421,
        "usage": {"input_tokens": 12213, "output_tokens": 58},
    }) + "\n").encode()


class _ScriptedStream:
    """Async stdout yielding scripted lines; read() serves stderr bytes."""

    def __init__(self, lines=(), data=b""):
        self._lines = list(lines)
        self._data = data

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self):
        return self._data


class _ScriptedProcess:
    """A grok-shaped CLI run: scripted stream-json stdout, stderr, and exit."""

    pid = 46

    def __init__(self, returncode=0, stdout_lines=(), stderr=b""):
        self.returncode = returncode
        self.stdout = _ScriptedStream(lines=stdout_lines)
        self.stderr = _ScriptedStream(data=stderr)

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_run_provider_grok_creates_session_then_resumes(
    tmp_enso, sample_config, monkeypatch,
):
    """Grok sessions are Enso-owned: --session-id first, --resume after."""
    rt = Runtime(sample_config)
    context = await _prepared_context(rt, sample_config, "grok")
    commands: list[tuple] = []

    async def fake_spawn(*args, **kwargs):
        commands.append(args)
        # The CLI reports back the session it was launched with.
        sid = (
            args[args.index("--session-id") + 1]
            if "--session-id" in args
            else args[args.index("--resume") + 1]
        )
        return _ScriptedProcess(stdout_lines=[_grok_result_line("ok", session_id=sid)])

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    provider = rt.make_provider("grok", context=context)
    async for _event in rt.run_provider(provider, "hi", "1", "grok-4.6", context=context):
        pass
    async for _event in rt.run_provider(provider, "again", "1", "grok-4.6", context=context):
        pass

    first, second = commands
    session_id = first[first.index("--session-id") + 1]
    assert "--resume" not in first
    assert second[second.index("--resume") + 1] == session_id
    assert "--session-id" not in second


@pytest.mark.asyncio
async def test_grok_retries_once_after_transient_auth_failure(
    tmp_enso, sample_config, monkeypatch,
):
    """A lapsed token fails the first headless call before the background
    refresh lands; dispatch retries exactly once on that signature."""
    rt = Runtime(sample_config)
    spawned: list[tuple] = []

    async def fake_spawn(*args, **kwargs):
        spawned.append(args)
        if len(spawned) == 1:
            return _ScriptedProcess(returncode=1, stderr=b"Error: Not signed in\n")
        return _ScriptedProcess(stdout_lines=[_grok_result_line("Signed-in reply.")])

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    ctx = _OutcomeCtx()
    outcome = await _process_request(rt, sample_config, "grok", "hello", "1", ctx)

    assert outcome == ("completed", None)
    assert len(spawned) == 2
    assert ctx.replies == ["Signed-in reply."]
    # The failed first spawn produced no events, so the session reverted;
    # the retry must re-read session state and create the session again.
    assert "--session-id" in spawned[1]
    assert "--resume" not in spawned[1]


@pytest.mark.asyncio
async def test_grok_does_not_retry_an_ordinary_provider_error(
    tmp_enso, sample_config, monkeypatch,
):
    rt = Runtime(sample_config)
    spawned: list[tuple] = []

    async def fake_spawn(*args, **kwargs):
        spawned.append(args)
        return _ScriptedProcess(returncode=1, stderr=b"Error: unknown model grok-9\n")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    ctx = _OutcomeCtx()
    outcome = await _process_request(rt, sample_config, "grok", "hello", "1", ctx)

    assert outcome == ("error", "provider_error")
    assert len(spawned) == 1
    assert any("unknown model" in reply for reply in ctx.replies)


@pytest.mark.asyncio
async def test_grok_surfaces_a_second_consecutive_auth_failure(
    tmp_enso, sample_config, monkeypatch,
):
    """One retry only: a still-failing login is a real error, not a loop."""
    rt = Runtime(sample_config)
    spawned: list[tuple] = []

    async def fake_spawn(*args, **kwargs):
        spawned.append(args)
        return _ScriptedProcess(returncode=1, stderr=b"Error: Not signed in\n")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    ctx = _OutcomeCtx()
    outcome = await _process_request(rt, sample_config, "grok", "hello", "1", ctx)

    assert outcome == ("error", "provider_error")
    assert len(spawned) == 2
    assert any("Not signed in" in reply for reply in ctx.replies)
