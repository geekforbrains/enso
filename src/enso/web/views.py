"""Page view models: everything a template needs, built synchronously from current state.

Each function reads the config, database, jobs, skills, audit, and files afresh, so a
plain refresh always shows the truth; the server runs them in a worker thread so the
scans and SQLite reads never stall the event loop. A failure in one source becomes that
page's ``error`` (or one section's) instead of a traceback, and a missing or invalid
``config.json`` leaves ``config_problems`` for the shared banner while the parts that do
not need it still render.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from math import ceil
from typing import Any
from urllib.parse import quote, urlencode

from .. import audit, db, doctor, runs, skills, tasks, workspaces
from .. import heartbeat as beats
from .. import log as logsetup
from ..config import (
    BUILTIN_STAGES,
    TRANSPORT_NAMES,
    Config,
    Paths,
    ProjectConfig,
    check_config,
    valid_workspace_name,
)
from ..jobs import Job, load_jobs
from ..scheduling import cron_slots
from . import Bind, files
from . import heartbeat as beatviews

LOG_TAIL_LINES = 200
RECENT_RUNS = 20  # on a job's page
DB_COMPANIONS = ("", "-wal", "-shm")


# -- Shared helpers (registered as template filters) --------------------------


def human_bytes(size: int | None) -> str:
    """``0 B``, ``12 KB``, ``1.2 MB``: enough precision to decide whether to clean up."""
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    return f"{int(value)} {unit}" if unit == "B" or value >= 10 else f"{value:.1f} {unit}"


def elapsed(seconds: int) -> str:
    """45s, 2m 05s, 1h 12m."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def duration(duration_ms: int | None) -> str:
    return elapsed(duration_ms // 1000) if duration_ms is not None else "-"


def when(stamp: str | datetime | None) -> str:
    """A stored UTC timestamp (or a datetime) as local wall-clock time, to the second."""
    moment = _moment(stamp)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S") if moment else "-"


def clock(stamp: str | datetime | None) -> str:
    """Just the wall-clock time; every row that shows it also shows a relative time,
    and a feed's hour heading carries the day, so the column stays one word wide."""
    moment = _moment(stamp)
    return moment.astimezone().strftime("%H:%M") if moment else "-"


def iso(stamp: str | datetime | None) -> str:
    """The same instant for ``<time datetime>``: ISO 8601 with its offset."""
    moment = _moment(stamp)
    return moment.astimezone().isoformat(timespec="seconds") if moment else ""


def ago(stamp: str | datetime | None) -> str:
    moment = _moment(stamp)
    if moment is None:
        return "-"
    return since(stamp) + " ago"


def since(stamp: str | datetime | None) -> str:
    """How long ago, without the word: the time a task has spent in its stage."""
    moment = _moment(stamp)
    if moment is None:
        return "-"
    seconds = int((datetime.now(UTC) - moment.astimezone(UTC)).total_seconds())
    return elapsed(max(0, seconds))


def until(stamp: str | datetime | None) -> str:
    moment = _moment(stamp)
    if moment is None:
        return "-"
    seconds = int((moment.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    return "in " + elapsed(max(0, seconds))


def heartbeat_next(stamp: str | datetime | None, state: str) -> str:
    moment = _moment(stamp)
    if state != "active" or moment is None:
        return "-"
    return "due" if moment <= datetime.now(UTC) else until(stamp)


def _moment(stamp: str | datetime | None) -> datetime | None:
    if stamp is None or stamp == "":
        return None
    if isinstance(stamp, datetime):
        moment = stamp
    else:
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def status_class(status: str | None) -> str:
    """The tone for any status word the pages show; never the only signal.

    ``timeout`` is amber rather than red: the run did not fail, it ran out of time,
    and the schedule chart is much easier to read when the two are distinguishable.
    Both still count as failures wherever failures are counted.
    """
    return {
        "ok": "ok",
        "active": "ok",
        "fulfilled": "ok",
        "expired": "warning",
        "cancelled": "warning",
        "paused": "muted",
        "ready": "ok",
        "quiet": "muted",
        "running": "running",
        "error": "error",
        "timeout": "warning",
        "prerun_error": "error",
        "warning": "warning",
        "no_work": "muted",
        "skipped": "muted",
    }.get(status or "", "muted")


def status_word(status: str | None) -> str:
    """The status as prose: ``no_work`` is a database value, ``no work`` is a label."""
    return (status or "").replace("_", " ") or "-"


def heartbeat_event_word(kind: str) -> str:
    return {
        "observed": "Source update",
        "noted": "Note",
        "waited": "Waiting",
        "fulfilled": "Completed",
        "resumed": "Checks resumed",
        "run_failed": "Assessment failed",
        "run_recovered": "Assessments recovered",
        "notice_delivered": "Notification delivered",
    }.get(kind, status_word(kind).capitalize())


def heartbeat_actor(actor: str | None) -> str:
    """Who acted, as a reader can use it.

    Actors are recorded as their origin: ``slack:U0AETSSDDEF`` or ``telegram:8140``
    for a person in chat, ``job:nightly`` for a job run, ``beat:HB-002`` for the beat
    acting under its own authority, ``user:gavin`` for this machine's CLI, and plain
    ``heartbeat`` for Enso itself. Only the chat forms need help: the member ID says
    nothing on a page, and what a reader wants is that a person did this from Slack.
    Every other form already reads, so it is left exactly as it was recorded, and the
    expanded event keeps the raw value either way.
    """
    transport, _, identity = (actor or "").partition(":")
    if identity and transport in TRANSPORT_NAMES:
        return f"via {transport.capitalize()}"
    return actor or "-"


# A string that starts this way is a filesystem path and reads as code rather than as
# prose; so does one under a key that names a file, because a provider may be configured
# as a bare command that `shutil.which` resolves rather than as a path with slashes.
PATH_STARTS = ("/", "~")
FILE_KEYS = ("path", "unit")
Piece = tuple[str, str]  # how to draw it, and what it says


def fact(value: object, key: str = "") -> tuple[list[Piece], list[Piece]]:
    """One of the doctor's facts as the pieces the Health page draws.

    ``doctor`` hands the viewer whatever a check happens to know -- a path, a flag, a
    list of names, or a map of those per provider -- so the shape of the value picks its
    rendering and no section has to know its own keys. The answer is the line you read
    first and the quiet line under it: a per-item map splits across both, anything else
    is a single piece.
    """
    if isinstance(value, Mapping):
        return _item(value)
    return [_piece(value, key)], []


def _item(item: Mapping) -> tuple[list[Piece], list[Piece]]:
    """A map of one thing's facts: what it is, then what is true of it.

    A flag reads as its own key, because ``executable`` says more under a provider's
    path than ``yes`` does. With nothing to lead on, as a transport has, the flags
    become the line themselves rather than leaving an empty one above them.
    """
    lead: list[Piece] = []
    quiet: list[Piece] = []
    for key, value in item.items():
        label = fact_label(key)
        if isinstance(value, bool):
            quiet.append(("word", label if value else f"not {label}"))
        elif isinstance(value, str) or not isinstance(value, Sequence):
            lead.append(_piece(value, key))
        else:
            names = _names(value)
            quiet.append(("names", names) if names else ("quiet", f"no {label}"))
    return (lead, quiet) if lead else (quiet, [])


def _piece(value: object, key: str = "") -> Piece:
    if value is None:
        return "quiet", "none"
    if isinstance(value, bool):
        return "word", "yes" if value else "no"
    if isinstance(value, str):
        path = key in FILE_KEYS or value.startswith(PATH_STARTS)
        return ("path", value) if path else ("word", value)
    if isinstance(value, Sequence):
        names = _names(value)
        return ("names", names) if names else ("quiet", "none")
    return "word", str(value)


def _names(values: Sequence) -> str:
    return ", ".join(str(value) for value in values)


def fact_label(key: str) -> str:
    """A doctor's key as a label: its own name, minus the underscores."""
    return key.replace("_", " ")


FILTERS: dict[str, Callable] = {
    "bytes": human_bytes,
    "duration": duration,
    "when": when,
    "clock": clock,
    "iso": iso,
    "ago": ago,
    "since": since,
    "until": until,
    "heartbeat_next": heartbeat_next,
    "status_class": status_class,
    "status_word": status_word,
    "heartbeat_event_word": heartbeat_event_word,
    "heartbeat_actor": heartbeat_actor,
    "task_tone": lambda state: task_tone(state),
    "fact": fact,
    "fact_label": fact_label,
    # The only filter that emits markup: it wraps nothing but the safe renderer's output.
    "output_markdown": files.render_output,
}


def task_tone(state: str) -> str:
    """The dot tone for a task state (``needs-you``, ``blocked``, ``active``, ...)."""
    return TASK_TONES.get(state, "muted")


def _attempt[T](work: Callable[[], T]) -> tuple[T | None, str | None]:
    """``(result, None)`` or ``(None, message)``: one source's failure stays local."""
    try:
        return work(), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _config(paths: Paths) -> tuple[Config | None, list[str]]:
    config, problems, _warnings = check_config(paths)
    return config, problems


# -- Run series (bars, sparklines, and the schedule chart) ----------------------

FAILED = ("error", "timeout", "prerun_error", "cancelled")
BAR_RUNS = 30  # outcomes behind a reliability bar chart
BAR_HEIGHT = 22  # SVG user units, matching svg.bars in app.css
SPARK_RUNS = 18  # outcomes behind a list-row sparkline
QUIET_BAR = 5  # a no-work poll is a stub, not a full bar


def bar_series(summaries: list[runs.RunSummary]) -> list[tuple[str, int]]:
    """``(tone, height)`` per run, oldest first, height proportional to duration."""
    recent = list(reversed(summaries[:BAR_RUNS]))
    longest = max((summary.duration_ms or 0 for summary in recent), default=0)
    series = []
    for summary in recent:
        if summary.status in ("no_work", "skipped"):
            height = QUIET_BAR
        elif longest:
            height = max(6, round((summary.duration_ms or 0) / longest * BAR_HEIGHT))
        else:
            height = BAR_HEIGHT
        series.append((status_class(summary.status), height))
    return series


def spark_tones(summaries: list[runs.RunSummary]) -> list[str]:
    """Just the tones, oldest first: the same history at list-row size."""
    return [status_class(summary.status) for summary in reversed(summaries[:SPARK_RUNS])]


def failures(summaries: Sequence[runs.RunSummary]) -> list[runs.RunSummary]:
    return [summary for summary in summaries if summary.status in FAILED]


# -- Today ----------------------------------------------------------------------

TODAY_SECTIONS = ("schedule", "activity", "reliability")
TODAY_BACK_HOURS = 24
TODAY_AHEAD_HOURS = 2
CHART_RANGES = (6, 12, 24)
CHART_BACK_HOURS = 6
TODAY_SCAN = 400  # runs read for the window; retention is normally far below this
CHART_WIDTH = 1000  # SVG user units; see the CSP note in _macros.html
CHART_MIN_EVENT = 6  # so a one-second run is still a visible mark
CHART_MAX_GHOSTS = 40
TICK_EVERY = {6: 1, 12: 2, 24: 3}
UP_NEXT = 7


@dataclass(frozen=True)
class ChartEvent:
    """One run on a lane, placed in the chart's user-unit space."""

    x: float
    width: float
    tone: str
    label: str
    run_id: str


@dataclass(frozen=True)
class ChartLane:
    job: str
    title: str
    schedule: str
    events: list[ChartEvent]
    ghosts: list[float]  # scheduled slots in the forward strip, bounded per lane


@dataclass(frozen=True)
class FeedItem:
    """Either one run worth reading, or a fold of consecutive no-work polls."""

    runs: list[runs.RunSummary]

    @property
    def folded(self) -> bool:
        return self.runs[0].status in ("no_work", "skipped")

    @property
    def jobs(self) -> list[str]:
        seen = dict.fromkeys(summary.job for summary in self.runs)
        return list(seen)


@dataclass(frozen=True)
class FeedGroup:
    """Runs under one hour heading, or a single unlabelled group when not grouping."""

    label: str | None
    total: int
    failed: int
    items: list[FeedItem]


def _feed(summaries: Sequence[runs.RunSummary], *, by_hour: bool) -> list[FeedGroup]:
    """Group by hour and fold consecutive no-work polls into one line each."""
    if not summaries:
        return []
    groups: list[FeedGroup] = []
    for label, bucket in _buckets(summaries, by_hour=by_hour):
        items: list[FeedItem] = []
        for summary in bucket:
            quiet = summary.status in ("no_work", "skipped")
            if quiet and items and items[-1].folded:
                items[-1].runs.append(summary)
            else:
                items.append(FeedItem([summary]))
        groups.append(
            FeedGroup(
                label,
                len(bucket),
                sum(1 for summary in bucket if summary.status in FAILED),
                items,
            )
        )
    return groups


def _buckets(
    summaries: Sequence[runs.RunSummary], *, by_hour: bool
) -> list[tuple[str | None, list[runs.RunSummary]]]:
    if not by_hour:
        return [(None, list(summaries))]
    today = datetime.now().astimezone().date()
    out: list[tuple[str | None, list[runs.RunSummary]]] = []
    for summary in summaries:
        moment = _moment(summary.started_at)
        if moment is None:
            label = "?"
        else:
            local = moment.astimezone()
            # The day only appears when it is not today, so the common case stays terse.
            label = local.strftime("%H:00" if local.date() == today else "%a %d, %H:00")
        if not out or out[-1][0] != label:
            out.append((label, []))
        out[-1][1].append(summary)
    return out


def today_model(
    paths: Paths, section: str, chart_back_hours: int = CHART_BACK_HOURS
) -> dict[str, Any]:
    """The landing page: what is scheduled, what happened, and how reliable it has been."""
    section = section if section in TODAY_SECTIONS else "schedule"
    if chart_back_hours not in CHART_RANGES:
        chart_back_hours = CHART_BACK_HOURS
    config, problems = _config(paths)
    now = datetime.now().astimezone()
    rows, jobs_error = _job_rows(paths, config, now)
    scanned, runs_error = _attempt(partial(runs.list_summaries, paths, limit=TODAY_SCAN))
    history = scanned or []
    activity, activity_error = _attempt(partial(beatviews.activity, paths, limit=TODAY_SCAN))
    beat_attention, beats_error = _attempt(
        partial(beatviews.beat_rows, paths, attention=True, limit=UP_NEXT)
    )
    attention_count, _attention_error = _attempt(
        partial(beatviews.beat_count, paths, attention=True)
    )
    beat_upcoming, upcoming_error = _attempt(
        partial(beatviews.beat_rows, paths, upcoming=True, limit=UP_NEXT)
    )

    top_of_hour = now.replace(minute=0, second=0, microsecond=0)
    start = top_of_hour - timedelta(hours=TODAY_BACK_HOURS)
    # Pin to the next hour so the real lookahead never shrinks below two hours.
    end = top_of_hour + timedelta(hours=TODAY_AHEAD_HOURS + 1)
    chart_start = end - timedelta(hours=chart_back_hours + TODAY_AHEAD_HOURS)
    window = [
        summary
        for summary in (activity or [])
        if (moment := _moment(summary.started_at)) is not None and moment.astimezone() >= start
    ]
    recent = _by_job(history)
    upcoming: list[dict[str, Any]] = [
        {
            "title": row.dir_name,
            "href": "/jobs/" + quote(row.dir_name, safe=""),
            "next_run": row.next_run,
            "timing": _schedule_note(row.job),
            "workspace": row.job.workspace if row.job else "-",
            "source": "job",
        }
        for row in rows
        if row.next_run
    ]
    if config and config.heartbeat.enabled:
        upcoming.extend(
            {
                "title": row.title,
                "href": "/heartbeats/" + row.ref,
                "next_run": _moment(row.next_check_at),
                "timing": row.ref,
                "workspace": row.workspace,
                "source": "heartbeat",
            }
            for row in (beat_upcoming or [])
        )
    upcoming.sort(key=lambda row: row["next_run"] or now)
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "section": section,
        "now": now,
        "back_hours": TODAY_BACK_HOURS,
        "ahead_hours": TODAY_AHEAD_HOURS,
        "chart_back_hours": chart_back_hours,
        "chart_ranges": CHART_RANGES,
        "chart_query": f"?range={chart_back_hours}" if chart_back_hours != CHART_BACK_HOURS else "",
        "reliability_limit": BAR_RUNS,
        "scan_limit": TODAY_SCAN,
        "failed": failures(window),
        "job_failed": failures([summary for summary in window if summary.source == "jobs"]),
        "window_total": len(window),
        "window_notable": sum(
            1 for summary in window if summary.status not in ("no_work", "skipped")
        ),
        "chart": _chart(rows, recent, chart_start, end, now, TICK_EVERY[chart_back_hours]),
        "feed": _feed(window, by_hour=True),
        "reliability": [
            {
                "row": row,
                "runs": summaries,
                "failed": len(failures(summaries)),
                "average": _average(summaries),
            }
            for row in rows
            if (summaries := recent.get(row.dir_name, [])[:BAR_RUNS])
        ],
        "upcoming": upcoming[:UP_NEXT],
        "beat_attention": beat_attention or [],
        "beat_attention_count": attention_count or 0,
        "beats_error": beats_error or upcoming_error,
        "heartbeat_enabled": config.heartbeat.enabled if config else None,
        "error": jobs_error,
        "runs_error": runs_error or activity_error,
    }


def _by_job(summaries: list[runs.RunSummary]) -> dict[str, list[runs.RunSummary]]:
    """One pass over the history instead of a query per job; newest first within each."""
    grouped: dict[str, list[runs.RunSummary]] = {}
    for summary in summaries:
        grouped.setdefault(summary.job, []).append(summary)
    return grouped


def _average(summaries: list[runs.RunSummary]) -> int | None:
    timed = [summary.duration_ms for summary in summaries if summary.duration_ms]
    return round(sum(timed) / len(timed)) if timed else None


def _chart(
    rows: list[JobRow],
    recent: dict[str, list[runs.RunSummary]],
    start: datetime,
    end: datetime,
    now: datetime,
    tick_every: int,
) -> dict[str, Any]:
    """Lanes, gridlines and tick labels, all in the SVG's 0..CHART_WIDTH user space."""
    span = (end - start).total_seconds()
    if span <= 0:  # a clock that moved backwards should not divide by zero
        return {"lanes": [], "columns": [], "gridlines": [], "now_x": 0.0, "width": CHART_WIDTH}

    def place(moment: datetime) -> float:
        return (moment - start).total_seconds() / span * CHART_WIDTH

    lanes = []
    for row in rows:
        events = []
        for summary in recent.get(row.dir_name, []):
            moment = _moment(summary.started_at)
            if moment is None or moment.astimezone() < start:
                continue
            seconds = (summary.duration_ms or 0) / 1000
            width = max(CHART_MIN_EVENT, seconds / span * CHART_WIDTH)
            events.append(
                ChartEvent(
                    round(place(moment.astimezone()), 2),
                    round(width, 2),
                    status_class(summary.status),
                    f"{status_word(summary.status)} at {clock(summary.started_at)}"
                    f", {duration(summary.duration_ms)}",
                    summary.id,
                )
            )
        ghosts = (
            [
                round(place(slot), 2)
                for slot in cron_slots(row.job.schedule, now, until=end, limit=CHART_MAX_GHOSTS)
            ]
            if row.next_run is not None and row.job and row.job.schedule
            else []
        )
        if events or ghosts:
            lanes.append(
                ChartLane(
                    row.dir_name,
                    row.dir_name,
                    _schedule_note(row.job),
                    events,
                    ghosts,
                )
            )

    # One cell per whole hour in the window; the labelled ones carry the hour.
    hours = int(span / 3600)
    columns = [
        (start + timedelta(hours=index)).strftime("%H")
        if (start + timedelta(hours=index)).hour % tick_every == 0
        else ""
        for index in range(hours)
    ]
    return {
        "lanes": lanes,
        "columns": columns,
        "gridlines": [round(place(start + timedelta(hours=index)), 2) for index in range(1, hours)],
        "now_x": round(place(now), 2),
        "width": CHART_WIDTH,
    }


def _schedule_note(job: Job | None) -> str:
    if job is None:
        return "unreadable"
    return job.schedule or "when work is ready"  # a stage job may carry no cron line


def _alarm(paths: Paths) -> bool:
    """Whether anything failed in the last day: the one signal every page carries.

    One indexed row, so the shell can show it without every page running the doctor.
    """
    attention, _error = _attempt(partial(beatviews.beat_count, paths, attention=True))
    if attention:
        return True
    recent, _error = _attempt(partial(beatviews.activity, paths, statuses=FAILED, limit=1))
    if not recent:
        return False
    moment = _moment(recent[0].started_at)
    return moment is not None and (
        datetime.now(UTC) - moment.astimezone(UTC) < timedelta(hours=TODAY_BACK_HOURS)
    )


# -- Health ---------------------------------------------------------------------


HEALTH_SECTIONS = ("doctor", "log")


def health_model(paths: Paths, bind: Bind | None, section: str = "doctor") -> dict[str, Any]:
    report, error = _attempt(partial(doctor.run, paths))
    problems = report.section("config").problems if report else _config(paths)[1]
    findings = (
        sum(len(entry.problems) + len(entry.warnings) for entry in report.sections) if report else 0
    )
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "section": section if section in HEALTH_SECTIONS else "doctor",
        "findings": findings,
        "report": report,
        "report_error": error,
        "database": _database(paths),
        "log": _log_tail(paths),
        "viewer": {
            "pid": os.getpid(),
            "url": bind.url if bind else None,
            "host": bind.host if bind else None,
            "port": bind.port if bind else None,
            "loopback": bind.loopback if bind else True,
            "home": str(paths.home),
        },
    }


def _database(paths: Paths) -> dict[str, Any]:
    found = []
    for suffix in DB_COMPANIONS:
        path = paths.db.with_name(paths.db.name + suffix)
        try:
            found.append((path.name, path.stat().st_size))
        except OSError:
            continue
    state: dict[str, Any] = {
        "path": str(paths.db),
        "exists": paths.db.exists(),
        "files": found,
        "total": sum(size for _name, size in found),
        "schema": None,
        "state": "missing",
        "error": None,
    }
    if not state["exists"]:
        return state
    try:
        with db.reader(paths) as con:
            state["schema"] = con.execute("PRAGMA user_version").fetchone()[0]
            state["runs"] = con.execute("SELECT count(*) FROM runs").fetchone()[0]
        state["state"] = "ok"
    except db.MissingDatabaseError as exc:
        state["error"] = str(exc)
    except db.UnreadableDatabaseError as exc:
        state["state"] = "error"
        state["error"] = str(exc)
    return state


def _log_tail(paths: Paths) -> dict[str, Any]:
    if not paths.log.exists():
        return {"path": str(paths.log), "exists": False, "lines": [], "error": None}
    lines, error = _attempt(partial(logsetup.tail, paths, LOG_TAIL_LINES))
    return {"path": str(paths.log), "exists": True, "lines": lines or [], "error": error}


# -- Workspaces -----------------------------------------------------------------


def workspaces_model(paths: Paths) -> dict[str, Any]:
    config, problems = _config(paths)
    report, error = _attempt(partial(audit.audit, paths, config=config))
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "home": report.home if report else None,
        "workspaces": report.workspaces if report else [],
        "error": error,
    }


