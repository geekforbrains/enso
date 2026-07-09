"""Tests for the tasks module."""

from __future__ import annotations

import os

import pytest

from enso import tasks
from enso.tasks import (
    STATUSES,
    add_attachment,
    claim_next_todo,
    create_task,
    delete_attachment,
    delete_task,
    get_task,
    load_tasks,
    save_task,
    set_status,
    slugify,
)


@pytest.fixture
def tasks_dir(tmp_path, monkeypatch):
    """Point the tasks module at a temporary directory."""
    d = str(tmp_path / "tasks")
    monkeypatch.setattr(tasks, "TASKS_DIR", d)
    return d


def test_statuses_constant():
    assert STATUSES == ["todo", "in_progress", "blocked", "done", "cancelled"]


def test_create_and_get(tasks_dir):
    task = create_task(
        "Research OFX parsers",
        description="Compare libs",
        tags=["research"],
        notify=True,
    )
    assert task.slug == "research-ofx-parsers"
    assert task.status == "todo"
    assert task.notify is True
    assert task.tags == ["research"]
    assert os.path.isfile(task.path)
    assert task.created == task.updated

    got = get_task("research-ofx-parsers")
    assert got is not None
    assert got.title == "Research OFX parsers"
    assert got.description == "Compare libs"
    assert got.tags == ["research"]
    assert got.notify is True
    assert got.status == "todo"
    assert got.blocked_reason is None


def test_create_with_provider_model(tasks_dir):
    create_task("Override", provider="claude", model="sonnet")
    got = get_task("override")
    assert got is not None
    assert got.provider == "claude"
    assert got.model == "sonnet"


def test_get_missing(tasks_dir):
    assert get_task("nope") is None


def test_load_tasks_empty(tasks_dir):
    assert load_tasks() == []


def test_load_tasks_sorted_by_updated_desc(tasks_dir, monkeypatch):
    stamps = iter(["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"])
    monkeypatch.setattr(tasks, "_utcnow", lambda: next(stamps))
    create_task("Alpha")
    create_task("Beta")
    assert [t.slug for t in load_tasks()] == ["beta", "alpha"]


def test_slug_collision(tasks_dir):
    t1 = create_task("Same Title")
    t2 = create_task("Same Title")
    t3 = create_task("Same Title")
    assert t1.slug == "same-title"
    assert t2.slug == "same-title-2"
    assert t3.slug == "same-title-3"
    # All three directories exist independently.
    assert len(load_tasks()) == 3


def test_slugify(tasks_dir):
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  Spaces   here  ") == "spaces-here"
    assert slugify("!!!") == "task"


def test_set_status_transitions(tasks_dir):
    create_task("Do a thing")
    t = set_status("do-a-thing", "in_progress")
    assert t.status == "in_progress"

    t = set_status("do-a-thing", "blocked", reason="need input")
    assert t.status == "blocked"
    assert t.blocked_reason == "need input"
    assert get_task("do-a-thing").blocked_reason == "need input"

    # Moving away from blocked clears the reason, on disk too.
    t = set_status("do-a-thing", "todo")
    assert t.status == "todo"
    assert t.blocked_reason is None
    assert get_task("do-a-thing").blocked_reason is None


def test_set_status_invalid(tasks_dir):
    create_task("X")
    with pytest.raises(ValueError):
        set_status("x", "frozen")


def test_set_status_missing(tasks_dir):
    with pytest.raises(FileNotFoundError):
        set_status("ghost", "done")


def test_save_task_bumps_updated(tasks_dir, monkeypatch):
    stamps = iter(["2026-01-01T00:00:00Z", "2026-06-06T06:06:06Z"])
    monkeypatch.setattr(tasks, "_utcnow", lambda: next(stamps))
    t = create_task("Bump me")
    assert t.updated == "2026-01-01T00:00:00Z"

    t.description = "changed"
    save_task(t)
    assert t.updated == "2026-06-06T06:06:06Z"

    reloaded = get_task("bump-me")
    assert reloaded.description == "changed"
    assert reloaded.created == "2026-01-01T00:00:00Z"
    assert reloaded.updated == "2026-06-06T06:06:06Z"


def test_claim_next_todo_picks_oldest_and_flips(tasks_dir, monkeypatch):
    stamps = iter(
        [
            "2026-01-01T00:00:00Z",  # old-task created
            "2026-01-02T00:00:00Z",  # new-task created
            "2026-01-03T00:00:00Z",  # claim save bump
        ]
    )
    monkeypatch.setattr(tasks, "_utcnow", lambda: next(stamps))
    create_task("Old task")
    create_task("New task")

    claimed = claim_next_todo()
    assert claimed is not None
    assert claimed.slug == "old-task"
    assert claimed.status == "in_progress"

    # Persisted to disk, and the newer task is untouched.
    assert get_task("old-task").status == "in_progress"
    assert get_task("new-task").status == "todo"


def test_claim_next_todo_none_when_nothing_todo(tasks_dir):
    create_task("A")
    set_status("a", "done")
    assert claim_next_todo() is None


def test_claim_next_todo_empty(tasks_dir):
    assert claim_next_todo() is None


def test_add_and_list_attachments(tasks_dir):
    create_task("With files")
    stored = add_attachment("with-files", "spec.pdf", b"%PDF-1.4 data")
    assert stored == "spec.pdf"

    t = get_task("with-files")
    assert t.attachment_names() == ["spec.pdf"]
    on_disk = os.path.join(t.attachments_dir, "spec.pdf")
    assert os.path.isfile(on_disk)
    with open(on_disk, "rb") as f:
        assert f.read() == b"%PDF-1.4 data"


def test_add_attachment_sanitizes_path(tasks_dir):
    create_task("Safe")
    stored = add_attachment("safe", "../../etc/passwd", b"x")
    assert stored == "passwd"
    t = get_task("safe")
    assert t.attachment_names() == ["passwd"]
    # Nothing escaped the attachments directory.
    assert os.path.isfile(os.path.join(t.attachments_dir, "passwd"))


def test_add_attachment_missing_task(tasks_dir):
    with pytest.raises(FileNotFoundError):
        add_attachment("ghost", "f.txt", b"x")


def test_delete_attachment(tasks_dir):
    create_task("Del")
    add_attachment("del", "a.txt", b"1")
    add_attachment("del", "b.txt", b"2")
    assert get_task("del").attachment_names() == ["a.txt", "b.txt"]

    delete_attachment("del", "a.txt")
    assert get_task("del").attachment_names() == ["b.txt"]

    # Deleting something that isn't there is a no-op.
    delete_attachment("del", "missing.txt")
    assert get_task("del").attachment_names() == ["b.txt"]


def test_delete_task(tasks_dir):
    create_task("Gone soon")
    assert get_task("gone-soon") is not None
    delete_task("gone-soon")
    assert get_task("gone-soon") is None
    # Idempotent.
    delete_task("gone-soon")
