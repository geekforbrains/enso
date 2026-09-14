"""Shared config reads, source error handling, and the viewer's attention indicator.

Page models use these helpers without depending on another page's implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial

from ..config import Config, Paths, check_config
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
