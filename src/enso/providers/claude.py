"""Claude CLI provider."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from . import BaseProvider, StreamEvent


def _get_project_dir(working_dir: str) -> str:
    """Derive Claude's project session directory from working_dir."""
    resolved = str(Path(working_dir).resolve())
    mangled = resolved.replace("/", "-").replace(".", "-")
    return os.path.expanduser(f"~/.claude/projects/{mangled}")


class ClaudeProvider(BaseProvider):
    name = "claude"

    default_models: ClassVar[list[str]] = ["opus", "sonnet", "haiku", "fable"]
    env_keys: ClassVar[tuple[str, ...]] = ("ANTHROPIC_API_KEY",)

    # Levels accepted by `claude --effort`. Models not listed in
    # _model_max_effort default to "high" (safe floor every supported
    # Claude model accepts). Current Opus and Sonnet families expose the
    # full range up through "max".
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high", "xhigh", "max"]
    _model_max_effort: ClassVar[dict[str, str]] = {
        "opus": "max",
        "claude-opus-4-7": "max",
        "sonnet": "max",
        "claude-sonnet-5": "max",
    }
    _default_max_effort = "high"

    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
    ) -> list[str]:
        """Build the Claude CLI command.

        Session IDs prefixed with 'new:' use --session-id to create an
        Enso-owned session. Existing sessions use --resume.
        """
        cmd = [
            self.path, "-p",
            "--output-format", "stream-json",
            "--verbose", "--dangerously-skip-permissions",
            "--model", model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        if session_id and session_id.startswith("new:"):
            cmd.extend(["--session-id", session_id.removeprefix("new:")])
        elif session_id:
            cmd.extend(["--resume", session_id])
        cmd.extend(["--", prompt])
        return cmd

    def build_batch_command(
        self, prompt: str, model: str, *, effort: str | None = None,
    ) -> list[str]:
        """Build command for batch execution (jobs). No session continuity."""
        cmd = [
            self.path, "-p",
            "--output-format", "text",
            "--dangerously-skip-permissions",
            "--model", model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
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
                if block_type == "text" and block.get("text"):
                    kind = "error" if is_api_error else "response"
                    events.append(StreamEvent(kind=kind, text=block["text"]))

        elif event_type == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, str) and result_text:
                kind = "error" if event.get("is_error") else "response"
                events.append(StreamEvent(kind=kind, text=result_text))
            elif event.get("is_error"):
                reason = event.get("terminal_reason")
                text = reason if isinstance(reason, str) and reason else "unknown error"
                events.append(StreamEvent(kind="error", text=text))

            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                events.append(StreamEvent(kind="session", session_id=session_id))

        return events

    def clear_session(self, session_id: str | None, working_dir: str) -> str:
        if not session_id:
            return "no session"
        clean_id = session_id.removeprefix("new:")
        session_file = Path(_get_project_dir(working_dir)) / f"{clean_id}.jsonl"
        if session_file.is_file():
            session_file.unlink()
            return f"deleted session {clean_id[:8]}"
        return f"session {clean_id[:8]} (no file found)"
