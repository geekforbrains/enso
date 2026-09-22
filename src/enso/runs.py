"""Job run history and bounded execution attempts, retained together in SQLite."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from . import db
from .config import Paths
from .jobs import Job

OUTPUT_KEEP = 1024 * 1024  # a run keeps the final 1 MiB of execution output
ID_LENGTH = 12
PAGE_SIZE = 500  # one viewer page holds the default retention (``runs.keep``)
ERROR_PREVIEW = 200  # how much of an error a summary carries
STATUSES = ("running", "ok", "error", "timeout", "no_work", "gate_error", "skipped")


@dataclass(frozen=True)
class Run:
    id: str
    job: str
    workspace: str
    kind: str
    provider: str | None
    model: str | None
    effort: str | None
    trigger: str  # schedule | manual | ready (a stage job fired because a task waited)
    started_at: str
    ended_at: str | None
    duration_ms: int | None
    status: str  # running | ok | error | timeout | no_work | gate_error | skipped
    exit_code: int | None
    output: str | None
    error: str | None
    session_id: str | None = None
    postrun_error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunAttempt:
    """One completed execution and its postrun check; zero means work did not start."""

    number: int
    status: str
    exit_code: int | None
    output: str
    error: str
    session_id: str | None
    duration_ms: int | None
    postrun_exit_code: int | None
    postrun_output: str
    postrun_error: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunSummary:
    """A run's list fields: everything but ``output`` and the full ``error``.

    A retained run can carry a megabyte of output, so anything that lists runs reads these
    instead of ``Run``. ``error_preview`` is the first ``ERROR_PREVIEW`` characters.
    """

    id: str
    job: str
    workspace: str
    kind: str
    provider: str | None
    model: str | None
    effort: str | None
    trigger: str
    started_at: str
    ended_at: str | None
    duration_ms: int | None
    status: str
    exit_code: int | None
    error_preview: str | None
    has_output: bool

    def as_dict(self) -> dict:
        return asdict(self)


_SUMMARY_SELECT = f"""SELECT id, workspace || ':' || job AS job, workspace,
        kind, provider, model, effort, trigger, started_at,
        ended_at, duration_ms, status, exit_code,
        substr(error, 1, {ERROR_PREVIEW}) AS error_preview,
        output IS NOT NULL AS has_output
   FROM runs"""


def _run(row: sqlite3.Row) -> Run:
    fields = {key: row[key] for key in Run.__dataclass_fields__}
    return Run(**{**fields, "job": f"{row['workspace']}:{row['job']}"})


def _summary(row: sqlite3.Row) -> RunSummary:
    fields = {key: row[key] for key in RunSummary.__dataclass_fields__}
    return RunSummary(**{**fields, "has_output": bool(fields["has_output"])})


def _filters(
    job: str | None,
    status: str | None,
    statuses: Sequence[str] | None = None,
    *,
    workspace: str | None = None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if workspace is not None:
        clauses.append("workspace = ?")
        params.append(workspace)
    if job:
        owner, _, name = job.partition(":")
        clauses.append("workspace = ? AND job = ?")
        params.extend((owner, name))
    if status:
        clauses.append("status = ?")
        params.append(status)
    if statuses is not None:
        # An empty set matches nothing, which is the honest answer for an empty set.
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})" if statuses else "0")
        params.extend(statuses)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


def start(
    paths: Paths, job: Job, trigger: str, *, effort: str | None, kind: str | None = None
) -> str:
    """Insert a ``running`` row before anything spawns; ``effort`` is the clamped level."""
    kind = kind or ("agent" if job.agent is not None else "command")
    agent = job.agent if kind == "agent" else None
    run_id = uuid.uuid4().hex[:ID_LENGTH]
    with db.transaction(paths) as con:
        con.execute(
            """INSERT INTO runs (id, job, workspace, kind, provider, model, effort, trigger,
                                 started_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
            (
                run_id,
                job.dir_name,
                job.workspace,
                kind,
                agent.provider if agent else None,
                agent.model if agent else None,
                effort if agent else None,
                trigger,
                db.now(),
            ),
        )
    return run_id


