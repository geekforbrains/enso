"""Durable heartbeat values shared by the CLI, scheduler, and read-only viewer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Agent

CLOSED_STATES = ("fulfilled", "cancelled", "expired")
STATES = ("active", "paused", *CLOSED_STATES)


class HeartbeatError(Exception):
    """An invalid definition or operation, with independent problems reported together."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else problems
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True, kw_only=True)
class Definition:
    """The current objective, authority, routing, and timing of one temporary situation."""

    title: str
    instructions: str
    completion: str
    allowed_actions: str
    workspace: str
    agent: Agent
    at: str | None = None
    schedule: str | None = None
    timezone: str = "UTC"
    gate: str | None = None
    notify: str | None = None
    notify_thread: str | None = None
    origin: dict[str, str] = field(default_factory=dict)
    timeout: int = 900
    gate_timeout: int = 120
    llm_checks: bool = False
    expires_at: str | None = None
    followup_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, kw_only=True)
class Beat(Definition):
    """A definition together with its durable scheduling and handling progress."""

    id: int
    state: str
    revision: int
    attention: bool
    at_consumed: bool
    checkpoint: dict[str, Any]
    handled_event_id: int
    next_check_at: str | None
    last_check_at: str | None
    last_check_status: str | None
    last_check_error: str | None
    last_success_at: str | None
    claim_run_id: str | None
    created_at: str
    updated_at: str
    closed_at: str | None

    @property
    def ref(self) -> str:
        return f"HB-{self.id:03d}"

    @property
    def closed(self) -> bool:
        return self.state in CLOSED_STATES

    @property
    def definition(self) -> Definition:
        return Definition(**{key: getattr(self, key) for key in Definition.__dataclass_fields__})

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ref": self.ref}


@dataclass(frozen=True)
class BeatEvent:
    """One meaningful observation, management change, or auditable action."""

    id: int
    beat_id: int
    kind: str
    actor: str
    run_id: str | None
    message: str
    payload: dict[str, Any]
    requires_handling: bool
    action_key: str | None
    action_status: str | None
    receipt: str | None
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ref": f"HB-{self.beat_id:03d}"}


@dataclass(frozen=True)
class BeatRun:
    """One provider attempt, with the definition and event cutoff it was allowed to handle."""

    id: str
    beat_id: int
    definition: Definition
    definition_revision: int
    input_cutoff: int
    trigger: str
    started_at: str
    ended_at: str | None
    status: str
    settlement: str | None
    duration_ms: int | None
    exit_code: int | None
    session_id: str | None
    output: str
    error: str

    @property
    def ref(self) -> str:
        return f"HB-{self.beat_id:03d}"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ref": self.ref}
