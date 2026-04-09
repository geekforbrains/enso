"""Provider abstraction — one interface, many agents."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)


def truncate_status(text: str, limit: int = 60) -> str:
    """Extract a short status line from thinking/narration text."""
    # Take first line, strip markdown
    line = text.strip().split("\n")[0].strip("*_#> ")
    if len(line) > limit:
        return line[:limit] + "…"
    return line


@dataclass
class StreamEvent:
    """Unified event type emitted by all providers during streaming."""

    kind: Literal["status", "response", "session", "error", "usage"]
    text: str = ""
    session_id: str | None = None
    usage: dict | None = None


class BaseProvider(ABC):
    """Base class for CLI agent providers."""

    name: str

    def __init__(self, path: str):
        self.path = path

    @abstractmethod
    def build_command(
        self, prompt: str, model: str, session_id: str | None = None
    ) -> list[str]:
        """Build the CLI command for interactive streaming."""

    @abstractmethod
    def build_batch_command(self, prompt: str, model: str) -> list[str]:
        """Build the CLI command for batch execution (text output, no streaming).

        Used by the job runner to capture final output without parsing
        streaming events.
        """

    @abstractmethod
    def parse_event(self, event: dict) -> list[StreamEvent]:
        """Parse a raw JSON event into StreamEvents."""

    def parse_line(self, line: str) -> dict | None:
        """Parse a raw stdout line into a JSON dict. Returns None to skip."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None

    def stderr_to_stdout(self) -> bool:
        """If True, merge stderr into stdout."""
        return False

    def stdout_limit(self) -> int | None:
        """Buffer limit for stdout, or None for default."""
        return None

    def format_response(self, parts: list[str]) -> str:
        """Combine response parts into final text. Default: last part wins."""
        return parts[-1] if parts else ""

    def clear_session(self, session_id: str | None, working_dir: str) -> str:
        """Clear session data. Returns human-readable summary."""
        return "session cleared" if session_id else "no session"


PROVIDER_NAMES = ["claude", "codex", "gemini"]


def get_provider(name: str, path: str) -> BaseProvider:
    """Create a provider instance by name.

    Uses lazy imports to avoid circular dependencies — provider
    subclasses import from this module.
    """
    from .claude import ClaudeProvider
    from .codex import CodexProvider
    from .gemini import GeminiProvider

    classes: dict[str, type[BaseProvider]] = {
        "claude": ClaudeProvider,
        "codex": CodexProvider,
        "gemini": GeminiProvider,
    }
    cls = classes.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider: {name}")
    return cls(path)
