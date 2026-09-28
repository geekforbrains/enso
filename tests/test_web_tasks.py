"""Task board, detail, and linked job/run pages against an isolated home."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import (
    commit_file,
    edit_project,
    git,
    load_job,
    write_job,
    write_project,
)

from enso import runs, tasks, web, workflows, worktrees
from enso.config import Config, Paths, load_config
from enso.web import files
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
    assert "<form" not in body or 'method="get"' in body
    assert "style=" not in body
    return body


def task_links(body: str) -> list[str]:
    """The task rows on a list page, in order."""
    return re.findall(r'<a class="row entity" href="/tasks/([A-Z]+-\d+)"', body)


def timeline_of(body: str) -> str:
    return body.split("<h2>Timeline</h2>", 1)[1]


def titles(body: str) -> list[str]:
    """The timeline's row titles in reading order, oldest first; steps are not rows."""
    return re.findall(r'<summary class="row">.*?<span class="title">([^<]*)</span>', body, re.S)


def stages(body: str) -> list[str]:
    """The stage visits the timeline opens with a header, in order."""
    return re.findall(r'<div class="stage-visit">.*?<span class="chip">([^<]*)</span>', body, re.S)


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
    taken = tasks.take(paths, config, "EN", "todo", run_id=run_id, actor="job:dev:todo")
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
    assert "7 tasks matching the filter." in by_project
    assert '<a class="project-link" href="/tasks?project=EN" aria-current="page">' in by_project
    assert '<input type="hidden" name="project" value="EN">' in by_project

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
        ("stage=blocked", ["EN-002"]),  # an open stage
        ("stage=done", ["EN-005"]),  # and a finished one, read from the capped history
        ("q=fences", ["EN-001"]),  # title search
        ("q=keep+the+fence+label", ["EN-001"]),  # body search
        ("q=++en-0005++", ["EN-005"]),  # the same reference normalization for finished work
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


async def test_empty_board_says_so(client: TestClient, project_config: Config) -> None:
    page = await html(client, "/tasks")
    assert task_links(page) == [] and board_groups(page) == []
    assert "No tasks yet." in page


async def test_empty_projects_explain_the_flow(
    client: TestClient, enso_home: Paths, project_config: Config
) -> None:
    edit_project(
        enso_home,
        stages=[
            "triage",
            {
                "name": "build",
                "command": "true",
                "checks": [
                    {"name": "Build <artifact>", "command": "true"},
                ],
            },
            {"name": "approve", "human": True, "max_returns": 0},
            {"name": "release", "return_to": "build"},
        ],
    )
    page = await html(client, "/tasks?project=EN")
    assert 'href="/tasks?project=MKT"' in page and "0 open" in page
    assert "Enso · Workflow" in page and 'href="/workspaces/default"' in page
    flow = page.split('<ol class="workflow-steps">')[1].split("</ol>")[0]
    assert re.findall(r'&amp;stage=([a-z]+)"', flow) == [
        "triage",
        "build",
        "approve",
        "release",
        "done",
    ]
    assert all(kind in flow for kind in ("Agent", "Command", "Human"))
    assert "Checks: Build &lt;artifact&gt;" in flow
    assert "May return to triage" in flow and "May return to build" in flow
    assert "May return to approve" not in flow
    assert "No tasks in project EN." in page


async def test_workflow_links_to_its_agent_job_prompts(
    client: TestClient, enso_home: Paths, project_config: Config
) -> None:
    edit_project(
        enso_home, stages=["triage", {"name": "build", "command": "true"}, "approve:human"]
    )
    write_job(enso_home, "triage", project="EN", stage="triage", prompt="Read the task spec.")
    write_job(enso_home, "backup", project="EN", stage="triage", enabled=False, name="Backup")
    write_job(enso_home, "triage", workspace="personal", project="EN", stage="triage")
    write_job(enso_home, "marketing", project="MKT", stage="draft")
    write_job(enso_home, "build", project="EN", stage="build")
    write_job(enso_home, "approve", project="EN", stage="approve")

    page = await html(client, "/tasks?project=EN")
    assert re.findall(r'href="(/jobs/[^"]+)"', page) == [
        "/jobs/default%3Abackup#prompt-head",
        "/jobs/default%3Atriage#prompt-head",
    ]
    assert "View instructions · Backup" in page
    assert 'href="/tasks?project=EN&amp;stage=triage"' in page
    job_page = await html(client, "/jobs/default%3Atriage")
    assert 'id="prompt-head"' in job_page and "Read the task spec." in job_page
    assert 'href="/tasks?project=EN&amp;stage=triage"' in job_page


