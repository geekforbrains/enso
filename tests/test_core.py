"""Tests for the runtime core."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from enso import core as core_module
from enso import messages
from enso.config import SKILL_TOMBSTONES_DIRNAME
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
from enso.providers.agy import AgyProvider
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
    path = sample_config["workspaces"]["default"]["path"]
    workspace = Workspace("test", path, "test", concurrency)
    policy = Policy("test", None, True, providers, providers[0], "*")
    return ExecutionContext(
        chat_key=chat_key,
        path=path,
        workspace_id=workspace.name,
        workspace=workspace,
        policy=policy,
        include_global_messages=include_global_messages,
        concurrency=concurrency,
        **kwargs,
    )


async def _process_request(
    runtime: Runtime,
    sample_config: dict,
    provider_name: str,
    prompt: str,
    chat_id: str,
    ctx: TransportContext,
):
    """Exercise request handling with a complete personal workspace binding."""
    context = await _prepared_context(runtime, sample_config, provider_name, chat_id)
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
    context = _execution_context(sample_config, chat_key, **kwargs)
    return await runtime._prepare_execution_context(provider_name, context)

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


def test_redacted_command_hides_codex_shared_and_user_instructions():
    rendered = _redacted_command(
        [
            "codex",
            "exec",
            "-c",
            'developer_instructions="shared secret"',
            "--",
            "user secret",
        ]
    )

    assert "shared secret" not in rendered
    assert "user secret" not in rendered
    assert "developer_instructions=<redacted>" in rendered
    assert "<prompt chars=11>" in rendered


def test_redacted_command_hides_grok_single_prompt():
    """Grok's prompt rides attached to its flag, not behind a separator."""
    rendered = _redacted_command(
        ["grok", "--output-format", "streaming-messages-json", "--single=secret prompt"]
    )
    assert "secret prompt" not in rendered
    assert "--single=<prompt chars=13>" in rendered


def test_redacted_command_hides_grok_rules_instructions():
    """The shared-instruction bundle rides attached to --rules= and must be
    redacted like codex's developer_instructions payload."""
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
    path = sample_config["workspaces"]["default"]["path"]

    with pytest.raises(TypeError):
        ExecutionContext(chat_key="chat", path=path, workspace_id="test")  # type: ignore[call-arg]


# -- Workspace setup --


def _legacy_agents_prompt() -> tuple[str, str]:
    """Return the current and exact pre-task-removal prompt templates.

    The legacy template is a checked-in fixture (the last bundled prompt
    that still documented the tasks system) so editing the current bundled
    prompt never breaks migration tests.
    """
    current = (
        importlib.resources.files("enso")
        .joinpath("prompts", "AGENTS.md")
        .read_text(encoding="utf-8")
    )
    legacy = (
        Path(__file__).parent / "data" / "legacy_tasks_agents.md"
    ).read_text(encoding="utf-8")
    assert hashlib.sha256(legacy.encode()).hexdigest() == (
        core_module._LEGACY_TASKS_AGENTS_SHA256
    )
    return current, legacy


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


def test_install_system_prompts_migrates_exact_legacy_template(sample_config):
    current, legacy = _legacy_agents_prompt()
    agents_file = Path(core_module.CONFIG_DIR, "AGENTS.md")
    agents_file.write_text(legacy)

    Runtime(sample_config).install_system_prompts()

    assert agents_file.read_text() == current


def test_legacy_prompt_migration_failure_preserves_original(
    sample_config, monkeypatch
):
    _, legacy = _legacy_agents_prompt()
    agents_file = Path(core_module.CONFIG_DIR, "AGENTS.md")
    agents_file.write_text(legacy)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("enso.core.os.replace", fail_replace)

    Runtime(sample_config).install_system_prompts()

    assert agents_file.read_text() == legacy
    assert list(agents_file.parent.glob("*.tmp")) == []


def test_install_system_prompts_preserves_customized_template(sample_config, caplog):
    _, legacy = _legacy_agents_prompt()
    agents_file = Path(core_module.CONFIG_DIR, "AGENTS.md")
    customized = legacy + "\n## Local instructions\nKeep this customization.\n"
    agents_file.write_text(customized)

    Runtime(sample_config).install_system_prompts()

    assert agents_file.read_text() == customized
    assert "contains retired task instructions" in caplog.text


