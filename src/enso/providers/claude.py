"""Claude CLI provider."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import ClassVar

from . import BaseProvider, StreamEvent, truncate_status


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

    # Levels accepted by `claude --effort`. Models not listed in
    # _model_max_effort default to "high" (safe floor every supported
    # Claude model accepts); Opus 4.7 is the only family that currently
    # exposes the full range up through "max".
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high", "xhigh", "max"]
    _model_max_effort: ClassVar[dict[str, str]] = {
        "opus": "max",
        "claude-opus-4-7": "max",
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

    def clear_session(self, session_id: str | None, working_dir: str) -> str:
        if not session_id:
            return "no session"
        clean_id = session_id.removeprefix("new:")
        session_file = Path(_get_project_dir(working_dir)) / f"{clean_id}.jsonl"
        if session_file.is_file():
            session_file.unlink()
            return f"deleted session {clean_id[:8]}"
        return f"session {clean_id[:8]} (no file found)"


class KageClaudeProvider(BaseProvider):
    """Claude provider that drives Claude Code through kage/tmux."""

    name = "claude"

    def __init__(
        self,
        path: str,
        *,
        timeout: int = 1800,
        restart: bool = True,
    ):
        super().__init__(path)
        self.timeout = timeout
        self.restart = restart

    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
    ) -> list[str]:
        clean_id = session_id.removeprefix("new:") if session_id else None
        cmd = [
            self.path, "claude",
            "--stream",
            "--stop-on-signal",
            "--timeout", str(self.timeout),
            "--model", model,
        ]
        if self.restart:
            cmd.append("--restart")
        if effort:
            cmd.extend(["--effort", effort])
        if clean_id:
            cmd.extend(["--session-id", clean_id])
        cmd.extend(["--", prompt])
        return cmd

    def build_batch_command(
        self, prompt: str, model: str, *, effort: str | None = None,
    ) -> list[str]:
        # --stream makes job completion ride kage's Stop hook instead of its
        # TUI done-marker scrape. The scrape only recognises sub-minute markers
        # (`✻ Verb for 5s`); any job whose turn runs past 60s renders the marker
        # in minutes and is missed, so the job hangs to the wall-clock timeout.
        # The Stop hook is format-independent. Stays ephemeral (no --session-id):
        # kage assigns its own uuid, giving an isolated transcript + events file.
        # --stop-on-signal lets kage tear down its tmux pane when the job runner
        # terminates this process on timeout/cancel; without it the underlying
        # Claude pane is orphaned (the pane lives in tmux's own session, outside
        # our process group).
        cmd = [
            self.path, "claude",
            "--stream",
            "--stop-on-signal",
            "--timeout", str(self.timeout),
            "--model", model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        cmd.extend(["--", prompt])
        return cmd

    def stdout_limit(self) -> int | None:
        # kage emits short JSONL summaries; asyncio's default buffer is plenty.
        return None

    def parse_batch_output(self, stdout: str) -> str:
        """Pull the final response (or error) out of kage's --stream JSONL.

        Batch jobs now stream, so stdout is newline-delimited JSON event
        envelopes. Collect the terminal `done` response, falling back to an
        `error` message. If nothing parses as an event (e.g. an unexpected
        plain-text emission), return the raw stripped stdout so a job's output
        is never silently dropped.
        """
        response_parts: list[str] = []
        error_text: str | None = None
        saw_event = False
        for line in stdout.splitlines():
            raw = self.parse_line(line)
            if raw is None:
                continue
            saw_event = True
            for ev in self.parse_event(raw):
                if ev.kind == "response":
                    response_parts.append(ev.text)
                elif ev.kind == "error":
                    error_text = ev.text
        if not saw_event:
            return stdout.strip()
        if response_parts:
            return self.format_response(response_parts).strip()
        return error_text or ""

    def parse_event(self, event: dict) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            events.append(StreamEvent(kind="session", session_id=session_id))

        status = event.get("status")
        if status == "progress":
            text = (
                event.get("summary")
                or event.get("tool")
                or event.get("event")
                or "Working..."
            )
            events.append(StreamEvent(kind="status", text=truncate_status(str(text))))
        elif status == "done":
            response = event.get("response")
            if isinstance(response, str) and response:
                events.append(StreamEvent(kind="response", text=response))
        elif status == "error":
            message = event.get("message") or event.get("reason") or "kage error"
            events.append(StreamEvent(kind="error", text=str(message)))

        return events

    def clear_session(self, session_id: str | None, working_dir: str) -> str:
        if not session_id:
            return "no session"

        clean_id = session_id.removeprefix("new:")
        parts: list[str] = []
        try:
            result = subprocess.run(
                [self.path, "session", "kill", "--session-id", clean_id],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=working_dir,
            )
            if result.returncode == 0:
                parts.append(f"stopped kage session {clean_id[:8]}")
            else:
                detail = (result.stderr or result.stdout).strip()
                parts.append(f"kage stop failed: {detail[:120] or result.returncode}")
        except FileNotFoundError:
            parts.append(f"kage not found for session {clean_id[:8]}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            parts.append(f"kage stop failed: {exc}")

        session_file = Path(_get_project_dir(working_dir)) / f"{clean_id}.jsonl"
        if session_file.is_file():
            session_file.unlink()
            parts.append(f"deleted transcript {clean_id[:8]}")
        else:
            parts.append(f"transcript {clean_id[:8]} (no file found)")
        return "; ".join(parts)
