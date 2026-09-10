"""Codex CLI provider."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import ClassVar

from . import SESSION_ID_RE, BaseProvider, StreamEvent, truncate_status

MODEL_ALIASES = {
    "astra": "gpt-6-astra",
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
}

# Codex runs every command through a login shell; the wrapper is noise in a status line.
_SHELL_WRAPPER_RE = re.compile(r"^/\S*?/(?:ba|z|k)?sh\s+-[a-z]*c\s+(.*)$", re.DOTALL)
_FILE_CHANGE_VERBS = {"add": "Writing", "delete": "Deleting", "update": "Editing"}


def resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


def _unwrap_command(command: str) -> str:
    match = _SHELL_WRAPPER_RE.match(command.strip())
    inner = match.group(1).strip() if match else command.strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
        inner = inner[1:-1]
    return inner


def _item_status(item: dict) -> str | None:
    """Describe a started Codex work item, or None when it isn't worth showing."""
    match item.get("type"):
        case "command_execution":
            command = _unwrap_command(item.get("command", ""))
            return truncate_status(f"Running {command}") if command else None
        case "file_change":
            names = [
                f"{_FILE_CHANGE_VERBS.get(change.get('kind') or '', 'Editing')} "
                f"{os.path.basename(change.get('path', 'file'))}"
                for change in item.get("changes") or []
                if isinstance(change, dict)
            ]
            if not names:
                return None
            extra = f" (+{len(names) - 1} more)" if len(names) > 1 else ""
            return truncate_status(names[0] + extra)
        case "reasoning":
            return "Thinking"
        case "web_search":
            query = item.get("query", "")
            return truncate_status(f"Searching: {query}") if query else "Searching"
        case "mcp_tool_call":
            return truncate_status(f"Using {item.get('tool') or item.get('name') or 'tool'}")
        case _:
            return None


class CodexProvider(BaseProvider):
    name = "codex"
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high", "xhigh", "max", "ultra"]
    model_max_effort: ClassVar[dict[str, str]] = {
        "gpt-6-astra": "ultra",
        "gpt-5.6-sol": "ultra",
        "gpt-5.6-terra": "ultra",
        "gpt-5.6-luna": "max",
    }
    default_max_effort = "xhigh"
    models: ClassVar[list[str]] = ["astra", "sol", "terra", "luna"]
    unattended_args: ClassVar[list[str]] = ["--dangerously-bypass-approvals-and-sandbox"]
    assigns_session_id = False
    # ``thread.started`` announces an opaque thread id, handed straight back to
    # ``codex exec resume``; the shared token contract is exactly what that needs to be.
    session_id_re: ClassVar[re.Pattern[str]] = SESSION_ID_RE

    @classmethod
    def max_effort(cls, model: str) -> str:
        return super().max_effort(resolve_model(model))

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
        cmd = [self.path, "exec"]
        if session_id and not batch:
            cmd.append("resume")
        cmd.extend(args)
        if not batch:
            cmd.append("--json")
        cmd.extend(["-m", resolve_model(model), "-c", f'model_reasoning_effort="{effort}"', "--"])
        if session_id and not batch:
            cmd.append(session_id)
        cmd.append(prompt)
        return cmd

    def parse_event(self, event: dict) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        match event.get("type", ""):
            case "error":
                msg = event.get("message")
                if isinstance(msg, str) and msg:
                    events.append(StreamEvent(kind="error", text=msg))
            case "item.started":
                status = _item_status(event.get("item", {}))
                if status:
                    events.append(StreamEvent(kind="status", text=status))
            case "item.completed":
                item = event.get("item", {})
                text = item.get("text")
                if item.get("type") == "agent_message" and isinstance(text, str) and text:
                    events.append(StreamEvent(kind="response", text=text))
            case "turn.failed":
                error = event.get("error")
                msg = error.get("message") if isinstance(error, dict) else error
                if not isinstance(msg, str) or not msg:
                    msg = event.get("message")
                if isinstance(msg, str) and msg:
                    events.append(StreamEvent(kind="error", text=msg))
            case "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    events.append(StreamEvent(kind="session", session_id=thread_id))
        return events

    def stderr_to_stdout(self) -> bool:
        return True