def test_install_system_prompts_updates_any_known_pristine_template(
    sample_config, monkeypatch,
):
    """An untouched prompt from an earlier release follows the bundle forward."""
    current, _ = _legacy_agents_prompt()
    previous = "# Enso\n\nformer pristine bundled prompt\n"
    agents_file = Path(core_module.CONFIG_DIR, "AGENTS.md")
    agents_file.write_text(previous)
    monkeypatch.setattr(
        core_module,
        "_PRISTINE_AGENTS_SHA256",
        frozenset({hashlib.sha256(previous.encode()).hexdigest()}),
    )

    Runtime(sample_config).install_system_prompts()

    assert agents_file.read_text() == current


def test_install_system_prompts_preserves_unknown_template(sample_config, caplog):
    """A prompt whose hash is unknown is customized — leave it entirely alone."""
    agents_file = Path(core_module.CONFIG_DIR, "AGENTS.md")
    customized = "# Enso\n\nMy own prompt.\n"
    agents_file.write_text(customized)

    Runtime(sample_config).install_system_prompts()

    assert agents_file.read_text() == customized
    assert "retired task instructions" not in caplog.text


def test_install_system_prompts_creates_docs_dir(tmp_enso, sample_config):
    Runtime(sample_config).install_system_prompts()

    assert Path(tmp_enso, "docs").is_dir()
    assert Path(tmp_enso, "jobs").is_dir()
    assert Path(tmp_enso, "AGENTS.md").is_file()
    assert Path(tmp_enso, "CLAUDE.md").is_symlink()
    assert Path(tmp_enso, ".claude", "skills").is_symlink()
    assert Path(tmp_enso, ".agents", "skills").is_symlink()
    assert Path(tmp_enso, ".claude", "settings.json").is_file()
    workspace_path = Path(sample_config["workspaces"]["default"]["path"])
    assert not (workspace_path / "AGENTS.md").exists()
    assert not (workspace_path / "CLAUDE.md").exists()


