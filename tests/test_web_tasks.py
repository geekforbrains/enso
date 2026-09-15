"""Task board, detail, and linked job/run pages against an isolated home."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import commit_file, git, load_job, write_config, write_job

from enso import db, runs, tasks, web, workflows, worktrees
from enso.config import Config, Paths, parse_config
from enso.web import tasks as taskviews
from enso.web.server import create_app


@pytest.fixture
async def client(enso_home: Paths) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(create_app(enso_home, web.Bind("127.0.0.1", 8787)))) as c:
        yield c


USER = "user:gavin"


@dataclass
class Board:
    paths: Paths
    config: Config
    run_id: str  # a live run holding EN-001


async def html(client: TestClient, path: str, status: int = 200) -> str:
    response = await client.get(path)
    body = await response.text()
    assert response.status == status, (path, response.status, body[:300])
    assert "Traceback" not in body
    depth = 0
    for token in re.findall(r"<a\b|</a>", body):
        depth += 1 if token == "<a" else -1
        assert 0 <= depth <= 1, f"nested or unbalanced <a> in {path}"
    assert depth == 0, f"unbalanced <a> in {path}"
    assert "<form" not in body or 'method="get"' in body
    assert "style=" not in body
    return body


def task_links(body: str) -> list[str]:
    """The task rows on a list page, in order."""
    return re.findall(r'<a class="row entity" href="/tasks/([A-Z]+-\d+)"', body)


def heading(body: str) -> str:
    """The page's own <h1>, so an assertion about it cannot match the nav or the body."""
    found = re.search(r'<div class="phead"><h1>(.*?)</h1>', body, re.S)
    assert found is not None, body[:300]
    return found.group(1)


def board_groups(body: str) -> list[tuple[str, str, list[str]]]:
    """The board as rendered: each group's heading, its count line, and its rows in order."""
    found = []
    for chunk in body.split('<div class="hourhead">')[1:]:
        head = re.search(r"<b>([^<]+)</b>\s*<span>([^<]+)</span>", chunk)
        assert head is not None, chunk[:200]
        found.append((head.group(1), head.group(2), task_links(chunk)))
    return found


def group_rows(body: str) -> dict[str, list[str]]:
    return {label: rows for label, _count, rows in board_groups(body)}


@pytest.fixture
def board(enso_home: Paths, project_config: Config) -> Board:
    """Every state the board can show: one task each, through the core API only."""
    paths, config = enso_home, project_config
    write_job(paths, "dev-todo")
    run_id = runs.start(paths, load_job(paths, config, "dev-todo"), "manual", effort="high")

    def add(project: str, title: str, **kwargs: object) -> tasks.Task:
        return tasks.create(paths, config, project, title, actor=USER, **kwargs)  # type: ignore[arg-type]

    def move(ref: str, move_id: str, message: str = "done") -> tasks.Task:
        return tasks.move(paths, config, ref, move_id, actor=USER, run_id=None, message=message)

    active = add(
        "EN",
        "Fix labelled Slack code fences",
        body="# Spec\n\n<script>alert(1)</script>\n\n- keep the fence label\n",
    )
    move(active.ref, "advance", "Scope confirmed.\nTouch slack_text.py only.")
    tasks.add_ref(paths, active.ref, "commit", "abc123", actor=USER, run_id=None)
    taken = tasks.take(paths, config, "EN", "todo", run_id=run_id, actor="job:dev-todo")
    assert taken is not None and taken.ref == "EN-001"
    blocked = add("EN", "Needs a decision")
    move(blocked.ref, "block", "Waiting on the API key\nsecond line stays off the row")
    add("EN", "Someday", backlog=True)  # EN-003
    ready = add("EN", "Ready to triage")  # EN-004
    done = add("EN", "Shipped")  # EN-005
    for _ in range(3):
        move(done.ref, "advance")
    dropped = add("EN", "Never mind")  # EN-006
    move(dropped.ref, "drop", "no longer wanted")
    human = add("MKT", "Approve the launch post")  # MKT-001
    move(human.ref, "advance", "drafted")
    flagged = add("EN", "Look at this")  # EN-007
    tasks.note(paths, flagged.ref, actor="slack:U1", run_id=None, message="look", attention=True)
    # EN-004 was held by a run that retention has since pruned: the event outlives the row.
    held = tasks.take(paths, config, "EN", "triage", run_id="deadbeef", actor="job:dev-triage")
    assert held is not None and held.ref == ready.ref
    tasks.release(
        paths,
        ready.ref,
        actor="enso",
        run_id="deadbeef",
        message="run deadbeef ended (error) without a handoff",
        reason="run_ended",
    )
    return Board(paths, config, run_id)


