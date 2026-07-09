"""One-off tasks — TASK.md files the agent drains through the job pipeline.

A task is a directory ``~/.enso/tasks/<slug>/`` holding a ``TASK.md``
(frontmatter + Markdown description) and an optional ``attachments/`` folder.
The file format mirrors jobs; the lifecycle is a small fixed status enum. See
``docs/specs/tasks.md`` and ``docs/specs/data-model.md`` for the full model.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .config import CONFIG_DIR
from .frontmatter import read, write

log = logging.getLogger(__name__)

TASKS_DIR = os.path.join(CONFIG_DIR, "tasks")

STATUSES = ["todo", "in_progress", "blocked", "done", "cancelled"]


def _utcnow() -> str:
    """Return the current time as an ISO-8601 UTC string (second precision)."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _fmt_ts(value: object) -> str:
    """Normalise a frontmatter timestamp to an ISO-8601 UTC string.

    PyYAML resolves an unquoted ISO timestamp to a ``datetime``; our own
    writes quote it back to a string. Coerce either shape to a stable string
    so ordering by ``updated`` stays lexicographic.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
        return dt.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass
class Task:
    """A one-off task parsed from a TASK.md file."""

    slug: str
    title: str
    status: str = "todo"
    tags: list[str] = field(default_factory=list)
    notify: bool = False
    provider: str | None = None
    model: str | None = None
    created: str = ""
    updated: str = ""
    blocked_reason: str | None = None
    result: str | None = None
    description: str = ""
    path: str = ""

    @property
    def task_dir(self) -> str:
        """Absolute path to the task's directory."""
        return os.path.join(TASKS_DIR, self.slug)

    @property
    def attachments_dir(self) -> str:
        """Absolute path to the task's attachments directory."""
        return os.path.join(self.task_dir, "attachments")

    def attachment_names(self) -> list[str]:
        """Return the names of files present in ``attachments/`` (sorted)."""
        d = self.attachments_dir
        if not os.path.isdir(d):
            return []
        return sorted(
            name
            for name in os.listdir(d)
            if os.path.isfile(os.path.join(d, name))
        )


def _task_from_meta(slug: str, meta: dict, body: str, path: str) -> Task:
    """Build a Task from parsed frontmatter, its body, slug, and file path."""
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    reason = meta.get("blocked_reason")
    result = meta.get("result")
    return Task(
        slug=slug,
        title=str(meta.get("title") or slug),
        status=str(meta.get("status") or "todo"),
        tags=[str(t) for t in tags],
        notify=bool(meta.get("notify", False)),
        provider=meta.get("provider"),
        model=meta.get("model"),
        created=_fmt_ts(meta.get("created")),
        updated=_fmt_ts(meta.get("updated")),
        blocked_reason=None if reason is None else str(reason),
        result=None if result is None else str(result),
        description=body.strip(),
        path=path,
    )


def _meta_from_task(task: Task) -> dict:
    """Build the frontmatter mapping for a task, omitting empty optionals."""
    meta: dict = {
        "title": task.title,
        "status": task.status,
        "tags": list(task.tags),
        "notify": bool(task.notify),
    }
    if task.provider is not None:
        meta["provider"] = task.provider
    if task.model is not None:
        meta["model"] = task.model
    meta["created"] = task.created
    meta["updated"] = task.updated
    if task.blocked_reason is not None:
        meta["blocked_reason"] = task.blocked_reason
    if task.result is not None:
        meta["result"] = task.result
    return meta


def slugify(title: str) -> str:
    """Derive a filesystem slug from a title.

    Lowercase, whitespace → ``-``, non-alphanumeric/dash characters stripped,
    collapsed dashes. Collisions against existing task directories are
    suffixed ``-2``, ``-3``, …
    """
    dashed = re.sub(r"\s+", "-", title.lower())
    cleaned = re.sub(r"[^a-z0-9-]", "", dashed)
    base = re.sub(r"-+", "-", cleaned).strip("-") or "task"

    slug = base
    n = 2
    while os.path.exists(os.path.join(TASKS_DIR, slug)):
        slug = f"{base}-{n}"
        n += 1
    return slug


def _sanitize_filename(filename: str) -> str:
    """Reduce an arbitrary filename to a safe basename inside attachments/."""
    name = os.path.basename(filename.replace("\\", "/").strip())
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    name = name.lstrip(".")
    return name or "attachment"


def load_tasks() -> list[Task]:
    """Load all tasks from ``~/.enso/tasks/``, newest ``updated`` first."""
    if not os.path.isdir(TASKS_DIR):
        return []
    tasks: list[Task] = []
    for entry in sorted(os.listdir(TASKS_DIR)):
        task_file = os.path.join(TASKS_DIR, entry, "TASK.md")
        if not os.path.isfile(task_file):
            continue
        try:
            meta, body = read(task_file)
        except OSError:
            log.warning("Could not read %s", task_file)
            continue
        tasks.append(_task_from_meta(entry, meta, body, task_file))
    tasks.sort(key=lambda t: t.updated, reverse=True)
    return tasks


