"""Tests for the runtime core."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from enso import core as core_module
from enso import messages
from enso.config import SKILL_TOMBSTONES_DIRNAME
from enso.core import (
    PROGRESS_MESSAGES,
    Runtime,
    _redacted_command,
    progress_text,
    split_text,
)
from enso.jobs import Job
from enso.providers import StreamEvent
from enso.providers.claude import ClaudeProvider

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


def test_progress_text_rotates_with_elapsed_seconds():
    assert progress_text(0) == f"(0s) {PROGRESS_MESSAGES[0]}"
    assert progress_text(1) == f"(1s) {PROGRESS_MESSAGES[1]}"
    elapsed = len(PROGRESS_MESSAGES)
    assert progress_text(elapsed) == f"({elapsed}s) {PROGRESS_MESSAGES[0]}"


def test_redacted_command_hides_agy_prompt():
    rendered = _redacted_command(["agy", "--model", "model", "--prompt", "secret prompt"])
    assert "secret prompt" not in rendered
    assert "<prompt chars=13>" in rendered


# -- Workspace setup --


def _legacy_agents_prompt() -> tuple[str, str]:
    """Return the current and exact pre-task-removal prompt templates."""
    current = (
        importlib.resources.files("enso")
        .joinpath("prompts", "AGENTS.md")
        .read_text(encoding="utf-8")
    )
    legacy = current.replace(
        "# For full usage:\n",
        "# Tasks — one-off work Enso completes on its own\n"
        'enso task create --title "…" --description "…"   '
        "# create a one-off task (--notify to be pinged)\n"
        "enso task list                       # show tasks and status\n"
        "enso task show <slug>                # task detail + result\n\n"
        "# For full usage:\n",
        1,
    ).replace(
        "## Deferred updates — use `enso message send`\n",
        "## Tasks\n\n"
        "For one-off work the user wants done *later* or in the background (not\n"
        "recurring, and not something to do right now), use the `tasks` skill. It\n"
        "covers when to make a task vs a job, and how to write a self-contained\n"
        "description the background task-runner can act on without this conversation's\n"
        'context — e.g. when the user says "let\'s make that a task."\n\n'
        "## Deferred updates — use `enso message send`\n",
        1,
    )
    assert hashlib.sha256(legacy.encode()).hexdigest() == (
        core_module._LEGACY_TASKS_AGENTS_SHA256
    )
    return current, legacy


def test_install_system_prompts_migrates_exact_legacy_template(sample_config):
    current, legacy = _legacy_agents_prompt()
    agents_file = Path(sample_config["working_dir"], "AGENTS.md")
    agents_file.write_text(legacy)

    Runtime(sample_config).install_system_prompts()

    assert agents_file.read_text() == current


def test_legacy_prompt_migration_failure_preserves_original(
    sample_config, monkeypatch
):
    _, legacy = _legacy_agents_prompt()
    agents_file = Path(sample_config["working_dir"], "AGENTS.md")
    agents_file.write_text(legacy)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("enso.core.os.replace", fail_replace)

    Runtime(sample_config).install_system_prompts()

    assert agents_file.read_text() == legacy
    assert list(agents_file.parent.glob("*.tmp")) == []


def test_install_system_prompts_preserves_customized_template(sample_config, caplog):
    _, legacy = _legacy_agents_prompt()
    agents_file = Path(sample_config["working_dir"], "AGENTS.md")
    customized = legacy + "\n## Local instructions\nKeep this customization.\n"
    agents_file.write_text(customized)

    Runtime(sample_config).install_system_prompts()

    assert agents_file.read_text() == customized
    assert "contains retired task instructions" in caplog.text


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
    assert rt.agent_timeout == 15 * 60
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
    provider = rt.make_provider("claude")
    assert isinstance(provider, ClaudeProvider)
    assert provider.path == "/custom/claude"


def test_make_provider_binds_working_dir(sample_config):
    """Providers see the directory their process will run in (agy needs it
    to pin conversations to the workspace project)."""
    rt = Runtime(sample_config)
    provider = rt.make_provider("agy")
    assert provider.working_dir == rt.working_dir


def test_make_provider_unknown_provider_raises(sample_config):
    rt = Runtime(sample_config)
    with pytest.raises(ValueError, match="Unknown provider"):
        rt.make_provider("retired")


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
    assert persisted["active_model_by_chat_provider"] == {}
    assert persisted["effort_by_chat_provider_model"] == {}
    assert persisted["session_by_chat_provider"] == {}


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
    assert persisted["active_model_by_chat_provider"] == {"7:claude": "sonnet"}
    assert persisted["effort_by_chat_provider_model"] == {"7:claude:sonnet": "low"}


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

    assert "__task_runner__" not in rt._job_last_run
    assert rt._job_last_run["real-job"] == datetime.fromisoformat(timestamp)
    persisted = json.loads(state_file.read_text())
    assert "__task_runner__" not in persisted["job_last_run"]
    assert persisted["job_last_run"]["real-job"] == timestamp


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


def test_get_active_effort_codex_clamps_to_model_cap(sample_config):
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "codex", "luna")] = "ultra"
    assert rt.get_active_effort("1", "codex", "luna") == "max"


def test_effort_state_persistence(tmp_enso, sample_config):
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("42", "claude", "opus")] = "xhigh"
    rt.save_state()

    rt2 = Runtime(sample_config)
    rt2.load_state()
    assert rt2.effort_by_chat_provider_model[("42", "claude", "opus")] == "xhigh"


class _FakeSpawnedProcess:
    pid = 42
    returncode = 0
    stdout = None
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
async def test_run_provider_injects_extra_env(tmp_enso, sample_config, monkeypatch):
    """extra_env reaches create_subprocess_exec merged on top of os.environ."""
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)

    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeSpawnedProcess()

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

    async def fake_spawn(*args, **kwargs):
        captured["env"] = kwargs.get("env", "SENTINEL_UNSET")
        return _FakeSpawnedProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    provider = rt.make_provider("claude")
    gen = rt.run_provider(provider, "hi", "1", "opus")
    try:
        async for _ in gen:
            pass
    except (TypeError, AssertionError):
        pass

    assert captured["env"] == "SENTINEL_UNSET"


@pytest.mark.asyncio
async def test_run_provider_handles_agy_plain_output_and_captures_session(
    tmp_enso, sample_config, monkeypatch,
):
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    session_id = "44444444-4444-4444-8444-444444444444"

    async def fake_spawn(*args, **kwargs):
        log_path = args[args.index("--log-file") + 1]
        Path(log_path).write_text(f"Print mode: conversation={session_id}, sending message\n")
        return _FakePlainProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    events = [
        event
        async for event in rt.run_provider(
            rt.make_provider("agy"), "hello", "1", "gemini-3.6-flash-high",
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
    sample_config["working_dir"] = os.path.join(tmp_enso, "workspace")
    rt = Runtime(sample_config)
    provider = rt.make_provider("agy")
    captured: dict[str, str] = {}

    async def fake_spawn(*args, **kwargs):
        captured["log_path"] = args[args.index("--log-file") + 1]
        raise OSError("spawn failed")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    with pytest.raises(OSError, match="spawn failed"):
        async for _event in rt.run_provider(
            provider, "hello", "1", "gemini-3.6-flash-high",
        ):
            pass

    assert not Path(captured["log_path"]).exists()
    assert provider._log_path is None


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


def test_get_or_create_session_agy(sample_config):
    """Agy creates its own session ID, captured from its private run log."""
    rt = Runtime(sample_config)
    assert rt._get_or_create_session("1", "agy") is None


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


@pytest.mark.asyncio
async def test_process_request_uses_normalized_status_and_plain_final_response(sample_config):
    rt = Runtime(sample_config)
    rt.effort_by_chat_provider_model[("1", "claude", "opus")] = "high"

    class FakeCtx:
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
    await rt.process_request("claude", "hello", "1", ctx)

    assert ctx.statuses == ["(0s) Thinking hard"]
    assert ctx.deleted == ["handle"]
    assert ctx.replies == ["Done"]


@pytest.mark.asyncio
async def test_process_request_timeout_stops_provider_and_queues_scoped_notice(
    tmp_enso, sample_config,
):
    rt = Runtime(sample_config)
    rt.agent_timeout = 0.01
    provider_cancelled = asyncio.Event()

    class FakeCtx:
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
        rt.process_request("claude", "hello", "chat-a", ctx), timeout=0.5,
    )

    assert provider_cancelled.is_set()
    assert ctx.statuses == ["(0s) Thinking hard"]
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

    class FakeCtx:
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

    await rt.process_request("claude", "hello", "chat-a", ctx)

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

    class FakeCtx:
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

    await rt.process_request("claude", "other chat", "chat-b", FakeCtx())
    assert "timed out" not in prompts_received[-1][1]
    assert len(messages.pending()) == 1

    await rt.process_request("agy", "what happened?", "chat-a", FakeCtx())
    assert "The previous agent turn timed out" in prompts_received[-1][1]
    assert prompts_received[-1][1].endswith("what happened?")
    assert messages.pending() == []


@pytest.mark.asyncio
async def test_manual_cancellation_does_not_queue_timeout_notice(
    tmp_enso, sample_config,
):
    rt = Runtime(sample_config)
    started = asyncio.Event()

    class FakeCtx:
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
        rt._run_request("claude", "hello", "chat-a", ctx),
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
        Path(log_path).write_text(
            f"Print mode: conversation={session_id}, sending message\n",
        )
        return HangingProcess()

    async def fake_terminate(process, label, *, grace=1.0):
        process.returncode = -15

    class FakeCtx:
        async def reply(self, text): pass
        async def reply_status(self, text): return "handle"
        async def edit_status(self, handle, text): pass
        async def delete_status(self, handle): pass
        async def send_typing(self): pass
        def get_origin_env(self): return {}

    rt._spawn_process = fake_spawn
    rt._terminate_process_tree = fake_terminate

    await asyncio.wait_for(
        rt.process_request("agy", "hello", "chat-a", FakeCtx()), timeout=0.5,
    )

    assert rt.session_by_chat_provider[("chat-a", "agy")] == session_id
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

    class FakeCtx:
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
        rt.process_request("claude", "hello", "chat-a", ctx),
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

    class FakeCtx:
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
        rt.process_request("claude", "hello", "chat-a", ctx),
    )
    await cleanup_started.wait()
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert ctx.edits == ["Stopped."]
    assert messages.pending() == []


@pytest.mark.asyncio
async def test_ticker_rotates_every_second(sample_config, monkeypatch):
    rt = Runtime(sample_config)
    stop = asyncio.Event()

    class FakeCtx:
        def __init__(self):
            self.edits = []

        async def edit_status(self, handle, text):
            self.edits.append(text)
            if len(self.edits) == 3:
                stop.set()

        async def send_typing(self): pass

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(core_module.asyncio, "sleep", no_wait)
    ctx = FakeCtx()
    state = {"elapsed": 0}

    await rt._run_ticker(ctx, "handle", state, stop)

    assert ctx.edits == [progress_text(1), progress_text(2), progress_text(3)]
