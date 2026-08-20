"""Google Antigravity CLI provider."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import unquote, urlparse

from . import BaseProvider, StreamEvent, truncate_status

if TYPE_CHECKING:
    from ..instructions import ValidatedInstructions

AGY_MODELS = [
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.5-flash-high",
    "gemini-3.5-flash-medium",
    "gemini-3.5-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
]

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ACTIVE_CONVERSATION_RE = re.compile(rf"Print mode: conversation=({_UUID})")
_CREATED_CONVERSATION_RE = re.compile(rf"Created conversation ({_UUID})")

# Agy has its own five-minute print watchdog but no disable sentinel: zero and
# negative durations time out immediately. Give finite Enso deadlines a small
# grace so Enso owns timeout cleanup and user messaging. For agent.timeout=0,
# Go's largest duration is practical infinity while remaining valid CLI input.
_PRINT_TIMEOUT_GRACE_SECONDS = 5
_MAX_GO_DURATION = "2562047h47m16.854775807s"
_MAX_GO_DURATION_SECONDS = 9_223_372_036

# Antigravity's project catalog. Undocumented storage — if the format moves,
# lookups miss and fresh conversations fall back to --new-project, which only
# costs a duplicate project entry.
_PROJECTS_DIR = Path("~/.gemini/config/projects")

# Print mode prints only the final answer, so live progress has to come from
# the conversation's trajectory store: one SQLite file per conversation whose
# `steps` table gains rows as the agent works. Each tool step embeds a JSON
# blob carrying a model-written `toolAction` label ("Counting lines"). Also
# undocumented — every read here is best-effort, and the runtime falls back
# to a plain elapsed timer when it yields nothing.
_CONVERSATIONS_DIR = Path("~/.gemini/antigravity-cli/conversations")
_TOOL_ACTION_RE = re.compile(rb'"toolAction"\s*:\s*"([^"]{1,200})"')
_PROGRESS_POLL_SECS = 0.5
# A conversation ID reaches the log a beat after launch; keep watching for it
# well past that, since a slow cold start delays the whole trajectory.
_CONVERSATION_WAIT_SECS = 60.0


def _with_shared_instructions(
    prompt: str,
    instructions: ValidatedInstructions | None,
) -> str:
    """Prepend Enso's explicit shared-instruction envelope to an Agy prompt."""
    if instructions is None:
        return prompt
    return (
        "<enso-shared-instructions>\n"
        f"{instructions.content}\n"
        "</enso-shared-instructions>\n\n"
        "<enso-user-prompt>\n"
        f"{prompt}"
    )


def _resource_uris(data: dict) -> list[str]:
    """Folder URIs a catalog entry claims, in both plain and git shapes."""
    resources = data.get("projectResources")
    entries = resources.get("resources") if isinstance(resources, dict) else None
    if not isinstance(entries, list):
        return []
    uris = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        git_folder = entry.get("gitFolder")
        uri = entry.get("folderUri") or (
            git_folder.get("folderUri") if isinstance(git_folder, dict) else None
        )
        if isinstance(uri, str) and uri.startswith("file://"):
            uris.append(uri)
    return uris


def find_project_id(working_dir: str) -> str | None:
    """Return the ID of the Antigravity project mapped to ``working_dir``.

    First match in filename order wins, so duplicate catalog entries for
    one directory resolve stably.
    """
    target = Path(working_dir).resolve()
    for entry in sorted(_PROJECTS_DIR.expanduser().glob("*.json")):
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        project_id = data.get("id")
        if not isinstance(project_id, str) or not project_id:
            continue
        for uri in _resource_uris(data):
            if Path(unquote(urlparse(uri).path)).resolve() == target:
                return project_id
    return None