def get_task(slug: str) -> Task | None:
    """Load a single task by slug, or ``None`` if it does not exist."""
    task_file = os.path.join(TASKS_DIR, slug, "TASK.md")
    if not os.path.isfile(task_file):
        return None
    meta, body = read(task_file)
    return _task_from_meta(slug, meta, body, task_file)


def create_task(
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    notify: bool = False,
    provider: str | None = None,
    model: str | None = None,
    status: str = "todo",
) -> Task:
    """Create a new task directory with a scaffolded TASK.md.

    ``created`` and ``updated`` are set to the same timestamp on creation.
    """
    slug = slugify(title)
    now = _utcnow()
    task = Task(
        slug=slug,
        title=title,
        status=status,
        tags=list(tags or []),
        notify=bool(notify),
        provider=provider,
        model=model,
        created=now,
        updated=now,
        blocked_reason=None,
        description=description,
        path=os.path.join(TASKS_DIR, slug, "TASK.md"),
    )
    os.makedirs(task.task_dir, exist_ok=True)
    write(task.path, _meta_from_task(task), task.description)
    log.info("Created task %s", slug)
    return task


def save_task(task: Task) -> None:
    """Write ``TASK.md`` for a task, bumping ``updated`` to now (atomic)."""
    task.updated = _utcnow()
    os.makedirs(task.task_dir, exist_ok=True)
    if not task.path:
        task.path = os.path.join(task.task_dir, "TASK.md")
    write(task.path, _meta_from_task(task), task.description)
    log.info("Saved task %s (status=%s)", task.slug, task.status)


def set_status(
    slug: str,
    status: str,
    reason: str | None = None,
    result: str | None = None,
) -> Task:
    """Transition a task to ``status``.

    ``blocked_reason`` is set only when moving to ``blocked`` (cleared
    otherwise). ``result`` — the outcome message shown on the task — is stored
    whenever provided and left untouched otherwise.
    """
    if status not in STATUSES:
        raise ValueError(f"Invalid status: {status!r}")
    task = get_task(slug)
    if task is None:
        raise FileNotFoundError(f"No such task: {slug}")
    task.status = status
    task.blocked_reason = reason if status == "blocked" else None
    if result is not None:
        task.result = result
    save_task(task)
    return task


def claim_next_todo() -> Task | None:
    """Claim the oldest ``todo`` task, flipping it to ``in_progress``.

    Returns the claimed task, or ``None`` when nothing is claimable. The claim
    is an atomic frontmatter rewrite performed before any agent spawns; the
    file is re-read at claim time so a task already moved by someone else is
    skipped rather than double-claimed.
    """
    candidates = [t for t in load_tasks() if t.status == "todo"]
    if not candidates:
        return None
    candidates.sort(key=lambda t: t.created)
    for candidate in candidates:
        fresh = get_task(candidate.slug)
        if fresh is None or fresh.status != "todo":
            continue
        fresh.status = "in_progress"
        save_task(fresh)
        log.info("Claimed task %s", fresh.slug)
        return fresh
    return None


def add_attachment(slug: str, filename: str, data: bytes) -> str:
    """Store ``data`` as an attachment on a task, returning the stored name.

    The filename is sanitised to a safe basename inside the task's
    ``attachments/`` directory. Written atomically (temp file in the same
    directory, fsync, then ``os.replace``).
    """
    task_dir = os.path.join(TASKS_DIR, slug)
    if not os.path.isdir(task_dir):
        raise FileNotFoundError(f"No such task: {slug}")
    stored = _sanitize_filename(filename)
    attach_dir = os.path.join(task_dir, "attachments")
    os.makedirs(attach_dir, exist_ok=True)
    dest = os.path.join(attach_dir, stored)

    fd, tmp = tempfile.mkstemp(dir=attach_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    log.info("Added attachment %s to task %s", stored, slug)
    return stored


def delete_attachment(slug: str, filename: str) -> None:
    """Delete an attachment from a task (no-op if it does not exist)."""
    stored = _sanitize_filename(filename)
    dest = os.path.join(TASKS_DIR, slug, "attachments", stored)
    with contextlib.suppress(FileNotFoundError):
        os.remove(dest)
        log.info("Deleted attachment %s from task %s", stored, slug)


def delete_task(slug: str) -> None:
    """Delete a task directory and everything under it (idempotent)."""
    task_dir = os.path.join(TASKS_DIR, slug)
    if os.path.isdir(task_dir):
        shutil.rmtree(task_dir)
        log.info("Deleted task %s", slug)
