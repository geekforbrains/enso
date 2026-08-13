"""Codex CLI provider."""

from __future__ import annotations

import json
import os
import re
from typing import ClassVar

from . import BaseProvider, StreamEvent, truncate_status

CODEX_MODEL_ALIASES = {
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
}

# Codex runs every command through a login shell; the wrapper is noise in a
# status line, so unwrap it back to what the model actually asked to run.
_SHELL_WRAPPER_RE = re.compile(r"^/\S*?/(?:ba|z|k)?sh\s+-[a-z]*c\s+(.*)$", re.DOTALL)
_FILE_CHANGE_VERBS = {"add": "Writing", "delete": "Deleting", "update": "Editing"}


def _unwrap_command(command: str) -> str:
    """Strip Codex's ``/bin/zsh -lc "…"`` wrapper from a command line."""
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
            changes = item.get("changes") or []
            names = [
                f"{_FILE_CHANGE_VERBS.get(change.get('kind') or '', 'Editing')} "
                f"{os.path.basename(change.get('path', 'file'))}"
                for change in changes
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
            tool = item.get("tool") or item.get("name") or "tool"
            return truncate_status(f"Using {tool}")
        case _:
            return None


def resolve_codex_model(model: str) -> str:
    """Translate Enso's short Codex model names to CLI model IDs."""
    return CODEX_MODEL_ALIASES.get(model, model)


def _reasoning_override(effort: str) -> str:
    """Return the TOML config override expected by Codex CLI."""
    return f'model_reasoning_effort="{effort}"'


class CodexProvider(BaseProvider):
    name = "codex"

    default_models: ClassVar[list[str]] = list(CODEX_MODEL_ALIASES)
    env_keys: ClassVar[tuple[str, ...]] = ("OPENAI_API_KEY",)

    # Reasoning-effort levels in the Codex 0.144 model catalog, least to most.
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high", "xhigh", "max", "ultra"]
    _model_max_effort: ClassVar[dict[str, str]] = {
        "gpt-5.6-sol": "ultra",
        "gpt-5.6-terra": "ultra",
        "gpt-5.6-luna": "max",
    }
    _default_max_effort = "xhigh"

    @classmethod
    def max_effort_for_model(cls, model: str) -> str:
        """Return the highest effort level advertised for a Codex model."""
        return super().max_effort_for_model(resolve_codex_model(model))

    @staticmethod
    def _permission_args(launch) -> list[str]:
        """Permission flags per the launch contract in permissions.md.

        A policy launch relies on the staged CODEX_HOME (set via the launch
        env) to select the operator's config, rejects unknown keys, and loads
        only staged rules — or none, when none are configured. Otherwise:
        today's bypass invocation.
        """
        if launch is not None and launch.mode == "policy":
            # --skip-git-repo-check bypasses the "are you in a git repo" UX
            # guard (a workspace need not be one); it is not a security
            # boundary. The staged CODEX_HOME selects the operator's profile.
            args = ["--strict-config", "--skip-git-repo-check"]
            if launch.ignore_rules:
                args.append("--ignore-rules")
            return args
        return ["--dangerously-bypass-approvals-and-sandbox"]

    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
        launch=None,
    ) -> list[str]:
        cli_model = resolve_codex_model(model)
        cmd = [self.path, "exec"]
        if session_id:
            cmd.append("resume")
        cmd.extend([*self._permission_args(launch), "--json", "-m", cli_model])
        if effort:
            cmd.extend(["-c", _reasoning_override(effort)])
        cmd.append("--")
        if session_id:
            cmd.append(session_id)
        cmd.append(prompt)
        return cmd

    def build_batch_command(
        self, prompt: str, model: str, *, effort: str | None = None, launch=None,
    ) -> list[str]:
        cli_model = resolve_codex_model(model)
        cmd = [
            self.path, "exec",
            *self._permission_args(launch),
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

        if event_type == "error":
            msg = event.get("message")
            if isinstance(msg, str) and msg:
                events.append(StreamEvent(kind="error", text=msg))
        elif event_type == "item.started":
            status = _item_status(event.get("item", {}))
            if status:
                events.append(StreamEvent(kind="status", text=status))
        elif event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value:
                    events.append(StreamEvent(kind="response", text=text_value))
        elif event_type == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                msg = error.get("message")
            elif isinstance(error, str):
                msg = error
            else:
                msg = None
            if not isinstance(msg, str) or not msg:
                msg = event.get("message")
            if isinstance(msg, str) and msg:
                events.append(StreamEvent(kind="error", text=msg))
        elif event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                events.append(StreamEvent(kind="session", session_id=thread_id))

        return events

    def stderr_to_stdout(self) -> bool:
        return True