async def test_unreadable_stage_jobs_preserve_the_workflow_and_tasks(
    client: TestClient, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable_jobs(*args, **kwargs):
        raise OSError("jobs unavailable")

    monkeypatch.setattr(taskviews, "load_jobs", unreadable_jobs)
    page = await html(client, "/tasks?project=EN")
    assert "Stage instructions could not be read" in page
    assert "Tasks could not be read" not in page
    assert "Enso · Workflow" in page and len(task_links(page)) == 7
    assert "View instructions" not in page


async def test_workspace_scope_filters_history_before_the_cap_and_links_back(
    client: TestClient, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_project(board.paths, "BLOG", {"name": "Blog", "stages": ["write"]}, workspace="personal")
    config = load_config(board.paths)
    for index in range(3):
        task = tasks.create(board.paths, config, "BLOG", f"Post {index}", actor=USER)
        if index:
            tasks.move(
                board.paths,
                config,
                task.ref,
                "advance",
                actor=USER,
                run_id=None,
                message="Published",
            )
    monkeypatch.setattr(taskviews, "DONE_LIMIT", 1)

    page = await html(client, "/tasks?workspace=default")
    assert "8 tasks matching the filter, 7 listed." in page
    assert group_rows(page)["Done"] == ["EN-006"]
    assert 'href="/tasks?project=BLOG' not in page
    scoped = await html(client, "/tasks?workspace=personal")
    assert "3 tasks matching the filter, 2 listed." in scoped
    assert "2 completed in the last 7 days" in scoped
    assert task_links(scoped) == ["BLOG-001", "BLOG-003"]
    assert '<input type="hidden" name="workspace" value="personal">' in scoped
    assert "Blog · personal" in scoped

    workspace = await html(client, "/workspaces/personal")
    assert 'href="/tasks?project=BLOG&amp;workspace=personal"' in workspace
    assert "write → done" in workspace and "1 open" in workspace
    assert 'href="/tasks?project=EN' not in workspace
    task_page = await html(client, "/tasks/BLOG-001")
    assert 'href="/tasks?project=BLOG&amp;workspace=personal"' in task_page
    assert 'href="/workspaces/personal"' in task_page


async def test_project_counts_ignore_task_search_and_retain_missing_definitions(
    client: TestClient, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = await html(client, "/tasks?project=EN&stage=todo&q=unmatched")
    flow = page.split('<ol class="workflow-steps">')[1].split("</ol>")[0]
    assert "1 task" in flow and "5 open" in page
    assert task_links(page) == []

    definition = board.paths.project("default", "EN") / "PROJECT.md"
    saved = definition.read_text()
    definition.unlink()
    orphan = await html(client, "/tasks?project=EN")
    assert "Project definition unavailable" in orphan
    assert 'href="/tasks?project=EN&amp;workspace=default"' in orphan
    assert len(task_links(orphan)) == 7
    definition.write_text(saved)

    def unreadable_counts(*args, **kwargs):
        raise OSError("counts unavailable")

    monkeypatch.setattr(tasks, "stage_counts", unreadable_counts)
    unavailable = await html(client, "/tasks?project=MKT")
    assert "Tasks could not be read" in unavailable
    assert "Count unavailable" in unavailable and "0 open" not in unavailable
    assert "Marketing · Workflow" in unavailable


async def test_task_page_shows_the_record(client: TestClient, board: Board) -> None:
    page = await html(client, "/tasks/en-1")  # the reference is normalised
    assert "Fix labelled Slack code fences" in page and "EN-001" in page
    assert '<a href="/tasks" aria-current="page">' in page
    assert "<h1>Spec</h1>" in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert(1)" not in page
    assert "<code>abc123</code>" in page  # refs
    handoff = page.split("<h2>Handoff</h2>", 1)[1].split("<h2>Refs</h2>", 1)[0]
    # The handoff is Markdown, and its single newline stays a line break.
    assert (
        '<div class="markdown"><p>Scope confirmed.<br />\nTouch slack_text.py only.</p>' in handoff
    )
    assert f'<a href="/runs/{board.run_id}"><code>{board.run_id}</code></a>' in page  # claim
    # Every event folds; the one the run recorded links to it from the opened row.
    run_link = (
        f'<a href="/runs/{board.run_id}">Open run <span class="mono">{board.run_id}</span></a>'
    )
    assert run_link in timeline_of(page)
    assert '<a class="row"' not in timeline_of(page)
    assert "Worktree" not in page  # the project has no repo
    # The stage is the pipeline chip and nothing else: the heading repeated it for nothing.
    assert '<span class="chip current">todo</span>' in page
    assert '<span class="tag' not in heading(page)
    assert "pruned</span>" not in page

    pruned = await html(client, "/tasks/EN-004")
    assert 'Run <span class="mono">deadbeef</span> pruned' in pruned
    assert 'href="/runs/deadbeef"' not in pruned
    assert "Run deadbeef ended without a handoff" in pruned  # recovery context
    assert titles(pruned) == ["Task created", "Starting work", "Run ended without a handoff"]

    blocked = await html(client, "/tasks/EN-002")
    assert '<span class="chip current">blocked</span>' in blocked  # off the pipeline, still shown
    assert titles(blocked) == ["Task created", "Blocked"]
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


async def test_task_timeline_folds_messages_into_rendered_markdown(
    client: TestClient, board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message shows as its words on one line; opening the event shows the Markdown."""
    message = "**Changed:** the `E6` email.\n\n1. Run it\n<script>unsafe()</script>"
    tasks.note(board.paths, "EN-001", actor="job:dev:todo", run_id=board.run_id, message=message)
    timeline = timeline_of(await html(client, "/tasks/EN-001"))
    note = timeline.split('<div class="task-event">')[-1]  # oldest first: the note is last
    assert '<span class="title">Added note</span>' in note
    preview = "Changed: the E6 email. Run it &lt;script&gt;unsafe()&lt;/script&gt;"
    assert f'<span class="message">{preview}</span>' in note
    assert "<strong>Changed:</strong> the <code>E6</code> email.</p>\n<ol>" in note
    assert "Run it<br />\n&lt;script&gt;unsafe()&lt;/script&gt;</li>" in note
    assert "<script>unsafe()" not in timeline
    # The row opens rather than links, so the run it came from is linked inside it.
    run = board.run_id
    assert f'<a href="/runs/{run}">Open run <span class="mono">{run}</span></a>' in note
    assert "<code>job:dev:todo</code>" in note  # the recorded actor stays readable

    # Past the render limit the message is shown as recorded rather than parsed.
    monkeypatch.setattr(files, "RENDER_LIMIT", 10)
    note = timeline_of(await html(client, "/tasks/EN-001")).split('<div class="task-event">')[-1]
    assert '<pre class="output bare" tabindex="0">**Changed:** the `E6` email.' in note
    assert '<span class="message">**Changed:** the `E6` email. 1. Run it' in note


async def test_task_timeline_groups_stage_visits_and_names_only_other_actors(
    client: TestClient, board: Board
) -> None:
    """A header names the job once; its rows leave the source empty, a person keeps a name."""
    page = await html(client, "/tasks/EN-001")
    assert stages(page) == ["triage", "todo"]
    assert titles(page) == ["Task created", "Accepted", "Commit abc123", "Starting work"]
    todo = timeline_of(page).split('<div class="stage-visit">')[2]
    assert '<span class="job">todo · claude opus · high effort</span>' in todo
    assert '<span class="who"></span>' in todo  # the stage's own run: the header says who
    assert '<span class="who">Terminal · gavin</span>' in todo  # the person who attached it
    assert 'title="Agent"' in todo and 'title="Person"' in todo
    assert "1 event" not in page and "5 events, oldest first" in page


async def test_task_timeline_names_slack_people_from_the_cache(
    client: TestClient, board: Board
) -> None:
    """Events from before names were recorded fall back to the Slack directory, then the id."""
    flagged = timeline_of(await html(client, "/tasks/EN-007"))
    assert '<span class="who">Slack · U1</span>' in flagged
    board.paths.cache.mkdir(parents=True, exist_ok=True)
    entry = {"id": "U1", "name": "gavin", "real_name": "Gavin", "display_name": ""}
    board.paths.slack_cache.write_text(
        json.dumps({"users": {"fetched_at": 0, "items": {"U1": entry}}})
    )
    named = timeline_of(await html(client, "/tasks/EN-007"))
    assert '<span class="who">Slack · Gavin</span>' in named
    assert "<code>slack:U1</code>" in named  # the opened row keeps the recorded identity


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
    project_config: Config,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edit_project(enso_home, repo=str(repo))
    config = load_config(enso_home)
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


def submit(board: Board, transaction: dict, times: int = 1) -> None:
    """Real in-run handoffs for EN-001; ``transaction`` then describes their transaction."""
    started = workflows.start(board.paths, board.config, "EN-001", board.run_id)
    for _ in range(times):
        tasks.move(
            board.paths,
            board.config,
            "EN-001",
            "advance",
            actor="job:dev:todo",
            run_id=board.run_id,
            message=transaction["message"],
        )
    transaction["id"] = started["id"]


def handoff_of(page: str) -> str:
    """The handoff row with its opened body and its steps."""
    return (
        timeline_of(page)
        .split('<div class="task-event" id="transaction-', 1)[1]
        .split('<div class="task-event"', 1)[0]
    )


def steps_of(page: str) -> list[str]:
    return re.findall(r'<li class="step">.*?<span class="title">([^<]*)</span>', page, re.S)


@pytest.mark.parametrize("status", ["submitted", "blocked", "accepted"])
async def test_task_timeline_holds_each_transaction_s_checks_and_evidence(
    client: TestClient,
    board: Board,
    transaction: dict,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """The handoff row carries what Workflow history held: checks, repairs, and evidence."""
    submit(board, transaction, times=2)  # the second is the repair after a failed check
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
    assert "Workflow history" not in page and "Lifecycle scripts" not in page
    assert f'href="#transaction-{transaction["id"]}"' in page  # the banner's "View evidence"
    handoff = handoff_of(page)
    decision = {
        "submitted": "Waiting for the run to stop",
        "blocked": "Blocked",
        "accepted": "Accepted",
    }[status]
    assert steps_of(handoff) == [
        "Unit tests check",
        "Repair 1 of 2",
        "Repair submitted",
        "Unit tests check",
        decision,
    ]
    # A check opens to its run facts and complete output, escaped as recorded.
    assert "<dt>Exit</dt><dd><code>1</code></dd>" in handoff
    assert "Test suite failed\nAssertion failed &lt;script&gt;unsafe()&lt;/script&gt;" in handoff
    assert "24 passed" in handoff and "<dt>Attempt</dt><dd>2</dd>" in handoff
    assert "<dt>Repairs</dt><dd>1 of 2</dd>" in handoff
    assert "<dt>Spec</dt><dd><code>spec-ve</code></dd>" in handoff
    assert f"<dt>Transaction</dt><dd><code>{transaction['id'][:8]}</code></dd>" in handoff
    assert "<script>unsafe()" not in page
    assert ("Repair limit exhausted" in handoff) == (status == "blocked")
    hook = timeline_of(page).split('<div class="task-event">')[-1]  # an untimed delivery is last
    assert '<span class="title">after-transition hook</span>' in hook
    assert "Notification unavailable" in hook
    if status == "submitted":
        assert "The task remains in todo until Enso accepts this handoff." in page
    assert ("Handoff accepted" in page) == (status == "accepted")


async def test_task_timeline_evidence_survives_a_pruned_run(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(board, transaction)
    transaction.update(status="accepted", ended_at=transaction["started_at"])
    transaction["checks"] = transaction["checks"][1:]
    transaction["checks"][0]["attempt"] = 1
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [transaction])
    monkeypatch.setattr(taskviews, "_existing_runs", lambda _paths, _ids: {})
    handoff = handoff_of(await html(client, "/tasks/EN-001"))
    assert f'Run <span class="mono">{board.run_id}</span> pruned' in handoff
    assert f'href="/runs/{board.run_id}"' not in handoff
    assert "24 passed" in handoff and steps_of(handoff) == ["Unit tests check", "Accepted"]
    assert "Operator verification" not in handoff


async def test_operator_verification_has_no_provider_run_link_while_active_or_completed(
    client: TestClient,
    enso_home: Paths,
    project_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edit_project(
        enso_home,
        stages=[
            {"name": "work", "checks": [{"name": "unit", "command": "echo manual-check-passed"}]}
        ],
    )
    config = load_config(enso_home)
    task = tasks.create(enso_home, config, "EN", "Verify the candidate", actor=USER)
    checking = asyncio.Event()
    finish_check = asyncio.Event()
    real_command = workflows.command

    async def paused_check(*args, **kwargs):
        checking.set()
        await finish_check.wait()
        return await real_command(*args, **kwargs)

    monkeypatch.setattr(workflows, "command", paused_check)
    verification = asyncio.create_task(
        workflows.verify_manual(enso_home, config, task.ref, "Operator reviewed the candidate")
    )
    try:
        await asyncio.wait_for(checking.wait(), timeout=5)
        held = tasks.get(enso_home, task.ref)
        run_id = held.claim_run_id
        assert run_id is not None and run_id.startswith("manual-")
        operator = tasks.actor_from_env(os.environ)  # the person verifying, not a placeholder
        assert held.claim_actor == operator
        assert not taskviews._existing_runs(enso_home, [run_id])
        active = await html(client, f"/tasks/{task.ref}")
        assert re.search(
            rf"<dt>Claim</dt>\s*<dd>Operator verification <code>{run_id}</code>", active
        )
        assert f'Operator verification <span class="mono">{run_id}</span>' in timeline_of(active)
        label = taskviews.timeline.who(operator, {}, {})
        assert f'<span class="who">{label} · verify</span>' in active  # the submitted handoff
        assert "pruned</span>" not in active and f'href="/runs/{run_id}"' not in active
        board = await html(client, "/tasks")
        assert f'Operator verification <span class="mono">{run_id}</span>' in board
        assert "claimed by run" not in board
    finally:
        finish_check.set()
        result = await verification
    assert result.status == "accepted"
    transaction = workflows.history(enso_home, task.ref)[0]
    assert transaction["checks"][0]["status"] == "passed"
    assert tasks.get(enso_home, task.ref).claim_run_id is None
    completed = await html(client, f"/tasks/{task.ref}")
    assert "Handoff accepted" in completed  # the banner, which links to the handoff's evidence
    assert "manual-check-passed" in handoff_of(completed)  # the opened check's output
    assert "Operator verification" in completed
    assert "pruned</span>" not in completed and f'href="/runs/{run_id}"' not in completed


async def test_task_timeline_warns_when_a_landing_was_not_accepted(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(board, transaction)
    transaction.update(
        status="blocked",
        checks=[],
        recovery_of="previous-transaction",
        integration={"phase": "applied", "candidate": "landed-sha", "target_sha": "old-target"},
    )
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [transaction])
    handoff = handoff_of(await html(client, "/tasks/EN-001"))
    assert steps_of(handoff) == ["Landed", "Not accepted"]
    assert "old-target → landed-sha" in handoff
    assert "Git landed, but Enso stopped before accepting." in handoff
    assert "<dt>Recovers</dt><dd><code>previous</code></dd>" in handoff


async def test_task_timeline_distinguishes_pending_checks_from_no_checks(
    client: TestClient, board: Board, transaction: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit(board, transaction)
    transaction.update(
        checks=[], hooks=[], stage_definition={"checks": [{"name": "lint", "command": "./lint"}]}
    )
    monkeypatch.setattr(workflows, "history", lambda _paths, _ref: [transaction])
    handoff = handoff_of(await html(client, "/tasks/EN-001"))
    assert steps_of(handoff) == ["lint check", "Waiting for the run to stop"]
    assert '<span class="tag">not yet run</span>' in handoff
    transaction.update(stage_definition={"checks": []}, status="accepted")
    unchecked = handoff_of(await html(client, "/tasks/EN-001"))
    assert steps_of(unchecked) == ["Accepted"] and "No checks configured" in unchecked


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
    # A hook with no transaction has no evidence panel: the timeline row carries every try.
    assert "Workflow history" not in page and "Lifecycle scripts" not in page
    hook = timeline_of(page).split('<div class="task-event">')[-1]
    assert '<span class="title">after:done hook</span>' in hook
    assert (
        '<span class="tag tag-ok">passed</span>' in hook and "Event <code>manual-e</code>" in hook
    )
    assert "Attempt 1: failed, exit 1, 1.3s\nTest suite failed" in hook
    assert "Attempt 2: passed, exit 0, 0.8s\n24 passed" in hook


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
    page = await html(client, "/jobs/default%3Adev-triage")
    assert "None" not in page  # the missing cron line never leaks as Python's None
    assert '<a href="/tasks?project=EN&amp;stage=triage">' in page
    today = await html(client, "/today/reliability")
    assert "<span>when work is ready</span>" in today and "None" not in today


async def test_tasks_pages_without_a_database(client: TestClient, enso_home: Paths) -> None:
    listing = await html(client, "/tasks")
    assert "Tasks could not be read" in listing and "enso.db does not exist" in listing
    page = await html(client, "/tasks/EN-001")
    assert "The task could not be read" in page and "enso.db does not exist" in page
    assert "Not found" in await html(client, "/tasks/nope", 404)
