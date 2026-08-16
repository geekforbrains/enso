"""Grok CLI provider (xAI Grok Build CLI)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import quote

from . import truncate_status
from .claude import ClaudeProvider
from .claude import _tool_status as _claude_tool_status

if TYPE_CHECKING:
    from ..instructions import InstructionBundle


def _tool_status(tool_name: str, tool_input: dict) -> str:
    """Describe a grok tool call in one human-readable line.

    Grok emits the Claude Messages wire format but with its own tool
    vocabulary; map the captured grok tool names here — preferring a
    model-written ``description`` exactly like Claude's mapping — and fall
    back to Claude's for anything unrecognized.
    """
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
            return _claude_tool_status(tool_name, tool_input)


def _session_dirs(working_dir: str, policy_dir: str | None) -> list[Path]:
    """Candidate session directories for a working dir.

    Sessions live under ``<home>/sessions/<percent-encoded resolved working
    dir>/``. Unrestricted launches use ``$GROK_HOME`` (default ``~/.grok``);
    policy launches write into the revision-keyed staged homes under the
    policy dir, so those are searched too when one is bound.
    """
    encoded = quote(str(Path(working_dir).resolve()), safe="")
    homes = [Path(os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok"))]
    if policy_dir:
        homes.extend(sorted(Path(policy_dir).glob(".runtime/grok-home/*")))
    return [home / "sessions" / encoded for home in homes]


class GrokProvider(ClaudeProvider):
    """Grok speaks the Anthropic Messages wire format on stdout, so event
    parsing and response formatting are inherited from ClaudeProvider;
    commands, sessions, and instruction injection follow grok's own flags.
    """

    name = "grok"

    default_models: ClassVar[list[str]] = ["grok-4.6", "grok-4.5"]
    # The CLI's cached OAuth session token is the primary credential; the
    # API key is a key-only fallback.
    env_keys: ClassVar[tuple[str, ...]] = ("XAI_API_KEY",)

    # Levels accepted by `grok --effort`. No "max" (unlike claude); every
    # advertised model supports the full range.
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high", "xhigh"]
    _model_max_effort: ClassVar[dict[str, str]] = {}
    _default_max_effort = "xhigh"

    _tool_status = staticmethod(_tool_status)

    @staticmethod
    def _permission_args(launch) -> list[str]:
        """Permission flags per the launch contract in permissions.md.

        A policy launch denies anything a prompt would have asked about
        (headless has nobody to ask); the operator's allow/deny rules come
        from the staged GROK_HOME selected via the launch env, never from
        flags. Otherwise: grok's own bypass flag for headless automation
        (deny rules still apply).
        """
        if launch is not None and launch.mode == "policy":
            return ["--permission-mode", "dontAsk"]
        return ["--always-approve"]

    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
        launch=None,
        instructions: InstructionBundle | None = None,
    ) -> list[str]:
        """Build the grok CLI command.

        Session IDs prefixed with 'new:' use --session-id to create an
        Enso-owned session. Existing sessions use --resume. The prompt rides
        attached to its flag (``--single=<prompt>``) because a detached
        value rejects hyphen-leading prompts.
        """
        cmd = [
            self.path,
            "--output-format", "streaming-messages-json",
            *self._permission_args(launch),
            "--model", model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        if instructions is not None:
            # Attached form: a detached value is rejected by the CLI's parser
            # when the content starts with "-" (markdown bullets, frontmatter).
            cmd.append(f"--rules={instructions.content}")
        if session_id and session_id.startswith("new:"):
            cmd.extend(["--session-id", session_id.removeprefix("new:")])
        elif session_id:
            cmd.extend(["--resume", session_id])
        cmd.append(f"--single={prompt}")
        return cmd

    def build_batch_command(
        self,
        prompt: str,
        model: str,
        *,
        effort: str | None = None,
        launch=None,
        instructions: InstructionBundle | None = None,
    ) -> list[str]:
        """Build command for batch execution (jobs). No session continuity."""
        cmd = [
            self.path,
            "--output-format", "plain",
            *self._permission_args(launch),
            "--model", model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        if instructions is not None:
            cmd.append(f"--rules={instructions.content}")
        cmd.append(f"--single={prompt}")
        return cmd

    def retryable_error(self, text: str) -> bool:
        """A lapsed OAuth token can fail the first headless call before the
        CLI's background refresh lands; that exact signature is worth one
        retry.
        """
        return "Not signed in" in text

    def clear_session(
        self,
        session_id: str | None,
        working_dir: str,
        *,
        policy_dir: str | None = None,
    ) -> str:
        """Delete the session's local directory tree.

        Policy launches store sessions inside the staged revision homes, so
        those are searched alongside the user home. Grok also syncs sessions
        to xAI's remote registry; removing the local copy does not remove any
        remote-synced one.
        """
        if not session_id:
            return "no session"
        clean_id = session_id.removeprefix("new:")
        deleted = False
        for sessions_dir in _session_dirs(working_dir, policy_dir):
            session_dir = sessions_dir / clean_id
            if session_dir.is_dir():
                shutil.rmtree(session_dir)
                deleted = True
        if deleted:
            return f"deleted session {clean_id[:8]}"
        return f"session {clean_id[:8]} (no file found)"