def workspace_model(paths: Paths, name: str) -> dict[str, Any] | None:
    if not valid_workspace_name(name) or not paths.workspace(name).is_dir():
        return None
    config, problems = _config(paths)
    report, error = _attempt(partial(audit.audit, paths, [name], config=config))
    roots = []
    for root in files.ROOTS:
        directory = paths.workspace(name) / root
        entries, size = files.tree_summary(directory) if directory.is_dir() else (0, 0)
        roots.append(
            {"name": root, "is_dir": directory.is_dir(), "entries": entries, "bytes": size}
        )
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "name": name,
        "path": str(paths.workspace(name)),
        "workspace": report.workspaces[0] if report else None,
        "home": report.home if report else None,
        "roots": roots,
        "error": error,
    }


def files_model(
    paths: Paths, name: str, root: str, relative: str, *, raw: bool = False
) -> dict[str, Any] | None:
    """A directory listing or a file view; None for anything that is not there or allowed."""
    try:
        resolved = files.resolve(paths, name, root, relative)
    except files.PathRejectedError, FileNotFoundError, OSError:
        return None
    model: dict[str, Any] = {
        "config_problems": _config(paths)[1],
        "alarm": _alarm(paths),
        "workspace": name,
        "root": root,
        "relative": "/".join(files.split_relative(relative)),
        "crumbs": files.crumbs(relative),
        "listing": None,
        "file": None,
        "raw": raw,
        "error": None,
    }
    target = resolved.target
    if not target.exists():
        return None
    if target.is_dir():
        model["listing"], model["error"] = _attempt(
            partial(files.listing, resolved, root, relative)
        )
    else:
        model["file"], model["error"] = _attempt(
            partial(files.view, resolved, root, relative, raw=raw)
        )
    return model


