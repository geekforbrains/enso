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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from math import ceil
from typing import Any
from urllib.parse import quote, urlencode

from .. import audit, db, doctor, instructions, runs, skills, tasks, workflows, workspaces
from .. import heartbeat as beats
from .. import log as logsetup
from ..config import Config, Paths, require_workspace
from ..jobs import Job, execution_kind, load_jobs
from ..scheduling import cron_slots
from . import Bind, common, files, filters
from . import heartbeat as beatviews
from . import tasks as taskviews

LOG_TAIL_LINES = 200
RECENT_RUNS = 20  # on a job's page
DB_COMPANIONS = ("", "-wal", "-shm")


def failures(summaries: Sequence[runs.RunSummary]) -> list[runs.RunSummary]:
    return [summary for summary in summaries if summary.status in common.FAILED]


# -- Today ----------------------------------------------------------------------

TODAY_SECTIONS = ("schedule", "activity", "reliability")
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
                sum(1 for summary in bucket if summary.status in common.FAILED),
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
        moment = filters.parse_time(summary.started_at)
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
    config, problems = common.read_config(paths)
    now = datetime.now().astimezone()
    rows, jobs_error = _job_rows(paths, config, now)
    scanned, runs_error = common.attempt(partial(runs.list_summaries, paths, limit=TODAY_SCAN))
    history = scanned or []
    activity, activity_error = common.attempt(partial(beatviews.activity, paths, limit=TODAY_SCAN))
    beat_attention, beats_error = common.attempt(
        partial(beatviews.beat_rows, paths, attention=True, limit=UP_NEXT)
    )
    attention_count, _attention_error = common.attempt(
        partial(beatviews.beat_count, paths, attention=True)
    )
    beat_upcoming, upcoming_error = common.attempt(
        partial(beatviews.beat_rows, paths, upcoming=True, limit=UP_NEXT)
    )

    top_of_hour = now.replace(minute=0, second=0, microsecond=0)
    start = top_of_hour - timedelta(hours=common.ACTIVITY_HOURS)
    # Pin to the next hour so the real lookahead never shrinks below two hours.
    end = top_of_hour + timedelta(hours=TODAY_AHEAD_HOURS + 1)
    chart_start = end - timedelta(hours=chart_back_hours + TODAY_AHEAD_HOURS)
    window = [
        summary
        for summary in (activity or [])
        if (moment := filters.parse_time(summary.started_at)) is not None
        and moment.astimezone() >= start
    ]
    recent = _by_job(history)
    upcoming: list[dict[str, Any]] = [
        {
            "title": row.ref,
            "href": "/jobs/" + quote(row.ref, safe=""),
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
                "next_run": filters.parse_time(row.next_check_at),
                "timing": row.ref,
                "workspace": row.workspace,
                "source": "heartbeat",
            }
            for row in (beat_upcoming or [])
        )
    upcoming.sort(key=lambda row: row["next_run"] or now)
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "section": section,
        "now": now,
        "back_hours": common.ACTIVITY_HOURS,
        "ahead_hours": TODAY_AHEAD_HOURS,
        "chart_back_hours": chart_back_hours,
        "chart_ranges": CHART_RANGES,
        "chart_query": f"?range={chart_back_hours}" if chart_back_hours != CHART_BACK_HOURS else "",
        "reliability_limit": filters.BAR_RUNS,
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
            if (summaries := recent.get(row.ref, [])[: filters.BAR_RUNS])
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
        for summary in recent.get(row.ref, []):
            moment = filters.parse_time(summary.started_at)
            if moment is None or moment.astimezone() < start:
                continue
            seconds = (summary.duration_ms or 0) / 1000
            width = max(CHART_MIN_EVENT, seconds / span * CHART_WIDTH)
            events.append(
                ChartEvent(
                    round(place(moment.astimezone()), 2),
                    round(width, 2),
                    filters.status_class(summary.status),
                    f"{filters.status_word(summary.status)} at {filters.clock(summary.started_at)}"
                    f", {filters.duration(summary.duration_ms)}",
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
                    row.ref,
                    row.ref,
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


# -- Health ---------------------------------------------------------------------


HEALTH_SECTIONS = ("doctor", "log")


def health_model(paths: Paths, bind: Bind | None, section: str = "doctor") -> dict[str, Any]:
    report, error = common.attempt(partial(doctor.run, paths))
    problems = report.section("config").problems if report else common.read_config(paths)[1]
    findings = (
        sum(len(entry.problems) + len(entry.warnings) for entry in report.sections) if report else 0
    )
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
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
    lines, error = common.attempt(partial(logsetup.tail, paths, LOG_TAIL_LINES))
    return {"path": str(paths.log), "exists": True, "lines": lines or [], "error": error}


# -- Workspaces -----------------------------------------------------------------


def workspaces_model(paths: Paths) -> dict[str, Any]:
    config, problems = common.read_config(paths)
    report, error = common.attempt(partial(audit.audit, paths, config=config))
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "home": report.home if report else None,
        "workspaces": report.workspaces if report else [],
        "error": error,
    }


HOME_SECTIONS = ("overview", "instructions")
WORKSPACE_SECTIONS = ("overview", "instructions", "files")
SAVED_NOTICE = "Instructions saved."


@dataclass(frozen=True)
class Rejected:
    """A save the editor refused: the operator's text, the revision it named, and why."""

    text: str
    expected: str | None
    error: str
    stale: bool


def home_model(
    paths: Paths, section: str = "overview", *, rejected: Rejected | None = None, notice: str = ""
) -> dict[str, Any]:
    home, error = common.attempt(partial(audit.audit_home, paths))
    names, _ = common.attempt(partial(workspaces.list_workspaces, paths))
    return {
        "config_problems": common.read_config(paths)[1],
        "alarm": common.alarm(paths),
        "section": section,
        "path": str(paths.home),
        "home": home,
        "workspace_count": len(names or []),
        "editor": (
            instructions_editor(paths, None, "/home/instructions", rejected, notice)
            if section == "instructions"
            else None
        ),
        "error": error,
    }


def workspace_model(
    paths: Paths,
    name: str,
    section: str = "overview",
    *,
    rejected: Rejected | None = None,
    notice: str = "",
) -> dict[str, Any] | None:
    try:
        require_workspace(paths, name)
    except ValueError:
        return None
    config, problems = common.read_config(paths)
    # Every tab carries the verdict in its heading, so the audit runs whichever is shown.
    report, error = common.attempt(partial(audit.audit, paths, [name], config=config))
    model: dict[str, Any] = {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "section": section,
        "name": name,
        "path": str(paths.workspace(name)),
        "workspace": report.workspaces[0] if report else None,
        "home": report.home if report else None,
        "error": error,
    }
    if section == "overview":
        model["projects"], model["projects_error"] = common.project_summaries(
            paths, config, workspace=name
        )
    elif section == "files":
        model["roots"] = _file_roots(paths, name)
    else:
        action = f"/workspaces/{quote(name, safe='')}/instructions"
        model["editor"] = instructions_editor(paths, name, action, rejected, notice)
    return model


def instructions_editor(
    paths: Paths,
    workspace: str | None,
    action: str,
    rejected: Rejected | None = None,
    notice: str = "",
) -> dict[str, Any]:
    """The AGENTS.md form. A refused save shows the submitted text, not the file's.

    After a stale save the form carries the file's current revision, so saving again
    deliberately replaces it; the current text is offered beside the form to compare.
    """
    try:
        current = instructions.read(paths, workspace)
    except (instructions.InstructionsError, ValueError) as exc:
        return {"readable": False, "error": str(exc), "notice": ""}
    editor: dict[str, Any] = {
        "readable": True,
        "action": action,
        "path": str(current.path),
        "exists": current.revision is not None,
        "text": current.text,
        "revision": current.revision or "",
        "error": "",
        "stale": False,
        "disk": None,
        "dirty": False,
        "notice": notice,
    }
    if rejected is not None:
        editor.update(
            text=rejected.text,
            error=rejected.error,
            stale=rejected.stale,
            dirty=True,
            notice="",
        )
        if rejected.stale:
            editor["disk"] = current.text if current.revision is not None else None
        else:
            editor["revision"] = rejected.expected or ""
    return editor


def _file_roots(paths: Paths, name: str) -> list[dict[str, Any]]:
    roots = []
    for root in files.ROOTS:
        directory = paths.workspace(name) / root
        if root != "uploads" and not directory.exists() and not directory.is_symlink():
            continue
        try:
            resolved = files.resolve(paths, name, root, "")
            entries, size = files.tree_summary(resolved.root)
        except OSError, files.PathRejectedError:
            entries, size = 0, 0
        roots.append(
            {"name": root, "is_dir": directory.is_dir(), "entries": entries, "bytes": size}
        )
    return roots


def files_model(
    paths: Paths, name: str, root: str, relative: str, *, raw: bool = False
) -> dict[str, Any] | None:
    """A directory listing or a file view; None for anything that is not there or allowed."""
    try:
        resolved = files.resolve(paths, name, root, relative)
    except files.PathRejectedError, FileNotFoundError, OSError:
        return None
    model: dict[str, Any] = {
        "config_problems": common.read_config(paths)[1],
        "alarm": common.alarm(paths),
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
        model["listing"], model["error"] = common.attempt(
            partial(files.listing, resolved, root, relative)
        )
    else:
        model["file"], model["error"] = common.attempt(
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
        resolved, failure = common.attempt(partial(skills.resolve, paths, workspace))
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
        "config_problems": common.read_config(paths)[1],
        "alarm": common.alarm(paths),
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

    ref: str
    job: Job | None
    problems: list[str]
    last: runs.RunSummary | None
    next_run: datetime | None
    kind: str | None = None

    @property
    def workspace(self) -> str:
        return self.ref.partition(":")[0]

    @property
    def name(self) -> str:
        """The job's directory name: its reference without the workspace."""
        return self.ref.partition(":")[2]


@dataclass(frozen=True)
class JobGroup:
    """One heading on the Jobs list: the workspace whose ``jobs/`` holds the rows under it."""

    workspace: str
    rows: list[JobRow]


def _job_groups(rows: list[JobRow]) -> list[JobGroup]:
    """The rows under their workspace, alphabetical; a workspace without jobs has no heading."""
    grouped: dict[str, list[JobRow]] = {}
    for row in rows:
        grouped.setdefault(row.workspace, []).append(row)
    return [JobGroup(name, grouped[name]) for name in sorted(grouped)]


def _job_kind(job: Job | None, config: Config | None) -> str | None:
    if job is None:
        return None
    if config is not None:
        return execution_kind(job, config)
    return "agent" if job.agent else "command" if job.command else "stage"


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
    loaded, error = common.attempt(partial(load_jobs, paths, config))
    found, job_problems = loaded if loaded is not None else ([], {})
    summaries, _runs_error = common.attempt(partial(runs.latest_summaries, paths))
    latest = summaries or {}
    rows = [
        JobRow(
            job.ref,
            job,
            job_problems.get(job.ref, []),
            latest.get(job.ref),
            _next_run(job, job_problems.get(job.ref, []), now),
            _job_kind(job, config),
        )
        for job in found
    ]
    parsed = {job.ref for job in found}
    rows.extend(
        JobRow(name, None, found_problems, latest.get(name), None)
        for name, found_problems in job_problems.items()
        if name not in parsed
    )
    return sorted(rows, key=lambda row: row.ref), error


def jobs_model(paths: Paths) -> dict[str, Any]:
    config, problems = common.read_config(paths)
    now = datetime.now().astimezone()
    rows, error = _job_rows(paths, config, now)
    scanned, runs_error = common.attempt(partial(runs.list_summaries, paths, limit=TODAY_SCAN))
    recent = _by_job(scanned or [])
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "rows": rows,
        "groups": _job_groups(rows),
        "recent": recent,
        "error": error,
        "runs_error": runs_error,
        "now": now,
    }


JOB_SECTIONS = ("overview", "history")


def job_model(paths: Paths, name: str, section: str = "overview") -> dict[str, Any] | None:
    config, problems = common.read_config(paths)
    loaded, error = common.attempt(partial(load_jobs, paths, config))
    found, job_problems = loaded if loaded is not None else ([], {})
    job = next((candidate for candidate in found if candidate.ref == name), None)
    if job is None and name not in job_problems:
        return None
    limit = runs.PAGE_SIZE if section == "history" else RECENT_RUNS
    summaries, runs_error = common.attempt(
        partial(runs.list_summaries, paths, job=name, limit=limit)
    )
    recent = summaries or []
    project = config.projects.get(job.project) if config and job and job.project else None
    stage = project.stage(job.stage) if project and job and job.stage else None
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "section": section if section in JOB_SECTIONS else "overview",
        "groups": _feed(recent, by_hour=False),
        "failed": len(failures(recent)),
        "average": _average(recent),
        "name": name,
        "job": job,
        "project": project,
        "stage_definition": stage,
        "kind": _job_kind(job, config),
        "command": job.command if job and job.command else stage.command if stage else None,
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


def _listing_link(base: str, filters: Mapping[str, str | None], page: int = 1) -> str:
    """``base`` under its set filters, naming the page only past the first."""
    values = {key: value for key, value in filters.items() if value}
    if page > 1:
        values["page"] = str(page)
    return base + ("?" + urlencode(values) if values else "")


def _listing(
    query: Mapping[str, str],
    base: str,
    page_size: int,
    counted: int | None,
    rows: Callable[..., Sequence[Any]],
    **filters: str | None,
) -> dict[str, Any]:
    """One page of a listing: its rows, the pager context, and the listing's own error.

    ``counted`` is the filtered total, or ``None`` when counting failed, which skips the
    listing: without a total there is no page to ask for. ``rows`` is called with the
    page's ``limit`` and ``offset``. The count fixes the page and its links; the rows fix
    the shown range, so an empty listing reads ``0-0 of N`` rather than a range it did
    not render.
    """
    total = counted or 0
    pages = max(1, ceil(total / page_size))
    page = min(_requested_page(query), pages)
    listed: Sequence[Any] = []
    error = None
    if counted is not None:
        fetched, error = common.attempt(
            partial(rows, limit=page_size, offset=(page - 1) * page_size)
        )
        listed = fetched or []
    start = (page - 1) * page_size + 1 if listed else 0
    return {
        "rows": list(listed),
        "error": error,
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "start": start,
        "end": start + len(listed) - 1 if listed else 0,
        "prev_link": _listing_link(base, filters, page - 1) if page > 1 else None,
        "next_link": _listing_link(base, filters, page + 1) if page < pages else None,
    }


def heartbeats_model(paths: Paths, query: Mapping[str, str]) -> dict[str, Any]:
    """A bounded current/previous list; every row is a report of saved beat state."""
    config, problems = common.read_config(paths)
    view = "previous" if query.get("view") == "previous" else "current"
    state = query.get("state") or None
    if state not in beatviews.state_choices(view):
        state = None
    attention = query.get("attention") == "1"
    counted, error = common.attempt(
        partial(beatviews.beat_count, paths, view=view, state=state, attention=attention)
    )
    listing = _listing(
        query,
        "/heartbeats",
        beatviews.PAGE_SIZE,
        counted,
        partial(beatviews.beat_rows, paths, view=view, state=state, attention=attention),
        view=view if view == "previous" else None,
        state=state,
        attention="1" if attention else None,
    )
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "view": view,
        "state": state,
        "attention": attention,
        "states": beatviews.state_choices(view),
        "heartbeat_enabled": config.heartbeat.enabled if config else None,
        "retention_days": config.heartbeat.retention_days if config else None,
        **listing,
        "error": error or listing["error"],
    }


def heartbeat_model(
    paths: Paths, ref: str, section: str = "overview", query: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """Current instructions with a paginated meaningful history and separate run summaries."""
    try:
        beat, error = common.attempt(partial(beats.get, paths, ref))
    except beats.HeartbeatError:
        return None
    if beat is None and (error is None or error.startswith("HeartbeatError:")):
        return None
    config, problems = common.read_config(paths)
    model = {
        "config_problems": problems,
        "alarm": common.alarm(paths),
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
    history, history_error = common.attempt(partial(beatviews.events, paths, beat.id, limit=1))
    history_count, latest = history or (0, [])
    runs_count, runs_error = common.attempt(
        partial(beatviews.activity_count, paths, beat_ref=beat.ref)
    )
    model.update(
        history_count=history_count,
        runs_count=runs_count or 0,
        latest_event=latest[0] if latest else None,
        error=error or history_error or runs_error,
        checkpoint_text=json.dumps(beat.checkpoint, ensure_ascii=False, indent=2),
    )
    base = "/heartbeats/" + beat.ref + "/" + section
    if section == "history":
        history_page = partial(beatviews.events, paths, beat.id)
        listing = _listing(
            query or {},
            base,
            beatviews.PAGE_SIZE,
            None if history is None else history_count,
            lambda **page: history_page(**page)[1],
        )
        model["events"] = listing.pop("rows")
    elif section == "runs":
        listing = _listing(
            query or {},
            base,
            beatviews.PAGE_SIZE,
            runs_count,
            partial(beatviews.activity, paths, beat_ref=beat.ref),
        )
        model["recent"] = listing.pop("rows")
    else:
        return model
    listing_error = listing.pop("error")
    model["error"] = model["error"] or listing_error
    model.update(listing)
    return model


def heartbeat_run_model(paths: Paths, run_id: str) -> dict[str, Any] | None:
    """A single saved assessment, including the definition and input boundary it saw."""
    run, error = common.attempt(partial(beats.get_run, paths, run_id))
    if run is None and error is None:
        return None
    return {
        "config_problems": common.read_config(paths)[1],
        "alarm": common.alarm(paths),
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
    wanted = _view_statuses(view)
    counted, error = common.attempt(
        partial(
            beatviews.activity_count, paths, source=source, job=job, status=status, statuses=wanted
        )
    )
    filters = {
        "view": view if view != "signal" else None,
        "source": source if source != "any" else None,
        "job": job,
        "status": status,
    }
    listing = _listing(
        query,
        "/runs",
        runs.PAGE_SIZE,
        counted,
        partial(beatviews.activity, paths, source=source, job=job, status=status, statuses=wanted),
        **filters,
    )
    rows: list[beatviews.ActivityRun] = listing["rows"]
    known, _names_error = common.attempt(partial(runs.job_names, paths))
    names = known or []
    if job and job not in names:
        names = sorted([*names, job])
    counts = _view_counts(paths, job, source)
    return {
        "config_problems": common.read_config(paths)[1],
        "alarm": common.alarm(paths),
        "groups": _feed(rows, by_hour=view == "all"),
        "view": view,
        "view_tabs": [
            (
                RUN_VIEW_LABELS[name],
                _listing_link("/runs", {**filters, "view": name if name != "signal" else None}),
                counts.get(name),
                "error" if name == "failed" else None,
            )
            for name in RUN_VIEWS
        ],
        "view_current": _listing_link("/runs", filters),
        "job": job,
        "source": source,
        "status": status,
        "job_names": names,
        "statuses": beatviews.RUN_STATUSES,
        **listing,
        "error": error or listing["error"],
    }


def _view_statuses(view: str) -> tuple[str, ...] | None:
    if view == "failed":
        return common.FAILED
    if view == "signal":
        return tuple(
            status for status in beatviews.RUN_STATUSES if status not in ("no_work", "skipped")
        )
    return None


def _view_counts(paths: Paths, job: str | None, source: str = "any") -> dict[str, int | None]:
    """The tab counts, under the job filter but not the view being counted."""
    counts: dict[str, int | None] = {}
    for name in RUN_VIEWS:
        counted, _error = common.attempt(
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
    run, error = common.attempt(partial(runs.get, paths, run_id))
    if run is None and error is None:
        return None
    attempts, attempt_error = (
        common.attempt(partial(runs.attempts, paths, run.id)) if run else ([], None)
    )
    config, problems = common.read_config(paths)
    timeout = _job_timeout(paths, config, run.job) if run else None
    related, _tasks_error = (
        common.attempt(partial(tasks.tasks_for_run, paths, run.id)) if run else ([], None)
    )
    workflow: list[dict[str, Any]] = []
    workflow_error: str | None = None
    if run:
        for ref, _title in related or []:
            history, failure = common.attempt(partial(workflows.history, paths, ref))
            workflow_error = workflow_error or failure
            workflow.extend(
                {**entry, "task_ref": ref}
                for entry in taskviews.workflow_rows(history or [], {run.id})
                if entry.get("run_id") == run.id
            )
    return {
        "config_problems": problems,
        "alarm": common.alarm(paths),
        "run": run,
        "attempts": attempts or [],
        "timeout": timeout,
        "tasks": related or [],
        "workflow": workflow,
        "error": error or attempt_error or workflow_error,
    }


def _job_timeout(paths: Paths, config: Config | None, name: str) -> int | None:
    """The job's allowed seconds, when its JOB.md is still there and readable."""
    loaded, _error = common.attempt(partial(load_jobs, paths, config))
    found, _problems = loaded if loaded is not None else ([], {})
    job = next((candidate for candidate in found if candidate.ref == name), None)
    return job.timeout if job else None
