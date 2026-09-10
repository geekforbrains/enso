"""Claude CLI provider."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from . import (
    SESSION_UUID_RE,
    BaseProvider,
    StreamEvent,
    session_path,
    truncate_status,
)


def tool_status(tool_name: str, tool_input: dict) -> str:
    """Describe a tool call in one line, preferring the model's own description."""
    description = tool_input.get("description")
    if isinstance(description, str) and description.strip():
        return truncate_status(description)
    match tool_name:
        case "Read":
            return f"Reading {os.path.basename(tool_input.get('file_path', 'file'))}"
        case "Write":
            return f"Writing {os.path.basename(tool_input.get('file_path', 'file'))}"
        case "Edit" | "NotebookEdit":
            return f"Editing {os.path.basename(tool_input.get('file_path', 'file'))}"
        case "Bash":
            return truncate_status(f"Running {tool_input.get('command', '')}")
        case "Glob":
            return f"Finding {truncate_status(tool_input.get('pattern', ''))}"
        case "Grep":
            return f"Searching for {truncate_status(tool_input.get('pattern', ''))}"
        case "WebFetch":
            return f"Fetching {truncate_status(tool_input.get('url', ''), 40)}"
        case "WebSearch":
            return f"Searching: {truncate_status(tool_input.get('query', ''))}"
        case "Agent" | "Task":
            return "Running subagent"
        case _:
            return f"Using {truncate_status(tool_name, 40)}"


def project_dir(cwd: str) -> Path:
    """Claude's per-project session directory for a working directory."""
    mangled = str(Path(cwd).resolve()).replace("/", "-").replace(".", "-")
    return Path("~/.claude/projects").expanduser() / mangled


class ClaudeProvider(BaseProvider):
    name = "claude"
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high", "xhigh", "max"]
    model_max_effort: ClassVar[dict[str, str]] = {
        "opus": "max",
        "claude-opus-4-7": "max",
        "sonnet": "max",
        "claude-sonnet-5": "max",
    }
    default_max_effort = "high"
    models: ClassVar[list[str]] = ["opus", "sonnet", "haiku"]
    unattended_args: ClassVar[list[str]] = ["--dangerously-skip-permissions"]
    # Enso mints the id and passes it as --session-id, which the CLI takes only as a UUID
    # and echoes back in its event stream; a transcript is that UUID plus .jsonl.
    session_id_re: ClassVar[re.Pattern[str]] = SESSION_UUID_RE

    # Grok speaks the same wire format with its own tool vocabulary.
    _tool_status = staticmethod(tool_status)

    @staticmethod
    def _terminal_reason(event: dict) -> str | None:
        reason = event.get("terminal_reason")
        return reason if isinstance(reason, str) and reason else None

    def command(
        self,
        prompt: str,
        model: str,
        effort: str,
        args: Sequence[str],
        *,
        session_id: str | None = None,
        new_session: bool = False,
        batch: bool = False,
        cwd: str = "",
    ) -> list[str]:
        cmd = [self.path, "-p", "--output-format", "text" if batch else "stream-json"]
        if not batch:
            cmd.append("--verbose")
        cmd.extend([*args, "--model", model, "--effort", effort])
        if session_id and not batch:
            cmd.extend(["--session-id" if new_session else "--resume", session_id])
        cmd.extend(["--", prompt])
        return cmd

    def parse_event(self, event: dict) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        event_type = event.get("type", "")
        if event_type == "assistant":
            message = event.get("message", {})
            is_api_error = bool(
                event.get("is_api_error_message")
                or message.get("is_api_error_message")
                or event.get("error")
            )
            for block in message.get("content", []):
                block_type = block.get("type")
                if block_type == "thinking":
                    # Recent models redact the thinking text; the activity still counts.
                    summary = truncate_status(block.get("thinking", "") or "")
                    events.append(StreamEvent(kind="status", text=summary or "Thinking"))
                elif block_type == "tool_use":
                    text = self._tool_status(block.get("name", ""), block.get("input", {}))
                    events.append(StreamEvent(kind="status", text=text))
                elif block_type == "text" and block.get("text"):
                    events.append(
                        StreamEvent(
                            kind="error" if is_api_error else "response", text=block["text"]
                        )
                    )
        elif event_type == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, str) and result_text:
                events.append(
                    StreamEvent(
                        kind="error" if event.get("is_error") else "response", text=result_text
                    )
                )
            elif event.get("is_error"):
                reason = self._terminal_reason(event) or "unknown error"
                events.append(StreamEvent(kind="error", text=reason))
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                events.append(StreamEvent(kind="session", session_id=session_id))
        return events

    def clear_session(self, session_id: str, cwd: str) -> str:
        self.check_session_id(session_id)
        session_file = session_path(project_dir(cwd), f"{session_id}.jsonl")
        if session_file.is_file():
            session_file.unlink()
            return f"deleted session {session_id[:8]}"
        return f"session {session_id[:8]} (no file found)"