# -- Skills ---------------------------------------------------------------------


@dataclass
class SkillRow:
    """One skill name, and every workspace whose agent can reach it.

    Listing a skill once per workspace produced hundreds of near-identical rows on a
    home with a handful of workspaces; the interesting facts are the name, its scope,
    whether it collides, and how widely it reaches.
    """

    name: str
    scope: str
    description: str
    path: str
    status: str
    notes: list[str]
    workspaces: list[str]


@dataclass(frozen=True)
class SkillGroup:
    """One heading on the Skills list: where the skills under it come from.

    A workspace is a directory inside the Enso home rather than a scope of its own, so
    its heading is qualified; the heading draws ``prefix`` quieter than the name itself.
    """

    label: str
    rows: list[SkillRow]
    prefix: str = ""


def skills_model(paths: Paths, selected: str | None) -> dict[str, Any] | None:
    names = workspaces.list_workspaces(paths)
    if selected is not None and selected not in names:
        return None
    shown: list[str | None] = [selected] if selected else list(names) or [None]
    grouped: dict[tuple[str, str, str], SkillRow] = {}
    error: str | None = None
    for workspace in shown:
        resolved, failure = _attempt(partial(skills.resolve, paths, workspace))
        error = error or failure
        for skill in resolved or []:
            key = (skill.name, skill.scope, str(skill.path))
            row = grouped.get(key)
            if row is None:
                row = SkillRow(
                    skill.name,
                    skill.scope,
                    skill.description or "",
                    str(skill.path),
                    skill_status(skill),
                    _skill_notes(skill),
                    [],
                )
                grouped[key] = row
            if workspace is not None:
                row.workspaces.append(workspace)
    rows = sorted(grouped.values(), key=lambda row: (_scope_order(row.scope), row.name))
    return {
        "config_problems": _config(paths)[1],
        "alarm": _alarm(paths),
        "workspaces": names,
        "selected": selected,
        "rows": rows,
        "groups": _skill_groups(rows),
        "reach": len(shown) if selected is None else 1,
        "errors": sum(1 for row in rows if row.status == "error"),
        "warnings": sum(1 for row in rows if row.status == "warning"),
        "error": error,
    }