class AgyProvider(BaseProvider):
    """Adapter for Antigravity's plain-text headless mode.

    Print mode never associates the working directory with a project, and a
    conversation is pinned to its project forever at creation. Fresh
    conversations therefore pass --project (existing catalog entry for the
    workspace) or --new-project (first use); resumes rely on the pin.
    """

    name = "agy"
    streaming_output = False
    default_models: ClassVar[list[str]] = AGY_MODELS
    env_keys: ClassVar[tuple[str, ...]] = ()

    # Agy's public model catalog already exposes concrete effort-qualified
    # slugs (for example, gemini-3.6-flash-low). Passing a separate --effort
    # with one of those slugs is rejected unless it happens to match, and the
    # Claude models reject --effort entirely. Keep /model as the single,
    # authoritative selector instead of exposing incompatible combinations.
    effort_levels: ClassVar[list[str]] = []

    def __init__(
        self,
        path: str,
        working_dir: str | None = None,
        *,
        timeout: int | float | None = None,
    ):
        super().__init__(path, working_dir, timeout=timeout)
        self._log_path: str | None = None
        self._session_id: str | None = None

    def _create_log_file(self) -> str:
        fd, path = tempfile.mkstemp(prefix="enso-agy-", suffix=".log")
        os.close(fd)
        self._log_path = path
        return path

    def _project_args(self) -> list[str]:
        project_id = find_project_id(self.working_dir or os.getcwd())
        return ["--project", project_id] if project_id else ["--new-project"]

    def _print_timeout_args(self) -> list[str]:
        if self.timeout is None:
            return []
        if self.timeout == 0:
            return ["--print-timeout", _MAX_GO_DURATION]
        duration = math.ceil(self.timeout) + _PRINT_TIMEOUT_GRACE_SECONDS
        if duration >= _MAX_GO_DURATION_SECONDS:
            return ["--print-timeout", _MAX_GO_DURATION]
        return ["--print-timeout", f"{duration}s"]

    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
        launch=None,
        instructions: ValidatedInstructions | None = None,
    ) -> list[str]:
        # ``launch`` is accepted for signature parity and ignored: agy has no
        # permission model, so policy-controlled dispatch refuses it upstream.
        self._session_id = session_id
        cmd = [
            self.path,
            "--dangerously-skip-permissions",
            "--log-file", self._create_log_file(),
            "--model", model,
        ]
        cmd.extend(self._print_timeout_args())
        if session_id:
            cmd.extend(["--conversation", session_id])
        else:
            cmd.extend(self._project_args())
        cmd.extend(["--prompt", _with_shared_instructions(prompt, instructions)])
        return cmd

    def build_batch_command(
        self,
        prompt: str,
        model: str,
        *,
        effort: str | None = None,
        launch=None,
        instructions: ValidatedInstructions | None = None,
    ) -> list[str]:
        cmd = [
            self.path,
            "--dangerously-skip-permissions",
            "--model", model,
        ]
        cmd.extend(self._print_timeout_args())
        cmd.extend(self._project_args())
        cmd.extend(["--prompt", _with_shared_instructions(prompt, instructions)])
        return cmd

    def finalize_events(self) -> list[StreamEvent]:
        path, self._log_path = self._log_path, None
        if not path:
            return []
        try:
            with open(path, encoding="utf-8", errors="replace") as log_file:
                content = log_file.read()
        except OSError:
            return []
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)

        active = _ACTIVE_CONVERSATION_RE.findall(content)
        created = _CREATED_CONVERSATION_RE.findall(content)
        session_id = active[-1] if active else (created[-1] if created else None)
        if not session_id:
            return []
        return [StreamEvent(kind="session", session_id=session_id.lower())]

    def parse_event(self, event: dict) -> list[StreamEvent]:
        return []

    # -- Live progress --

    async def _await_conversation_id(self) -> str | None:
        """Wait for the conversation ID the CLI logs once it starts working.

        A resumed conversation already knows its ID; a fresh one only learns
        it when the CLI announces the conversation it created.
        """
        if self._session_id:
            return self._session_id
        deadline = asyncio.get_running_loop().time() + _CONVERSATION_WAIT_SECS
        while asyncio.get_running_loop().time() < deadline:
            path = self._log_path
            if path is None:
                return None  # the run finished before a conversation appeared
            try:
                content = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            for pattern in (_ACTIVE_CONVERSATION_RE, _CREATED_CONVERSATION_RE):
                found = pattern.findall(content)
                if found:
                    return found[-1].lower()
            await asyncio.sleep(_PROGRESS_POLL_SECS)
        return None

    @staticmethod
    def _read_steps(db_path: Path, after_idx: int) -> list[tuple[int, bytes | None]]:
        """Read trajectory steps newer than ``after_idx``.

        Opened read-only so a concurrent write from the CLI can't be
        disturbed by the reader.
        """
        uri = f"file:{db_path}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as con:
            return con.execute(
                "SELECT idx, step_payload FROM steps WHERE idx > ? ORDER BY idx",
                (after_idx,),
            ).fetchall()

    async def poll_progress(self) -> AsyncIterator[StreamEvent]:
        """Emit status events by tailing the conversation's trajectory."""
        conversation_id = await self._await_conversation_id()
        if not conversation_id:
            return
        db_path = _CONVERSATIONS_DIR.expanduser() / f"{conversation_id}.db"

        last_idx = -1
        last_action: str | None = None
        while True:
            try:
                rows = await asyncio.to_thread(self._read_steps, db_path, last_idx)
            except (sqlite3.Error, OSError):
                # The store may not exist yet, or may be mid-write; retry.
                rows = []
            for idx, payload in rows:
                last_idx = max(last_idx, idx)
                if not payload:
                    continue
                match = _TOOL_ACTION_RE.search(payload)
                if not match:
                    continue
                # Each tool writes an intent step and an execution step
                # carrying the same label; only announce the change.
                action = truncate_status(match.group(1).decode(errors="replace"))
                if action and action != last_action:
                    last_action = action
                    yield StreamEvent(kind="status", text=action)
            await asyncio.sleep(_PROGRESS_POLL_SECS)
