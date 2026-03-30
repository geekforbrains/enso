"""Codex CLI provider."""

from __future__ import annotations

import json

from . import BaseProvider, StreamEvent


def _format_status(event: dict) -> str | None:
    """Extract human-readable status from a Codex JSON event."""
    event_type = event.get("type", "")
    item = event.get("item", {})
    item_type = item.get("type", "")

    if event_type == "item.started":
        match item_type:
            case "command_execution":
                cmd = item.get("command", "")
                if "-lc " in cmd:
                    cmd = cmd.split("-lc ", 1)[1].strip("'\"")
                return f"Running {cmd[:50]}{'...' if len(cmd) > 50 else ''}"
            case "reasoning":
                return "Thinking..."
            case "file_changes":
                return "Editing files..."
            case "web_searches":
                return "Searching the web..."
            case "mcp_tool_calls":
                return "Using tool..."

    if event_type == "item.completed" and item_type == "reasoning":
        text = item.get("text", "")
        if text:
            clean = text.strip("*").strip()
            return clean[:40] + "..." if len(clean) > 40 else clean

    return None


class CodexProvider(BaseProvider):
    name = "codex"

    def build_command(
        self, prompt: str, model: str, session_id: str | None = None
    ) -> list[str]:
        if session_id:
            return [
                self.path, "exec", "resume",
                "--dangerously-bypass-approvals-and-sandbox", "--json",
                "-m", model, "--", session_id, prompt,
            ]
        return [
            self.path, "exec",
            "--dangerously-bypass-approvals-and-sandbox", "--json",
            "-m", model, "--", prompt,
        ]

    def build_batch_command(self, prompt: str, model: str) -> list[str]:
        return [
            self.path, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m", model, "--", prompt,
        ]

    def parse_line(self, line: str) -> dict | None:
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None

    def parse_event(self, event: dict) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        event_type = event.get("type", "")

        status = _format_status(event)
        if status:
            events.append(StreamEvent(kind="status", text=status))

        if event_type == "turn.started":
            events.append(StreamEvent(kind="status", text="Working..."))
        elif event_type == "error":
            msg = event.get("message")
            if isinstance(msg, str) and msg:
                events.append(StreamEvent(kind="error", text=msg))
                if "reconnect" in msg.lower():
                    events.append(StreamEvent(kind="status", text="Reconnecting..."))
        elif event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value:
                    events.append(StreamEvent(kind="response", text=text_value))
        elif event_type == "turn.failed":
            msg = event.get("message", "")
            if msg:
                events.append(StreamEvent(kind="error", text=msg))
        elif event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                events.append(StreamEvent(kind="session", session_id=thread_id))

        return events

    def stdout_limit(self) -> int | None:
        return 10 * 1024 * 1024

    def stderr_to_stdout(self) -> bool:
        return True