async def test_tasks_board_groups_every_state(client: TestClient, board: Board) -> None:
    """One board: every state lands in its group, in the fixed order, and nothing repeats."""
    body = await html(client, "/tasks")
    assert '<a href="/tasks" aria-current="page">' in body  # the top nav tab
    assert 'aria-label="Views"' not in body  # no view tabs; the dropdowns filter instead
    assert board_groups(body) == [
        ("Blocked", "3 tasks · needs you", ["EN-002", "MKT-001", "EN-007"]),
        ("Active", "1 task", ["EN-001"]),
        ("Ready", "1 task", ["EN-004"]),
        ("Backlog", "1 task", ["EN-003"]),
        ("Done", "2 tasks", ["EN-006", "EN-005"]),
    ]  # blocked oldest in stage first; done newest first, cancelled listed with it
    listed = task_links(body)
    assert len(listed) == len(set(listed)) == 8  # every task once, and only once
    assert "8 tasks." in body and "1 completed in the last 7 days" in body
    assert "up to 200 finished and cancelled tasks" in body
    assert f'claimed by run <span class="mono">{board.run_id}</span>' in body
    # The dot is the only place the state appears, and a row never carries a message.
    assert "Waiting on the API key" not in body
    assert "waiting on you" not in body and 'class="err"' not in body
    # An unknown parameter is ignored, so an old bookmarked tab still renders the board.
    assert task_links(await html(client, "/tasks?view=done")) == listed


async def test_ready_group_reads_down_the_pipeline(client: TestClient, board: Board) -> None:
    """Ready is one flat list ordered by project, then that project's own stage order."""
    paths, config = board.paths, board.config

    def advance(ref: str, times: int) -> None:
        for _ in range(times):
            tasks.move(paths, config, ref, "advance", actor=USER, run_id=None, message="on")

    todo = tasks.create(paths, config, "EN", "Middle of the pipeline", actor=USER)  # EN-008
    advance(todo.ref, 1)
    review = tasks.create(paths, config, "EN", "Late in the pipeline", actor=USER)  # EN-009
    advance(review.ref, 2)
    other = tasks.create(paths, config, "MKT", "First draft", actor=USER)  # MKT-002
    body = await html(client, "/tasks")
    assert group_rows(body)["Ready"] == ["EN-004", todo.ref, review.ref, other.ref]
    assert "Enso · triage" not in body  # one list, not a heading per project and stage


async def test_backlog_group_lists_oldest_in_stage_first(client: TestClient, board: Board) -> None:
    older = tasks.create(
        board.paths, board.config, "EN", "Waiting longer", actor=USER, backlog=True
    )
    with sqlite3.connect(board.paths.db) as con:
        con.execute(
            "UPDATE _enso_tasks SET entered_stage_at = '2020-01-01T00:00:00.000000+00:00' "
            "WHERE ref = ?",
            [older.ref],
        )
    assert group_rows(await html(client, "/tasks"))["Backlog"] == [older.ref, "EN-003"]
    # Flagging one moves it to Blocked: a task is in one group, never in two at once.
    tasks.note(board.paths, "EN-003", actor=USER, run_id=None, message="look", attention=True)
    groups = group_rows(await html(client, "/tasks"))
    assert groups["Backlog"] == [older.ref]
    assert groups["Blocked"] == ["EN-002", "EN-003", "MKT-001", "EN-007"]


