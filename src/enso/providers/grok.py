"""Grok CLI provider (xAI Grok Build CLI)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

from . import session_path, truncate_status
from .claude import ClaudeProvider
from .claude import tool_status as claude_tool_status

# Grok's terminal reasons are terse; expand the ones an operator would otherwise guess at.
_TERMINAL_REASONS = {
    "cancelled": "cancelled — a denied tool call or an interrupted run",
}


def tool_status(tool_name: str, tool_input: dict) -> str:
    """Grok's tool vocabulary on the Claude wire format; unknown tools fall back to Claude's."""
    description = tool_input.get("description")
    if isinstance(description, str) and description.strip():
        return truncate_status(description)
    match tool_name:
        case "list_dir":
            return f"Listing {os.path.basename(tool_input.get('target_directory', 'directory'))}"
        case "read_file":
            return f"Reading {os.path.basename(tool_input.get('target_file', 'file'))}"
        case "grep":
            return f"Searching for {truncate_status(tool_input.get('pattern', ''))}"
        case "write":
            return f"Writing {os.path.basename(tool_input.get('file_path', 'file'))}"
        case "search_replace":
            return f"Editing {os.path.basename(tool_input.get('file_path', 'file'))}"
        case "run_terminal_command":
            return truncate_status(f"Running {tool_input.get('command', '')}")
        case _:
            return claude_tool_status(tool_name, tool_input)


def sessions_dir(cwd: str) -> Path:
    """``$GROK_HOME/sessions/<percent-encoded resolved cwd>``."""
    home = Path(os.environ.get("GROK_HOME") or "~/.grok").expanduser()
    return home / "sessions" / quote(str(Path(cwd).resolve()), safe="")


class GrokProvider(ClaudeProvider):
    """Same stdout wire format as Claude; its own flags and sessions.

    Its sessions are one directory each, named for the UUID Enso passes as ``--session-id``,
    so the identifier contract is Claude's for the same reason the wire format is.
    """

    name = "grok"
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high", "xhigh"]
    model_max_effort: ClassVar[dict[str, str]] = {}
    default_max_effort = "xhigh"
    models: ClassVar[list[str]] = ["grok-4.6", "grok-4.5"]
    unattended_args: ClassVar[list[str]] = ["--always-approve"]

    _tool_status = staticmethod(tool_status)

    @staticmethod
    def _terminal_reason(event: dict) -> str | None:
        """Grok names the reason in ``stop_reason``/``errors``, not ``terminal_reason``."""
        candidates: list[object] = [event.get("terminal_reason"), event.get("stop_reason")]
        errors = event.get("errors")
        if isinstance(errors, list):
            candidates.append(", ".join(item for item in errors if isinstance(item, str) and item))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                return _TERMINAL_REASONS.get(candidate, candidate)
        return None

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
        # The prompt rides attached to its flag (--single=): a detached value
        # starting with "-" (a markdown bullet) is rejected by the parser.
        output = "plain" if batch else "streaming-messages-json"
        cmd = [self.path, "--output-format", output, *args, "--model", model, "--effort", effort]
        if session_id and not batch:
            cmd.extend(["--session-id" if new_session else "--resume", session_id])
        cmd.append(f"--single={prompt}")
        return cmd

    def retryable_error(self, text: str) -> bool:
        """A lapsed OAuth token can fail the first headless call before the refresh lands."""
        return "Not signed in" in text

    def clear_session(self, session_id: str, cwd: str) -> str:
        self.check_session_id(session_id)
        session_dir = session_path(sessions_dir(cwd), session_id)
        if session_dir.is_dir():
            shutil.rmtree(session_dir)
            return f"deleted session {session_id[:8]}"
        return f"session {session_id[:8]} (no directory found)"
