"""Tests for provider abstraction and implementations."""

from __future__ import annotations

import pytest

from enso.providers import PROVIDER_CLASSES, PROVIDER_NAMES, provider_class
from enso.providers.agy import AGY_MODELS, AgyProvider
from enso.providers.claude import ClaudeProvider
from enso.providers.codex import CODEX_MODEL_ALIASES, CodexProvider

# -- Registry --


def test_supported_provider_names():
    assert PROVIDER_NAMES == ["claude", "codex", "agy"]
    # PROVIDER_NAMES is derived from the registry — the single source of truth.
    assert list(PROVIDER_CLASSES) == PROVIDER_NAMES


def test_provider_class_lookup():
    assert provider_class("claude") is ClaudeProvider
    assert provider_class("codex") is CodexProvider
    assert provider_class("agy") is AgyProvider


def test_provider_class_unknown():
    with pytest.raises(ValueError, match="Unknown provider"):
        provider_class("unknown")


def test_registry_declares_config_defaults():
    """Every provider carries the defaults config derives from the registry."""
    for cls in PROVIDER_CLASSES.values():
        assert cls.default_models
        assert isinstance(cls.env_keys, tuple)


# -- Command building --


def test_claude_build_command_no_session():
    """Without session_id, no --resume or --session-id flags."""
    p = ClaudeProvider("claude")
    cmd = p.build_command("hello", "sonnet")
    assert "--resume" not in cmd
    assert "--session-id" not in cmd
    assert "--continue" not in cmd


def test_claude_build_command_new_session():
    """New session (new: prefix) uses --session-id."""
    p = ClaudeProvider("claude")
    cmd = p.build_command("hello", "sonnet", session_id="new:abc-123")
    assert "--session-id" in cmd
    assert "abc-123" in cmd
    assert "new:" not in " ".join(cmd)


def test_claude_build_command_resume():
    """Existing session uses --resume."""
    p = ClaudeProvider("claude")
    cmd = p.build_command("hello", "sonnet", session_id="abc-123")
    assert "--resume" in cmd
    assert "abc-123" in cmd


def test_claude_build_batch_command():
    p = ClaudeProvider("claude")
    cmd = p.build_batch_command("hello", "opus")
    assert "text" in cmd
    assert "stream-json" not in cmd
    assert "--verbose" not in cmd
    assert "--continue" not in cmd


def test_codex_build_command():
    p = CodexProvider("codex")
    cmd = p.build_command("hello", "gpt-5.3-codex")
    assert cmd[0] == "codex"
    assert "exec" in cmd
    assert "--json" in cmd


def test_codex_build_command_resume():
    p = CodexProvider("codex")
    cmd = p.build_command("hello", "gpt-5.3-codex", session_id="thread_123")
    assert "resume" in cmd
    assert "thread_123" in cmd


def test_codex_build_batch_command():
    p = CodexProvider("codex")
    cmd = p.build_batch_command("hello", "gpt-5.3-codex")
    assert "--json" not in cmd


def test_codex_model_aliases_apply_to_all_command_modes():
    p = CodexProvider("codex")
    for alias, model_id in CODEX_MODEL_ALIASES.items():
        commands = [
            p.build_command("hello", alias),
            p.build_command("hello", alias, session_id="thread_123"),
            p.build_batch_command("hello", alias),
        ]
        for cmd in commands:
            assert cmd[cmd.index("-m") + 1] == model_id


def test_codex_unknown_model_passes_through():
    p = CodexProvider("codex")
    cmd = p.build_batch_command("hello", "gpt-5.5")
    assert cmd[cmd.index("-m") + 1] == "gpt-5.5"


