"""Shared config reads, project summaries, source errors, and the attention indicator.

Page models use these helpers without depending on another page's implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import urlencode

from .. import tasks
from ..config import Config, Paths, Stage, check_config
from . import filters
from . import heartbeat as beatviews

FAILED = ("error", "timeout", "prerun_error", "cancelled")
ACTIVITY_HOURS = 24  # the shared attention window and Today activity history


def attempt[T](work: Callable[[], T]) -> tuple[T | None, str | None]:
    """``(result, None)`` or ``(None, message)``: one source's failure stays local."""
    try:
        return work(), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def read_config(paths: Paths) -> tuple[Config | None, list[str]]:
    """Read current config and its problems for the shared page banner."""
    config, problems, _warnings = check_config(paths)
    return config, problems


@dataclass(frozen=True)
class ProjectSummary:
    """A project and its recorded task counts; ``None`` means counts could not be read."""

    key: str
    name: str
    workspace: str
    stages: tuple[Stage, ...]
    counts: dict[str, int] | None
    href: str

    @property
    def open_tasks(self) -> int | None:
        if self.counts is None:
            return None
        return sum(count for stage, count in self.counts.items() if stage not in tasks.FINISHED)


def project_summaries(
    paths: Paths, config: Config | None, *, workspace: str | None = None
) -> tuple[list[ProjectSummary], str | None]:
    """Keep empty projects and orphaned task history reachable from Tasks and Workspaces."""
    counts, error = attempt(partial(tasks.stage_counts, paths, workspace=workspace))
    projects = config.projects if config else {}
    owners = set(counts or {}) | {
        (project.workspace, project.key)
        for project in projects.values()
        if workspace is None or project.workspace == workspace
    }
    rows = []
    for owner, key in owners:
        project = projects.get(key)
        if project and project.workspace != owner:
            project = None
        # Missing definitions still link to the recorded owner, even with no workspace filter.
        scope = workspace if project else owner
        query = {"project": key, **({"workspace": scope} if scope else {})}
        rows.append(
            ProjectSummary(
                key,
                project.name if project else key,
                owner,
                project.stages if project else (),
                counts.get((owner, key), {}) if counts is not None else None,
                "/tasks?" + urlencode(query),
            )
        )
    return sorted(rows, key=lambda row: (row.name.casefold(), row.key, row.workspace)), error


def alarm(paths: Paths) -> bool:
    """Whether anything failed in the last day: the one signal every page carries.

    One indexed row, so the shell can show it without every page running the doctor.
    """
    attention, _error = attempt(partial(beatviews.beat_count, paths, attention=True))
    if attention:
        return True
    recent, _error = attempt(partial(beatviews.activity, paths, statuses=FAILED, limit=1))
    if not recent:
        return False
    moment = filters.parse_time(recent[0].started_at)
    return moment is not None and (
        datetime.now(UTC) - moment.astimezone(UTC) < timedelta(hours=ACTIVITY_HOURS)
    )
