"""Tests for provider abstraction and implementations."""

from __future__ import annotations

from enso.providers import get_provider
from enso.providers.claude import (
    EFFORT_LEVELS,
    ClaudeProvider,
    KageClaudeProvider,
    clamp_effort,
    max_effort_for_model,
)
from enso.providers.codex import (
    CODEX_MODEL_ALIASES,
    CodexProvider,
)
from enso.providers.codex import (
    EFFORT_LEVELS as CODEX_EFFORT_LEVELS,
)
from enso.providers.codex import (
    clamp_effort as clamp_codex_effort,
)
from enso.providers.codex import (
    max_effort_for_model as max_codex_effort_for_model,
)
from enso.providers.gemini import GeminiProvider

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


def test_kage_claude_build_command_new_session():
    p = KageClaudeProvider("kage", timeout=300)
    cmd = p.build_command("hello", "sonnet", session_id="new:abc-123", effort="high")
    assert cmd[:2] == ["kage", "claude"]
    assert "--stream" in cmd
    assert "--stop-on-signal" in cmd
    assert "--restart" in cmd
    assert "--timeout" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "300"
    assert cmd[cmd.index("--session-id") + 1] == "abc-123"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert "new:" not in " ".join(cmd)
    assert cmd[-2:] == ["--", "hello"]


def test_kage_claude_build_command_without_restart():
    p = KageClaudeProvider("kage", restart=False)
    cmd = p.build_command("hello", "opus", session_id="abc-123")
    assert "--restart" not in cmd


def test_kage_claude_build_batch_command():
    p = KageClaudeProvider("kage", timeout=600)
    cmd = p.build_batch_command("hello", "opus", effort="max")
    assert cmd[:2] == ["kage", "claude"]
    # Jobs stream so completion rides the Stop hook (format-independent) instead
    # of kage's TUI done-marker scrape, which silently misses turns over 60s.
    assert "--stream" in cmd
    # Still ephemeral — no caller-managed session id; kage assigns its own uuid
    # and tears the pane down on exit.
    assert "--session-id" not in cmd
    # batch must request signal-based teardown so a killed job doesn't orphan
    # its tmux pane.
    assert "--stop-on-signal" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "600"
    assert cmd[cmd.index("--effort") + 1] == "max"
    assert cmd[-2:] == ["--", "hello"]


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


def test_gemini_build_command():
    p = GeminiProvider("gemini")
    cmd = p.build_command("hello", "gemini-2.5-pro")
    assert "stream-json" in cmd
    assert "--yolo" in cmd


def test_gemini_build_command_resume():
    p = GeminiProvider("gemini")
    cmd = p.build_command("hello", "gemini-2.5-pro", session_id="sess_abc")
    assert "--resume" in cmd
    assert "sess_abc" in cmd


def test_gemini_build_batch_command():
    p = GeminiProvider("gemini")
    cmd = p.build_batch_command("hello", "gemini-2.5-pro")
    assert "text" in cmd
    assert "stream-json" not in cmd


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
    assert len(events) == 1
    assert events[0].kind == "status"
    assert "Reading" in events[0].text


def test_kage_claude_parse_progress():
    p = KageClaudeProvider("kage")
    events = p.parse_event({
        "status": "progress",
        "session_id": "sid-123",
        "event": "PreToolUse",
        "tool": "Bash",
        "summary": "pytest",
    })
    assert events[0].kind == "session"
    assert events[0].session_id == "sid-123"
    assert events[1].kind == "status"
    assert events[1].text == "pytest"


def test_kage_claude_parse_done():
    p = KageClaudeProvider("kage")
    events = p.parse_event({
        "status": "done",
        "session_id": "sid-123",
        "response": "Done!",
    })
    assert any(e.kind == "session" and e.session_id == "sid-123" for e in events)
    assert any(e.kind == "response" and e.text == "Done!" for e in events)


def test_kage_claude_parse_error():
    p = KageClaudeProvider("kage")
    events = p.parse_event({
        "status": "error",
        "reason": "timeout",
        "message": "no done marker",
    })
    assert len(events) == 1
    assert events[0].kind == "error"
    assert events[0].text == "no done marker"


# -- Batch (job) output parsing --
#
# Jobs now run with --stream, so their stdout is newline-delimited JSON. The
# job runner must extract the final response (or error) from that stream rather
# than treating the raw JSONL blob as the answer.


def _jsonl(*objs):
    import json as _json
    return "\n".join(_json.dumps(o) for o in objs)


def test_kage_parse_batch_output_extracts_done_response():
    p = KageClaudeProvider("kage")
    out = _jsonl(
        {"status": "progress", "session_id": "sid", "event": "PreToolUse",
         "tool": "Bash", "summary": "rg"},
        {"status": "done", "session_id": "sid", "response": "All clear. 1 note filed."},
    )
    assert p.parse_batch_output(out) == "All clear. 1 note filed."


