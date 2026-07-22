"""Provider abstraction — one interface, many agents."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Literal

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

    # Models offered when the provider has no configured model list.
    default_models: ClassVar[list[str]] = []
    # API-key env vars the provider's CLI needs (snapshotted into service envs).
    env_keys: ClassVar[tuple[str, ...]] = ()
    # Reasoning-effort levels the provider accepts, ordered least → most.
    # Empty means the provider has no effort control.
    effort_levels: ClassVar[list[str]] = []
    # Highest effort per model; unlisted models fall back to _default_max_effort.
    _model_max_effort: ClassVar[dict[str, str]] = {}
    _default_max_effort: ClassVar[str] = ""

    def __init__(self, path: str):
        self.path = path

    @classmethod
    def max_effort_for_model(cls, model: str) -> str:
        """Return the highest effort level the given model supports."""
        return cls._model_max_effort.get(model, cls._default_max_effort)

    @classmethod
    def supported_efforts(cls, model: str) -> list[str]:
        """Effort levels the given model supports, ordered least → most."""
        if not cls.effort_levels:
            return []
        cap = cls.max_effort_for_model(model)
        return cls.effort_levels[: cls.effort_levels.index(cap) + 1]

    @classmethod
    def clamp_effort(cls, effort: str, model: str) -> str:
        """Degrade ``effort`` to the highest level the model actually supports."""
        if effort not in cls.effort_levels:
            return effort
        supported = cls.supported_efforts(model)
        return effort if effort in supported else supported[-1]

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model: str,
        session_id: str | None = None,
        *,
        effort: str | None = None,
    ) -> list[str]:
        """Build the CLI command for interactive streaming.

        ``effort`` is an optional reasoning-effort level; providers that
        don't support it ignore the argument.
        """

    @abstractmethod
    def build_batch_command(
        self, prompt: str, model: str, *, effort: str | None = None,
    ) -> list[str]:
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
        """Buffer limit for stdout, or None for asyncio's 64 KiB default.

        Generous default so one long JSON event line can't overrun the
        stream buffer.
        """
        return 10 * 1024 * 1024

    def format_response(self, parts: list[str]) -> str:
        """Combine response parts into final text. Default: last part wins."""
        return parts[-1] if parts else ""

    def parse_batch_output(self, stdout: str) -> str:
        """Extract the final answer from a finished batch (job) run's stdout.

        Default: the batch command emits plain text, so return it stripped.
        Providers whose batch command streams JSON override this to pull the
        final response/error out of the event stream.
        """
        return stdout.strip()

    def clear_session(self, session_id: str | None, working_dir: str) -> str:
        """Clear session data. Returns human-readable summary."""
        return "session cleared" if session_id else "no session"


# Provider registry — the single source of truth for supported providers.
# Imported at the bottom so subclasses can import BaseProvider from here.
from .claude import ClaudeProvider  # noqa: E402
from .codex import CodexProvider  # noqa: E402

PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    ClaudeProvider.name: ClaudeProvider,
    CodexProvider.name: CodexProvider,
}
PROVIDER_NAMES = list(PROVIDER_CLASSES)


def provider_class(name: str) -> type[BaseProvider]:
    """Return the provider class for a name."""
    cls = PROVIDER_CLASSES.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider: {name}")
    return cls