async def test_flagged_claim_moves_to_blocked_and_keeps_its_run(
    client: TestClient, board: Board
) -> None:
    """A claimed task flagged for attention joins Blocked, warning dot and claim intact."""
    tasks.note(board.paths, "EN-001", actor="slack:U1", run_id=None, message="hm", attention=True)
    body = await html(client, "/tasks")
    groups = group_rows(body)
    assert "EN-001" in groups["Blocked"]
    assert "Active" not in groups  # the emptied group is left out, not shown with a zero
    row = body[body.index('href="/tasks/EN-001"') :]
    assert '<span class="pin warning" role="img" aria-label="needs you"' in row[:180]
    assert f'claimed by run <span class="mono">{board.run_id}</span>' in row


async def test_tasks_board_reads_a_bounded_amount(
    client: TestClient, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The board never walks a task's events, and reads the finished history by count and page."""

    read_events = Mock(side_effect=AssertionError("a list page must not read task events"))
    monkeypatch.setattr(tasks, "events", read_events)
    # The count line counts this week in SQL while the Done group lists the newest page: an
    # old finish drops out of the count but stays listed, and the list is cut, the count not.
    with sqlite3.connect(board.paths.db) as con:
        con.execute(
            "UPDATE _enso_tasks SET entered_stage_at = '2020-01-01T00:00:00.000000+00:00' "
            "WHERE ref = 'EN-005'"
        )
    aged = await html(client, "/tasks")
    assert "Tasks could not be read" not in aged
    assert group_rows(aged)["Done"] == ["EN-006", "EN-005"]
    assert "8 tasks." in aged and "0 completed in the last 7 days" in aged

    monkeypatch.setattr(taskviews, "DONE_LIMIT", 1)
    capped = await html(client, "/tasks")
    assert task_links(capped) == [
        "EN-002", "MKT-001", "EN-007", "EN-001", "EN-004", "EN-003", "EN-006"
    ]  # fmt: skip
    # The count still includes the old finish beyond the cap, and the line says so.
    assert "8 tasks, 7 listed." in capped
    assert "Done lists up to 1 finished and cancelled tasks." in capped
    assert task_links(await html(client, "/tasks?q=shipped")) == ["EN-005"]
    read_events.assert_not_called()


async def test_tasks_filters_and_empty_states(client: TestClient, board: Board) -> None:
    by_project = await html(client, "/tasks?project=EN")
    assert board_groups(by_project) == [
        ("Blocked", "2 tasks · needs you", ["EN-002", "EN-007"]),
        ("Active", "1 task", ["EN-001"]),
        ("Ready", "1 task", ["EN-004"]),
        ("Backlog", "1 task", ["EN-003"]),
        ("Done", "2 tasks", ["EN-006", "EN-005"]),
    ]
    assert "7 tasks matching the filter." in by_project
    assert '<option value="EN" selected>' in by_project

    assert "No tasks matching “nothing here”." in await html(client, "/tasks?q=nothing+here")
    assert "No tasks in project ZZ." in await html(client, "/tasks?project=zz")
    assert "No tasks in stage review." in await html(client, "/tasks?stage=review")
    assert "No tasks in project MKT in stage backlog." in await html(
        client, "/tasks?project=MKT&stage=backlog"
    )
    assert "No tasks in stage backlog matching “shipped”." in await html(
        client, "/tasks?stage=backlog&q=shipped"
    )
    odd = await html(client, "/tasks?stage=../x&q=%3Cb%3Ebold%3C/b%3E")
    assert task_links(odd) == [] and "<b>bold</b>" not in odd  # bad stage dropped, q escaped


async def test_board_filters(client: TestClient, board: Board) -> None:
    # These requests only read the same board; build it once for all filter cases.
    for query, expected in [
        ("project=MKT", ["MKT-001"]),
        ("project=mkt", ["MKT-001"]),
        ("stage=blocked", ["EN-002"]),
        ("stage=backlog", ["EN-003"]),
        ("stage=done", ["EN-005"]),
        ("stage=cancelled", ["EN-006"]),
        ("q=fences", ["EN-001"]),  # title search
        ("q=keep+the+fence+label", ["EN-001"]),  # body search
        ("q=++en-0005++", ["EN-005"]),  # the same reference normalization for finished work
        ("q=++en-0003++", ["EN-003"]),
        ("q=%25", []),  # LIKE wildcards are literal search text
        ("project=EN&stage=done&q=shipped", ["EN-005"]),
        ("project=MKT&stage=done&q=shipped", []),
    ]:
        page = await html(client, f"/tasks?{query}")
        assert task_links(page) == expected, query
        if expected:
            assert "1 task matching the filter." in page, query
        else:
            assert board_groups(page) == [] and "No tasks " in page, query


async def test_task_search_keeps_unicode_uppercase_reference_matches(
    client: TestClient, enso_home: Paths, raw_config_projects: dict
) -> None:
    raw_config_projects["projects"]["ID"] = {
        "name": "Support",
        "workspace": "default",
        "stages": ["triage"],
    }
    config, problems, _ = parse_config(raw_config_projects, enso_home)
    assert config is not None, problems
    write_config(enso_home, raw_config_projects)
    db.migrate(enso_home)
    task = tasks.create(enso_home, config, "ID", "Handle ticket", actor=USER)
    # Dotless i is outside the reference grammar, but uppercases to the stored ASCII ID.
    for finished in (False, True):
        if finished:
            tasks.move(
                enso_home, config, task.ref, "advance", actor=USER, run_id=None, message="resolved"
            )
        body = await html(client, "/tasks?q=%C4%B1d-001")
        assert task_links(body) == ["ID-001"]
        assert "1 task matching the filter." in body
        assert f"{int(finished)} completed in the last 7 days" in body


async def test_empty_board_says_so(client: TestClient, project_config: Config) -> None:
    page = await html(client, "/tasks")
    assert task_links(page) == [] and board_groups(page) == []
    assert "No tasks yet." in page


async def test_task_page_shows_the_record(client: TestClient, board: Board) -> None:
    page = await html(client, "/tasks/en-1")  # the reference is normalised
    assert "Fix labelled Slack code fences" in page and "EN-001" in page
    assert '<a href="/tasks" aria-current="page">' in page
    assert "<h1>Spec</h1>" in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)" not in page
    assert "<code>abc123</code>" in page  # refs
    assert "Scope confirmed.\nTouch slack_text.py only." in page  # the handoff, verbatim
    assert f'<a href="/runs/{board.run_id}"><code>{board.run_id}</code></a>' in page  # claim
    assert f'<a class="row" href="/runs/{board.run_id}">' in page  # the taken event links
    assert f'· run <span class="mono">{board.run_id}</span>' in page  # named in full, in the title
    assert "<b>run</b>" not in page  # the trail is the relative time in every timeline row
    assert "Worktree" not in page  # the project has no repo
    # The stage is the pipeline chip and nothing else: the heading repeated it for nothing.
    assert '<span class="chip current">todo</span>' in page
    assert '<span class="tag' not in heading(page)
    assert "run pruned" not in page

    pruned = await html(client, "/tasks/EN-004")
    assert "run pruned" in pruned and 'href="/runs/deadbeef"' not in pruned
    assert "Run deadbeef ended without a handoff" in pruned  # recovery context
    assert "released (run ended)" in pruned and "taken" in pruned

    blocked = await html(client, "/tasks/EN-002")
    assert '<span class="chip current">blocked</span>' in blocked  # off the pipeline, still shown
    assert "block: triage → blocked" in blocked
    assert "second line stays off the row" in blocked  # the timeline keeps the whole message

    # A stage off the pipeline still shows, and only attention still qualifies the heading.
    assert '<span class="chip current">cancelled</span>' in await html(client, "/tasks/EN-006")
    assert '<span class="chip current">done</span>' in await html(client, "/tasks/EN-005")
    flagged = heading(await html(client, "/tasks/EN-007"))
    assert '<span class="tag tag-warning">needs attention</span>' in flagged
    assert flagged.count('<span class="tag') == 1

    assert "Not found" in await html(client, "/tasks/EN-999", 404)
    assert "Not found" in await html(client, "/tasks/nope", 404)
    assert "Not found" in await html(client, "/tasks/EN-1;DROP", 404)