def _skill_groups(rows: list[SkillRow]) -> list[SkillGroup]:
    """The rows under where they come from: each workspace by name, then enso, then user.

    A workspace-scope skill sits in one workspace's ``skills/`` directory and the row key
    includes that path, so only that workspace's resolution can reach the row and its
    ``workspaces`` list holds exactly that one name. A group with nothing in it is never
    created, so an absent scope simply has no heading.
    """
    owned: dict[str, list[SkillRow]] = {}
    shared: dict[str, list[SkillRow]] = {}
    for row in rows:
        if row.scope == "workspace" and row.workspaces:
            owned.setdefault(row.workspaces[0], []).append(row)
        else:
            shared.setdefault(row.scope, []).append(row)
    groups = [SkillGroup(name, owned[name], "enso /") for name in sorted(owned)]
    groups += [SkillGroup(scope, shared[scope]) for scope in skills.SCOPES if scope in shared]
    return groups


def skill_model(paths: Paths, name: str, selected: str | None) -> dict[str, Any] | None:
    """Every entry providing this skill name, across scopes; None when nothing does.

    A collision is two entries sharing a name, so the page is keyed by the name and
    lists what claims it. That makes the Skills row able to drop its notes and still
    have somewhere to send you.
    """
    listing = skills_model(paths, selected)
    if listing is None:
        return None
    matches = [row for row in listing["rows"] if row.name == name]
    if not matches:
        return None
    return {
        "config_problems": listing["config_problems"],
        "alarm": listing["alarm"],
        "name": name,
        "selected": selected,
        "rows": matches,
        "reach": listing["reach"],
        "status": (
            "error"
            if any(row.status == "error" for row in matches)
            else "warning"
            if any(row.status == "warning" for row in matches)
            else "active"
        ),
        "error": listing["error"],
    }


