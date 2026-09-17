"""Observable heartbeat lifecycle, pending-input boundaries, and action recovery."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import beat_runs

from enso import db, heartbeat
from enso.config import Agent, WorkspaceConfig
from enso.heartbeat.validation import timestamp


@pytest.fixture
def definition():
    return {
        "title": "Refund for order 123",
        "instructions": "Follow this refund until it reaches my account.",
        "completion": "The money posted and the receipt is saved.",
        "allowed_actions": "Read order status and notify me.",
        "workspace": "default",
        "schedule": "*/5 * * * *",
        "llm_checks": True,
    }


@pytest.fixture
def clock(monkeypatch):
    current = datetime(2030, 1, 2, 10, tzinfo=UTC)
    monkeypatch.setattr(db, "now", lambda: current.isoformat(timespec="microseconds"))
    return current


def active(config, definition):
    beat = heartbeat.create(config, definition)
    return heartbeat.resume(config, beat.ref)


def test_definition_validation_collects_independent_problems(config, definition):
    invalid = {
        **definition,
        "title": "",
        "workspace": "../outside",
        "agent": {"provider": "claude", "model": "missing", "effort": "wrong"},
        "schedule": "@daily",
        "timezone": "Wrong/Zone",
        "gate": "../../run.sh",
        "origin": [],
        "timeout": True,
        "unknown": True,
    }
    with pytest.raises(heartbeat.HeartbeatError) as failure:
        heartbeat.create(config, invalid)
    problems = failure.value.problems
    for field in (
        "title",
        "workspace",
        "agent.model",
        "agent.effort",
        "schedule",
        "timezone",
        "gate",
        "origin",
        "timeout",
        "unknown",
    ):
        assert any(field in problem for problem in problems), problems
    assert not config.paths.db.exists()


@pytest.mark.parametrize(
    "value", ["2030-01-02", "2030-01-02T08:30:00", "0001-01-01T00:00:00+23:00"]
)
def test_timestamps_refuse_implicit_timezones_and_overflow(value):
    with pytest.raises(heartbeat.HeartbeatError, match="UTC offset"):
        timestamp(value, "at")


def test_create_snapshots_agent_origin_and_starts_paused(config, definition, clock):
    agent = Agent("codex", "sol", "high")
    configured = replace(config, workspaces={"default": WorkspaceConfig(agent=agent)})
    beat = heartbeat.create(
        configured,
        {
            **definition,
            "origin": {"transport": "slack", "channel": "C2", "thread_ts": "123.456"},
        },
    )
    assert beat.ref == "HB-001" and beat.state == "paused"
    assert beat.agent == agent
    assert beat.notify == "slack:C2" and beat.notify_thread == "123.456"
    assert heartbeat.due(config, clock + timedelta(days=1)) == []
    assert heartbeat.get(config.paths, "hb-1").agent == agent
    assert heartbeat.list_beats(config.paths, workspace="default") == [beat]
    assert heartbeat.list_beats(config.paths, workspace="other") == []


def test_ungated_recurring_checks_require_explicit_disclosure(config, definition):
    del definition["llm_checks"]
    with pytest.raises(heartbeat.HeartbeatError, match="LLM on every due check"):
        heartbeat.create(config, definition)
    gated = heartbeat.create(config, {**definition, "gate": "gate.sh"})
    with pytest.raises(heartbeat.HeartbeatError, match="validate"):
        heartbeat.resume(config, gated.ref)
    assert heartbeat.get(config.paths, gated.ref).state == "paused"


def test_resume_checks_gate_without_execution_and_rejects_bad_paths(config, definition, tmp_path):
    beat = heartbeat.create(config, {**definition, "gate": "gate.sh"})
    directory = config.paths.workspace_heartbeat(beat.workspace) / beat.ref
    directory.mkdir(parents=True)
    marker = tmp_path / "executed"
    gate = directory / "gate.sh"
    gate.write_text(f"#!/bin/bash\ntouch '{marker}'\nexit 1\n")
    assert heartbeat.resume(config, beat.ref).state == "active"
    assert not marker.exists()
    heartbeat.pause(config, beat.ref)
    gate.write_text("if true; then\nsecret_value=do-not-display\n")
    with pytest.raises(heartbeat.HeartbeatError, match="syntax") as error:
        heartbeat.resume(config, beat.ref)
    assert "do-not-display" not in str(error.value)
    gate.unlink()
    outside = tmp_path / "outside.sh"
    outside.write_text("exit 0\n")
    gate.symlink_to(outside)
    with pytest.raises(heartbeat.HeartbeatError, match="symlinks"):
        heartbeat.resume(config, beat.ref)


def test_gate_size_and_directory_symlinks_are_rejected(config, definition, tmp_path):
    beat = heartbeat.create(config, {**definition, "gate": "gate.sh"})
    directory = config.paths.workspace_heartbeat(beat.workspace) / beat.ref
    directory.mkdir(parents=True)
    (directory / "gate.sh").write_text("#" * (64 * 1024 + 1))
    with pytest.raises(heartbeat.HeartbeatError, match="64 KiB"):
        heartbeat.resume(config, beat.ref)
    (directory / "gate.sh").unlink()
    directory.rmdir()
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(heartbeat.HeartbeatError, match="symlinks"):
        heartbeat.resume(config, beat.ref)


def test_recorded_workspace_cannot_be_transferred(config, definition):
    config.paths.workspace("team").mkdir()
    beat = heartbeat.create(config, definition)
    with pytest.raises(heartbeat.HeartbeatError, match="workspace cannot be changed"):
        heartbeat.update(config, beat.ref, {"workspace": "team"})
    assert heartbeat.get(config.paths, beat.ref) == beat
    assert len(heartbeat.history(config.paths, beat.ref)) == 1


def test_check_history_records_transitions_without_per_poll_rows(config, definition, clock):
    beat = active(config, definition)
    initial = len(heartbeat.history(config.paths, beat.ref))
    for minute in range(1, 20):
        assert (
            heartbeat.record_check(
                config, beat.ref, "quiet", checked_at=clock + timedelta(minutes=minute)
            )
            is None
        )
    assert len(heartbeat.history(config.paths, beat.ref)) == initial
    failed = heartbeat.record_check(config, beat.ref, "error", error="Mail service unavailable")
    assert failed.kind == "check_failed"
    assert heartbeat.record_check(config, beat.ref, "error", error="Still unavailable") is None
    assert heartbeat.get(config.paths, beat.ref).attention
    recovered = heartbeat.record_check(config, beat.ref, "quiet")
    assert recovered.kind == "check_recovered"
    state = heartbeat.get(config.paths, beat.ref)
    assert not state.attention and state.last_check_error is None
    assert state.last_success_at == db.now()
    assert len(heartbeat.history(config.paths, beat.ref)) == initial + 2


def test_observations_deduplicate_pending_and_wait_acknowledges_only_run_input(config, definition):
    beat = active(config, definition)
    first = heartbeat.persist_observation(config, beat.ref, "Refund approved")
    assert heartbeat.persist_observation(config, beat.ref, "Refund approved") == first
    run = heartbeat.start_run(config, beat.ref)
    newer = heartbeat.persist_observation(config, beat.ref, "Bank transfer started")
    packet = heartbeat.context(config.paths, beat.ref, run_id=run.id)
    assert packet["latest_event"]["id"] == first.id and packet["unhandled_count"] == 1
    assert packet["history_command"] == f"enso heartbeat history {beat.ref} --json"
    with pytest.raises(heartbeat.HeartbeatError, match="new observations"):
        heartbeat.complete(config, beat.ref, message="Finished", run_id=run.id)
    result = heartbeat.wait(
        config,
        beat.ref,
        message="Approved; transfer remains pending",
        run_id=run.id,
        checkpoint={"seen": "approved"},
    )
    assert result.checkpoint == {"seen": "approved"}
    assert result.handled_event_id == run.input_cutoff
    assert heartbeat.history(config.paths, beat.ref, unhandled=True) == [newer]
    heartbeat.finish_run(config, run.id, status="ok")
    again = heartbeat.persist_observation(config, beat.ref, "Refund approved")
    assert again.id > first.id


def test_event_history_paginates_and_preserves_source_time(config, definition):
    beat = heartbeat.create(config, definition)
    first = heartbeat.note(config, beat.ref, "First", occurred_at="2029-12-01T08:00:00-08:00")
    second = heartbeat.note(config, beat.ref, "Second")
    third = heartbeat.note(config, beat.ref, "Third")
    assert heartbeat.history(config.paths, beat.ref, kind="noted", limit=2) == [third, second]
    assert heartbeat.history(config.paths, beat.ref, before_id=second.id, kind="noted") == [first]
    assert heartbeat.history(config.paths, beat.ref, after_id=first.id) == [second, third]
    assert first.payload["occurred_at"] == "2029-12-01T16:00:00.000000+00:00"
    assert "occurred_at" not in second.payload


def test_revision_changes_block_stale_completion_and_claims(config, definition):
    beat = active(config, definition)
    run = heartbeat.start_run(config, beat.ref)
    with pytest.raises(heartbeat.HeartbeatError, match="already held"):
        heartbeat.start_run(config, beat.ref)
    changed = heartbeat.update(
        config, beat.ref, {"instructions": "Wait for my approval before notifying."}
    )
    with pytest.raises(heartbeat.HeartbeatError, match="changed"):
        heartbeat.complete(config, beat.ref, message="Done", run_id=run.id)
    with pytest.raises(heartbeat.HeartbeatError, match="changed"):
        heartbeat.update(
            config, beat.ref, {"title": "Stale title"}, expected_revision=beat.revision
        )
    with pytest.raises(heartbeat.HeartbeatError, match="cannot edit"):
        heartbeat.update(config, beat.ref, {"title": "Run edit"}, run_id=run.id)
    event = heartbeat.history(config.paths, beat.ref, kind="updated")[0]
    assert event.payload["changes"]["instructions"] == {
        "before": definition["instructions"],
        "after": changed.instructions,
    }
    assert (
        heartbeat.get_run(config.paths, run.id).definition.instructions
        == definition["instructions"]
    )


def test_cancel_stops_settlement_and_keeps_history(config, definition):
    beat = active(config, definition)
    run = heartbeat.start_run(config, beat.ref)
    heartbeat.cancel(config, beat.ref, message="No longer needed")
    with pytest.raises(heartbeat.HeartbeatError):
        heartbeat.wait(config, beat.ref, message="Keep waiting", run_id=run.id)
    heartbeat.finish_run(config, run.id, status="cancelled")
    assert heartbeat.list_beats(config.paths) == []
    assert heartbeat.list_beats(config.paths, state="cancelled")[0].ref == beat.ref
    with pytest.raises(heartbeat.HeartbeatError, match="closed"):
        heartbeat.resume(config, beat.ref)


def test_uncertain_actions_prevent_duplicate_sends_and_fulfillment(config, definition):
    beat = active(config, definition)
    run = heartbeat.start_run(config, beat.ref)
    heartbeat.begin_action(
        config, beat.ref, "notify-approval", "Send approval notice", run_id=run.id
    )
    with pytest.raises(heartbeat.HeartbeatError, match="pending"):
        heartbeat.begin_action(config, beat.ref, "notify-approval", "Again", run_id=run.id)
    with pytest.raises(heartbeat.HeartbeatError, match="pending"):
        heartbeat.complete(config, beat.ref, message="Done", run_id=run.id)
    heartbeat.finish_run(config, run.id, status="error", error="Connection lost")
    latest = heartbeat.context(config.paths, beat.ref)["actions"][0]
    assert latest["action_status"] == "uncertain"
    next_run = heartbeat.start_run(config, beat.ref)
    with pytest.raises(heartbeat.HeartbeatError, match="uncertain"):
        heartbeat.begin_action(config, beat.ref, "notify-approval", "Again", run_id=next_run.id)
    heartbeat.resolve_action(
        config,
        beat.ref,
        "notify-approval",
        "succeeded",
        "Found the delivered message",
        receipt="message-123",
    )
    with pytest.raises(heartbeat.HeartbeatError, match="succeeded"):
        heartbeat.begin_action(config, beat.ref, "notify-approval", "Again", run_id=next_run.id)
    completed = heartbeat.complete(
        config, beat.ref, message="Approved, notified, receipt saved", run_id=next_run.id
    )
    assert completed.state == "fulfilled"


@pytest.mark.parametrize("change", ["edit", "pause", "cancel", "finish"])
def test_original_action_can_report_known_result_after_invalidation(config, definition, change):
    beat = active(config, definition)
    run = heartbeat.start_run(config, beat.ref)
    heartbeat.begin_action(config, beat.ref, "send", "Send the confirmation", run_id=run.id)
    if change == "edit":
        heartbeat.update(config, beat.ref, {"title": "Updated title"})
    elif change == "finish":
        heartbeat.finish_run(config, run.id, status="cancelled")
    else:
        getattr(heartbeat, change)(config, beat.ref)
    receipt = heartbeat.resolve_action(
        config,
        beat.ref,
        "send",
        "succeeded",
        "Delivery confirmed",
        receipt="message-123",
        run_id=run.id,
    )
    assert receipt.receipt == "message-123" and receipt.action_status == "succeeded"
    with pytest.raises(heartbeat.HeartbeatError):
        heartbeat.begin_action(config, beat.ref, "another", "Another send", run_id=run.id)


def test_failed_action_can_retry_but_foreign_run_cannot_resolve_it(config, definition):
    beat = active(config, definition)
    run = heartbeat.start_run(config, beat.ref)
    heartbeat.begin_action(config, beat.ref, "send", "Send", run_id=run.id)
    other = active(config, definition)
    other_run = heartbeat.start_run(config, other.ref)
    with pytest.raises(heartbeat.HeartbeatError, match="does not belong"):
        heartbeat.resolve_action(
            config, beat.ref, "send", "succeeded", "Pretend delivered", run_id=other_run.id
        )
    heartbeat.resolve_action(
        config, beat.ref, "send", "failed", "Service confirmed no delivery", run_id=run.id
    )
    assert (
        heartbeat.begin_action(
            config, beat.ref, "send", "Retry confirmed failure", run_id=run.id
        ).action_status
        == "pending"
    )


def test_success_without_explicit_settlement_retains_pending_input(config, definition):
    beat = active(config, definition)
    observation = heartbeat.persist_observation(config, beat.ref, "A reply arrived")
    run = heartbeat.start_run(config, beat.ref)
    result = heartbeat.finish_run(config, run.id, status="ok", output="All done")
    assert result.status == "error" and "without recording" in result.error
    assert heartbeat.history(config.paths, beat.ref, unhandled=True) == [observation]
    assert heartbeat.get(config.paths, beat.ref).attention
    assert heartbeat.finish_run(config, run.id, status="ok") == result


def test_one_shot_followup_survives_resume_and_unrelated_edits(config, definition, clock):
    definition.pop("schedule")
    definition["at"] = (clock + timedelta(minutes=1)).isoformat()
    beat = active(config, definition)
    check_at = clock + timedelta(minutes=2)
    heartbeat.record_check(config, beat.ref, "ready", checked_at=check_at)
    run = heartbeat.start_run(config, beat.ref)
    followup = (clock + timedelta(hours=2)).isoformat()
    waited = heartbeat.wait(
        config,
        beat.ref,
        message="Reply sent; wait for agreement",
        run_id=run.id,
        followup_at=followup,
    )
    assert waited.next_check_at == timestamp(followup, "followup_at")
    heartbeat.finish_run(config, run.id, status="ok")
    heartbeat.pause(config, beat.ref)
    assert heartbeat.resume(config, beat.ref).next_check_at == waited.next_check_at
    assert (
        heartbeat.update(config, beat.ref, {"title": "Still awaiting agreement"}).next_check_at
        == waited.next_check_at
    )
    assert heartbeat.due(config, clock + timedelta(hours=1)) == []
    assert [b.ref for b in heartbeat.due(config, clock + timedelta(hours=3))] == [beat.ref]


def test_consumed_one_shot_requires_explicit_rescheduling(config, definition, clock):
    definition.pop("schedule")
    definition["at"] = clock.isoformat()
    beat = active(config, definition)
    event = heartbeat.record_check(config, beat.ref, "quiet", checked_at=clock)
    assert event.kind == "one_shot_not_ready"
    assert heartbeat.get(config.paths, beat.ref).attention
    heartbeat.pause(config, beat.ref)
    heartbeat.resume(config, beat.ref)
    assert heartbeat.due(config, clock + timedelta(days=1)) == []
    new_at = (clock + timedelta(hours=1)).isoformat()
    updated = heartbeat.update(config, beat.ref, {"at": new_at})
    assert not updated.at_consumed
    assert heartbeat.due(config, clock + timedelta(hours=2))[0].ref == beat.ref


def test_recovery_and_pruning_skip_live_locks_and_never_reuse_ids(config, definition, clock):
    beat = active(config, definition)
    run = heartbeat.start_run(config, beat.ref)
    lock = heartbeat.acquire_lock(config.paths, beat.ref)
    assert lock is not None
    with lock:
        assert heartbeat.recover(config) == []
    assert heartbeat.recover(config)[0].id == run.id
    assert heartbeat.recover(config) == []
    heartbeat.cancel(config, beat.ref)
    future = clock + timedelta(days=40)
    lock_file = config.paths.heartbeat / ".locks" / f"{beat.ref}.lock"
    lock = heartbeat.acquire_lock(config.paths, beat.ref)
    with lock:
        assert heartbeat.prune(config, now=future) == {}
        assert heartbeat.get(config.paths, beat.ref) is not None
    assert heartbeat.prune(config, now=future) == {beat.ref: "1 undelivered notification(s)"}
    heartbeat.notice_delivered(config, heartbeat.pending_notices(config.paths)[0], 1)
    assert heartbeat.prune(config, now=future) == {}
    assert heartbeat.get(config.paths, beat.ref) is None and not lock_file.exists()
    assert heartbeat.history(config.paths, beat.ref) == beat_runs(config.paths, beat.ref) == []
    assert heartbeat.create(config, definition).ref == "HB-002"


def test_disabled_heartbeat_preserves_records_and_blocks_execution(config, definition, clock):
    beat = active(config, definition)
    disabled = replace(config, heartbeat=replace(config.heartbeat, enabled=False))
    assert heartbeat.due(disabled, clock + timedelta(days=1)) == []
    with pytest.raises(heartbeat.HeartbeatError, match="disabled"):
        heartbeat.start_run(disabled, beat.ref)
    with pytest.raises(heartbeat.HeartbeatError, match="disabled"):
        heartbeat.update(disabled, beat.ref, {"title": "Change active intent"})
    heartbeat.pause(disabled, beat.ref)
    with pytest.raises(heartbeat.HeartbeatError, match="disabled"):
        heartbeat.resume(disabled, beat.ref)
    heartbeat.cancel(disabled, beat.ref)
    heartbeat.acquire_lock(config.paths, beat.ref).close()
    assert heartbeat.prune(disabled, now=clock + timedelta(days=60)) == {}
    assert heartbeat.get(config.paths, beat.ref).state == "cancelled"
    assert (config.paths.heartbeat / ".locks" / f"{beat.ref}.lock").is_file()


def test_pruning_keeps_unreconciled_actions_and_undelivered_notices(config, definition, clock):
    future = clock + timedelta(days=40)
    acted = active(config, definition)
    run = heartbeat.start_run(config, acted.ref)
    heartbeat.begin_action(config, acted.ref, "send-email", "Sending", run_id=run.id)
    heartbeat.cancel(config, acted.ref)
    heartbeat.finish_run(config, run.id, status="cancelled")
    expired = heartbeat.expire(config, active(config, definition).ref)
    quiet = replace(config, slack=replace(config.slack, notify=""))
    unaddressed = heartbeat.expire(quiet, active(quiet, definition).ref)
    assert unaddressed.notify is None
    assert heartbeat.prune(config, now=future) == {
        acted.ref: "1 unresolved action(s) and 1 undelivered notification(s)",
        expired.ref: "1 undelivered notification(s)",
    }
    assert heartbeat.get(config.paths, unaddressed.ref) is None
    heartbeat.resolve_action(config, acted.ref, "send-email", "failed", "The email never left")
    assert heartbeat.prune(config, now=future) == {
        acted.ref: "1 undelivered notification(s)",
        expired.ref: "1 undelivered notification(s)",
    }
    for outbox_id, notice in enumerate(heartbeat.pending_notices(config.paths), start=1):
        heartbeat.notice_delivered(config, notice, outbox_id)
    assert heartbeat.prune(config, now=future) == {}
    assert heartbeat.list_beats(config.paths, include_closed=True) == []


def test_pruning_removes_scripts_before_records_and_reclaims_orphan_locks(
    config, definition, clock, monkeypatch
):
    from enso.heartbeat import store

    future = clock + timedelta(days=40)
    gated = heartbeat.create(config, {**definition, "gate": "gate.sh"})
    directory = config.paths.workspace_heartbeat(gated.workspace) / gated.ref
    directory.mkdir(parents=True)
    (directory / "gate.sh").write_text("exit 1\n")
    heartbeat.cancel(config, heartbeat.resume(config, gated.ref).ref)
    config.paths.workspace("team").mkdir()
    homeless = active(config, {**definition, "workspace": "team"})
    heartbeat.cancel(config, homeless.ref)
    shutil.rmtree(config.paths.workspace("team"))
    locks = config.paths.heartbeat / ".locks"
    locks.mkdir(parents=True)
    for name in ("HB-042.lock", "HB-7.lock", "notes.txt"):
        (locks / name).write_text("")

    def refused(path):
        raise PermissionError(path)

    monkeypatch.setattr(store.shutil, "rmtree", refused)
    assert heartbeat.prune(config, now=future) == {
        gated.ref: "its script directory could not be removed (PermissionError)"
    }
    assert heartbeat.get(config.paths, gated.ref).state == "cancelled"
    assert (directory / "gate.sh").is_file() and (locks / f"{gated.ref}.lock").is_file()
    assert heartbeat.get(config.paths, homeless.ref) is None
    monkeypatch.undo()
    assert heartbeat.prune(config, now=future) == {}
    assert heartbeat.get(config.paths, gated.ref) is None and not directory.exists()
    assert sorted(path.name for path in locks.iterdir()) == ["HB-7.lock", "notes.txt"]


def test_lock_taken_on_an_unlinked_inode_is_stale(config, monkeypatch):
    from enso.heartbeat import store

    real = store.acquire_file_lock

    def raced(path):
        lock = real(path)
        path.unlink()  # pruning removed the file between this open and its flock
        return lock

    monkeypatch.setattr(store, "acquire_file_lock", raced)
    assert heartbeat.acquire_lock(config.paths, "HB-001") is None
    monkeypatch.undo()
    lock = heartbeat.acquire_lock(config.paths, "HB-001")
    assert lock is not None
    lock.close()


def test_expiry_is_not_fulfillment_and_requires_attention(config, definition, clock):
    beat = active(config, {**definition, "expires_at": (clock + timedelta(minutes=1)).isoformat()})
    assert heartbeat.due(config, clock + timedelta(minutes=2))[0].ref == beat.ref
    expired = heartbeat.expire(config, beat.ref)
    assert expired.state == "expired" and expired.attention
    assert (
        heartbeat.history(config.paths, beat.ref)[0].message
        == "Deadline passed without fulfillment"
    )


def test_early_one_shot_followup_is_rejected_without_changing_original_time(
    config, definition, clock
):
    definition.pop("schedule")
    definition["at"] = (clock + timedelta(hours=1)).isoformat()
    followup = (clock + timedelta(minutes=1)).isoformat()
    with pytest.raises(heartbeat.HeartbeatError, match="first assessment"):
        heartbeat.create(config, {**definition, "followup_at": followup})
    beat = active(config, definition)
    with pytest.raises(heartbeat.HeartbeatError, match="first assessment"):
        heartbeat.update(config, beat.ref, {"followup_at": followup})
    assert heartbeat.get(config.paths, beat.ref) == beat


def test_expiry_blocks_new_effects_and_settlement_but_keeps_factual_receipts(
    config, definition, clock, monkeypatch
):
    beat = active(config, {**definition, "expires_at": (clock + timedelta(minutes=1)).isoformat()})
    run = heartbeat.start_run(config, beat.ref)
    heartbeat.begin_action(config, beat.ref, "send", "Authorized send", run_id=run.id)
    monkeypatch.setattr(
        db, "now", lambda: (clock + timedelta(minutes=2)).isoformat(timespec="microseconds")
    )
    with pytest.raises(heartbeat.HeartbeatError, match="expires_at"):
        heartbeat.assert_run_active(config, beat.ref, run.id)
    with pytest.raises(heartbeat.HeartbeatError, match="expires_at"):
        heartbeat.begin_action(config, beat.ref, "another", "Another send", run_id=run.id)
    receipt = heartbeat.resolve_action(
        config,
        beat.ref,
        "send",
        "succeeded",
        message="Sent before expiry",
        receipt="message-123",
        run_id=run.id,
    )
    assert receipt.receipt == "message-123"
    with pytest.raises(heartbeat.HeartbeatError, match="expires_at"):
        heartbeat.complete(config, beat.ref, message="Finished", run_id=run.id)
    heartbeat.finish_run(config, run.id, status="cancelled", error="Expired")
    with pytest.raises(heartbeat.HeartbeatError, match="expires_at"):
        heartbeat.start_run(config, beat.ref)


def test_expiry_revision_guard_preserves_concurrent_deadline_extension(config, definition, clock):
    beat = active(config, {**definition, "expires_at": (clock + timedelta(minutes=1)).isoformat()})
    updated = heartbeat.update(
        config, beat.ref, {"expires_at": (clock + timedelta(hours=1)).isoformat()}
    )
    with pytest.raises(heartbeat.HeartbeatError, match="changed"):
        heartbeat.expire(config, beat.ref, expected_revision=beat.revision)
    assert heartbeat.get(config.paths, beat.ref) == updated
