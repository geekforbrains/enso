"""Antigravity CLI provider (Google's ``agy``).

The odd one out: Antigravity does not work in the process's current directory, it works in
the folder registered for a *project* in ``~/.gemini/config/projects/``. Launched without a
project it sees no ``AGENTS.md`` and none of the workspace's skills, so every launch has to
name one — ``--new-project`` the first time in a workspace, which registers the directory
it was started in, then ``--project <id>`` for good. That catalog is Antigravity's own and
Enso only ever reads it: an Enso-side copy of the mapping could go stale, and a stale id
is a turn that runs in someone else's directory.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlparse

from . import SESSION_UUID_RE, BaseProvider, StreamEvent, session_path, truncate_status

# Antigravity's own state. Unexpanded at module scope on purpose: expanding at use time
# keeps a test's monkeypatched HOME in effect and the operator's real ~/.gemini out of it.
PROJECTS_DIR = Path("~/.gemini/config/projects")
CONVERSATIONS_DIR = Path("~/.gemini/antigravity-cli/conversations")

# agy's own print watchdog defaults to 5m0s, well under Enso's turn and job timeouts, and
# it kills the turn from the inside. A fixed generous ceiling leaves Enso's deadline the
# only one that decides when a run has gone on too long.
PRINT_TIMEOUT = "24h"

# Tool arguments are PascalCase and the key depends on the tool; take the first that looks
# like a path so an unlisted tool still says something useful.
_PATH_KEYS = ("AbsolutePath", "TargetFile", "FilePath", "DirectoryPath", "SearchDirectory")


def _text(params: dict, *keys: str) -> str:
    """The first non-empty string among ``keys``."""
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def tool_status(tool_name: str, params: dict) -> str:
    """Describe an Antigravity tool call in one line."""
    name = os.path.basename(_text(params, *_PATH_KEYS).rstrip("/"))
    match tool_name:
        case "view_file" | "read_resource":
            return f"Reading {name or 'file'}"
        case "write_to_file":
            return f"Writing {name or 'file'}"
        case "replace_file_content" | "multi_replace_file_content" | "sed_file" | "notebook_edit":
            return f"Editing {name or 'file'}"
        case "list_dir":
            return f"Listing {name or 'directory'}"
        case "grep_search":
            return f"Searching for {truncate_status(_text(params, 'Query', 'Pattern'))}"
        case "find_by_name":
            return f"Finding {truncate_status(_text(params, 'Pattern', 'Query'))}"
        case "run_command" | "command_status":
            return truncate_status(f"Running {_text(params, 'CommandLine')}")
        case "search_web":
            return f"Searching: {truncate_status(_text(params, 'Query'))}"
        case "read_url_content" | "open_browser_url":
            return f"Fetching {truncate_status(_text(params, 'Url'), 40)}"
        case "invoke_subagent" | "browser_subagent" | "define_subagent":
            return "Running subagent"
        case _:
            return f"Using {truncate_status(tool_name, 40)}"


def _first_folder_uri(entry: dict) -> str | None:
    """A catalog entry's first folder URI, in either the plain or the nested git shape.

    Strictly the first *resource*, not the first one that happens to parse: a resource that
    is not a local folder ends the lookup instead of being skipped, because skipping it
    would promote the second folder into the first one's place and pin a turn to a
    directory the project does not work in.
    """
    resources = entry.get("projectResources")
    listed = resources.get("resources") if isinstance(resources, dict) else None
    if not isinstance(listed, list) or not listed:
        return None
    first = listed[0]
    if not isinstance(first, dict):
        return None
    git_folder = first.get("gitFolder")
    uri = first.get("folderUri") or (
        git_folder.get("folderUri") if isinstance(git_folder, dict) else None
    )
    return uri if isinstance(uri, str) and uri.startswith("file://") else None


def project_id(cwd: str) -> str | None:
    """The Antigravity project registered for ``cwd``, or None to register a new one.

    Only an entry whose *first* folder is ``cwd`` counts. A multi-root project works in its
    first folder, so matching any of them could pin a turn to a directory that is not the
    workspace — and every way of failing here (no catalog, unreadable entry, a project
    rooted elsewhere) has the same safe answer: no id, so the caller registers the
    directory it is actually starting in. Entries are read in filename order so duplicates
    for one directory resolve to the same id every time.
    """
    if not cwd:
        return None
    target = Path(cwd).resolve()
    try:
        entries = sorted(PROJECTS_DIR.expanduser().glob("*.json"))
    except OSError:
        return None
    for path in entries:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError, UnicodeDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        found = entry.get("id")
        uri = _first_folder_uri(entry)
        if not isinstance(found, str) or not found or not uri:
            continue
        if Path(unquote(urlparse(uri).path)).resolve() == target:
            return found
    return None


def embedded_effort(model: str) -> str | None:
    """The reasoning effort an Antigravity model id already carries, if any."""
    suffix = model.rsplit("-", 1)[-1]
    return suffix if suffix in AgyProvider.effort_levels else None


class AgyProvider(BaseProvider):
    """Print mode over ``stream-json``, pinned to a project, resumed by conversation id."""

    name = "agy"
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high"]
    default_max_effort = "high"
    # The live catalog is ``agy models``; these are the current generations, and config
    # accepts any id the CLI knows.
    models: ClassVar[list[str]] = [
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
    ]
    unattended_args: ClassVar[list[str]] = ["--dangerously-skip-permissions"]
    # agy mints the conversation id and announces it on the first line of the stream.
    assigns_session_id = False
    # That id is a UUID, and it is also the name of the conversation's store file.
    session_id_re: ClassVar[re.Pattern[str]] = SESSION_UUID_RE

    @classmethod
    def max_effort(cls, model: str) -> str:
        """An Antigravity model id names its own effort; that is the cap."""
        return embedded_effort(model) or cls.default_max_effort

    @classmethod
    def clamp_effort(cls, effort: str, model: str) -> str:
        """Report the model's embedded effort, whether above or below the request."""
        return embedded_effort(model) or super().clamp_effort(effort, model)

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
        # No --effort: every model id either carries its effort already (and the CLI
        # rejects the combination) or refuses the flag outright. The prompt rides attached
        # to its flag, so a prompt opening with a markdown bullet is not read as one.
        resuming = bool(session_id) and not batch
        cmd = [
            self.path,
            "--print-timeout",
            PRINT_TIMEOUT,
            "--output-format",
            "text" if batch else "stream-json",
            *args,
            "--model",
            model,
        ]
        found = project_id(cwd)
        if found:
            cmd.extend(["--project", found])
        elif not resuming:
            # First launch in this workspace: --new-project registers the directory the CLI
            # is started in, so the pin is correct by construction. On a resume the
            # conversation carries its own pin, and asking for another project here would
            # only litter the catalog on every turn.
            cmd.append("--new-project")
        if resuming:
            cmd.extend(["--conversation", str(session_id)])
        cmd.append(f"--prompt={prompt}")
        return cmd

    def parse_event(self, event: dict) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        match event.get("event", ""):
            case "init":
                # The first line of a turn. Recording the id here means a run that dies
                # halfway still leaves a conversation the next turn can resume.
                conversation = event.get("conversation_id")
                if isinstance(conversation, str) and conversation:
                    events.append(StreamEvent(kind="session", session_id=conversation))
            case "step_update":
                step = event.get("step_update") or {}
                events.extend(self._step_events(step) if isinstance(step, dict) else [])
            case "result":
                result = event.get("result") or {}
                events.extend(self._result_events(result) if isinstance(result, dict) else [])
        return events

    @staticmethod
    def _step_events(step: dict) -> list[StreamEvent]:
        """Status for a work step; the answer itself only arrives with the result."""
        match step.get("step_type"):
            case "tool":
                # A tool is reported ACTIVE then DONE, and sometimes only DONE. Repeating
                # the same status text costs nothing; missing one costs a stalled line.
                info = step.get("tool_info")
                params = info.get("parameters") if isinstance(info, dict) else None
                name = step.get("tool_name")
                return [
                    StreamEvent(
                        kind="status",
                        text=tool_status(
                            name if isinstance(name, str) else "",
                            params if isinstance(params, dict) else {},
                        ),
                    )
                ]
            case "thinking":
                return [StreamEvent(kind="status", text="Thinking")]
            case _:
                return []

    @staticmethod
    def _result_events(result: dict) -> list[StreamEvent]:
        """The turn's answer, or the reason there is none.

        Response text is taken here rather than from the ``agent_response`` deltas: those
        are fragments, and Enso keeps the last part it is given, not their concatenation.
        """
        status = result.get("status")
        text = result.get("response")
        error = result.get("error")
        if isinstance(status, str) and status and status != "SUCCESS":
            for candidate in (error, text, status):
                if isinstance(candidate, str) and candidate.strip():
                    return [StreamEvent(kind="error", text=candidate)]
            return [StreamEvent(kind="error", text="unknown error")]
        if isinstance(text, str) and text.strip():
            return [StreamEvent(kind="response", text=text)]
        return []

    def clear_session(self, session_id: str, cwd: str) -> str:
        """Delete the conversation's store; ``cwd`` is unused, agy keeps them all together.

        Antigravity's ``conversation_summaries.db`` index is left alone: Enso removes the
        data its own turns created and does not edit another tool's index.
        """
        self.check_session_id(session_id)
        store = session_path(CONVERSATIONS_DIR, f"{session_id}.db")
        existed = store.is_file()
        for path in (store, *(store.with_name(store.name + tail) for tail in ("-wal", "-shm"))):
            if path.is_file():
                path.unlink()
        if existed:
            return f"deleted session {session_id[:8]}"
        return f"session {session_id[:8]} (no file found)"
