"""Shared scheduling isolates subsystems and keeps wall-clock times across DST."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from enso import scheduling


@pytest.mark.parametrize(
    ("after", "cron", "expected"),
    [
        ("2026-03-07T09:00:00-08:00", "0 9 * * *", "2026-03-08T09:00:00-07:00"),
        ("2026-10-31T09:00:00-07:00", "0 9 * * *", "2026-11-01T09:00:00-08:00"),
        # A missing time moves forward through the spring gap.
        ("2026-03-07T03:00:00-08:00", "30 2 * * *", "2026-03-08T03:30:00-07:00"),
        # Repeated times run on their first occurrence, never twice after the fallback.
        ("2026-11-01T00:00:00-07:00", "30 1 * * *", "2026-11-01T01:30:00-07:00"),
        ("2026-11-01T01:00:00-08:00", "30 1 * * *", "2026-11-02T01:30:00-08:00"),
        # The caller's timestamp need not already use the schedule's zone.
        ("2026-09-01T15:00:00+00:00", "0 9 * * *", "2026-09-01T09:00:00-07:00"),
    ],
)
def test_named_timezone_schedule(after, cron, expected):
    due = scheduling.next_cron(cron, datetime.fromisoformat(after), "America/Los_Angeles")
    assert due.isoformat() == expected


def test_cron_slots_excludes_start_and_includes_bound():
    after = datetime(2026, 9, 7, 12, tzinfo=UTC)
    until = after + timedelta(hours=1)
    assert list(scheduling.cron_slots("*/15 * * * *", after, until=until, limit=40)) == [
        after + timedelta(minutes=minutes) for minutes in (15, 30, 45, 60)
    ]
    assert list(scheduling.cron_slots("* * * * *", after, until=after, limit=40)) == []
    assert list(scheduling.cron_slots("* * * * *", until, until=after, limit=40)) == []


def test_cron_slots_caps_every_minute_schedule():
    after = datetime(2026, 9, 7, 12, tzinfo=UTC)
    until = after + timedelta(hours=3)
    slots = list(scheduling.cron_slots("* * * * *", after, until=until, limit=40))
    assert slots == [after + timedelta(minutes=minute) for minute in range(1, 41)]
    assert list(scheduling.cron_slots("* * * * *", after, until=until, limit=0)) == []


def test_cron_slots_preserves_named_timezone_across_daylight_saving():
    slots = list(
        scheduling.cron_slots(
            "0 9 * * *",
            datetime.fromisoformat("2026-10-31T09:00:00-07:00"),
            "America/Los_Angeles",
            until=datetime.fromisoformat("2026-11-02T09:00:00-08:00"),
            limit=40,
        )
    )
    assert [slot.isoformat() for slot in slots] == [
        "2026-11-01T09:00:00-08:00",
        "2026-11-02T09:00:00-08:00",
    ]


async def test_slow_and_failed_checks_do_not_block_other_checks(monkeypatch, caplog):
    sleeping = asyncio.Queue()
    ticks = asyncio.Queue()
    slow_started = asyncio.Event()
    slow_cleaned = asyncio.Event()
    fast_calls = asyncio.Queue()
    failures = asyncio.Queue()
    slow_calls = []

    async def sleep(delay):
        await sleeping.put(delay)
        await ticks.get()

    async def slow(now):
        slow_calls.append(now)
        slow_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            slow_cleaned.set()

    async def fast(now):
        await fast_calls.put(now)

    async def failing(now):
        await failures.put(now)
        raise ValueError("broken source")

    monkeypatch.setattr(scheduling.asyncio, "sleep", sleep)
    monkeypatch.setattr(scheduling, "time", SimpleNamespace(time=lambda: 125))
    running = asyncio.create_task(
        scheduling.minute_loop({"slow": slow, "fast": fast, "broken": failing})
    )
    try:
        for _ in range(2):
            assert await asyncio.wait_for(sleeping.get(), 1) == 55
            await ticks.put(None)
            assert (await asyncio.wait_for(fast_calls.get(), 1)).tzinfo is not None
            await asyncio.wait_for(failures.get(), 1)
        await asyncio.wait_for(slow_started.wait(), 1)
        assert len(slow_calls) == 1
        assert caplog.text.count("broken scheduler tick failed") == 2
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert slow_cleaned.is_set()


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
async def test_invalid_interval_is_rejected(interval):
    with pytest.raises(ValueError, match="positive and finite"):
        await scheduling.minute_loop({}, interval=interval)
