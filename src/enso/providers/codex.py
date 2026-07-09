"""Codex CLI provider."""

from __future__ import annotations

import json

from . import BaseProvider, StreamEvent, truncate_status

CODEX_MODEL_ALIASES = {
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
}

# Reasoning-effort levels in the Codex 0.144 model catalog, least to most.
EFFORT_LEVELS: list[str] = ["low", "medium", "high", "xhigh", "max", "ultra"]

_MODEL_MAX_EFFORT = {
    "gpt-5.6-sol": "ultra",
    "gpt-5.6-terra": "ultra",
    "gpt-5.6-luna": "max",
}


def resolve_codex_model(model: str) -> str:
    """Translate Enso's short Codex model names to CLI model IDs."""
    return CODEX_MODEL_ALIASES.get(model, model)


def max_effort_for_model(model: str) -> str:
    """Return the highest effort level advertised for a Codex model."""
    return _MODEL_MAX_EFFORT.get(resolve_codex_model(model), "xhigh")


def clamp_effort(effort: str, model: str) -> str:
    """Degrade ``effort`` to the highest level the model accepts."""
    if effort not in EFFORT_LEVELS:
        return effort
    cap = max_effort_for_model(model)
    return EFFORT_LEVELS[min(EFFORT_LEVELS.index(effort), EFFORT_LEVELS.index(cap))]


def _reasoning_override(effort: str) -> str:
    """Return the TOML config override expected by Codex CLI."""
    return f'model_reasoning_effort="{effort}"'


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
                return f"Running `{cmd[:50]}{'…' if len(cmd) > 50 else ''}`"
            case "file_changes":
                return "Editing files…"
            case "web_searches":
                return "Searching the web…"
            case "mcp_tool_calls":
                return "Using tool…"

    if event_type == "item.completed" and item_type == "reasoning":
        text = item.get("text", "")
        if text:
            return truncate_status(text)

    return None


class CodexProvider(BaseProvider):
    name = "codex"

    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
    ) -> list[str]:
        cli_model = resolve_codex_model(model)
        cmd = [self.path, "exec"]
        if session_id:
            cmd.append("resume")
        cmd.extend([
            "--dangerously-bypass-approvals-and-sandbox", "--json",
            "-m", cli_model,
        ])
        if effort:
            cmd.extend(["-c", _reasoning_override(effort)])
        cmd.append("--")
        if session_id:
            cmd.append(session_id)
        cmd.append(prompt)
        return cmd

    def build_batch_command(
        self, prompt: str, model: str, *, effort: str | None = None,
    ) -> list[str]:
        cli_model = resolve_codex_model(model)
        cmd = [
            self.path, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "-m", cli_model,
        ]
        if effort:
            cmd.extend(["-c", _reasoning_override(effort)])
        cmd.extend(["--", prompt])
        return cmd

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
            events.append(StreamEvent(kind="status", text="Working…"))
        elif event_type == "error":
            msg = event.get("message")
            if isinstance(msg, str) and msg:
                events.append(StreamEvent(kind="error", text=msg))
                if "reconnect" in msg.lower():
                    events.append(StreamEvent(kind="status", text="Reconnecting…"))
        elif event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value:
                    events.append(StreamEvent(kind="status", text=truncate_status(text_value)))
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