def _scope_order(scope: str) -> int:
    order = {scope: index for index, scope in enumerate(skills.SCOPES)}
    return order.get(scope, len(order))


def _skill_notes(skill: skills.Skill) -> list[str]:
    notes = list(skill.problems)
    if skill.collision:
        notes.append(
            f"collides with the {' and '.join(skill.collides_with)} scope ({skill.collision})"
        )
    return notes


def skill_status(skill: skills.Skill) -> str:
    """One word per row: ``error``, ``warning``, or ``active``."""
    if skill.problems or skill.collision == skills.ERROR:
        return "error"
    if skill.collision == skills.WARNING:
        return "warning"
    return "active"


# -- Jobs -----------------------------------------------------------------------


@dataclass
class JobRow:
    """A job directory as listed: parsed or not, with its latest run and next slot."""

    dir_name: str
    job: Job | None
    problems: list[str]
    last: runs.RunSummary | None
    next_run: datetime | None


def _next_run(job: Job | None, problems: list[str], now: datetime) -> datetime | None:
    if job is None or problems or not job.enabled:
        return None
    try:
        return job.next_run(now)
    except Exception:  # an unparsable schedule is already listed as a problem
        return None


def _job_rows(
    paths: Paths, config: Config | None, now: datetime
) -> tuple[list[JobRow], str | None]:
    """Every job directory as a row, parsed or not. Shared by Jobs and Today."""
    loaded, error = _attempt(partial(load_jobs, paths, config))
    found, job_problems = loaded if loaded is not None else ([], {})
    summaries, _runs_error = _attempt(partial(runs.latest_summaries, paths))
    latest = summaries or {}
    rows = [
        JobRow(
            job.dir_name,
            job,
            job_problems.get(job.dir_name, []),
            latest.get(job.dir_name),
            _next_run(job, job_problems.get(job.dir_name, []), now),
        )
        for job in found
    ]
    parsed = {job.dir_name for job in found}
    rows.extend(
        JobRow(name, None, found_problems, latest.get(name), None)
        for name, found_problems in job_problems.items()
        if name not in parsed
    )
    return sorted(rows, key=lambda row: row.dir_name), error


def jobs_model(paths: Paths) -> dict[str, Any]:
    config, problems = _config(paths)
    now = datetime.now().astimezone()
    rows, error = _job_rows(paths, config, now)
    scanned, runs_error = _attempt(partial(runs.list_summaries, paths, limit=TODAY_SCAN))
    recent = _by_job(scanned or [])
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "rows": rows,
        "recent": recent,
        "error": error,
        "runs_error": runs_error,
        "now": now,
    }


JOB_SECTIONS = ("overview", "history")


def job_model(paths: Paths, name: str, section: str = "overview") -> dict[str, Any] | None:
    config, problems = _config(paths)
    loaded, error = _attempt(partial(load_jobs, paths, config))
    found, job_problems = loaded if loaded is not None else ([], {})
    job = next((candidate for candidate in found if candidate.dir_name == name), None)
    if job is None and name not in job_problems:
        return None
    limit = runs.PAGE_SIZE if section == "history" else RECENT_RUNS
    summaries, runs_error = _attempt(partial(runs.list_summaries, paths, job=name, limit=limit))
    recent = summaries or []
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "section": section if section in JOB_SECTIONS else "overview",
        "groups": _feed(recent, by_hour=False),
        "failed": len(failures(recent)),
        "average": _average(recent),
        "name": name,
        "job": job,
        "problems": job_problems.get(name, []),
        # JOB.md's body is Markdown, so the page renders it rather than showing the source.
        "prompt": files.render_markdown(job.prompt) if job and job.prompt else None,
        "last": recent[0] if recent else None,
        "recent": recent,
        "next_run": _next_run(job, job_problems.get(name, []), datetime.now().astimezone()),
        "error": error,
        "runs_error": runs_error,
        "runs_query": urlencode({"job": name}),
    }


# -- Runs -----------------------------------------------------------------------


HEARTBEAT_SECTIONS = ("overview", "history", "runs")


def _requested_page(query: Mapping[str, str]) -> int:
    raw = query.get("page", "1")
    return max(1, int(raw)) if len(raw) < 9 and raw.isascii() and raw.isdecimal() else 1


def _beat_page(
    query: Mapping[str, str], total: int, base: str, **filters: str | None
) -> dict[str, Any]:
    number = _requested_page(query)
    pages = max(1, ceil(total / beatviews.PAGE_SIZE))
    page = min(max(1, number), pages)

    def link(number: int) -> str | None:
        if not 1 <= number <= pages:
            return None
        values = {key: value for key, value in filters.items() if value}
        if number > 1:
            values["page"] = str(number)
        return base + ("?" + urlencode(values) if values else "")

    return {
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": beatviews.PAGE_SIZE,
        "start": (page - 1) * beatviews.PAGE_SIZE + 1 if total else 0,
        "end": min(page * beatviews.PAGE_SIZE, total),
        "prev_link": link(page - 1),
        "next_link": link(page + 1),
    }


