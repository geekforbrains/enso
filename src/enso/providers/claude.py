"""Claude CLI provider."""

from __future__ import annotations

import os
from pathlib import Path

from . import BaseProvider, StreamEvent, truncate_status

# Reasoning-effort levels accepted by `claude --effort`, ordered least → most.
EFFORT_LEVELS: list[str] = ["low", "medium", "high", "xhigh", "max"]

# Maximum effort supported per model. Models not listed default to "high"
# (safe floor that every supported Claude model accepts). Opus 4.7 is the
# only family that currently exposes the full range up through "max".
_MODEL_MAX_EFFORT: dict[str, str] = {
    "opus": "max",
    "claude-opus-4-7": "max",
}


def max_effort_for_model(model: str) -> str:
    """Return the highest effort level the given model supports."""
    return _MODEL_MAX_EFFORT.get(model, "high")


def clamp_effort(effort: str, model: str) -> str:
    """Degrade ``effort`` to the highest level the model actually supports."""
    if effort not in EFFORT_LEVELS:
        return effort
    cap = max_effort_for_model(model)
    req_idx = EFFORT_LEVELS.index(effort)
    cap_idx = EFFORT_LEVELS.index(cap)
    return EFFORT_LEVELS[min(req_idx, cap_idx)]


def _format_tool_status(tool_name: str, tool_input: dict) -> str:
    """Format tool usage into human-readable status."""
    match tool_name:
        case "Read":
            return f"Reading {os.path.basename(tool_input.get('file_path', 'file'))}"
        case "Write":
            return f"Writing {os.path.basename(tool_input.get('file_path', 'file'))}"
        case "Edit":
            return f"Editing {os.path.basename(tool_input.get('file_path', 'file'))}"
        case "Bash":
            cmd = tool_input.get("command", "")
            return f"Running `{cmd[:50]}{'…' if len(cmd) > 50 else ''}`"
        case "Glob":
            return f"Finding {tool_input.get('pattern', '')}"
        case "Grep":
            return f"Searching for '{tool_input.get('pattern', '')}'"
        case "WebFetch":
            return f"Fetching {tool_input.get('url', '')[:40]}"
        case "WebSearch":
            return f"Searching: {tool_input.get('query', '')}"
        case "Agent":
            return "Running subagent…"
        case _:
            return f"Using {tool_name}"


def _get_project_dir(working_dir: str) -> str:
    """Derive Claude's project session directory from working_dir."""
    resolved = str(Path(working_dir).resolve())
    mangled = resolved.replace("/", "-").replace(".", "-")
    return os.path.expanduser(f"~/.claude/projects/{mangled}")


class ClaudeProvider(BaseProvider):
    name = "claude"

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
            for block in message.get("content", []):
                block_type = block.get("type")
                if block_type == "thinking":
                    text = block.get("thinking", "")
                    if text:
                        events.append(StreamEvent(
                            kind="status", text=truncate_status(text),
                        ))
                elif block_type == "tool_use":
                    events.append(StreamEvent(
                        kind="status",
                        text=_format_tool_status(
                            block.get("name", ""), block.get("input", {})
                        ),
                    ))
                elif block_type == "text" and block.get("text"):
                    events.append(StreamEvent(kind="response", text=block["text"]))

            # Track per-turn usage from each assistant event. The last
            # one reflects the actual context window fill for the final
            # API call (earlier turns re-count cached tokens).
            usage = message.get("usage", {})
            if usage:
                self._last_usage = usage

        elif event_type == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, str) and result_text:
                events.append(StreamEvent(kind="response", text=result_text))

            session_id = event.get("session_id")
            if isinstance(session_id, str) and session_id:
                events.append(StreamEvent(kind="session", session_id=session_id))

            # Emit context window usage from the last assistant turn.
            # modelUsage provides the context window size; _last_usage
            # (from the final assistant event) gives the actual token
            # counts for the last API call — which reflects how full
            # the context window really is.
            model_usage = event.get("modelUsage", {})
            last = getattr(self, "_last_usage", None)
            if model_usage and last:
                model_data = next(iter(model_usage.values()), {})
                context_window = model_data.get("contextWindow", 0)
                total_tokens = (
                    last.get("input_tokens", 0)
                    + last.get("cache_creation_input_tokens", 0)
                    + last.get("cache_read_input_tokens", 0)
                    + last.get("output_tokens", 0)
                )
                if context_window:
                    events.append(StreamEvent(
                        kind="usage",
                        usage={
                            "total_tokens": total_tokens,
                            "context_window": context_window,
                            "pct": round(total_tokens / context_window * 100),
                        },
                    ))

        return events

    def stdout_limit(self) -> int | None:
        return 10 * 1024 * 1024

    def clear_session(self, session_id: str | None, working_dir: str) -> str:
        if not session_id:
            return "no session"
        clean_id = session_id.removeprefix("new:")
        session_file = Path(_get_project_dir(working_dir)) / f"{clean_id}.jsonl"
        if session_file.is_file():
            session_file.unlink()
            return f"deleted session {clean_id[:8]}"
        return f"session {clean_id[:8]} (no file found)"