def test_kage_parse_batch_output_surfaces_error():
    p = KageClaudeProvider("kage")
    out = _jsonl(
        {"status": "progress", "event": "PreToolUse", "tool": "Bash"},
        {"status": "error", "reason": "timeout", "message": "no done marker"},
    )
    assert p.parse_batch_output(out) == "no done marker"


def test_kage_parse_batch_output_non_stream_fallback():
    """Defensive: plain (non-JSON) stdout is returned stripped, unchanged."""
    p = KageClaudeProvider("kage")
    assert p.parse_batch_output("  just text, not a stream\n") == "just text, not a stream"


def test_base_provider_parse_batch_output_passthrough():
    """Non-streaming providers (gemini/codex) keep raw batch stdout."""
    g = GeminiProvider("gemini")
    assert g.parse_batch_output("  hello world \n") == "hello world"


def test_codex_parse_agent_message():
    p = CodexProvider("codex")
    events = p.parse_event({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "Done!"},
    })
    assert any(e.kind == "response" and e.text == "Done!" for e in events)


def test_codex_parse_session():
    p = CodexProvider("codex")
    events = p.parse_event({"type": "thread.started", "thread_id": "t_123"})
    assert any(e.kind == "session" and e.session_id == "t_123" for e in events)


def test_gemini_parse_message():
    p = GeminiProvider("gemini")
    events = p.parse_event({"type": "message", "role": "assistant", "content": "Hi!"})
    assert len(events) == 1
    assert events[0].kind == "response"
    assert events[0].text == "Hi!"


def test_gemini_parse_session():
    p = GeminiProvider("gemini")
    events = p.parse_event({"type": "init", "session_id": "s_abc"})
    assert any(e.kind == "session" and e.session_id == "s_abc" for e in events)


def test_gemini_format_response():
    p = GeminiProvider("gemini")
    assert p.format_response(["Hello ", "world"]) == "Hello world"


# -- Factory --


def test_get_provider():
    p = get_provider("claude", "/usr/bin/claude")
    assert isinstance(p, ClaudeProvider)
    assert p.path == "/usr/bin/claude"


def test_get_provider_kage_claude():
    p = get_provider("claude", "claude", {
        "runner": "kage",
        "kage_path": "/usr/bin/kage",
        "kage_timeout": 900,
        "kage_restart": False,
    })
    assert isinstance(p, KageClaudeProvider)
    assert p.path == "/usr/bin/kage"
    assert p.timeout == 900
    assert p.restart is False


def test_get_provider_unknown():
    import pytest
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("unknown", "path")


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


def test_gemini_ignores_effort():
    """Gemini accepts the shared kwarg but emits no effort flag."""
    g = GeminiProvider("gemini")
    assert "--effort" not in g.build_command("hi", "gemini-2.5-pro", effort="xhigh")
    assert "--effort" not in g.build_batch_command("hi", "gemini-2.5-pro", effort="xhigh")


# -- Effort: clamping --


def test_effort_levels_ordered():
    assert EFFORT_LEVELS == ["low", "medium", "high", "xhigh", "max"]


def test_max_effort_opus_is_max():
    assert max_effort_for_model("opus") == "max"
    assert max_effort_for_model("claude-opus-4-7") == "max"


def test_max_effort_other_models_capped_at_high():
    assert max_effort_for_model("sonnet") == "high"
    assert max_effort_for_model("haiku") == "high"
    assert max_effort_for_model("unknown-model") == "high"


def test_clamp_effort_within_cap():
    # Opus supports everything — no clamping.
    assert clamp_effort("max", "opus") == "max"
    assert clamp_effort("xhigh", "opus") == "xhigh"
    assert clamp_effort("low", "opus") == "low"


def test_clamp_effort_degrades_to_cap():
    # Sonnet caps at high — xhigh/max clamp down.
    assert clamp_effort("max", "sonnet") == "high"
    assert clamp_effort("xhigh", "sonnet") == "high"
    assert clamp_effort("high", "sonnet") == "high"
    assert clamp_effort("medium", "sonnet") == "medium"


def test_clamp_effort_unknown_level_passthrough():
    assert clamp_effort("bogus", "opus") == "bogus"


def test_codex_effort_levels_ordered():
    assert CODEX_EFFORT_LEVELS == ["low", "medium", "high", "xhigh", "max", "ultra"]


def test_codex_model_effort_caps():
    assert max_codex_effort_for_model("sol") == "ultra"
    assert max_codex_effort_for_model("gpt-5.6-terra") == "ultra"
    assert max_codex_effort_for_model("luna") == "max"
    assert max_codex_effort_for_model("gpt-5.5") == "xhigh"


def test_codex_clamp_effort():
    assert clamp_codex_effort("ultra", "sol") == "ultra"
    assert clamp_codex_effort("ultra", "luna") == "max"
    assert clamp_codex_effort("max", "gpt-5.5") == "xhigh"
    assert clamp_codex_effort("high", "luna") == "high"