def test_docs_skill_is_bundled(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    Runtime._install_bundled_skills(str(skills_dir))

    assert (skills_dir / "docs" / "SKILL.md").is_file()


def test_tables_skill_is_bundled(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    Runtime._install_bundled_skills(str(skills_dir))

    skill = skills_dir / "tables" / "SKILL.md"
    assert skill.is_file()
    content = skill.read_text(encoding="utf-8")
    assert "enso table list" in content
    assert "sqlite3 ~/.enso/enso.db" in content
    assert "runs" in content
    assert "_enso_" in content
    assert ".bail on" in content
    assert "PRAGMA foreign_keys = ON;" in content


def test_bundled_prompt_documents_the_doc_commands():
    """AGENTS.md is always in context; the doc surface has to appear there."""
    current, _ = _legacy_agents_prompt()
    assert "enso doc list" in current
    assert "enso doc create" in current
    assert "`docs` skill" in current


def test_bundled_prompt_documents_the_table_commands():
    """AGENTS.md keeps the data-table discovery surface always available."""
    current, _ = _legacy_agents_prompt()
    assert "enso table list" in current
    assert "enso table schema" in current
    assert "enso table register" in current
    assert "`tables` skill" in current


def test_bundled_skills_are_seeded_once(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    Runtime._install_bundled_skills(str(skills_dir))
    skill_file = skills_dir / "jobs" / "SKILL.md"
    assert skill_file.is_file()

    skill_file.write_text("locally edited through the dashboard\n")
    Runtime._install_bundled_skills(str(skills_dir))

    assert skill_file.read_text() == "locally edited through the dashboard\n"


def test_bundled_skill_tombstone_prevents_reseeding(tmp_path):
    skills_dir = tmp_path / "skills"
    tombstones = skills_dir / SKILL_TOMBSTONES_DIRNAME
    tombstones.mkdir(parents=True)
    (tombstones / "jobs.deleted").write_text("")

    Runtime._install_bundled_skills(str(skills_dir))

    assert not (skills_dir / "jobs").exists()
    assert (skills_dir / "slack" / "SKILL.md").is_file()


def test_bundled_skills_update_only_known_pristine_files(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "jobs"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    previous = "former pristine bundled jobs skill\n"
    skill_file.write_text(previous)
    monkeypatch.setattr(
        core_module,
        "_BUNDLED_SKILL_PRISTINE_HASHES",
        {
            ("jobs", "SKILL.md"): frozenset({
                hashlib.sha256(previous.encode()).hexdigest()
            })
        },
    )

    Runtime._install_bundled_skills(str(skills_dir))

    current = (
        importlib.resources.files("enso")
        .joinpath("skills", "jobs", "SKILL.md")
        .read_text(encoding="utf-8")
    )
    assert skill_file.read_text() == current


def test_bundled_skills_preserve_symlink_even_when_target_hash_is_known(
    tmp_path, monkeypatch
):
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "jobs"
    skill_dir.mkdir(parents=True)
    target = tmp_path / "custom-jobs-skill.md"
    previous = "former pristine bundled jobs skill\n"
    target.write_text(previous)
    skill_file = skill_dir / "SKILL.md"
    skill_file.symlink_to(target)
    monkeypatch.setattr(
        core_module,
        "_BUNDLED_SKILL_PRISTINE_HASHES",
        {
            ("jobs", "SKILL.md"): frozenset({
                hashlib.sha256(previous.encode()).hexdigest()
            })
        },
    )

    Runtime._install_bundled_skills(str(skills_dir))

    assert skill_file.is_symlink()
    assert target.read_text() == previous


def test_retire_legacy_tasks_skill_only_when_pristine(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    task_dir = skills_dir / "tasks"
    task_dir.mkdir(parents=True)
    pristine = "former bundled task skill\n"
    monkeypatch.setattr(
        core_module,
        "_LEGACY_TASKS_SKILL_SHA256",
        hashlib.sha256(pristine.encode()).hexdigest(),
    )

    (task_dir / "SKILL.md").write_text(pristine)
    Runtime._retire_legacy_tasks_skill(str(skills_dir))
    assert not task_dir.exists()

    task_dir.mkdir()
    (task_dir / "SKILL.md").write_text(pristine + "customized\n")
    Runtime._retire_legacy_tasks_skill(str(skills_dir))
    assert task_dir.is_dir()

    (task_dir / "SKILL.md").write_text(pristine)
    (task_dir / "notes.md").write_text("user-owned companion file\n")
    Runtime._retire_legacy_tasks_skill(str(skills_dir))
    assert task_dir.is_dir()


def test_retire_legacy_tasks_skill_preserves_directory_symlink(tmp_path, caplog):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    target = tmp_path / "custom-task-skill"
    target.mkdir()
    skill_file = target / "SKILL.md"
    skill_file.write_text("custom task skill\n")
    task_link = skills_dir / "tasks"
    task_link.symlink_to(target, target_is_directory=True)

    Runtime._retire_legacy_tasks_skill(str(skills_dir))

    assert task_link.is_symlink()
    assert skill_file.read_text() == "custom task skill\n"
    assert "Preserving customized retired tasks skill" in caplog.text


# -- Runtime state --


def test_runtime_defaults(sample_config):
    rt = Runtime(sample_config)
    assert rt.get_active_provider("1") == "claude"
    assert rt.get_active_model("1", "claude") == "opus"
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


def test_runtime_provider_switch(sample_config):
    rt = Runtime(sample_config)
    rt.active_provider_by_chat["1"] = "codex"
    assert rt.get_active_provider("1") == "codex"


def test_runtime_model_switch(sample_config):
    rt = Runtime(sample_config)
    rt.active_model_by_chat_provider[("1", "claude")] = "sonnet"
    assert rt.get_active_model("1", "claude") == "sonnet"


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


def test_runtime_state_persistence(tmp_enso, sample_config):
    """State survives save/load roundtrip."""

    rt = Runtime(sample_config)
    rt.active_provider_by_chat["42"] = "codex"
    rt.session_by_chat_provider[("42", "codex")] = "sess_123"
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()
    assert rt2.active_provider_by_chat["42"] == "codex"
    assert rt2.session_by_chat_provider[("42", "codex")] == "sess_123"


def test_runtime_state_roundtrip_preserves_opaque_team_keys(tmp_enso, sample_config):
    key = "teams:0123456789abcdef"
    rt = Runtime(sample_config)
    model = rt.models["codex"][0]
    rt.active_provider_by_chat[key] = "codex"
    rt.active_model_by_chat_provider[(key, "codex")] = model
    rt.effort_by_chat_provider_model[(key, "codex", model)] = "high"
    rt.session_by_chat_provider[(key, "codex")] = "session-1"
    rt.save_state()

    loaded = Runtime(sample_config)
    loaded.load_state()

    assert loaded.active_provider_by_chat[key] == "codex"
    assert loaded.active_model_by_chat_provider[(key, "codex")] == model
    assert loaded.effort_by_chat_provider_model[(key, "codex", model)] == "high"
    assert loaded.session_by_chat_provider[(key, "codex")] == "session-1"


def test_load_state_removes_unsupported_provider_entries(tmp_enso, sample_config):
    state_file = Path(tmp_enso) / "state.json"
    state_file.write_text(json.dumps({
        "active_provider_by_chat": {"42": "retired"},
        "active_model_by_chat_provider": {"42:retired": "old-model"},
        "effort_by_chat_provider_model": {"42:retired:old-model": "high"},
        "session_by_chat_provider": {"42:retired": "old-session"},
    }))

    rt = Runtime(sample_config)
    rt.load_state()

    assert rt.get_active_provider("42") == "claude"
    assert rt.active_provider_by_chat == {}
    assert rt.active_model_by_chat_provider == {}
    assert rt.effort_by_chat_provider_model == {}
    assert rt.session_by_chat_provider == {}
    persisted = json.loads(state_file.read_text())
    assert persisted["active_provider_by_chat"] == {}
    assert persisted["active_model_by_chat_provider"] == []
    assert persisted["effort_by_chat_provider_model"] == []
    assert persisted["session_by_chat_provider"] == []


def test_load_state_removes_entries_for_unconfigured_models(tmp_enso, sample_config):
    """Model and effort state for models no longer in config is pruned;
    entries for configured models survive."""
    state_file = Path(tmp_enso) / "state.json"
    state_file.write_text(json.dumps({
        "active_model_by_chat_provider": {
            "42:claude": "removed-model",
            "7:claude": "sonnet",
        },
        "effort_by_chat_provider_model": {
            "42:claude:removed-model": "high",
            "7:claude:sonnet": "low",
        },
    }))

    rt = Runtime(sample_config)  # claude models: opus, sonnet
    rt.load_state()

    assert rt.active_model_by_chat_provider == {("7", "claude"): "sonnet"}
    assert rt.effort_by_chat_provider_model == {("7", "claude", "sonnet"): "low"}
    persisted = json.loads(state_file.read_text())
    assert persisted["active_model_by_chat_provider"] == [
        {"chat": "7", "provider": "claude", "model": "sonnet"}
    ]
    assert persisted["effort_by_chat_provider_model"] == [
        {"chat": "7", "provider": "claude", "model": "sonnet", "effort": "low"}
    ]


def test_load_state_removes_effort_for_provider_without_effort_control(
    tmp_enso, sample_config,
):
    sample_config["providers"]["agy"]["models"] = list(AgyProvider.default_models)
    state_file = Path(tmp_enso) / "state.json"
    model = AgyProvider.default_models[0]
    state_file.write_text(json.dumps({
        "active_provider_by_chat": {"7": "agy"},
        "effort_by_chat_provider_model": {f"7:agy:{model}": "low"},
    }))

    rt = Runtime(sample_config)
    rt.load_state()

    assert rt.effort_by_chat_provider_model == {}
    assert rt.get_active_effort("7", "agy", model) is None
    persisted = json.loads(state_file.read_text())
    assert persisted["effort_by_chat_provider_model"] == []


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
    rt.effort_by_chat_provider_model[("42", "claude", "opus")] = "low"
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
    sample_config["providers"]["claude"]["models"].append("haiku")
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "claude", "haiku")] = "max"
    assert rt.get_active_effort("1", "claude", "haiku") == "high"


def test_get_active_effort_codex_clamps_to_model_cap(sample_config):
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "codex", "luna")] = "ultra"
    assert rt.get_active_effort("1", "codex", "luna") == "max"


def test_effort_state_persistence(tmp_enso, sample_config):
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("42", "claude", "opus")] = "xhigh"
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()
    assert rt2.effort_by_chat_provider_model[("42", "claude", "opus")] == "xhigh"


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
async def test_execution_preparation_snapshots_current_shared_instructions(
    tmp_enso, sample_config
):
    source = Path(tmp_enso, "AGENTS.md")
    rt = Runtime(sample_config)

    first = await rt._prepare_execution_context(
        "claude", _execution_context(sample_config)
    )
    source.write_text("# Revised shared instructions\n", encoding="utf-8")
    second = await rt._prepare_execution_context(
        "claude", _execution_context(sample_config)
    )

    assert first.instructions is not None and second.instructions is not None
    assert first.instructions.revision != second.instructions.revision
    assert Path(first.instructions.snapshot_path).read_text(encoding="utf-8") == (
        "# Test shared instructions\n"
    )
    assert second.instructions.content == "# Revised shared instructions\n"