def heartbeats_model(paths: Paths, query: Mapping[str, str]) -> dict[str, Any]:
    """A bounded current/previous list; every row is a report of saved beat state."""
    config, problems = _config(paths)
    view = "previous" if query.get("view") == "previous" else "current"
    state = query.get("state") or None
    if state not in beatviews.state_choices(view):
        state = None
    attention = query.get("attention") == "1"
    total, error = _attempt(
        partial(beatviews.beat_count, paths, view=view, state=state, attention=attention)
    )
    pagination = _beat_page(
        query,
        total or 0,
        "/heartbeats",
        view=view if view == "previous" else None,
        state=state,
        attention="1" if attention else None,
    )
    rows, rows_error = _attempt(
        partial(
            beatviews.beat_rows,
            paths,
            view=view,
            state=state,
            attention=attention,
            limit=beatviews.PAGE_SIZE,
            offset=(pagination["page"] - 1) * beatviews.PAGE_SIZE,
        )
    )
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "view": view,
        "state": state,
        "attention": attention,
        "states": beatviews.state_choices(view),
        "rows": rows or [],
        "heartbeat_enabled": config.heartbeat.enabled if config else None,
        "retention_days": config.heartbeat.retention_days if config else None,
        "error": error or rows_error,
        **pagination,
    }


def heartbeat_model(
    paths: Paths, ref: str, section: str = "overview", query: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """Current instructions with a paginated meaningful history and separate run summaries."""
    try:
        beat, error = _attempt(partial(beats.get, paths, ref))
    except beats.HeartbeatError:
        return None
    if beat is None and (error is None or error.startswith("HeartbeatError:")):
        return None
    config, problems = _config(paths)
    model = {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "beat": beat,
        "error": error,
        "section": section,
        "heartbeat_enabled": config.heartbeat.enabled if config else None,
        "events": [],
        "recent": [],
        "latest_event": None,
        "history_count": 0,
        "runs_count": 0,
    }
    if beat is None:
        return model
    history, history_error = _attempt(partial(beatviews.events, paths, beat.id, limit=1))
    history_count, latest = history or (0, [])
    runs_count, runs_error = _attempt(partial(beatviews.activity_count, paths, beat_ref=beat.ref))
    model.update(
        history_count=history_count,
        runs_count=runs_count or 0,
        latest_event=latest[0] if latest else None,
        error=error or history_error or runs_error,
        checkpoint_text=json.dumps(beat.checkpoint, ensure_ascii=False, indent=2),
    )
    base = "/heartbeats/" + beat.ref
    total = (runs_count or 0) if section == "runs" else history_count
    pagination = _beat_page(query or {}, total, base + "/" + section)
    model.update(pagination)
    if section == "history":
        result, error = _attempt(
            partial(
                beatviews.events,
                paths,
                beat.id,
                limit=beatviews.PAGE_SIZE,
                offset=(pagination["page"] - 1) * beatviews.PAGE_SIZE,
            )
        )
        model["events"] = result[1] if result else []
        model["error"] = model["error"] or error
    elif section == "runs":
        recent, error = _attempt(
            partial(
                beatviews.activity,
                paths,
                beat_ref=beat.ref,
                limit=beatviews.PAGE_SIZE,
                offset=(pagination["page"] - 1) * beatviews.PAGE_SIZE,
            )
        )
        model["recent"] = recent or []
        model["error"] = model["error"] or error
    return model


def heartbeat_run_model(paths: Paths, run_id: str) -> dict[str, Any] | None:
    """A single saved assessment, including the definition and input boundary it saw."""
    run, error = _attempt(partial(beats.get_run, paths, run_id))
    if run is None and error is None:
        return None
    return {
        "config_problems": _config(paths)[1],
        "alarm": _alarm(paths),
        "run": run,
        "error": error,
    }


RUN_VIEWS = ("signal", "failed", "all")
RUN_VIEW_LABELS = {"signal": "Acted", "failed": "Failed", "all": "All"}


def runs_model(paths: Paths, query: Mapping[str, str]) -> dict[str, Any]:
    """One page of summaries under the ``view``, ``job`` and ``status`` filters.

    ``view`` is the headline filter: ``signal`` (the default) leaves out the polls that
    found no work, which on a home with a five-minute job are the overwhelming majority
    of retained runs and bury everything that did something. ``all`` restores them,
    folded, and ``status`` still narrows to exactly one outcome.
    """
    view = query.get("view", "").strip() or "signal"
    if view not in RUN_VIEWS:
        view = "signal"
    job = query.get("job", "").strip() or None
    source = query.get("source", "any")
    if source not in ("any", "jobs", "heartbeat"):
        source = "any"
    if source == "heartbeat":
        job = None
    status = query.get("status", "").strip() or None
    if status is not None and status not in beatviews.RUN_STATUSES:
        status = None
    if status is not None:  # an explicit status is more specific than the view
        view = "all"
    page = _requested_page(query)
    wanted = _view_statuses(view)
    counted, error = _attempt(
        partial(
            beatviews.activity_count, paths, source=source, job=job, status=status, statuses=wanted
        )
    )
    total = counted or 0
    pages = max(1, ceil(total / runs.PAGE_SIZE))
    page = min(page, pages)
    rows: list[beatviews.ActivityRun] = []
    if error is None:
        listed, error = _attempt(
            partial(
                beatviews.activity,
                paths,
                source=source,
                job=job,
                status=status,
                statuses=wanted,
                limit=runs.PAGE_SIZE,
                offset=(page - 1) * runs.PAGE_SIZE,
            )
        )
        rows = listed or []
    known, _names_error = _attempt(partial(runs.job_names, paths))
    names = known or []
    if job and job not in names:
        names = sorted([*names, job])

    def link(number: int, for_view: str = view) -> str | None:
        if number < 1 or number > pages:
            return None
        params = {
            key: value
            for key, value in (
                ("view", for_view if for_view != "signal" else None),
                ("source", source if source != "any" else None),
                ("job", job),
                ("status", status),
            )
            if value
        }
        if number > 1:
            params["page"] = str(number)
        return f"/runs?{urlencode(params)}" if params else "/runs"

    counts = _view_counts(paths, job, source)
    start = (page - 1) * runs.PAGE_SIZE + 1 if rows else 0
    return {
        "config_problems": _config(paths)[1],
        "alarm": _alarm(paths),
        "rows": rows,
        "groups": _feed(rows, by_hour=view == "all"),
        "total": total,
        "page": page,
        "pages": pages,
        "start": start,
        "end": start + len(rows) - 1 if rows else 0,
        "page_size": runs.PAGE_SIZE,
        "view": view,
        "view_tabs": [
            (
                RUN_VIEW_LABELS[name],
                link(1, name) or "/runs",
                counts.get(name),
                "error" if name == "failed" else None,
            )
            for name in RUN_VIEWS
        ],
        "view_current": link(1, view) or "/runs",
        "job": job,
        "source": source,
        "status": status,
        "job_names": names,
        "statuses": beatviews.RUN_STATUSES,
        "prev_link": link(page - 1),
        "next_link": link(page + 1),
        "error": error,
    }


def _view_statuses(view: str) -> tuple[str, ...] | None:
    if view == "failed":
        return FAILED
    if view == "signal":
        return tuple(
            status for status in beatviews.RUN_STATUSES if status not in ("no_work", "skipped")
        )
    return None


def _view_counts(paths: Paths, job: str | None, source: str = "any") -> dict[str, int | None]:
    """The tab counts, under the job filter but not the view being counted."""
    counts: dict[str, int | None] = {}
    for name in RUN_VIEWS:
        counted, _error = _attempt(
            partial(
                beatviews.activity_count,
                paths,
                source=source,
                job=job,
                statuses=_view_statuses(name),
            )
        )
        counts[name] = counted
    return counts


def run_model(paths: Paths, run_id: str) -> dict[str, Any] | None:
    run, error = _attempt(partial(runs.get, paths, run_id))
    if run is None and error is None:
        return None
    attempts, attempt_error = _attempt(partial(runs.attempts, paths, run.id)) if run else ([], None)
    config, problems = _config(paths)
    timeout = _job_timeout(paths, config, run.job) if run else None
    related, _tasks_error = _attempt(partial(tasks.run_tasks, paths, run.id)) if run else ([], None)
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "run": run,
        "attempts": attempts or [],
        "timeout": timeout,
        "tasks": related or [],
        "error": error or attempt_error,
    }


