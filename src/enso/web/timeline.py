"""The task timeline: a task's events as stage visits, each row saying who acted.

``enso.tasks`` records the events, and ``enso.workflows`` keeps the transactions and
lifecycle deliveries that judged them. This module only arranges what they recorded, oldest
first: one section per stay in a stage, one row per thing that happened, and the checks,
repairs, and Enso's decision under the handoff they judged. A section header names the job
and model that worked the stage, so its rows name a source only when it is someone else.
See docs/web.md#tasks.
"""

from __future__ import annotations

import re
from collections.abc import Container, Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import pairwise
from typing import Any

from markupsafe import Markup

from .. import formatting, tasks
from ..config import ProjectConfig, Stage
from ..transport_registry import TRANSPORTS
from . import files
from .filters import clock, parse_time

# A folded row shows one line of its message (two on a phone); the rest is never drawn.
PREVIEW_CHARS = 200
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_ACTIVE = ("working", "submitted", "checking", "repairing")
# A check's or hook's recorded status as the tone of its icon and tag.
_RESULT_TONES = {
    "passed": "ok",
    "delivered": "ok",
    "running": "running",
    "checking": "running",
    "failed": "error",
    "error": "error",
    "timeout": "warning",
    "interrupted": "warning",
    "cancelled": "warning",
}
_MOVE_TITLES = {"advance": "Advanced", "return": "Sent back", "resume": "Resumed"}
_RELEASE_TITLES = {"run_ended": "Run ended without a handoff", "deferred": "Deferred"}
# Events whose title and tone never depend on their payload; ``None`` previews the message.
_TITLES: dict[str, tuple[str, str, str | None]] = {
    "created": ("Task created", "muted", None),
    "taken": ("Starting work", "running", ""),
    "rules_approved": ("Rules approved", "ok", None),
    "workflow_reset": ("Budget reset", "muted", None),
    "workflow_recovered": ("Verification recovered", "warning", None),
    "accepted": ("Accepted", "ok", None),
    "worktree_adopted": ("Worktree adopted", "muted", None),
    "worktree_cleanup": ("Worktree cleanup needs attention", "warning", None),
}


# One fact in an opened row: its label, its value, and whether the value is a literal.
Fact = tuple[str, str, bool]


@dataclass(frozen=True)
class Step:
    """One step under a handoff: a check, a repair, a landing, or Enso's decision.

    A check opens to ``output``, its complete recorded output with terminal colour codes
    removed, under ``facts`` such as its exit code and attempt.
    """

    source: str
    tone: str
    title: str
    who: str = ""
    detail: str = ""
    tag: tuple[str, str] | None = None
    value: str = ""
    output: str = ""
    facts: tuple[Fact, ...] = ()


@dataclass(frozen=True)
class Entry:
    """One row: what happened, and, when the section header does not already say, who.

    The opened row shows ``message`` as rendered Markdown, or ``text`` as recorded when it
    is terminal output or too large to render, and the raw actor and run it came from. A
    handoff also opens to why Enso stopped it, if it did, and to its transaction's evidence.
    """

    at: str
    source: str  # person, agent, enso, or script
    tone: str
    title: str
    who: str = ""
    preview: str = ""
    text: str = ""
    message: Markup | None = None
    tag: tuple[str, str] | None = None
    value: str = ""
    actor: str = ""
    run_id: str | None = None
    run: str | None = None  # ``live``, ``pruned``, or ``operator``
    record: tuple[str, str] | None = None  # an event the row came from
    steps: tuple[Step, ...] = ()
    reason: str = ""
    facts: tuple[Fact, ...] = ()
    transaction: str = ""  # the full id, so a link can reach the handoff that holds it


@dataclass(frozen=True)
class Section:
    """One stay in a stage: where, since when, for how long, and which job worked it."""

    stage: str
    source: str
    at: str
    duration: str
    job: str
    entries: tuple[Entry, ...]


