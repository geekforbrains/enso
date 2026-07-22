"""Google Antigravity CLI provider."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlparse

from . import BaseProvider, StreamEvent

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

# Antigravity's project catalog. Undocumented storage — if the format moves,
# lookups miss and fresh conversations fall back to --new-project, which only
# costs a duplicate project entry.
_PROJECTS_DIR = Path("~/.gemini/config/projects")


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
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high"]
    _default_max_effort = "high"

    def __init__(self, path: str, working_dir: str | None = None):
        super().__init__(path, working_dir)
        self._log_path: str | None = None

    def _create_log_file(self) -> str:
        fd, path = tempfile.mkstemp(prefix="enso-agy-", suffix=".log")
        os.close(fd)
        self._log_path = path
        return path

    def _project_args(self) -> list[str]:
        project_id = find_project_id(self.working_dir or os.getcwd())
        return ["--project", project_id] if project_id else ["--new-project"]

    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
    ) -> list[str]:
        cmd = [
            self.path,
            "--dangerously-skip-permissions",
            "--log-file", self._create_log_file(),
            "--model", model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        if session_id:
            cmd.extend(["--conversation", session_id])
        else:
            cmd.extend(self._project_args())
        cmd.extend(["--prompt", prompt])
        return cmd

    def build_batch_command(
        self, prompt: str, model: str, *, effort: str | None = None,
    ) -> list[str]:
        cmd = [
            self.path,
            "--dangerously-skip-permissions",
            "--model", model,
        ]
        if effort:
            cmd.extend(["--effort", effort])
        cmd.extend(self._project_args())
        cmd.extend(["--prompt", prompt])
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
