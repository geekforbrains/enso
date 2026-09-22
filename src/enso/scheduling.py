"""Shared minute scheduling and wall-clock cron arithmetic for background work.

Callers retain their own readiness and recovery rules; the clock only dispatches independent
checks and ensures a slow or failed subsystem cannot stop the others from being checked.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter

log = logging.getLogger(__name__)

TICK_SECONDS = 60
CRON_FIELDS = ("minute", "hour", "day-of-month", "month", "day-of-week")
type Tick = Callable[[datetime], Awaitable[None]]


def schedule_problem(schedule: str) -> str | None:
    """Why a schedule is not an accepted five-field cron expression, or None."""
    if len(schedule.split()) != len(CRON_FIELDS):
        return (
            f"schedule {schedule!r} must be exactly five fields, {' '.join(CRON_FIELDS)}; "
            "Enso schedules at minute resolution, so a seconds or year field and aliases "
            "such as @daily are not accepted"
        )
    if not croniter.is_valid(schedule):
        return f"schedule {schedule!r} is not a cron expression"
    try:
        # Syntactic validity alone accepts impossible dates such as February 31.
        croniter(schedule, datetime(2000, 1, 1)).get_next(datetime)
    except ValueError, OverflowError:
        return f"schedule {schedule!r} has no reachable calendar date"
    return None


def next_cron(schedule: str, after: datetime, timezone: str | None = None) -> datetime:
    """Find the next wall-clock slot, using the host's zone unless one is named.

    Local jobs preserve their existing C-library DST mapping. Named zones choose the first
    occurrence of repeated times and move nonexistent spring times forward by the DST gap.
    """
    if timezone is None:
        since = after.astimezone().replace(tzinfo=None)
        return croniter(schedule, since).get_next(datetime).astimezone()
    zone = ZoneInfo(timezone)
    since = after.astimezone(zone).replace(tzinfo=None)
    slots = croniter(schedule, since)
    while True:
        wall = slots.get_next(datetime)
        candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC).astimezone(zone)
        if candidate.timestamp() > after.timestamp():
            return candidate


def cron_slots(
    schedule: str,
    after: datetime,
    timezone: str | None = None,
    *,
    until: datetime,
    limit: int,
) -> Iterator[datetime]:
    """Yield at most limit slots after the start, including the upper bound.

    Each step uses next_cron's wall-clock and daylight-saving rules.
    """
    for _ in range(limit):
        if after.timestamp() >= until.timestamp():
            return
        slot = next_cron(schedule, after, timezone)
        if slot.timestamp() > until.timestamp():
            return
        yield slot
        after = slot


async def minute_loop(callbacks: Mapping[str, Tick], *, interval: float = TICK_SECONDS) -> None:
    """Dispatch named checks on aligned ticks without overlapping each check with itself.

    Each check logs its own failure and can be retried on the next tick. Cancellation waits
    for active check cleanup; the caller still owns any background runs those checks started.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("scheduler interval must be positive and finite")
    active: dict[str, asyncio.Task[None]] = {}

    async def check(name: str, callback: Tick, now: datetime) -> None:
        try:
            await callback(now)
        except Exception:
            log.exception("%s scheduler tick failed", name)

    log.info("scheduler started: %s", ", ".join(callbacks))
    try:
        while True:
            await asyncio.sleep(interval - time.time() % interval)
            now = datetime.now().astimezone()
            for name, callback in callbacks.items():
                previous = active.get(name)
                if previous is None or previous.done():
                    active[name] = asyncio.create_task(
                        check(name, callback, now), name=f"scheduler:{name}"
                    )
    finally:
        for task in active.values():
            task.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)
