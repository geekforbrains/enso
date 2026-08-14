"""Claude CLI provider."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from . import BaseProvider, StreamEvent, truncate_status


def _tool_status(tool_name: str, tool_input: dict) -> str:
    """Describe a tool call in one human-readable line.

    Some tools (Bash, Task) carry a model-written ``description``, which
    reads better than anything derived from the raw arguments; fall back to
    formatting the arguments for the tools that don't.
    """
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

    @staticmethod
    def _permission_args(launch) -> list[str]:
        """Permission flags per the launch contract in permissions.md.

        A policy launch loads exactly the operator's settings file, excludes
        the operator's user settings (so their personal rules cannot widen a
        workspace) while leaving the CLI's own instruction and skill discovery
        working, denies anything a prompt would have asked about (headless has
        nobody to ask), and loads exactly the policy's declared MCP servers —
        the conventional mcp.json when present, none otherwise — never the
        operator's ambient ones. Otherwise: today's bypass invocation.
        """
        if launch is not None and launch.mode == "policy":
            args = [
                "--settings", launch.policy_path,
                "--permission-mode", "dontAsk",
                "--setting-sources", "project",
                "--strict-mcp-config",
            ]
            if launch.mcp_config:
                args.extend(["--mcp-config", launch.mcp_config])
            return args
        return ["--dangerously-skip-permissions"]

    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
        launch=None,
    ) -> list[str]:
        """Build the Claude CLI command.

        Session IDs prefixed with 'new:' use --session-id to create an
        Enso-owned session. Existing sessions use --resume.
        """
        cmd = [
            self.path, "-p",
            "--output-format", "stream-json",
            "--verbose",
            *self._permission_args(launch),
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
        self, prompt: str, model: str, *, effort: str | None = None, launch=None,
    ) -> list[str]:
        """Build command for batch execution (jobs). No session continuity."""
        cmd = [
            self.path, "-p",
            "--output-format", "text",
            *self._permission_args(launch),
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
                if block_type == "thinking":
                    # Recent models return the thinking text redacted, so
                    # only the fact that it happened is reliably available.
                    summary = truncate_status(block.get("thinking", "") or "")
                    events.append(
                        StreamEvent(kind="status", text=summary or "Thinking")
                    )
                elif block_type == "tool_use":
                    events.append(StreamEvent(
                        kind="status",
                        text=_tool_status(block.get("name", ""), block.get("input", {})),
                ))
                elif block_type == "text" and block.get("text"):
                    events.append(StreamEvent(
                        kind="error" if is_api_error else "response",
                        text=block["text"],
                    ))

        elif event_type == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, str) and result_text:
                events.append(StreamEvent(
                    kind="error" if event.get("is_error") else "response",
                    text=result_text,
                ))
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
