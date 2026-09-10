"""OpenCode CLI provider for operator-selected models, including OpenRouter."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
from typing import ClassVar

from . import SESSION_ID_RE, BaseProvider, StreamEvent, truncate_status

CLEAR_SESSION_TIMEOUT = 10
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _text(values: dict, *keys: str) -> str:
    """Return the first non-empty string under ``keys``."""
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _basename(values: dict, *keys: str) -> str:
    value = _text(values, *keys).rstrip("/")
    return os.path.basename(value) if value else ""


def tool_status(tool_name: str, tool_input: dict) -> str:
    """Describe an OpenCode tool event in one short line."""
    description = _text(tool_input, "description")
    if description:
        return truncate_status(description)
    name = tool_name.casefold()
    match name:
        case "read":
            return f"Reading {_basename(tool_input, 'filePath', 'file_path', 'path') or 'file'}"
        case "write":
            return f"Writing {_basename(tool_input, 'filePath', 'file_path', 'path') or 'file'}"
        case "edit" | "multiedit" | "apply_patch" | "patch":
            return f"Editing {_basename(tool_input, 'filePath', 'file_path', 'path') or 'files'}"
        case "list" | "list_dir":
            return f"Listing {_basename(tool_input, 'path', 'directory') or 'directory'}"
        case "glob":
            pattern = _text(tool_input, "pattern")
            return truncate_status(f"Finding {pattern}") if pattern else "Finding files"
        case "grep":
            pattern = _text(tool_input, "pattern", "query")
            return truncate_status(f"Searching for {pattern}") if pattern else "Searching files"
        case "bash" | "shell" | "run_command":
            command = _text(tool_input, "command")
            return truncate_status(f"Running {command}") if command else "Running command"
        case "webfetch" | "web_fetch" | "fetch":
            url = _text(tool_input, "url")
            return truncate_status(f"Fetching {url}") if url else "Fetching page"
        case "websearch" | "web_search":
            query = _text(tool_input, "query")
            return truncate_status(f"Searching: {query}") if query else "Searching the web"
        case "task" | "agent":
            return "Running subagent"
        case "skill":
            skill = _text(tool_input, "name")
            return truncate_status(f"Loading {skill}") if skill else "Loading skill"
        case _:
            return f"Using {truncate_status(tool_name, 40) or 'tool'}"


def _error_text(event: dict) -> str:
    """Extract the useful message from OpenCode's nested error variants."""
    error = event.get("error")
    if isinstance(error, str) and error.strip():
        return error
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict):
            message = _text(data, "message")
            if message:
                return message
        message = _text(error, "message", "name")
        if message:
            return message
    return _text(event, "message") or "unknown error"


class OpenCodeProvider(BaseProvider):
    """Headless ``opencode run`` with JSONL events and CLI-owned session ids."""

    name: ClassVar[str] = "opencode"
    # OpenCode calls effort a model variant. Providers expose different subsets, so this
    # union is passed through literally and an operator may choose the one their model knows.
    effort_levels: ClassVar[list[str]] = [
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    default_max_effort = "max"
    # The catalog is open-ended. Setup needs one useful default; task 018 supplies discovery.
    models: ClassVar[list[str]] = ["openrouter/deepseek/deepseek-v4-flash"]
    unattended_args: ClassVar[list[str]] = ["--auto"]
    assigns_session_id = False
    # OpenCode announces an opaque ``ses_…`` id and takes it back on ``-s`` and
    # ``session delete``, so the shared token contract is the whole requirement.
    session_id_re: ClassVar[re.Pattern[str]] = SESSION_ID_RE

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
        # JSON is also required for jobs: OpenCode's default format includes a decorated
        # model header, while format_batch_output extracts only the actual answer.
        cmd = [
            self.path,
            "run",
            "-m",
            model,
            "--variant",
            effort,
            *args,
            "--format",
            "json",
        ]
        if cwd:
            # OpenCode resolves its project root from $PWD before the process cwd, and only
            # --dir overrides that. Without this, a service that inherited a $PWD for another
            # directory would silently work against the wrong project.
            cmd.extend(["--dir", cwd])
        if session_id and not batch:
            cmd.extend(["-s", session_id])
        cmd.extend(["--", prompt])
        return cmd

    def parse_event(self, event: dict) -> list[StreamEvent]:
        part = event.get("part")
        part = part if isinstance(part, dict) else {}
        # OpenCode stamps every event with the session it created, and a run can fail before
        # it ever reaches a step. Announcing the id from whatever event carries it first, and
        # ahead of that event's own error or text, is what leaves a failed turn something the
        # conversation can resume or clear instead of an orphaned session.
        session_id = event.get("sessionID")
        events: list[StreamEvent] = []
        if isinstance(session_id, str) and session_id:
            events.append(StreamEvent(kind="session", session_id=session_id))
        match event.get("type"):
            case "step_start":
                events.append(StreamEvent(kind="status", text="Thinking"))
            case "tool_use":
                tool = part.get("tool")
                state = part.get("state")
                tool_input = state.get("input") if isinstance(state, dict) else None
                events.append(
                    StreamEvent(
                        kind="status",
                        text=tool_status(
                            tool if isinstance(tool, str) else "",
                            tool_input if isinstance(tool_input, dict) else {},
                        ),
                    )
                )
            case "text":
                text = part.get("text")
                if isinstance(text, str) and text:
                    events.append(StreamEvent(kind="response", text=text))
            case "error":
                events.append(StreamEvent(kind="error", text=_error_text(event)))
        return events

    def format_batch_output(self, stdout: str) -> str:
        """Extract the last answer or error without requiring a final step event."""
        final: list[str] = []
        plain: list[str] = []
        for line in stdout.splitlines():
            event = self.parse_line(line)
            if event is None:
                if line.strip():
                    plain.append(line.strip())
                continue
            for parsed in self.parse_event(event):
                if parsed.kind in {"response", "error"} and parsed.text:
                    final.append(parsed.text)
        return final[-1] if final else "\n".join(plain).strip()

    def clear_session(self, session_id: str, cwd: str) -> str:
        """Ask OpenCode to delete its session, with a bounded synchronous call."""
        # Checked even though there is no path here: an option-like id would otherwise be
        # read as a flag by the CLI rather than as the session to delete.
        self.check_session_id(session_id)
        try:
            result = subprocess.run(
                [self.path, "session", "delete", session_id],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=CLEAR_SESSION_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"session delete timed out after {CLEAR_SESSION_TIMEOUT}s") from exc
        output = _ANSI_ESCAPE_RE.sub("", "\n".join((result.stdout, result.stderr))).strip()
        if result.returncode == 0:
            return f"deleted session {session_id[:8]}"
        if "Session not found" in output:
            return f"session {session_id[:8]} (not found)"
        detail = truncate_status(output) or f"exit status {result.returncode}"
        raise RuntimeError(f"session delete failed: {detail}")