@pytest.mark.asyncio
async def test_execution_preparation_records_launch_only_after_instructions_validate(
    tmp_enso, sample_config
):
    Path(tmp_enso, "AGENTS.md").unlink()
    recorded = []
    rt = Runtime(sample_config)
    context = _execution_context(
        sample_config,
        on_launch=lambda launch: recorded.append(launch),
    )

    with pytest.raises(InstructionError, match="shared instruction file is missing"):
        await rt._prepare_execution_context("claude", context)

    assert recorded == []


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
    flag = captured["command"].index("--append-system-prompt-file")
    snapshot = Path(captured["command"][flag + 1])
    assert snapshot.read_text(encoding="utf-8") == "# Test shared instructions\n"


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


def test_prune_clears_effort(tmp_enso, sample_config):
    """Stale conversations drop their effort settings too."""
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
    rt.active_provider_by_chat["old_chat"] = "claude"
    rt.session_by_chat_provider[("old_chat", "claude")] = "old_session"
    rt._last_active["old_chat"] = datetime.now() - timedelta(days=60)

    rt.active_provider_by_chat["fresh_chat"] = "codex"
    rt.session_by_chat_provider[("fresh_chat", "codex")] = "fresh_session"
    rt._last_active["fresh_chat"] = datetime.now()

    rt.save_state()

    # Load into a new runtime — pruning should remove old_chat
    rt2 = Runtime(sample_config)
    rt2.load_state()

    assert "old_chat" not in rt2.active_provider_by_chat
    assert ("old_chat", "claude") not in rt2.session_by_chat_provider
    assert "old_chat" not in rt2._last_active
    # Fresh one survives
    assert rt2.active_provider_by_chat["fresh_chat"] == "codex"
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
    await rt.process_request(
        "claude",
        "user message",
        "1",
        FakeCtx(),
        context=_execution_context(sample_config),
    )

    # Messages should have been consumed
    assert messages.pending() == []
    assert len(prompts_received) == 1
    assert "background info" in prompts_received[0]
    assert "user message" in prompts_received[0]


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
    # Two distinct conversations, same workspace — must not overlap.
    await asyncio.gather(
        rt._run_request("claude", "a", _OutcomeCtx(), ctx_for("k1")),
        rt._run_request("claude", "b", _OutcomeCtx(), ctx_for("k2")),
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
    await asyncio.gather(
        rt._run_request(
            "claude",
            "a",
            _OutcomeCtx(),
            _execution_context(sample_config, "k1", include_global_messages=True),
        ),
        rt._run_request(
            "claude",
            "b",
            _OutcomeCtx(),
            _execution_context(sample_config, "k2", include_global_messages=True),
        ),
    )
    assert peak == 1


@pytest.mark.asyncio
async def test_process_request_returns_terminal_outcome(sample_config):
    rt = Runtime(sample_config)

    async def ok_run(*a, **k):
        yield StreamEvent(kind="response", text="hi")
    rt.run_provider = ok_run
    context = _execution_context(sample_config)
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
    rt.effort_by_chat_provider_model[("1", "claude", "opus")] = "high"

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
    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

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
async def test_process_request_invalid_surface_stays_ordinary_text(sample_config):
    rt = Runtime(sample_config)
    response = "```enso-surface\n{not json}\n```"

    class FakeCtx(TransportContext):
        rich_markdown_enabled = True

        def __init__(self):
            self.rich_replies = []
            self.publications = []

        async def reply(self, text): raise AssertionError("unexpected plain reply")
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

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.publications == []
    assert ctx.rich_replies == [response]


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

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

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
async def test_process_request_invalid_structured_response_stays_ordinary_text(sample_config):
    rt = Runtime(sample_config)
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

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

    assert ctx.messages == []
    assert ctx.replies == []
    assert ctx.rich_replies == [response]


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

    async def fake_run(*args, **kwargs):
        yield StreamEvent(kind="response", text=response)

    ctx = FakeCtx()
    rt.run_provider = fake_run

    await _process_request(rt, sample_config, "claude", "hello", "1", ctx)

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
    request = asyncio.create_task(
        rt._run_request(
            "claude", "hello", ctx, _execution_context(sample_config, "chat-a")
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
    rt.agent_timeout = 0.01
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
        timeout=0.5,
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


def test_retire_legacy_skill_tools_removes_pristine_copies(
    sample_config, tmp_enso, monkeypatch,
):
    """Pristine retired tool scripts vanish from both the skill and tools dirs."""
    skills_dir = os.path.join(tmp_enso, "skills")
    skill_dir = os.path.join(skills_dir, "slack")
    os.makedirs(skill_dir)
    content = "print('legacy tool')\n"
    pristine = hashlib.sha256(content.encode()).hexdigest()
    Path(skill_dir, "slack_search.py").write_text(content)
    tools_dir = os.path.join(
        sample_config["workspaces"]["default"]["path"], "tools"
    )
    os.makedirs(tools_dir)
    Path(tools_dir, "slack_search.py").write_text(content)
    monkeypatch.setattr(
        core_module,
        "_RETIRED_SKILL_TOOL_HASHES",
        {("slack", "slack_search.py"): frozenset({pristine})},
    )

    Runtime(sample_config)._retire_legacy_skill_tools(skills_dir)

    assert not os.path.exists(os.path.join(skill_dir, "slack_search.py"))
    assert not os.path.exists(os.path.join(tools_dir, "slack_search.py"))


def test_retire_legacy_skill_tools_preserves_customized_copy(
    sample_config, tmp_enso, monkeypatch,
):
    skills_dir = os.path.join(tmp_enso, "skills")
    skill_dir = os.path.join(skills_dir, "slack")
    os.makedirs(skill_dir)
    Path(skill_dir, "slack_search.py").write_text("print('user changed this')\n")
    monkeypatch.setattr(
        core_module,
        "_RETIRED_SKILL_TOOL_HASHES",
        {("slack", "slack_search.py"): frozenset({"0" * 64})},
    )

    Runtime(sample_config)._retire_legacy_skill_tools(skills_dir)

    assert os.path.exists(os.path.join(skill_dir, "slack_search.py"))


def test_skill_tools_do_not_fall_back_when_default_workspace_is_absent(
    sample_config, tmp_path,
):
    default = sample_config["workspaces"].pop("default")
    sample_config["workspaces"]["personal"] = default
    sample_config["transports"]["telegram"]["workspace"] = "personal"
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "custom"
    skill_dir.mkdir(parents=True)
    (skill_dir / "tool.py").write_text("print('custom')\n")

    runtime = Runtime(sample_config)
    runtime._install_skill_tools(str(skills_dir))

    assert runtime._default_workspace_path() is None
    assert not (Path(default["path"]) / "tools" / "tool.py").exists()
