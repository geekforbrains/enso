"""Heartbeat viewer pages, mixed activity, safe pagination, and read-only boundaries."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import load_job, write_job

from enso import db, heartbeat, runs, web
from enso.config import save_config
from enso.web import filters, server
from enso.web import heartbeat as reading


@pytest.fixture
def saved(config):
    save_config(config.paths, config.raw)
    return config


@pytest.fixture
async def client(enso_home):
    async with TestClient(
        TestServer(server.create_app(enso_home, web.Bind("127.0.0.1", 8787)))
    ) as client:
        yield client


def beat(config, **patch):
    current = heartbeat.create(
        config,
        {
            "title": "Dinner with friends",
            "instructions": "Agree on a dinner time.",
            "completion": "A final time is agreed and saved.",
            "allowed_actions": "Reply about Friday after 6pm.",
            "workspace": "default",
            "schedule": "*/5 * * * *",
            "llm_checks": True,
            **patch,
        },
    )
    return heartbeat.resume(config, current.ref)


def assessment(config, current, *, status="ok", output="provider-output-sentinel", started_at=None):
    run = heartbeat.start_run(config, current.ref, trigger="new_events", started_at=started_at)
    if status == "ok":
        heartbeat.wait(config, current.ref, message="Waiting for the final answer", run_id=run.id)
    return heartbeat.finish_run(
        config,
        run.id,
        status=status,
        output=output,
        error="Provider unavailable" if status == "error" else "",
    )


async def page(client, path):
    response = await client.get(path)
    assert response.status == 200, await response.text()
    return await response.text()


class Anchors(HTMLParser):
    """Ensure clickable rows do not contain a second destination."""

    def __init__(self):
        super().__init__()
        self.open = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            assert not self.open, "nested anchors"
            self.open = True

    def handle_endtag(self, tag):
        if tag == "a":
            self.open = False


async def test_current_previous_state_and_attention_filters_are_paginated(
    client, saved, monkeypatch
):
    monkeypatch.setattr(reading, "PAGE_SIZE", 2)
    active = beat(saved)
    paused = beat(saved, title="Waiting for a refund")
    heartbeat.pause(saved, paused.ref)
    attention = beat(saved, title="Source needs a new login")
    heartbeat.record_check(saved, attention.ref, "error", error="Source login expired")
    closed = beat(saved, title="Agreement saved")
    heartbeat.complete(saved, closed.ref, message="Agreement is saved in notes")
    first = await page(client, "/heartbeats")
    assert "Showing 1\u20132 of 3 heartbeats" in first and "needs attention" in first
    assert f'href="/heartbeats/{closed.ref}"' not in first
    second = await page(client, "/heartbeats?page=2")
    assert (
        "Showing 3\u20133 of 3 heartbeats" in second
        and f'href="/heartbeats/{active.ref}"' in second
    )
    previous = await page(client, "/heartbeats?view=previous")
    assert f'href="/heartbeats/{closed.ref}"' in previous and "fulfilled" in previous
    filtered = await page(client, "/heartbeats?state=paused")
    assert f'href="/heartbeats/{paused.ref}"' in filtered and "of 1 heartbeat" in filtered
    filtered = await page(client, "/heartbeats?attention=1")
    assert f'href="/heartbeats/{attention.ref}"' in filtered and "of 1 heartbeat" in filtered
    for body in (first, second, previous, filtered):
        Anchors().feed(body)


async def test_detail_shows_current_intent_saved_agent_and_checks(client, saved):
    current = beat(
        saved,
        title="<b>Dinner</b>",
        instructions="<script>untrusted()</script>",
        notify="slack:C1",
        notify_thread="123.456",
        timezone="America/Vancouver",
    )
    heartbeat.record_check(saved, current.ref, "quiet")
    body = await page(client, f"/heartbeats/{current.ref}")
    for text in (
        "Completion condition",
        "Allowed actions",
        "Saved agent",
        "Last successful check",
        "America/Vancouver",
        "123.456",
    ):
        assert text in body
    assert "&lt;script&gt;untrusted()&lt;/script&gt;" in body and "<script>untrusted()" not in body
    assert "&lt;b&gt;Dinner&lt;/b&gt;" in body and "quiet" in body
    Anchors().feed(body)


async def test_history_receipts_times_delivery_and_clipping_are_visible(client, saved, monkeypatch):
    current = beat(saved)
    source = datetime.now(UTC) - timedelta(hours=1)
    note = heartbeat.note(
        saved,
        current.ref,
        "<img src=x onerror=alert(1)> source update",
        occurred_at=source.isoformat(),
    )
    run = heartbeat.start_run(saved, current.ref)
    heartbeat.begin_action(
        saved, current.ref, "dinner-reply", "Send an authorized reply", run_id=run.id
    )
    receipt = heartbeat.resolve_action(
        saved,
        current.ref,
        "dinner-reply",
        "succeeded",
        message="Reply delivered",
        receipt='{"message_id":"<script>receipt</script>"}',
        run_id=run.id,
    )
    heartbeat.wait(saved, current.ref, message="Await agreement", run_id=run.id)
    heartbeat.finish_run(saved, run.id, status="ok")
    failure = heartbeat.record_check(saved, current.ref, "error", error="Check failed")
    heartbeat.notice_delivered(saved, failure, 123)
    body = await page(client, f"/heartbeats/{current.ref}/history")
    assert "Source time" in body and filters.when(source) in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "&lt;script&gt;receipt&lt;/script&gt;" in body and "Notification delivered" in body
    assert '"outbox_id":123' in body.replace("&#34;", '"')
    assert f'href="/heartbeats/runs/{run.id}"' in body
    # Every event is a fold inside one panel, the way every other list is built, and each
    # keeps the anchor that links to it.
    assert body.count('<div class="panel rows">') == 1
    for event in (note, receipt):
        assert f'<details class="fold beat-event" id="event-{event.id}">' in body
    assert '<details class="panel beat-event"' not in body
    Anchors().feed(body)
    long = heartbeat.note(saved, current.ref, "x" * (reading.EVENT_PREVIEW + 20))
    monkeypatch.setattr(reading, "PAGE_SIZE", 2)
    first = await page(client, f"/heartbeats/{current.ref}/history")
    assert "Message shortened" in first and "x" * (reading.EVENT_PREVIEW + 1) not in first
    assert f"history {current.ref} --after {long.id - 1} --limit 1 --json" in first
    assert f"/heartbeats/{current.ref}/history?page=2" in first
    second = await page(client, f"/heartbeats/{current.ref}/history?page=2")
    assert f'id="event-{long.id}"' not in second
    assert second.count('<div class="panel rows">') == 1


async def test_list_failures_never_claim_rows(client, saved, monkeypatch):
    """A failed listing reads 0-0 of the counted total; a failed count lists nothing."""
    current = beat(saved)
    beat(saved, title="Second")

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reading, "beat_rows", locked)
    body = await page(client, "/heartbeats")
    assert "Showing 0\u20130 of 2 heartbeats." in body and "database is locked" in body
    assert f'href="/heartbeats/{current.ref}"' not in body

    listed = []
    monkeypatch.setattr(reading, "beat_count", locked)
    monkeypatch.setattr(reading, "beat_rows", lambda *args, **kwargs: listed.append(kwargs) or [])
    body = await page(client, "/heartbeats")
    assert "No current heartbeats." in body and "database is locked" in body
    assert not listed


@pytest.mark.parametrize("section,count", [("history", "events"), ("runs", "activity_count")])
async def test_detail_count_failures_remain_visible(client, saved, monkeypatch, section, count):
    current = beat(saved)
    assessment(saved, current)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reading, count, locked)
    body = await page(client, f"/heartbeats/{current.ref}/{section}")
    assert "The heartbeat could not be read" in body
    assert "database is locked" in body


async def test_history_names_the_actor_and_keeps_the_recorded_value(client, saved):
    current = beat(saved)
    heartbeat.note(saved, current.ref, "A person asked", actor="slack:U0AETSSDDEF")
    heartbeat.note(saved, current.ref, "A job looked", actor="job:default:nightly")
    heartbeat.record_check(saved, current.ref, "error", error="Source login expired")
    body = await page(client, f"/heartbeats/{current.ref}/history")
    # The summary says a person did this from Slack; the member ID is meaningless there.
    assert "<span>via Slack</span>" in body
    assert "<span>slack:U0AETSSDDEF</span>" not in body
    # Other origins already read, so they are shown exactly as recorded.
    assert "<span>job:default:nightly</span>" in body and "<span>heartbeat</span>" in body
    # The expanded body is the record: it keeps the raw value.
    assert "<dt>Actor</dt><dd>slack:U0AETSSDDEF</dd>" in body
    Anchors().feed(body)


async def test_run_detail_keeps_saved_definition_and_bounded_input_identity(client, saved):
    current = beat(saved, instructions="Original authorization")
    run = assessment(saved, current, output="<script>provider output</script>")
    heartbeat.update(saved, current.ref, {"instructions": "Changed authorization"})
    detail = await page(client, f"/heartbeats/runs/{run.id}")
    assert "Original authorization" in detail and "Changed authorization" not in detail
    for value in (
        "Input cutoff",
        str(run.input_cutoff),
        "new events",
        "wait",
        "Instructions revision",
    ):
        assert value in detail
    # A heartbeat's output is a provider transcript like a job run's, so it renders the
    # same way, with anything that looks like markup escaped rather than live.
    assert '<div class="markdown output-md">' in detail
    assert "&lt;script&gt;provider output&lt;/script&gt;" in detail
    assert "<script>provider output</script>" not in detail
    assert '<a href="/heartbeats" aria-current="page"' in detail or '/heartbeats"' in detail
    rows = await page(client, f"/heartbeats/{current.ref}/runs")
    assert f'href="/heartbeats/runs/{run.id}"' in rows and "provider output" not in rows
    Anchors().feed(detail)
    Anchors().feed(rows)


async def test_mixed_runs_source_status_job_and_pagination_keep_bounded_summaries(
    client, saved, monkeypatch
):
    monkeypatch.setattr(runs, "PAGE_SIZE", 2)
    db.initialize(saved.paths)
    write_job(saved.paths)
    job = load_job(saved.paths, saved)
    job_id = runs.start(saved.paths, job, "manual", effort="high")
    runs.finish(saved.paths, job_id, status="ok", output="job-output-sentinel" * 10000)
    current = beat(saved)
    first = assessment(saved, current, output="beat-output-sentinel" * 10000)
    second = assessment(saved, current, status="error")
    mixed = await page(client, "/runs?view=all")
    assert "Showing 1\u20132 of 3 runs" in mixed
    assert f'href="/heartbeats/runs/{first.id}"' in mixed
    assert f'href="/heartbeats/runs/{second.id}"' in mixed
    assert "output-sentinel" not in mixed
    assert "dinner with friends heartbeat" in mixed
    jobs = await page(client, "/runs?source=jobs")
    assert f'href="/runs/{job_id}"' in jobs and "/heartbeats/runs/" not in jobs
    beats = await page(client, "/runs?source=heartbeat&status=error")
    assert f'href="/heartbeats/runs/{second.id}"' in beats
    assert f'href="/heartbeats/runs/{first.id}"' not in beats
    assert "source=heartbeat" in beats and '<option value="error" selected' in beats
    retained_job = await page(client, "/runs?source=heartbeat&job=default%3Anightly")
    assert f'href="/heartbeats/runs/{first.id}"' in retained_job
    assert 'name="job" disabled>' in retained_job
    jobs = await page(client, "/runs?source=any&job=default%3Anightly")
    assert f'href="/runs/{job_id}"' in jobs and "/heartbeats/runs/" not in jobs
    summary = reading.activity(saved.paths)[0]
    assert not hasattr(summary, "output") and len(summary.error_preview or "") <= runs.ERROR_PREVIEW
    Anchors().feed(mixed)


async def test_today_includes_beat_activity_upcoming_and_attention_but_charts_jobs(client, saved):
    current = beat(saved, title="Dinner planning")
    run = assessment(saved, current, status="error")
    scheduled = beat(saved, title="Mortgage follow-up")
    schedule = await page(client, "/today")
    assert "Heartbeats needing attention" in schedule and "Job schedule" in schedule
    assert f'href="/heartbeats/{current.ref}"' in schedule
    assert f'href="/heartbeats/{scheduled.ref}"' in schedule and "Mortgage follow-up" in schedule
    activity = await page(client, "/today/activity")
    assert f'href="/heartbeats/runs/{run.id}"' in activity
    reliability = await page(client, "/today/reliability")
    assert (
        "latest runs per job" in reliability
        and 'class="row entity" href="/jobs/' not in reliability
    )
    save_config(saved.paths, {**saved.raw, "heartbeat": {"enabled": False}})
    disabled = await page(client, "/heartbeats")
    assert "Heartbeat is turned off" in disabled and "Dinner planning" in disabled
    schedule = await page(client, "/today")
    assert f'href="/heartbeats/{scheduled.ref}"' not in schedule


@pytest.mark.parametrize("value", ["²", "9" * 5000, "-1", "0", "wrong"])
async def test_invalid_page_values_are_safe_for_both_lists(client, saved, value):
    current = beat(saved)
    assert (await client.get("/heartbeats", params={"page": value})).status == 200
    assert (await client.get("/runs", params={"page": value})).status == 200
    assert (
        await client.get(f"/heartbeats/{current.ref}/history", params={"page": value})
    ).status == 200


@pytest.mark.parametrize(
    "path",
    [
        "/heartbeats/wrong",
        "/heartbeats/HB-999",
        "/heartbeats/HB-001/wrong",
        "/heartbeats/runs/unknown",
    ],
)
async def test_missing_refs_and_sections_return_404(client, saved, path):
    beat(saved)
    assert (await client.get(path)).status == 404


async def test_heartbeat_pages_are_get_only_and_do_not_change_persisted_state(client, saved):
    current = beat(saved)
    run = assessment(saved, current)
    before = saved.paths.db.read_bytes()
    for path in (
        "/heartbeats",
        f"/heartbeats/{current.ref}",
        f"/heartbeats/{current.ref}/history",
        f"/heartbeats/{current.ref}/runs",
        f"/heartbeats/runs/{run.id}",
    ):
        assert (await client.get(path)).status == 200
        assert (await client.post(path)).status == 405
    assert saved.paths.db.read_bytes() == before