def _job_timeout(paths: Paths, config: Config | None, name: str) -> int | None:
    """The job's allowed seconds, when its JOB.md is still there and readable."""
    loaded, _error = _attempt(partial(load_jobs, paths, config))
    found, _problems = loaded if loaded is not None else ([], {})
    job = next((candidate for candidate in found if candidate.dir_name == name), None)
    return job.timeout if job else None


# -- Tasks ----------------------------------------------------------------------


# The board reads top to bottom: what needs a person first, then what the agents hold,
# then what is waiting, then what is finished. Each entry is a group's key, its heading,
# and the qualifier the heading adds after the count.
BOARD_GROUPS = (
    ("blocked", "Blocked", "needs you"),
    ("active", "Active", ""),
    ("ready", "Ready", ""),
    ("backlog", "Backlog", ""),
    ("done", "Done", ""),
)
# Which group lists a task, by the state ``_task_state`` gives it. Every state maps to
# exactly one group, so a task is on the board once and never in two places.
TASK_GROUP_OF = {
    "blocked": "blocked",
    "needs-you": "blocked",
    "active": "active",
    "ready": "ready",
    "backlog": "backlog",
    "done": "done",
    "cancelled": "done",
}
# The dot is the row's whole state; these are the tones it takes.
TASK_TONES = {
    "needs-you": "warning",
    "blocked": "error",
    "active": "running",
    "ready": "ok",
    "backlog": "muted",
    "done": "ok",
    "cancelled": "muted",
}
DONE_WINDOW = timedelta(days=7)  # the count line says this week's finishes; the list shows more
DONE_LIMIT = 200
GIT_TIMEOUT = 5.0
_STAGE_NAME = re.compile(r"[a-z][a-z0-9-]{0,23}")


@dataclass(frozen=True)
class TaskRow:
    """One task as listed: its state for the dot, and the project's name for the detail line."""

    task: tasks.Task
    state: str
    project_name: str


@dataclass(frozen=True)
class TaskGroup:
    label: str
    rows: list[TaskRow]
    note: str = ""  # a qualifier the heading adds after the count


@dataclass(frozen=True)
class TimelineEntry:
    """One event as a row: a label for what happened, and whether its run can be opened."""

    event: tasks.TaskEvent
    label: str
    tone: str
    run: str | None  # ``live`` when the run row exists, ``pruned`` when it is gone


def _project_of(config: Config | None, task: tasks.Task) -> ProjectConfig | None:
    return config.projects.get(task.project) if config else None


def _task_state(task: tasks.Task, project: ProjectConfig | None) -> str:
    if task.finished:
        return task.stage
    if task.stage == "blocked":
        return "blocked"
    stage = project.stage(task.stage) if project else None
    if (stage and stage.human) or task.attention:
        return "needs-you"
    if task.claim_run_id:
        return "active"
    if task.stage == "backlog":
        return "backlog"
    return "ready" if stage else "needs-you"


def _task_row(config: Config | None, task: tasks.Task) -> TaskRow:
    project = _project_of(config, task)
    return TaskRow(task, _task_state(task, project), project.name if project else task.project)


def _ready_order(config: Config | None, row: TaskRow) -> tuple[str, int, str]:
    """Ready reads down the pipeline: by project, then the project's stage order."""
    project = _project_of(config, row.task)
    index = project.index(row.task.stage) if project and project.stage(row.task.stage) else 999
    return (row.task.project, index, row.task.stage)


def _board_groups(
    config: Config | None, live: list[TaskRow], finished: list[TaskRow]
) -> list[TaskGroup]:
    """The five groups in reading order, each sorted its own way; an empty group is dropped.

    Every task lands in one group, chosen by its state, so the board never lists the same
    task twice. The finished rows arrive newest first from the query and keep that order.
    """
    held: dict[str, list[TaskRow]] = {key: [] for key, _label, _note in BOARD_GROUPS}
    for row in live:
        held[TASK_GROUP_OF[row.state]].append(row)
    held["blocked"].sort(key=lambda row: row.task.entered_stage_at)  # longest wait on top
    held["active"].sort(key=lambda row: row.task.claim_at or "", reverse=True)
    held["ready"].sort(key=partial(_ready_order, config))
    held["backlog"].sort(key=lambda row: row.task.entered_stage_at)
    held["done"] = finished
    return [TaskGroup(label, held[key], note) for key, label, note in BOARD_GROUPS if held[key]]


