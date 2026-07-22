"""Codex CLI provider."""

from __future__ import annotations

import json
from typing import ClassVar

from . import BaseProvider, StreamEvent

CODEX_MODEL_ALIASES = {
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
}


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

        if event_type == "error":
            msg = event.get("message")
            if isinstance(msg, str) and msg:
                events.append(StreamEvent(kind="error", text=msg))
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

    def stderr_to_stdout(self) -> bool:
        return True