def finish(
    paths: Paths,
    run_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    output: str = "",
    error: str = "",
    postrun_error: str = "",
    session_id: str | None = None,
) -> int | None:
    """Close a run with its outcome, duration, and the tail of its output; returns the duration."""
    ended_at = db.now()
    ended = datetime.fromisoformat(ended_at)
    with db.transaction(paths) as con:
        row = con.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        started = datetime.fromisoformat(row["started_at"])
        # A wall clock stepped back between start and finish must not record a
        # negative duration, which the CLI would print as "-1s".
        duration_ms = max(0, (ended - started) // timedelta(milliseconds=1))
        con.execute(
            """UPDATE runs SET ended_at = ?, duration_ms = ?, status = ?, exit_code = ?,
                               output = ?, error = ?, postrun_error = ?, session_id = ?
               WHERE id = ?""",
            (
                ended_at,
                duration_ms,
                status,
                exit_code,
                output[-OUTPUT_KEEP:] or None,
                error or None,
                postrun_error[-OUTPUT_KEEP:] or None,
                session_id,
                run_id,
            ),
        )
    return duration_ms


def record_attempt(
    paths: Paths,
    run_id: str,
    *,
    number: int,
    status: str,
    exit_code: int | None,
    output: str,
    error: str,
    session_id: str | None,
    postrun_exit_code: int | None = None,
    postrun_output: str = "",
    postrun_error: str = "",
    duration_ms: int | None = None,
) -> None:
    """Persist a completed turn, then update it after its check; never create an orphan.

    Text keeps its final ``OUTPUT_KEEP`` characters per field. The enclosing run remains
    running until its owner calls ``finish``, so a crash during checking retains the turn.
    """
    with db.transaction(paths) as con:
        con.execute(
            """INSERT INTO _enso_run_attempts
                 (run_id, number, status, exit_code, output, error, session_id,
                  duration_ms, postrun_exit_code, postrun_output, postrun_error)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? WHERE EXISTS (
                 SELECT 1 FROM runs WHERE id = ?)
               ON CONFLICT (run_id, number) DO UPDATE SET
                 status = excluded.status, exit_code = excluded.exit_code,
                 output = excluded.output, error = excluded.error,
                 session_id = excluded.session_id, duration_ms = excluded.duration_ms,
                 postrun_exit_code = excluded.postrun_exit_code,
                 postrun_output = excluded.postrun_output,
                 postrun_error = excluded.postrun_error""",
            (
                run_id,
                number,
                status,
                exit_code,
                output[-OUTPUT_KEEP:],
                error[-OUTPUT_KEEP:],
                session_id,
                duration_ms,
                postrun_exit_code,
                postrun_output[-OUTPUT_KEEP:],
                postrun_error[-OUTPUT_KEEP:],
                run_id,
            ),
        )


def attempts(paths: Paths, run_id: str) -> list[RunAttempt]:
    """Completed attempts in order, without creating state."""
    try:
        with db.reader(paths) as con:
            rows = con.execute(
                "SELECT * FROM _enso_run_attempts WHERE run_id = ? ORDER BY number", (run_id,)
            ).fetchall()
    except db.MissingDatabaseError:
        return []
    return [
        RunAttempt(**{key: row[key] for key in RunAttempt.__dataclass_fields__}) for row in rows
    ]


def get(paths: Paths, run_id: str) -> Run | None:
    """The run with this id, or with this unique id prefix; read-only, None with no history."""
    if not run_id:
        return None
    # A genuine id is hex, so ``%`` and ``_`` in the argument are literal characters the
    # caller typed, never LIKE wildcards. The prefix match stays in SQL: a retained run
    # holds up to a megabyte of output, so matching in Python would read every one.
    prefix = run_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    try:
        with db.reader(paths) as con:
            rows = con.execute(
                r"SELECT * FROM runs WHERE id = ? OR id LIKE ? ESCAPE '\' LIMIT 2",
                (run_id, f"{prefix}%"),
            ).fetchall()
    except db.MissingDatabaseError:
        return None
    exact = [row for row in rows if row["id"] == run_id]
    if exact:
        return _run(exact[0])
    return _run(rows[0]) if len(rows) == 1 else None


def list_runs(
    paths: Paths, *, job: str | None = None, limit: int = 20, workspace: str | None = None
) -> list[Run]:
    """Newest first, optionally for one workspace and/or job."""
    where, params = _filters(job, None, workspace=workspace)
    params.append(limit)
    with db.transaction(paths) as con:
        rows = con.execute(
            f"SELECT * FROM runs {where} ORDER BY started_at DESC, rowid DESC LIMIT ?", params
        ).fetchall()
    return [_run(row) for row in rows]


def latest(paths: Paths) -> dict[str, Run]:
    """The most recent run of every job."""
    with db.transaction(paths) as con:
        rows = con.execute(
            "SELECT * FROM runs WHERE rowid IN "
            "(SELECT max(rowid) FROM runs GROUP BY workspace, job)"
        ).fetchall()
    return {run.job: run for run in map(_run, rows)}


# -- Summaries: read-only, never touching ``output`` --------------------------


def list_summaries(
    paths: Paths,
    *,
    job: str | None = None,
    status: str | None = None,
    statuses: Sequence[str] | None = None,
    limit: int = PAGE_SIZE,
    offset: int = 0,
) -> list[RunSummary]:
    """Newest first, filtered and paged; an absent database is empty history.

    ``status`` narrows to one outcome, ``statuses`` to a set of them; both apply.
    """
    where, params = _filters(job, status, statuses)
    try:
        with db.reader(paths) as con:
            rows = con.execute(
                f"{_SUMMARY_SELECT} {where} ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
    except db.MissingDatabaseError:
        return []
    return [_summary(row) for row in rows]


def count(
    paths: Paths,
    *,
    job: str | None = None,
    status: str | None = None,
    statuses: Sequence[str] | None = None,
) -> int:
    """How many runs match the same filters ``list_summaries`` takes."""
    where, params = _filters(job, status, statuses)
    try:
        with db.reader(paths) as con:
            return con.execute(f"SELECT count(*) FROM runs {where}", params).fetchone()[0]
    except db.MissingDatabaseError:
        return 0


def latest_summaries(paths: Paths) -> dict[str, RunSummary]:
    """The most recent run of every job, without output."""
    try:
        with db.reader(paths) as con:
            rows = con.execute(
                f"{_SUMMARY_SELECT} WHERE rowid IN "
                "(SELECT max(rowid) FROM runs GROUP BY workspace, job)"
            ).fetchall()
    except db.MissingDatabaseError:
        return {}
    return {row["job"]: _summary(row) for row in rows}


def job_names(paths: Paths) -> list[str]:
    """Every job that has ever run, for a filter; the job directory may be long gone."""
    try:
        with db.reader(paths) as con:
            rows = con.execute(
                "SELECT DISTINCT workspace || ':' || job AS job FROM runs ORDER BY job"
            ).fetchall()
    except db.MissingDatabaseError:
        return []
    return [row["job"] for row in rows]


def unfinished(paths: Paths) -> list[Run]:
    """Rows still marked running; after a restart these are orphans unless a lock says otherwise."""
    with db.transaction(paths) as con:
        rows = con.execute(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at, rowid"
        ).fetchall()
    return [_run(row) for row in rows]


def abandon(paths: Paths, run_id: str, error: str) -> bool:
    """Close a still-``running`` row whose end nobody saw; False when its owner beat us."""
    # No end time or duration: they would date from the recovery, not from the run.
    with db.transaction(paths) as con:
        cursor = con.execute(
            "UPDATE runs SET status = 'error', error = ? WHERE id = ? AND status = 'running'",
            (error, run_id),
        )
        return cursor.rowcount == 1


def prune(paths: Paths, keep: int, max_age_days: int) -> int:
    """Drop finished runs beyond the newest ``keep`` or older than ``max_age_days``."""
    cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    with db.transaction(paths) as con:
        cursor = con.execute(
            """DELETE FROM runs WHERE status != 'running' AND (started_at < ? OR id NOT IN (
                 SELECT id FROM runs WHERE status != 'running'
                 ORDER BY started_at DESC, rowid DESC LIMIT ?))""",
            (cutoff, keep),
        )
        return cursor.rowcount