def tasks_model(paths: Paths, query: Mapping[str, str]) -> dict[str, Any]:
    """One board under the ``project``, ``stage`` and ``q`` filters.

    Every matching task is on the page, in five groups the filters narrow together. The
    whole history is counted in SQL; at most ``DONE_LIMIT`` finished tasks are listed.
    """
    config, problems = _config(paths)
    projects = config.projects if config else {}
    project = query.get("project", "").strip().upper() or None
    stage = query.get("stage", "").strip().lower() or None
    if stage is not None and not _STAGE_NAME.fullmatch(stage):
        stage = None
    q = tasks.clean_text(query.get("q", ""), single_line=True)[:200]
    # The four live groups share one read of the unfinished tasks, which is the working set;
    # the finished history is counted and capped separately so it never sets the page's cost.
    live: list[tasks.Task] = []
    error: str | None = None
    if stage not in tasks.FINISHED:
        listed, error = _attempt(
            partial(
                tasks.list_tasks,
                paths,
                project=project,
                stage=stage,
                query=q or None,
                config=config,
            )
        )
        live = listed or []
    finished, finished_error = _attempt(
        partial(
            tasks.finished_tasks,
            paths,
            since=datetime.now(UTC) - DONE_WINDOW,
            limit=DONE_LIMIT,
            project=project,
            stage=stage,
            query=q or None,
        )
    )
    history = finished or tasks.FinishedTasks(rows=[], total=0, done_count=0)
    done_tasks = history.rows
    error = error or finished_error
    groups = _board_groups(
        config,
        [_task_row(config, task) for task in live],
        [_task_row(config, task) for task in done_tasks],
    )
    stages = list(BUILTIN_STAGES)
    for key, entry in projects.items():
        if project in (None, key):
            stages.extend(name for name in entry.stage_names if name not in stages)
    if stage and stage not in stages:
        stages.append(stage)
    project_keys = sorted({*projects, *(task.project for task in (*live, *done_tasks))})
    if project and project not in project_keys:
        project_keys.append(project)
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "groups": groups,
        "listed": sum(len(group.rows) for group in groups),
        "total": len(live) + history.total,
        "done_count": history.done_count,
        "done_days": DONE_WINDOW.days,
        "done_limit": DONE_LIMIT,
        "project": project,
        "stage": stage,
        "q": q,
        "projects": [(key, projects[key].name if key in projects else key) for key in project_keys],
        "stages": stages,
        "empty": _tasks_empty(project, stage, q),
        "error": error,
    }


def _tasks_empty(project: str | None, stage: str | None, q: str) -> str:
    """Say which filter emptied the board, so a blank page is not mistaken for a quiet one."""
    narrowed = [
        text
        for text, value in (
            (f"in project {project}", project),
            (f"in stage {stage}", stage),
            (f"matching “{q}”", q),
        )
        if value
    ]
    return f"No tasks {' '.join(narrowed)}." if narrowed else "No tasks yet."


def _existing_runs(paths: Paths, ids: list[str]) -> set[str]:
    """Which of these run ids still have a row; task events outlive pruned runs."""
    if not ids:
        return set()
    with db.reader(paths) as con:
        rows = con.execute(
            f"SELECT id FROM runs WHERE id IN ({', '.join('?' for _ in ids)})", ids
        ).fetchall()
    return {row["id"] for row in rows}


def _timeline(history: list[tasks.TaskEvent], live: set[str]) -> list[TimelineEntry]:
    entries = []
    for event in history:
        label, tone = _event_label(event)
        run = None if event.run_id is None else "live" if event.run_id in live else "pruned"
        entries.append(TimelineEntry(event, label, tone, run))
    return entries


def _event_label(event: tasks.TaskEvent) -> tuple[str, str]:
    payload = event.payload
    match event.kind:
        case "moved":
            move = str(payload.get("move") or "moved")
            tone = {"blocked": "warning", "cancelled": "muted"}.get(event.to_stage or "", "ok")
            return f"{move}: {event.from_stage} → {event.to_stage}", tone
        case "taken":
            return "taken", "running"
        case "released":
            reason = str(payload.get("reason") or "").replace("_", " ")
            return (f"released ({reason})" if reason else "released"), (
                "warning" if reason == "run ended" else "muted"
            )
        case "created":
            return (f"created in {event.to_stage}" if event.to_stage else "created"), "muted"
        case "noted":
            return ("note, needs attention" if payload.get("attention") else "note"), (
                "warning" if payload.get("attention") else "muted"
            )
        case "ref":
            return f"ref {payload.get('kind', '')} {payload.get('value', '')}".strip(), "muted"
        case _:
            return event.kind, "muted"


def _git(cwd: Any, *args: str) -> str | None:
    """One read-only Git query; None on any failure, because the page must always render."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError, subprocess.SubprocessError, ValueError:
        return None
    return done.stdout.strip()[:200] if done.returncode == 0 else None


def _worktree(paths: Paths, project: ProjectConfig | None, ref: str) -> dict[str, Any] | None:
    """The task's worktree panel, or None when the project has no repo or no worktree yet."""
    if project is None or project.repo is None:
        return None
    path = paths.worktrees / project.key / ref
    if not path.is_dir():
        return None
    branch = f"enso/{ref}"
    base = _git(project.repo, "symbolic-ref", "--short", "HEAD")
    counted = _git(path, "rev-list", "--count", f"{base}..{branch}", "--") if base else None
    return {
        "path": str(path),
        "branch": branch,
        "base": base,
        "ahead": int(counted) if counted is not None and counted.isdigit() else None,
    }


def task_model(paths: Paths, ref_text: str) -> dict[str, Any] | None:
    """One task: header, spec, refs, worktree, and the timeline."""
    try:
        ref = tasks.parse_ref(ref_text)
        task = tasks.get(paths, ref)
    except tasks.TaskError:
        return None
    except Exception as exc:  # the database is missing or unreadable: say so on the page
        return {
            "config_problems": _config(paths)[1],
            "alarm": _alarm(paths),
            "ref": ref_text,
            "task": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    config, problems = _config(paths)
    project = _project_of(config, task)
    ctx: dict[str, Any] | None = None
    ctx_error: str | None = None
    if config is not None and project is not None:
        ctx, ctx_error = _attempt(partial(tasks.context, paths, config, ref, env={}))
    history, events_error = _attempt(partial(tasks.events, paths, ref))
    attached, refs_error = _attempt(partial(tasks.refs, paths, ref))
    ids = sorted({event.run_id for event in history or [] if event.run_id})
    live, _runs_error = _attempt(partial(_existing_runs, paths, ids))
    return {
        "config_problems": problems,
        "alarm": _alarm(paths),
        "ref": ref,
        "task": task,
        "project": project,
        "project_name": project.name if project else task.project,
        "stages": list(project.stage_names) if project else [],
        "spec": files.render_markdown(task.body) if task.body else None,
        "refs": attached or [],
        "worktree": _worktree(paths, project, ref),
        "handoff": ctx["handoff"] if ctx else None,
        "recovery": ctx["recovery"] if ctx else None,
        # The verdict offers the run only while its row exists; pruned runs are named, not linked.
        "recovery_link": (
            f"/runs/{ctx['recovery']['run_id']}"
            if ctx and ctx["recovery"] and ctx["recovery"]["run_id"] in (live or set())
            else None
        ),
        "timeline": _timeline(history or [], live or set()),
        # The context read only feeds the handoff and the recovery notice, so it reports last;
        # without this it would fail silently and the page would simply omit both.
        "error": events_error or refs_error or ctx_error,
    }