@dataclass
class _Handoff:
    """A submitted handoff while the stream is read; it becomes one row with its steps."""

    first: tasks.TaskEvent
    tx: dict[str, Any]
    resubmits: list[tasks.TaskEvent] = field(default_factory=list)
    accepted: tasks.TaskEvent | None = None
    blocked: tasks.TaskEvent | None = None


@dataclass
class _Visit:
    stage: str
    at: str
    items: list[Entry | _Handoff] = field(default_factory=list)
    takes: list[tasks.TaskEvent] = field(default_factory=list)


def operator_verification(run_id: str | None) -> bool:
    """Manual checks have an execution ID but never create a provider run row."""
    return bool(run_id and re.fullmatch(r"manual-[0-9a-f]{32}", run_id))


def run_kind(run_id: str | None, live: Container[str]) -> str | None:
    if run_id is None:
        return None
    if operator_verification(run_id):
        return "operator"
    return "live" if run_id in live else "pruned"


def _person(actor: str) -> bool:
    kind, _, identity = actor.partition(":")
    return kind == "user" or (kind in TRANSPORTS and bool(identity))


def _enso(actor: str) -> bool:
    return actor == tasks.ENSO_ACTOR or actor.startswith(f"{tasks.ENSO_ACTOR}:")


def who(actor: str, payload: Mapping[str, Any], names: Mapping[str, str]) -> str:
    """A person by transport and name, a job by name; Enso itself needs no label."""
    kind, _, identity = actor.partition(":")
    if kind == "job":
        return "Job · " + identity.rpartition(":")[2]
    if kind == "user":
        return "" if identity == "verify" else f"Terminal · {identity}"
    if kind in TRANSPORTS and identity:
        name = str(payload.get("actor_name") or "") or names.get(actor) or identity
        return f"{kind.capitalize()} · {name}"
    return "" if _enso(actor) else actor


