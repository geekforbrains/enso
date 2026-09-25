"""The task timeline's arrangement: stage visits, folded handoffs, and who each row names."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from typing import Any

from enso import tasks
from enso.web import timeline

SHA = "093415ab7aec8f721a49fed305c3d2ceff55c72d"
TARGET = "ebb2f6a35084fa9e0c08ab5e7c99d7930f91b7c9"
MANUAL = "manual-" + "6ebc1e35a69d411db6cf829ecb6bc832"
NOW = datetime(2026, 9, 25, 17, 0, tzinfo=UTC)
GATES = {"name": "gates", "command": "./check.sh gates"}


class History:
    """Task events with increasing ids and times, as the database would return them."""

    def __init__(self) -> None:
        self.events: list[tasks.TaskEvent] = []
        self.ids = count(1)

    def add(self, kind: str, actor: str, run_id: str | None = None, **fields: Any) -> str:
        number = next(self.ids)
        stamp = f"2026-09-25T15:{number:02d}:00.000000+00:00"
        self.events.append(
            tasks.TaskEvent(
                id=number,
                task_id=1,
                kind=kind,
                actor=actor,
                run_id=run_id,
                from_stage=fields.get("from_stage"),
                to_stage=fields.get("to_stage"),
                message=fields.get("message", ""),
                payload=fields.get("payload", {}),
                created_at=stamp,
            )
        )
        return stamp


def passed(name: str, attempt: int, duration_ms: int, output: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "status": "passed",
        "attempt": attempt,
        "exit_code": 0,
        "duration_ms": duration_ms,
        "output": output,
        "error": "",
    }


def arrange(
    history: History, transactions: list[dict[str, Any]], lifecycle: list[dict[str, Any]]
) -> list[timeline.Section]:
    return timeline.build(
        list(reversed(history.events)),  # newest first, as ``tasks.events`` returns them
        transactions,
        lifecycle,
        {},
        None,
        {"slack:U1": "Gavin"},
        base="develop",
        finished=True,
        now=NOW,
    )


def rows(section: timeline.Section) -> list[tuple[str, str, str]]:
    return [(entry.source, entry.title, entry.who) for entry in section.entries]


def steps(entry: timeline.Entry) -> list[tuple[str, str, str, str]]:
    return [(step.source, step.title, step.who, step.detail) for step in entry.steps]


def test_timeline_folds_each_handoff_with_its_checks_and_enso_decision() -> None:
    """TT-001's shape: a blocked handoff, a verified retry, a person's move, an integration."""
    history = History()
    agent = {"kind": "agent", "provider": "claude", "model": "opus", "effort": "xhigh"}
    history.add("created", "slack:U1", to_stage="build")
    history.add("taken", "job:ws:tt-build", "r1", payload={"stage": "build", "execution": agent})
    history.add("ref", "job:ws:tt-build", "r1", payload={"kind": "commit", "value": SHA[:7]})
    move = {"transaction_id": "t1", "move": "advance"}
    history.add(
        "submitted",
        "job:ws:tt-build",
        "r1",
        from_stage="build",
        to_stage="review",
        message="**Changed:** E6 retired",
        payload=move,
    )
    blocked_at = history.add(
        "moved",
        "enso",
        "r1",
        from_stage="build",
        to_stage="blocked",
        message="Acceptance rule inputs changed: engine_test.go",
        payload={"move": "block", "transaction_id": "t1"},
    )
    history.add(
        "moved", "slack:U1", from_stage="blocked", to_stage="build", payload={"move": "resume"}
    )
    history.add(
        "submitted",
        "user:gavin",
        MANUAL,
        from_stage="build",
        to_stage="review",
        message="Test edits approved",
        payload={"transaction_id": "t2", "move": "advance"},
    )
    history.add(
        "moved",
        "user:gavin",
        MANUAL,
        from_stage="build",
        to_stage="review",
        message="Test edits approved",
        payload={"move": "advance"},
    )  # an acceptance recorded before moves carried their transaction
    history.add(
        "accepted",
        "enso",
        MANUAL,
        from_stage="build",
        to_stage="review",
        payload={"transaction_id": "t2"},
    )
    history.add(
        "moved",
        "slack:U1",
        from_stage="review",
        to_stage="merge",
        message="QA passed",
        payload={"move": "advance"},
    )
    history.add("taken", "job:ws:tt-merge", "r3", payload={"execution": {"kind": "integration"}})
    history.add(
        "submitted",
        "enso",
        "r3",
        from_stage="merge",
        to_stage="done",
        message="Integration requested",
        payload={"transaction_id": "t3", "move": "advance"},
    )
    history.add(
        "moved",
        "enso",
        "r3",
        from_stage="merge",
        to_stage="done",
        payload={"move": "advance", "transaction_id": "t3"},
    )
    history.add(
        "accepted",
        "enso",
        "r3",
        from_stage="merge",
        to_stage="done",
        payload={"transaction_id": "t3"},
    )
    checks = {"checks": [GATES]}
    transactions = [
        {"id": "t1", "status": "blocked", "stage_definition": checks, "checks": []},
        {
            "id": "t2",
            "status": "accepted",
            "candidate": SHA,
            "stage_definition": checks,
            "checks": [passed("gates", 1, 9297, "go test ./...\nok")],
        },
        {
            "id": "t3",
            "status": "accepted",
            "candidate": SHA,
            "stage_definition": {**checks, "integrate": True},
            "checks": [passed("gates", 1, 11400, "e2e: none")],
            "integration": {"phase": "applied", "target_sha": TARGET, "candidate": SHA},
        },
    ]
    hook = {
        "event_id": "ffb69f45e6da4f889c6af618c7c468b4",
        "name": "after:blocked",
        "command": "./notify.sh blocked",
        "to_stage": "blocked",
        "status": "delivered",
        "created_at": blocked_at.replace("00.000000", "00.500000"),
        "deliveries": [{"attempt": 1, "status": "passed", "exit_code": 0, "duration_ms": 1173}],
    }
    build, blocked, verified, review, merge, done = arrange(history, transactions, [hook])

    assert (build.stage, build.source, build.job) == (
        "build",
        "agent",
        "tt-build · claude opus · xhigh effort",
    )
    # The stage's own run needs no name: the header already says which job did the work.
    assert rows(build) == [
        ("person", "Task created", "Slack · Gavin"),
        ("agent", "Starting work", ""),
        ("agent", "Commit 093415a", ""),
        ("agent", "Handoff → review", ""),
    ]
    handoff = build.entries[-1]
    assert handoff.preview == "Changed: E6 retired" and handoff.tone == "warning"
    assert steps(handoff) == [
        ("enso", "Blocked the handoff", "", "Acceptance rule inputs changed: engine_test.go")
    ]
    assert handoff.reason == "Acceptance rule inputs changed: engine_test.go"
    assert handoff.transaction == "t1"

    assert (blocked.stage, blocked.source, blocked.duration) == ("blocked", "person", "1m 00s")
    notify = blocked.entries[0]
    assert (notify.source, notify.title, notify.who) == (
        "script",
        "after:blocked hook",
        "./notify.sh blocked",
    )
    assert (notify.tag, notify.value) == (("ok", "passed"), "1.2s")
    assert rows(blocked)[1:] == [("person", "Resumed → build", "Slack · Gavin")]

    assert (verified.source, verified.job) == ("person", "")
    retry = verified.entries[0]
    assert (retry.title, retry.who, retry.tone) == (
        "Handoff → review",
        "Terminal · gavin · verify",
        "ok",
    )
    assert steps(retry) == [
        ("script", "gates check", "./check.sh gates", ""),  # a long passing log stays in the panel
        ("enso", "Accepted", "", "1 of 1 checks passed on 093415a"),
    ]
    assert retry.steps[0].value == "9.3s" and retry.steps[0].tag == ("ok", "passed")
    # The check opens to everything it printed; the handoff opens to its evidence.
    assert retry.steps[0].output == "go test ./...\nok"
    assert retry.steps[0].facts == (
        ("Exit", "0", True),
        ("Attempt", "1", False),
        ("Took", "9.3s", False),
    )
    assert retry.facts == (("Transaction", "t2", True),)  # the accepting step names the candidate

    assert rows(review) == [("person", "Advanced → merge", "Slack · Gavin")]
    assert (merge.source, merge.job) == ("enso", "tt-merge · no model")
    assert rows(merge) == [("enso", "Starting work", ""), ("enso", "Integration → done", "")]
    landing = merge.entries[-1]
    assert landing.preview == ""  # Enso's own boilerplate; the steps say what happened
    assert steps(landing) == [
        ("script", "gates check", "./check.sh gates", "e2e: none"),
        ("enso", "Landed on develop", "", "ebb2f6a → 093415a"),
        ("enso", "Accepted", "", "1 of 1 checks passed on 093415a"),
    ]
    assert (done.stage, done.source, done.duration, done.entries) == ("done", "enso", "", ())


def test_timeline_shows_a_failed_check_its_repair_and_the_retry_under_one_handoff() -> None:
    history = History()
    history.add("created", "user:gavin", to_stage="todo")
    history.add("taken", "job:ws:todo", "r1", payload={"execution": {"kind": "agent"}})
    for message in ("First try", "Fixed the order"):
        history.add(
            "submitted",
            "job:ws:todo",
            "r1",
            from_stage="todo",
            to_stage="review",
            message=message,
            payload={"transaction_id": "t1", "move": "advance"},
        )
    history.add(
        "moved",
        "job:ws:todo",
        "r1",
        from_stage="todo",
        to_stage="review",
        payload={"move": "advance", "transaction_id": "t1"},
    )
    history.add(
        "accepted",
        "enso",
        "r1",
        from_stage="todo",
        to_stage="review",
        payload={"transaction_id": "t1"},
    )
    failed = {
        **passed("unit", 1, 8200, "FAIL TestRulesV3Sequence"),
        "status": "failed",
        "exit_code": 1,
        "error": "\x1b[31mTest suite failed\x1b[0m\nmore detail",
    }
    transactions = [
        {
            "id": "t1",
            "status": "accepted",
            "max_repairs": 2,
            "stage_definition": {"checks": [{"name": "unit", "command": "pytest"}]},
            "checks": [failed, passed("unit", 2, 800, "24 passed")],
        }
    ]
    todo, review = arrange(history, transactions, [])
    assert [entry.title for entry in todo.entries] == [
        "Task created",
        "Starting work",
        "Handoff → review",
    ]
    assert steps(todo.entries[-1]) == [
        ("script", "unit check", "pytest", "Test suite failed"),
        ("enso", "Repair 1 of 2", "", "Sent the failure back to the run"),
        ("agent", "Repair submitted", "", "Fixed the order"),
        ("script", "unit check", "pytest", "24 passed"),
        ("enso", "Accepted", "", "1 of 1 checks passed"),
    ]
    assert todo.entries[-1].steps[0].tag == ("error", "failed")
    assert review.entries == ()


def test_timeline_labels_people_and_other_jobs_but_not_enso() -> None:
    names = {"slack:U1": "From cache"}
    assert timeline.who("slack:U1", {"actor_name": "Recorded"}, names) == "Slack · Recorded"
    assert timeline.who("slack:U1", {}, names) == "Slack · From cache"
    assert timeline.who("telegram:8140", {"actor_name": "Gavin"}, {}) == "Telegram · Gavin"
    assert timeline.who("slack:U2", {}, names) == "Slack · U2"
    assert timeline.who("user:gavin", {}, {}) == "Terminal · gavin"
    assert timeline.who("user:verify", {}, {}) == ""  # the old placeholder names nobody
    assert timeline.who("job:dev:tt-intake", {}, {}) == "Job · tt-intake"
    assert timeline.who("enso", {}, {}) == timeline.who("enso:worktrees", {}, {}) == ""