async def test_task_page_reports_a_failed_context_read(
    client: TestClient, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context read feeds the handoff and the recovery notice; a failure must be visible."""

    def boom(*args: object, **kwargs: object) -> dict:
        raise RuntimeError("context is unreadable")

    monkeypatch.setattr(tasks, "context", boom)
    page = await html(client, "/tasks/EN-001")
    assert "The task could not be read" in page
    assert "RuntimeError: context is unreadable" in page
    assert "<h2>Handoff</h2>" not in page  # the handoff is the part that went missing
    assert "Fix labelled Slack code fences" in page  # the rest of the record still renders


async def test_task_page_worktree_panel(
    client: TestClient,
    enso_home: Paths,
    raw_config_projects: dict,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_config_projects["projects"]["EN"]["repo"] = str(repo)
    config, problems, _ = parse_config(raw_config_projects, enso_home)
    assert config is not None, problems
    write_config(enso_home, raw_config_projects)
    db.migrate(enso_home)
    task = tasks.create(enso_home, config, "EN", "With a branch", actor=USER)
    assert "Worktree" not in await html(client, f"/tasks/{task.ref}")  # nothing prepared yet

    worktree = repo / ".worktrees" / task.ref
    worktree.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-q", "-b", f"enso/{task.ref}", str(worktree))
    commit_file(worktree, "change", "x\n", "work")
    git(repo, "checkout", "-q", "-b", "other")
    record = {
        "path": str(worktree),
        "repo": str(repo),
        "branch": f"enso/{task.ref}",
        "base": "main",
        "start_revision": git(repo, "rev-parse", "main"),
        "status": "retained",
        "error": "Cleanup deferred: untracked files need attention",
    }
    monkeypatch.setattr(worktrees, "lookup", lambda _paths, _ref: record)
    page = await html(client, f"/tasks/{task.ref}")
    assert "Worktree" in page and f"<code>enso/{task.ref}</code>" in page
    assert f"<code>{worktree}</code>" in page and "<code>main</code>" in page
    assert "1 commit ahead of main" in page
    assert "retained" in page and record["error"] in page
    assert "Starting revision" in page

    # Git failing must never take the page down: an unreadable worktree still renders.
    (worktree / ".git").unlink()
    broken = await html(client, f"/tasks/{task.ref}")
    assert "Worktree" in broken and "unknown; git could not count them" in broken

    # Completed cleanup keeps the audit record without presenting missing Git as a failure.
    record.update(status="removed", error="")
    removed = await html(client, f"/tasks/{task.ref}")
    assert "removed" in removed and f"<code>{worktree}</code>" in removed
    assert "Commits ahead" not in removed and "git could not count" not in removed


@pytest.fixture
def transaction(board: Board) -> dict:
    return {
        "id": "transaction-1",
        "run_id": board.run_id,
        "stage": "todo",
        "to_stage": "review",
        "status": "submitted",
        "candidate": "candidate-revision",
        "spec_hash": "spec-version",
        "workflow_hash": "workflow-version",
        "started_at": "2026-09-14T12:00:00+00:00",
        "ended_at": None,
        "message": "Agent says everything passed <script>unsafe()</script>",
        "error": "",
        "repairs": 1,
        "max_repairs": 2,
        "checks": [
            {
                "name": "Unit tests",
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 1300,
                "attempt": 1,
                "output": "Assertion failed <script>unsafe()</script>",
                "error": "Test suite failed",
            },
            {
                "name": "Unit tests",
                "status": "passed",
                "exit_code": 0,
                "duration_ms": 800,
                "attempt": 2,
                "output": "24 passed",
                "error": "",
            },
        ],
        "hooks": [
            {
                "name": "after-transition",
                "status": "failed",
                "event_id": "event-123",
                "exit_code": 2,
                "duration_ms": 100,
                "attempt": 1,
                "error": "Notification unavailable",
                "output": "",
            }
        ],
    }


@pytest.mark.parametrize("status", ["submitted", "checking", "repairing", "blocked", "accepted"])
async def test_task_workflow_distinguishes_submission_checks_and_acceptance(
    client: TestClient,
    board: Board,
    transaction: dict,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    transaction["status"] = status
    if status == "blocked":
        transaction["error"] = "Repair limit exhausted"
    if status == "accepted":
        transaction["ended_at"] = "2026-09-14T12:01:00+00:00"
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [transaction])
    monkeypatch.setattr(workflows, "event_history", lambda _paths, _ref: transaction["hooks"])
    page = await html(client, "/tasks/EN-001")
    assert '<span class="chip current">todo</span>' in page
    assert '<span class="chip current">review</span>' not in page
    assert "Workflow history" in page and "1 transaction, newest first" in page
    assert "candidate-revision" in page and "spec-version" in page and "workflow-version" in page
    assert "1 of 2 allowed" in page and "attempt 1" in page and "attempt 2" in page
    assert "Test suite failed" in page and "24 passed" in page
    assert "Lifecycle scripts" in page and "event-123" in page
    assert "Notification unavailable" in page
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in page
    assert "<script>unsafe()" not in page
    if status in ("submitted", "checking", "repairing"):
        assert "The task remains in todo until Enso accepts this handoff." in page
    assert ("Handoff accepted" in page) == (status == "accepted")
    if status == "blocked":
        assert "Repair limit exhausted" in page


async def test_task_workflow_evidence_survives_a_pruned_run(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction.update(run_id="pruned-run", status="accepted", ended_at=transaction["started_at"])
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [transaction])
    page = await html(client, "/tasks/EN-001")
    assert "run pruned; workflow evidence retained" in page
    assert 'href="/runs/pruned-run"' not in page
    assert "24 passed" in page and "Handoff accepted" in page


async def test_task_workflow_shows_integration_before_acceptance(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction.update(
        status="blocked",
        recovery_of="previous-transaction",
        integration={"phase": "applied", "candidate": "landed-sha", "target_sha": "old-target"},
    )
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [transaction])
    page = await html(client, "/tasks/EN-001")
    assert "Git integration completed, but this handoff was not accepted" in page
    assert "Recovery requires fresh verification" in page
    assert "landed-sha" in page and "old-target" in page
    assert 'href="#transaction-previous-transaction"' in page


async def test_task_workflow_distinguishes_pending_checks_from_no_checks(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction.update(checks=[], hooks=[], stage_definition={"checks": [{"name": "lint"}]})
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [transaction])
    page = await html(client, "/tasks/EN-001")
    assert "Not yet run: lint." in page and "No required checks configured." not in page
    transaction.update(stage_definition={"checks": []}, status="accepted")
    unchecked = await html(client, "/tasks/EN-001")
    assert "No required checks configured." in unchecked and "Not yet run:" not in unchecked


async def test_task_lifecycle_history_includes_manual_moves_and_delivery_retries(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = {
        "event_id": "manual-event",
        "name": "after:done",
        "from_stage": "review",
        "to_stage": "done",
        "status": "delivered",
        "attempts": 2,
        "transaction_id": None,
        "deliveries": [
            {**transaction["checks"][0], "name": "after:done"},
            {**transaction["checks"][1], "name": "after:done"},
        ],
    }
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [])
    monkeypatch.setattr(workflows, "event_history", lambda _paths, _ref: [event])
    page = await html(client, "/tasks/EN-001")
    assert "Workflow history" not in page and "Lifecycle scripts" in page
    assert "manual-event" in page and "delivered" in page
    assert "Test suite failed" in page and "24 passed" in page
    assert "attempt 1" in page and "attempt 2" in page


async def test_board_loads_transaction_summaries_once_without_evidence(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction["status"] = "checking"
    summary = Mock(return_value={"EN-001": transaction})
    monkeypatch.setattr(workflows, "current_summaries", summary)
    monkeypatch.setattr(workflows, "history", Mock(side_effect=AssertionError("detail-only data")))
    page = await html(client, "/tasks")
    summary.assert_called_once()
    assert set(summary.call_args.args[1]) == {
        "EN-001",
        "EN-002",
        "EN-003",
        "EN-004",
        "EN-007",
        "MKT-001",
    }
    assert "Running required checks" in page and "candidate-revision" not in page
    assert "Test suite failed" not in page and group_rows(page)["Active"] == ["EN-001"]


async def test_run_page_separates_provider_output_from_workflow_evidence(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction.update(status="blocked", error="Required lint check failed")
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [transaction])
    runs.finish(board.paths, board.run_id, status="ok", exit_code=0, output="Everything passed!")
    page = await html(client, f"/runs/{board.run_id}")
    assert "Everything passed!" in page and "Workflow blocked" in page
    assert "Required lint check failed" in page and "Task workflow" in page
    assert 'href="/tasks/EN-001#workflow"' in page


async def test_run_page_links_to_its_task(client: TestClient, board: Board) -> None:
    page = await html(client, f"/runs/{board.run_id}")
    assert "<dt>Task</dt>" in page
    assert '<a href="/tasks/EN-001"><code>EN-001</code> Fix labelled Slack code fences</a>' in page
    other = runs.start(
        board.paths, load_job(board.paths, board.config, "dev-todo"), "manual", effort="high"
    )
    assert "<dt>Task</dt>" not in await html(client, f"/runs/{other}")


async def test_stage_job_pages_say_when_work_is_ready(client: TestClient, board: Board) -> None:
    """A stage job with no cron line says so in prose on every page; only cron is a literal."""
    write_job(board.paths, "dev-triage", project="EN", stage="triage", omit=["schedule"])
    job = load_job(board.paths, board.config, "dev-triage")
    run_id = runs.start(board.paths, job, "ready", effort="high")  # so Today's reliability lists it
    runs.finish(board.paths, run_id, status="ok", exit_code=0)
    listing = await html(client, "/jobs")
    assert "<span>when work is ready</span>" in listing
    assert "<code>when work is ready</code>" not in listing
    assert "<code>0 9 * * *</code>" in listing  # the scheduled dev-todo row keeps its literal
    page = await html(client, "/jobs/dev-triage")
    assert "None" not in page  # the missing cron line never leaks as Python's None
    assert "a stage job: it claims a ready task there" in page  # the Stage row
    assert "none; it fires when work is ready" in page
    assert '<a href="/tasks?project=EN&amp;stage=triage">' in page
    assert "project capacity is enforced separately" in page
    assert "the stage job default" not in page
    assert "when a task is ready</span>" in page  # the Next run row
    today = await html(client, "/today/reliability")
    assert "<span>when work is ready</span>" in today and "None" not in today


async def test_tasks_pages_without_a_database(client: TestClient, enso_home: Paths) -> None:
    listing = await html(client, "/tasks")
    assert "Tasks could not be read" in listing and "enso.db does not exist" in listing
    page = await html(client, "/tasks/EN-001")
    assert "The task could not be read" in page and "enso.db does not exist" in page
    assert "Not found" in await html(client, "/tasks/nope", 404)
