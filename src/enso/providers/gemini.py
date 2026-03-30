"""Gemini CLI provider."""

from __future__ import annotations

import os

from . import BaseProvider, StreamEvent


def _format_status(event: dict) -> str | None:
    """Extract human-readable status from a Gemini JSON event."""
    if event.get("type") != "tool_use":
        return None

    tool_name = event.get("tool_name", "")
    params = event.get("parameters", {})

    match tool_name:
        case "read_file":
            return f"Reading {os.path.basename(params.get('file_path', 'file'))}..."
        case "read_many_files":
            return "Reading files..."
        case "write_file":
            return f"Writing {os.path.basename(params.get('file_path', 'file'))}..."
        case "replace":
            return f"Editing {os.path.basename(params.get('file_path', 'file'))}..."
        case "run_shell_command":
            cmd = params.get("command", "")
            return f"Running {cmd[:50]}{'...' if len(cmd) > 50 else ''}"
        case "list_directory":
            return f"Listing {params.get('dir_path', '.')}..."
        case "glob" | "find_files":
            return f"Finding {params.get('pattern', '')}..."
        case "web_fetch":
            return f"Fetching {params.get('url', '')[:40]}..."
        case "google_web_search":
            return f"Searching: {params.get('query', '')}..."
        case _:
            return f"Using {tool_name}..."


class GeminiProvider(BaseProvider):
    name = "gemini"

    def build_command(
        self, prompt: str, model: str, session_id: str | None = None
    ) -> list[str]:
        cmd = [
            self.path, "-p",
            "--output-format", "stream-json",
            "--yolo", "-m", model,
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        cmd.extend(["--", prompt])
        return cmd

    def build_batch_command(self, prompt: str, model: str) -> list[str]:
        return [
            self.path, "-p",
            "--output-format", "text",
            "--yolo", "-m", model,
            "--", prompt,
        ]

    def parse_event(self, event: dict) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        event_type = event.get("type", "")

        status = _format_status(event)
        if status:
            events.append(StreamEvent(kind="status", text=status))

        if event_type == "message" and event.get("role") == "assistant":
            content = event.get("content", "")
            if content:
                events.append(StreamEvent(kind="response", text=content))
        elif event_type == "error":
            msg = event.get("message", "")
            if msg:
                events.append(StreamEvent(kind="error", text=msg))
                events.append(StreamEvent(kind="status", text=f"Error: {msg[:40]}"))
        elif event_type == "init":
            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                events.append(StreamEvent(kind="session", session_id=session_id))

        return events

    def stdout_limit(self) -> int | None:
        return 10 * 1024 * 1024

    def format_response(self, parts: list[str]) -> str:
        """Gemini streams content in chunks; join them all."""
        return "".join(parts)