def test_agy_build_command_creates_resumable_yolo_session_command():
    provider = AgyProvider("agy")
    cmd = provider.build_command(
        "hello", "gemini-3.6-flash-high", session_id="session-123", effort="low",
    )
    try:
        assert cmd[0] == "agy"
        assert "--dangerously-skip-permissions" in cmd
        assert cmd[cmd.index("--model") + 1] == "gemini-3.6-flash-high"
        assert cmd[cmd.index("--effort") + 1] == "low"
        assert cmd[cmd.index("--conversation") + 1] == "session-123"
        assert cmd[cmd.index("--prompt") + 1] == "hello"
        assert "--log-file" in cmd
    finally:
        provider.finalize_events()


def test_agy_build_command_without_session_starts_fresh():
    provider = AgyProvider("agy")
    cmd = provider.build_command("hello", AGY_MODELS[0])
    try:
        assert "--conversation" not in cmd
    finally:
        provider.finalize_events()


def test_agy_batch_command_is_plain_yolo_output():
    cmd = AgyProvider("agy").build_batch_command(
        "hello", "gemini-3.6-flash-low", effort="medium",
    )
    assert cmd == [
        "agy", "--dangerously-skip-permissions",
        "--model", "gemini-3.6-flash-low",
        "--effort", "medium",
        "--prompt", "hello",
    ]


def test_agy_preserves_multiline_final_output():
    provider = AgyProvider("agy")
    events = provider.parse_complete_output("First paragraph.\n\n```py\nprint('hi')\n```\n")
    assert len(events) == 1
    assert events[0].kind == "response"
    assert events[0].text == "First paragraph.\n\n```py\nprint('hi')\n```"


def test_agy_finalize_captures_authoritative_session_and_removes_log(tmp_path):
    log_file = tmp_path / "agy.log"
    log_file.write_text(
        "Created conversation 11111111-1111-4111-8111-111111111111\n"
        "Print mode: conversation=22222222-2222-4222-8222-222222222222, sending message\n"
    )
    provider = AgyProvider("agy")
    provider._log_path = str(log_file)

    events = provider.finalize_events()

    assert [(event.kind, event.session_id) for event in events] == [
        ("session", "22222222-2222-4222-8222-222222222222"),
    ]
    assert not log_file.exists()


def test_agy_finalize_falls_back_to_created_session(tmp_path):
    log_file = tmp_path / "agy.log"
    log_file.write_text(
        "Created conversation 33333333-3333-4333-8333-333333333333\n"
    )
    provider = AgyProvider("agy")
    provider._log_path = str(log_file)

    events = provider.finalize_events()

    assert events[0].session_id == "33333333-3333-4333-8333-333333333333"
    assert not log_file.exists()


# -- Event parsing --


def test_claude_parse_result():
    p = ClaudeProvider("claude")
    events = p.parse_event({"type": "result", "result": "Hello!"})
    assert len(events) == 1
    assert events[0].kind == "response"
    assert events[0].text == "Hello!"


def test_claude_parse_tool_use():
    p = ClaudeProvider("claude")
    events = p.parse_event({
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/foo.py"}}
            ]
        },
    })
    assert events == []


# -- Batch (job) output parsing --


def test_base_provider_parse_batch_output_passthrough():
    """Non-streaming providers keep raw batch stdout."""
    provider = CodexProvider("codex")
    assert provider.parse_batch_output("  hello world \n") == "hello world"


def test_codex_parse_agent_message():
    p = CodexProvider("codex")
    events = p.parse_event({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "Done!"},
    })
    assert [(e.kind, e.text) for e in events] == [("response", "Done!")]


def test_codex_parse_session():
    p = CodexProvider("codex")
    events = p.parse_event({"type": "thread.started", "thread_id": "t_123"})
    assert any(e.kind == "session" and e.session_id == "t_123" for e in events)


# -- Stream buffering --


def test_stdout_limit_generous_default():
    """One long JSON event line (e.g. a full response) must not overrun the buffer."""
    assert ClaudeProvider("claude").stdout_limit() == 10 * 1024 * 1024
    assert CodexProvider("codex").stdout_limit() == 10 * 1024 * 1024


def test_agy_effort_levels_and_models():
    assert AgyProvider.effort_levels == ["low", "medium", "high"]
    assert AgyProvider.default_models == AGY_MODELS


