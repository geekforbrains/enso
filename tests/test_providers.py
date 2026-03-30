"""Tests for provider abstraction and implementations."""

from __future__ import annotations

from enso.providers import get_provider
from enso.providers.claude import ClaudeProvider
from enso.providers.codex import CodexProvider
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


def test_get_provider_unknown():
    import pytest
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("unknown", "path")
