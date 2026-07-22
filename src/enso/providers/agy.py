"""Google Antigravity CLI provider."""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from typing import ClassVar

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


class AgyProvider(BaseProvider):
    """Adapter for Antigravity's plain-text headless mode."""

    name = "agy"
    streaming_output = False
    default_models: ClassVar[list[str]] = AGY_MODELS
    env_keys: ClassVar[tuple[str, ...]] = ()
    effort_levels: ClassVar[list[str]] = ["low", "medium", "high"]
    _default_max_effort = "high"

    def __init__(self, path: str):
        super().__init__(path)
        self._log_path: str | None = None

    def _create_log_file(self) -> str:
        fd, path = tempfile.mkstemp(prefix="enso-agy-", suffix=".log")
        os.close(fd)
        self._log_path = path
        return path

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