# -- Effort: command building --


def test_claude_build_command_with_effort():
    p = ClaudeProvider("claude")
    cmd = p.build_command("hi", "opus", effort="xhigh")
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "xhigh"


def test_claude_build_command_without_effort():
    p = ClaudeProvider("claude")
    cmd = p.build_command("hi", "opus")
    assert "--effort" not in cmd


def test_claude_build_batch_command_with_effort():
    p = ClaudeProvider("claude")
    cmd = p.build_batch_command("hi", "opus", effort="max")
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "max"
    # Prompt is still last and sentinel is preserved
    assert cmd[-1] == "hi"
    assert cmd[-2] == "--"


def test_codex_build_commands_with_effort():
    c = CodexProvider("codex")
    commands = [
        c.build_command("hi", "sol", effort="ultra"),
        c.build_command("hi", "terra", session_id="thread_123", effort="ultra"),
        c.build_batch_command("hi", "luna", effort="max"),
    ]
    for cmd in commands[:2]:
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="ultra"'
    assert commands[2][commands[2].index("-c") + 1] == 'model_reasoning_effort="max"'
    assert all("--effort" not in cmd for cmd in commands)


def test_codex_build_command_without_effort_has_no_override():
    c = CodexProvider("codex")
    assert "model_reasoning_effort" not in " ".join(c.build_command("hi", "sol"))
    assert "model_reasoning_effort" not in " ".join(c.build_batch_command("hi", "sol"))


# -- Effort: clamping --


def test_effort_levels_ordered():
    assert ClaudeProvider.effort_levels == ["low", "medium", "high", "xhigh", "max"]


def test_max_effort_opus_is_max():
    assert ClaudeProvider.max_effort_for_model("opus") == "max"
    assert ClaudeProvider.max_effort_for_model("claude-opus-4-7") == "max"


def test_max_effort_other_models_capped_at_high():
    assert ClaudeProvider.max_effort_for_model("sonnet") == "high"
    assert ClaudeProvider.max_effort_for_model("haiku") == "high"
    assert ClaudeProvider.max_effort_for_model("unknown-model") == "high"


def test_clamp_effort_within_cap():
    # Opus supports everything — no clamping.
    assert ClaudeProvider.clamp_effort("max", "opus") == "max"
    assert ClaudeProvider.clamp_effort("xhigh", "opus") == "xhigh"
    assert ClaudeProvider.clamp_effort("low", "opus") == "low"


def test_clamp_effort_degrades_to_cap():
    # Sonnet caps at high — xhigh/max clamp down.
    assert ClaudeProvider.clamp_effort("max", "sonnet") == "high"
    assert ClaudeProvider.clamp_effort("xhigh", "sonnet") == "high"
    assert ClaudeProvider.clamp_effort("high", "sonnet") == "high"
    assert ClaudeProvider.clamp_effort("medium", "sonnet") == "medium"


def test_clamp_effort_unknown_level_passthrough():
    assert ClaudeProvider.clamp_effort("bogus", "opus") == "bogus"


def test_codex_effort_levels_ordered():
    assert CodexProvider.effort_levels == ["low", "medium", "high", "xhigh", "max", "ultra"]


def test_codex_model_effort_caps():
    assert CodexProvider.max_effort_for_model("sol") == "ultra"
    assert CodexProvider.max_effort_for_model("gpt-5.6-terra") == "ultra"
    assert CodexProvider.max_effort_for_model("luna") == "max"
    assert CodexProvider.max_effort_for_model("gpt-5.5") == "xhigh"


def test_codex_clamp_effort():
    assert CodexProvider.clamp_effort("ultra", "sol") == "ultra"
    assert CodexProvider.clamp_effort("ultra", "luna") == "max"
    assert CodexProvider.clamp_effort("max", "gpt-5.5") == "xhigh"
    assert CodexProvider.clamp_effort("high", "luna") == "high"