def _seconds(duration_ms: Any) -> str:
    if not isinstance(duration_ms, int):
        return ""
    if duration_ms < 60_000:
        return f"{duration_ms / 1000:.1f}s"
    return formatting.format_elapsed(duration_ms // 1000)


def _span(start: str, end: str | datetime | None) -> str:
    begun, ended = parse_time(start), parse_time(end)
    if begun is None or ended is None:
        return ""
    return formatting.format_elapsed(max(0, int((ended - begun).total_seconds())))


def _time(stamp: Any) -> str:
    moment = parse_time(stamp)
    return moment.astimezone().strftime("%H:%M:%S") if moment else ""


def _commit(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", str(value or "")))


def _lines(text: str) -> list[str]:
    return [line.strip() for line in _ANSI.sub("", text or "").splitlines() if line.strip()]


def _short(value: Any) -> str:
    text = str(value or "")
    return text[:7] if _COMMIT.fullmatch(text) else text


def _preview(text: str) -> str:
    return formatting.preview(files.plain_text(text), PREVIEW_CHARS) if text else ""


def _settings(execution: Mapping[str, Any]) -> str:
    """``claude opus · xhigh effort`` for a model run; runs without one say so."""
    kind = execution.get("kind")
    if kind in ("integration", "command"):
        return "no model"
    if kind != "agent":
        return ""
    model = " ".join(str(execution[key]) for key in ("provider", "model") if execution.get(key))
    effort = f"{execution['effort']} effort" if execution.get("effort") else ""
    return " · ".join(part for part in (model, effort) if part)


def _job_source(execution: Mapping[str, Any], stage: Stage | None) -> str:
    kind = execution.get("kind")
    if kind == "agent":
        return "agent"
    if kind == "integration":
        return "enso"
    if kind == "command":
        return "script"
    # The run row was pruned and the event predates recorded executions: go by the stage.
    if stage is not None and stage.integrate:
        return "enso"
    if stage is not None and stage.command:
        return "script"
    return "agent"


class _Reader:
    """The state shared while one task's history is arranged."""

    def __init__(
        self,
        transactions: list[dict[str, Any]],
        runs: Mapping[str, Mapping[str, Any]],
        project: ProjectConfig | None,
        names: Mapping[str, str],
        base: str | None,
    ) -> None:
        self.transactions = {tx["id"]: tx for tx in transactions if tx.get("id")}
        self.runs = runs
        self.project = project
        self.names = names
        self.base = base
        self.executions: dict[str, Mapping[str, Any]] = {}

    def execution(self, run_id: str | None) -> Mapping[str, Any]:
        if not run_id:
            return {}
        return self.runs.get(run_id) or self.executions.get(run_id) or {}

    def stage(self, name: str | None) -> Stage | None:
        return self.project.stage(name) if self.project and name else None

    def source(self, event: tasks.TaskEvent, stage: str | None) -> str:
        if _enso(event.actor):
            return "enso"
        if event.actor.startswith("job:"):
            return _job_source(self.execution(event.run_id), self.stage(stage))
        return "person"

    def who(self, event: tasks.TaskEvent) -> str:
        label = who(event.actor, event.payload, self.names)
        if operator_verification(event.run_id):
            return f"{label} · verify" if label else "Operator verification"
        return label

    def entry(self, event: tasks.TaskEvent, stage: str) -> Entry:
        title, tone, preview = self._describe(event)
        text = event.message
        tag = (
            ("warning", "needs attention") if event.kind == "noted" and tone == "warning" else None
        )
        message = files.render_output(text) if text else None
        if event.kind == "ref":
            value = str(event.payload.get("value", ""))
            message = Markup("<p><code>{}</code></p>").format(value)
        return Entry(
            at=event.created_at,
            source=self.source(event, event.from_stage or stage),
            tone=tone,
            title=title,
            who=self.who(event),
            preview=_preview(text) if preview is None else preview,
            text=text,
            message=message,
            tag=tag,
            actor=event.actor,
            run_id=event.run_id,
            run=run_kind(event.run_id, self.runs),
        )

    def _describe(self, event: tasks.TaskEvent) -> tuple[str, str, str | None]:
        """Title, tone, and a preview when the message is not the preview itself."""
        payload = event.payload
        match event.kind:
            case "noted":
                return "Added note", ("warning" if payload.get("attention") else "muted"), None
            case "ref":
                kind, value = str(payload.get("kind", "")), str(payload.get("value", ""))
                if kind == "commit":
                    return f"Commit {_short(value)}", "muted", ""
                return kind.capitalize() or "Ref", "muted", value
            case "released":
                reason = str(payload.get("reason") or "")
                title = _RELEASE_TITLES.get(reason, "Released")
                return title, ("warning" if reason == "run_ended" else "muted"), None
            case "moved":
                return self._move(event)
            case "edited":
                changed = ", ".join(
                    {"after_ref": "dependency"}.get(key, key) for key in sorted(payload)
                )
                return "Spec edited", "muted", f"Changed {changed}" if changed else ""
            case "worktree_removed":
                return (
                    "Worktree removed",
                    "muted",
                    event.message.removeprefix("Removed clean worktree "),
                )
        return _TITLES.get(event.kind, (event.kind.replace("_", " ").capitalize(), "muted", None))

    def _move(self, event: tasks.TaskEvent) -> tuple[str, str, str | None]:
        move = str(event.payload.get("move") or "")
        if move == "block":
            return "Blocked", "warning", None
        if move == "drop":
            return "Cancelled", "muted", None
        verb = _MOVE_TITLES.get(move, move.capitalize() or "Moved")
        return f"{verb} → {event.to_stage}", "ok", None

    def worktree(self, event: tasks.TaskEvent) -> Entry:
        """Setup and teardown run the project's own commands; everything else is Enso's."""
        payload = event.payload
        passed = payload.get("status") == "passed"
        command = None
        if self.project is not None:
            command = (
                self.project.setup
                if event.kind == "worktree_setup"
                else self.project.hooks.get("teardown")
            )
        output = str(payload.get("output") or "")
        if event.kind == "worktree_setup":
            title = "Worktree ready" if passed else "Worktree setup failed"
            copy = payload.get("copy") or {}
            copied = int(copy.get("cloned", 0)) + int(copy.get("copied", 0))
            preview = f"Copied {copied} file{'s' if copied != 1 else ''}" if copy else ""
            if copy.get("skipped"):
                preview += f", skipped {copy['skipped']}"
            text = event.message + (f"\n\n{output.rstrip()}" if output.strip() else "")
        else:
            title, preview, text = "Teardown", "", output.rstrip()
        tone = "ok" if passed else "error"
        return Entry(
            at=event.created_at,
            source="script" if command else "enso",
            tone=tone,
            title=title,
            who=command or "",
            preview=preview if passed else _preview(event.message),
            text=_ANSI.sub("", text),
            tag=(tone, "passed" if passed else "failed") if command else None,
            actor=event.actor,
        )

    def hook(self, event: dict[str, Any]) -> Entry:
        deliveries = event.get("deliveries") or []
        last = deliveries[-1] if deliveries else event  # the event keeps its last result too
        status = str(event.get("status") or "pending")
        tone = _RESULT_TONES.get(status, "muted")
        word = {"delivered": "passed"}.get(status, status)
        lines = _lines(str(last.get("error") or last.get("output") or ""))
        attempts = []
        for number, item in enumerate(deliveries, 1):
            said = _ANSI.sub("", str(item.get("error") or item.get("output") or "")).rstrip()
            attempts.append(
                f"Attempt {item.get('attempt', number)}: {item.get('status', '')}, "
                f"exit {item.get('exit_code')}, {_seconds(item.get('duration_ms')) or '-'}"
                + (f"\n{said}" if said else "")
            )
        text = "\n\n".join(attempts)
        return Entry(
            at=str(last.get("at") or event.get("created_at") or ""),
            source="script",
            tone=tone,
            title=f"{event.get('name', 'lifecycle')} hook",
            who=str(event.get("command") or ""),
            preview=lines[0] if lines else "",
            text=text or "Waiting for delivery.",
            tag=(tone, word),
            value=_seconds(last.get("duration_ms")),
            record=("Event", str(event.get("event_id", ""))[:8]),
        )

    def handoff(self, handoff: _Handoff) -> Entry:
        first, tx = handoff.first, handoff.tx
        definition = tx.get("stage_definition") or {}
        to = first.to_stage or tx.get("to_stage") or ""
        if definition.get("integrate"):
            title = f"Integration → {to}"
        elif first.payload.get("move") == "return":
            title = f"Sent back → {to}"
        else:
            title = f"Handoff → {to}"
        status = tx.get("status")
        if handoff.accepted is not None or status == "accepted":
            tone = "ok"
        elif handoff.blocked is not None or status == "blocked":
            tone = "warning"
        else:
            tone = "running" if status in _ACTIVE else "muted"
        reason = handoff.blocked.message if handoff.blocked is not None else ""
        if not reason and status == "blocked":
            reason = str(tx.get("error") or "")
        # Enso writes an integration's message itself; its steps say what happened.
        message = "" if definition.get("integrate") else first.message
        return Entry(
            at=first.created_at,
            source=self.source(first, first.from_stage),
            tone=tone,
            title=title,
            who=self.who(first),
            preview=_preview(message),
            text=message,
            message=files.render_output(message) if message else None,
            actor=first.actor,
            run_id=first.run_id,
            run=run_kind(first.run_id, self.runs),
            steps=tuple(self._steps(handoff)),
            reason=reason,
            facts=_evidence(handoff),
            transaction=str(tx.get("id") or ""),
        )

    def _steps(self, handoff: _Handoff) -> Iterator[Step]:
        """Each submission's checks, the repairs between them, then how Enso decided."""
        yield from self._attempts(handoff)
        yield from self._landing(handoff)
        yield from self._decision(handoff)

    def _attempts(self, handoff: _Handoff) -> Iterator[Step]:
        tx = handoff.tx
        definition = tx.get("stage_definition") or {}
        commands = {
            check.get("name"): str(check.get("command") or "")
            for check in definition.get("checks") or []
        }
        checks = tx.get("checks") or []
        submissions = [handoff.first, *handoff.resubmits]
        for number, submission in enumerate(submissions, 1):
            if number > 1:
                yield Step(
                    self.source(submission, submission.from_stage),
                    "running",
                    "Repair submitted",
                    who=self.who(submission) if submission.actor != handoff.first.actor else "",
                    detail=_preview(submission.message),
                    value=clock(submission.created_at),
                )
            ran = [check for check in checks if check.get("attempt", 1) == number]
            yield from (_check(check, commands) for check in ran)
            if number < len(submissions):
                yield Step(
                    "enso",
                    "warning",
                    f"Repair {number} of {tx.get('max_repairs', number)}",
                    detail="Sent the failure back to the run",
                )
            elif tx.get("status") in ("submitted", "checking"):
                names = {check.get("name") for check in ran}
                for name, command in commands.items():
                    if name not in names:
                        yield Step(
                            "script",
                            "muted",
                            f"{name} check",
                            who=command,
                            tag=("muted", "not yet run"),
                        )

    def _landing(self, handoff: _Handoff) -> Iterator[Step]:
        integration = handoff.tx.get("integration") or {}
        if not integration:
            return
        landed = f"{_short(integration.get('target_sha'))} → {_short(integration.get('candidate'))}"
        if integration.get("phase") == "applied":
            where = f"Landed on {self.base}" if self.base else "Landed"
            yield Step(
                "enso", "ok", where, detail=landed, value=clock(integration.get("applied_at"))
            )
        else:
            yield Step("enso", "warning", "Landing started", detail=landed)
        if handoff.accepted is None and handoff.tx.get("status") not in _ACTIVE:
            # Git already moved; recovery must recheck the landed work before accepting it.
            yield Step(
                "enso",
                "warning",
                "Not accepted",
                detail="Git landed, but Enso stopped before accepting. Recovery rechecks "
                "the landed work against the target before accepting it.",
            )

    def _decision(self, handoff: _Handoff) -> Iterator[Step]:
        tx = handoff.tx
        status = tx.get("status")
        attempts = 1 + len(handoff.resubmits)
        last = [check for check in tx.get("checks") or [] if check.get("attempt", 1) == attempts]
        if handoff.accepted is not None:
            yield Step(
                "enso",
                "ok",
                "Accepted",
                detail=self._verdict(tx, last),
                value=clock(handoff.accepted.created_at),
            )
        elif status == "accepted":  # accepted before its event was read
            yield Step("enso", "ok", "Accepted", detail=self._verdict(tx, last))
        elif tx.get("integration") and status not in _ACTIVE:
            return  # the landing's own warning says what is left to do
        elif handoff.blocked is not None:
            yield Step(
                "enso",
                "warning",
                "Blocked the handoff",
                detail=" ".join(handoff.blocked.message.split()),
                value=clock(handoff.blocked.created_at),
            )
        elif status == "blocked":
            yield Step(
                "enso", "warning", "Blocked", detail=" ".join(str(tx.get("error") or "").split())
            )
        elif status == "submitted":
            yield Step(
                "enso", "running", "Waiting for the run to stop", detail="Checks start after it"
            )
        elif status == "repairing":
            yield Step(
                "enso",
                "warning",
                f"Repair {attempts} of {tx.get('max_repairs', attempts)}",
                detail="Waiting for a new handoff",
            )
        elif status == "checking" and not any(check.get("status") == "running" for check in last):
            yield Step("enso", "running", "Checking")

    @staticmethod
    def _verdict(tx: dict[str, Any], checks: list[dict[str, Any]]) -> str:
        if checks:
            passed = sum(1 for check in checks if check.get("status") == "passed")
            candidate = str(tx.get("candidate") or "")
            on = f" on {candidate[:7]}" if re.fullmatch(r"[0-9a-f]{40}", candidate) else ""
            return f"{passed} of {len(checks)} checks passed{on}"
        if not (tx.get("stage_definition") or {}).get("checks"):
            return "No checks configured"
        return "Returns run no checks" if tx.get("move") == "return" else "No checks ran"


def _check(check: dict[str, Any], commands: Mapping[Any, str]) -> Step:
    status = str(check.get("status") or "pending")
    tone = _RESULT_TONES.get(status, "muted")
    if status == "passed":
        output = _lines(str(check.get("output") or ""))
        detail = output[0] if len(output) == 1 else ""
    else:
        lines = _lines(str(check.get("error") or "")) or _lines(str(check.get("output") or ""))[-1:]
        detail = lines[0] if lines else ""
    recorded = "\n".join(
        _ANSI.sub("", str(check.get(key) or "")).rstrip() for key in ("error", "output")
    ).strip("\n")
    took = "" if status == "running" else _seconds(check.get("duration_ms"))
    code = check.get("exit_code")
    facts: list[Fact] = [("Exit", "-" if code is None else str(code), True)]
    facts.append(("Attempt", str(check.get("attempt", 1)), False))
    if took:
        facts.append(("Took", took, False))
    return Step(
        "script",
        tone,
        f"{check.get('name', 'check')} check",
        who=commands.get(check.get("name"), ""),
        detail=detail,
        tag=(tone, status),
        value=took,
        output=recorded or ("Running…" if status == "running" else "No output recorded."),
        facts=tuple(facts),
    )


def _evidence(handoff: _Handoff) -> tuple[Fact, ...]:
    """What Workflow history used to hold: the transaction's inputs, budget, and span.

    The candidate is left out when Enso's accepting step already names it.
    """
    tx = handoff.tx
    if not tx:
        return ()
    attempts = 1 + len(handoff.resubmits)
    checked = any(check.get("attempt", 1) == attempts for check in tx.get("checks") or [])
    facts: list[Fact] = []
    candidate = tx.get("candidate")
    if _commit(candidate) and not (checked and handoff.accepted is not None):
        facts.append(("Candidate", str(candidate)[:7], True))
    if "max_repairs" in tx:
        facts.append(("Repairs", f"{tx.get('repairs', 0)} of {tx['max_repairs']}", False))
    for label, key in (("Spec", "spec_hash"), ("Workflow", "workflow_hash")):
        if tx.get(key):
            facts.append((label, str(tx[key])[:7], True))
    if tx.get("started_at"):
        ended = _time(tx.get("ended_at")) or "in progress"
        facts.append(("Ran", f"{_time(tx['started_at'])} → {ended}", False))
    if tx.get("recovery_of"):
        facts.append(("Recovers", str(tx["recovery_of"])[:8], True))
    facts.append(("Transaction", str(tx.get("id", ""))[:8], True))
    return tuple(facts)


def _folded_moves(events: list[tasks.TaskEvent]) -> set[int]:
    """Moves recorded with an acceptance: the handoff row already shows them."""
    return {
        before.id
        for before, event in pairwise(events)
        if event.kind == "accepted" and before.kind == "moved" and before.run_id == event.run_id
    }


class _Stream:
    """Reads events in order, opening a visit at each move and folding each handoff."""

    def __init__(self, reader: _Reader, events: list[tasks.TaskEvent]) -> None:
        self.reader = reader
        self.folded = _folded_moves(events)
        self.visits: list[_Visit] = []
        self.by_transaction: dict[str, _Handoff] = {}
        self.by_run: dict[str, _Handoff] = {}

    def visit(self, stage: str | None, at: str) -> _Visit:
        if not self.visits:
            self.visits.append(_Visit(stage or "", at))
        return self.visits[-1]

    def hook(self, hook: dict[str, Any], at: str) -> None:
        self.visit(hook.get("to_stage"), at).items.append(self.reader.hook(hook))

    def add(self, event: tasks.TaskEvent) -> None:
        current = self.visit(
            event.to_stage if event.kind == "created" else event.from_stage, event.created_at
        )
        key = str(event.payload.get("transaction_id") or "")
        if event.kind == "moved":
            self._move(event, current, key)
        elif event.kind == "submitted":
            existing = self.by_transaction.get(key)
            if existing is not None and existing.accepted is None and existing.blocked is None:
                existing.resubmits.append(event)
                return
            handoff = _Handoff(event, self.reader.transactions.get(key) or {})
            self.by_transaction[key] = handoff
            if event.run_id:
                self.by_run[event.run_id] = handoff
            current.items.append(handoff)
        elif event.kind == "accepted" and key in self.by_transaction:
            self.by_transaction[key].accepted = event
        elif event.kind in ("worktree_setup", "worktree_teardown"):
            current.items.append(self.reader.worktree(event))
        else:
            if event.kind == "taken":
                current.takes.append(event)
                execution = event.payload.get("execution")
                if event.run_id and isinstance(execution, dict):
                    self.reader.executions[event.run_id] = execution
            current.items.append(self.reader.entry(event, current.stage))

    def _move(self, event: tasks.TaskEvent, current: _Visit, key: str) -> None:
        """A move closes the visit; Enso's block of a pending handoff is that handoff's step."""
        handoff = self.by_transaction.get(key) or self.by_run.get(event.run_id or "")
        if event.id in self.folded:
            pass
        elif (
            _enso(event.actor)
            and event.payload.get("move") == "block"
            and handoff is not None
            and handoff.accepted is None
            and handoff.blocked is None
        ):
            handoff.blocked = event
        else:
            current.items.append(self.reader.entry(event, current.stage))
        self.visits.append(_Visit(event.to_stage or "", event.created_at))


def build(
    history: list[tasks.TaskEvent],
    transactions: list[dict[str, Any]],
    lifecycle: list[dict[str, Any]],
    live: Mapping[str, Mapping[str, Any]],
    project: ProjectConfig | None,
    names: Mapping[str, str],
    *,
    base: str | None,
    finished: bool,
    now: datetime,
) -> list[Section]:
    """The task's history as stage visits, oldest first.

    ``live`` maps each retained run to its execution (kind, provider, model, effort).
    Lifecycle hooks are placed by when they were queued, which is just after their move.
    """
    reader = _Reader(transactions, live, project, names, base)
    events = sorted(history, key=lambda event: event.id)
    # A delivery with no queue time has no place in the order; it goes after everything.
    hooks = sorted(
        (hook for hook in lifecycle if hook.get("created_at")), key=lambda hook: hook["created_at"]
    )
    hooks += [hook for hook in lifecycle if not hook.get("created_at")]
    stream = _Stream(reader, events)
    for event in events:
        while hooks and hooks[0].get("created_at") and hooks[0]["created_at"] < event.created_at:
            stream.hook(hooks.pop(0), event.created_at)
        stream.add(event)
    for hook in hooks:
        stream.hook(hook, str(hook.get("created_at") or ""))
    visits = stream.visits
    sections = []
    for index, stay in enumerate(visits):
        ended: str | datetime | None = (
            visits[index + 1].at if index + 1 < len(visits) else None if finished else now
        )
        sections.append(_section(reader, stay, _span(stay.at, ended)))
    return sections


def _job_label(actor: str, execution: Mapping[str, Any]) -> str:
    return " · ".join(part for part in (actor.rpartition(":")[2], _settings(execution)) if part)


def _section(reader: _Reader, stay: _Visit, duration: str) -> Section:
    """Name the stage's job once; its own rows then leave the source column empty."""
    works = {_job_label(take.actor, reader.execution(take.run_id)) for take in stay.takes}
    owner = stay.takes[0].actor if stay.takes else None
    shared = len(works) == 1
    if stay.takes:
        source = reader.source(stay.takes[0], stay.stage)
    else:
        source = "enso" if stay.stage == "done" else "person"
    entries = []
    for item in stay.items:
        entry = reader.handoff(item) if isinstance(item, _Handoff) else item
        # A script row's source is its command, which the header never says.
        if entry.actor == owner or (_enso(entry.actor) and entry.source != "script"):
            entry = replace(entry, who="")
        if entry.title == "Starting work" and not shared:
            entry = replace(entry, preview=_job_label(entry.actor, reader.execution(entry.run_id)))
        entries.append(entry)
    job = next(iter(works)) if shared else ""
    return Section(stay.stage, source, stay.at, duration, job, tuple(entries))
