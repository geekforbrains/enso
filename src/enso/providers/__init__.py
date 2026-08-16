"""Provider abstraction — one interface, many agents."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

if TYPE_CHECKING:
    from ..instructions import InstructionBundle
    from ..policy import Launch

# Status text is shown in a chat bubble alongside a header, so it has to
# stay on one short line.
STATUS_TEXT_LIMIT = 80


def truncate_status(text: str, limit: int = STATUS_TEXT_LIMIT) -> str:
    """Collapse status text to a single line that fits a status message."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


@dataclass
class StreamEvent:
    """Unified event type emitted by all providers."""

    kind: Literal["response", "session", "error", "status"]
    text: str = ""
    session_id: str | None = None


class BaseProvider(ABC):
    """Base class for CLI agent providers."""

    name: str
    # True when stdout is a provider event stream; False when it is one final response.
    streaming_output: ClassVar[bool] = True

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

    def __init__(
        self,
        path: str,
        working_dir: str | None = None,
        *,
        timeout: int | float | None = None,
    ):
        self.path = path
        # Directory the runtime runs the provider process in (subprocess cwd).
        self.working_dir = working_dir
        # Outer Enso deadline for this invocation. Providers with their own
        # CLI watchdog can use this to keep Enso's timeout authoritative.
        self.timeout = timeout

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
        launch: Launch | None = None,
        instructions: InstructionBundle | None = None,
    ) -> list[str]:
        """Build the CLI command for interactive streaming.

        ``effort`` is an optional reasoning-effort level; providers that
        don't support it ignore the argument. ``launch`` selects the policy
        contract: None or an unrestricted launch keeps the bypass invocation,
        while a policy launch substitutes the provider's non-bypass flags
        (see docs/specs/permissions.md).
        """

    @abstractmethod
    def build_batch_command(
        self,
        prompt: str,
        model: str,
        *,
        effort: str | None = None,
        launch: Launch | None = None,
        instructions: InstructionBundle | None = None,
    ) -> list[str]:
        """Build the CLI command for batch execution (text output, no streaming).

        Used by the job runner to capture final output without parsing
        streaming events. ``launch`` behaves as in ``build_command``.
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
        """
        return stdout.strip()

    def parse_complete_output(self, stdout: str) -> list[StreamEvent]:
        """Parse one completed, non-streaming stdout payload."""
        text = stdout.strip()
        return [StreamEvent(kind="response", text=text)] if text else []

    def finalize_events(self) -> list[StreamEvent]:
        """Return metadata discovered outside stdout after a process finishes."""
        return []

    async def poll_progress(self) -> AsyncIterator[StreamEvent]:
        """Yield ``status`` events discovered outside stdout while running.

        The runtime drains this concurrently with the provider process, so
        providers whose stdout carries no progress (see ``streaming_output``)
        can still report activity. Progress is decorative: the runtime
        swallows whatever this raises and falls back to the elapsed timer,
        so implementations may read best-effort sources freely.
        """
        return
        yield  # pragma: no cover - makes this an async generator

    def clear_session(
        self,
        session_id: str | None,
        working_dir: str,
        *,
        policy_dir: str | None = None,
    ) -> str:
        """Clear session data. Returns human-readable summary.

        ``policy_dir`` names the bound policy's directory when the workspace
        runs restricted, for providers whose sessions live in staged homes.
        """
        return "session cleared" if session_id else "no session"

    def retryable_error(self, text: str) -> bool:
        """True when ``text`` is a transient failure worth one retry."""
        return False


# Provider registry — the single source of truth for supported providers.
# Imported at the bottom so subclasses can import BaseProvider from here.
from .agy import AgyProvider  # noqa: E402
from .claude import ClaudeProvider  # noqa: E402
from .codex import CodexProvider  # noqa: E402
from .grok import GrokProvider  # noqa: E402

PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    ClaudeProvider.name: ClaudeProvider,
    CodexProvider.name: CodexProvider,
    AgyProvider.name: AgyProvider,
    GrokProvider.name: GrokProvider,
}
PROVIDER_NAMES = list(PROVIDER_CLASSES)


def provider_class(name: str) -> type[BaseProvider]:
    """Return the provider class for a name."""
    cls = PROVIDER_CLASSES.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider: {name}")
    return cls
