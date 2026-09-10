"""Provider adapters: one interface over the supported agent CLIs."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

# Status text sits on one line of a chat status message.
STATUS_TEXT_LIMIT = 80

# A session id is provider output, and three of the CLIs name a file or directory after it,
# so it is validated before Enso stores it, resumes it, or hands it to a deletion. The
# shared contract is one opaque token: non-empty, bounded, and free of path separators,
# control characters, and a leading dot or dash. Nothing it accepts can name a parent
# directory, an absolute path, or a command-line option.
SESSION_ID_MAX = 128
SESSION_ID_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Claude, Grok and Antigravity identify a session by UUID: Enso mints the Claude and Grok
# ones itself and passes them as ``--session-id``, and agy announces its conversation id.
SESSION_UUID_RE: re.Pattern[str] = re.compile(
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)


class SessionIdError(ValueError):
    """A session id falls outside the identifier contract of the provider that owns it."""


def truncate_status(text: str, limit: int = STATUS_TEXT_LIMIT) -> str:
    """Collapse status text to a single line that fits a status message."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def session_path(root: Path, name: str) -> Path:
    """Resolve ``name`` under ``root``, proving the result stays inside that directory.

    The second line of defence behind session-id validation. A row written by an older
    version, or edited by hand, must not be able to point a deletion at anything outside the
    provider's own session store, whatever that row happens to contain.
    """
    base = root.expanduser().resolve()
    candidate = (base / name).resolve()
    if candidate == base or base not in candidate.parents:
        raise SessionIdError(f"session path escapes {base}: {name[:60]!r}")
    return candidate


@dataclass
class StreamEvent:
    """Unified event emitted by every provider's stdout parser."""

    kind: Literal["response", "session", "error", "status"]
    text: str = ""
    session_id: str | None = None


class BaseProvider(ABC):
    """Base class for CLI agent providers."""

    name: ClassVar[str]
    # Reasoning-effort levels the CLI accepts, least to most.
    effort_levels: ClassVar[list[str]] = []
    # Highest effort per model; unlisted models fall back to default_max_effort.
    model_max_effort: ClassVar[dict[str, str]] = {}
    default_max_effort: ClassVar[str] = ""
    # What ``enso setup`` writes when the operator has not
    # chosen: the model aliases the CLI ships with, and the flag that lets it run
    # unattended with full access.
    models: ClassVar[list[str]] = []
    unattended_args: ClassVar[list[str]] = []
    # True when Enso assigns the session id up front (--session-id); False when
    # the CLI announces its own id in the event stream.
    assigns_session_id: ClassVar[bool] = True
    # The shape of the session ids this CLI works with. Every provider states its own.
    session_id_re: ClassVar[re.Pattern[str]] = SESSION_ID_RE

    def __init__(self, path: str):
        self.path = path

    @classmethod
    def valid_session_id(cls, session_id: str) -> bool:
        """True when ``session_id`` is one this provider's CLI could really have produced."""
        return (
            0 < len(session_id) <= SESSION_ID_MAX
            and cls.session_id_re.fullmatch(session_id) is not None
        )

    @classmethod
    def check_session_id(cls, session_id: str) -> str:
        """Return ``session_id`` unchanged, or raise SessionIdError if it breaks the contract."""
        if not cls.valid_session_id(session_id):
            raise SessionIdError(f"invalid {cls.name} session id: {session_id[:60]!r}")
        return session_id

    @classmethod
    def max_effort(cls, model: str) -> str:
        return cls.model_max_effort.get(model, cls.default_max_effort)

    @classmethod
    def clamp_effort(cls, effort: str, model: str) -> str:
        """Degrade ``effort`` to the highest level the model supports."""
        levels = cls.effort_levels
        if effort not in levels:
            return effort
        cap = levels.index(cls.max_effort(model))
        return effort if levels.index(effort) <= cap else levels[cap]

    @abstractmethod
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
        """Build the CLI command; ``args`` are the operator's permission flags, verbatim.

        ``cwd`` is the workspace the CLI will be started in. Only a provider that does not
        take the process cwd as its root needs it: Antigravity works in a registered project
        and has to name the right one, and OpenCode prefers an inherited ``$PWD`` unless the
        root is stated explicitly.
        """

    def parse_line(self, line: str) -> dict | None:
        """Parse one stdout line into a raw event, or None to skip it."""
        stripped = line.strip()
        if not stripped.startswith("{"):
            return None
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None

    @abstractmethod
    def parse_event(self, event: dict) -> list[StreamEvent]:
        """Turn one raw event into zero or more StreamEvents."""

    def stderr_to_stdout(self) -> bool:
        return False

    def format_response(self, parts: list[str]) -> str:
        """Combine response parts into the final text; the last part wins."""
        return parts[-1] if parts else ""

    def format_batch_output(self, stdout: str) -> str:
        """Return a batch process's user-facing output."""
        return stdout.strip()

    def clear_session(self, session_id: str, cwd: str) -> str:
        """Delete local session data; returns a one-line summary."""
        return f"forgot session {session_id[:8]}"

    def retryable_error(self, text: str) -> bool:
        """True when ``text`` is a transient failure worth one retry."""
        return False


# Imported last so subclasses can import BaseProvider from this module.
from .agy import AgyProvider  # noqa: E402
from .claude import ClaudeProvider  # noqa: E402
from .codex import CodexProvider  # noqa: E402
from .grok import GrokProvider  # noqa: E402
from .opencode import OpenCodeProvider  # noqa: E402

# Order is the order ``enso setup`` offers them in, so claude stays the default.
PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    ClaudeProvider.name: ClaudeProvider,
    CodexProvider.name: CodexProvider,
    GrokProvider.name: GrokProvider,
    AgyProvider.name: AgyProvider,
    OpenCodeProvider.name: OpenCodeProvider,
}


def provider_class(name: str) -> type[BaseProvider]:
    cls = PROVIDER_CLASSES.get(name)
    if cls is None:
        raise ValueError(f"Unknown provider: {name}")
    return cls


def stored_session_id_ok(provider: str, session_id: str) -> bool:
    """True when a stored row's id still matches its provider's contract.

    A name Enso no longer knows has no contract to check against, so its row is left for
    whatever does know it rather than being judged here.
    """
    cls = PROVIDER_CLASSES.get(provider)
    return cls is None or cls.valid_session_id(session_id)


def make_provider(name: str, path: str) -> BaseProvider:
    """Instantiate the named provider bound to its CLI path."""
    return provider_class(name)(path)
