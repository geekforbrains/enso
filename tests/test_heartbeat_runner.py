"""Heartbeat admission, durable evidence, cancellation, and notification boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeTransport, beat_runs, write_workspace

from enso import db, execution, heartbeat
from enso.config import load_config, save_config
from enso.heartbeat import runner as module
from enso.heartbeat.runner import HeartbeatRunner


@pytest.fixture
def runtime(config, monkeypatch):
    save_config(config.paths, config.raw)
    clock = [datetime(2030, 1, 2, 10, tzinfo=UTC)]
    monkeypatch.setattr(db, "now", lambda: clock[0].isoformat(timespec="microseconds"))
    monkeypatch.setattr(module, "WATCH_SECONDS", 0.01)
    return config, clock


def make_beat(config, *, gate=None, **changes):
    data = {
        "title": "Refund for order 123",
        "instructions": "Follow this refund until it reaches my account.",
        "completion": "The refund posted and the receipt was saved.",
        "allowed_actions": "Read the refund status and notify me.",
        "workspace": "default",
        "schedule": "*/5 * * * *",
        "llm_checks": True,
        **changes,
    }
    if data.get("at"):
        data.pop("schedule")
    if gate is not None:
        data["gate"] = "gate.sh"
    beat = heartbeat.create(config, data)
    if gate is not None:
        directory = config.paths.workspace_heartbeat(beat.workspace) / beat.ref
        directory.mkdir(parents=True)
        (directory / "gate.sh").write_text(gate)
    return heartbeat.resume(config, beat.ref)


async def drain(runner):
    tasks = list(runner._running.values())
    if runner._notice_task is not None:
        tasks.append(runner._notice_task)
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            raise result


async def tick(runner, clock, *, minutes=5):
    clock[0] += timedelta(minutes=minutes)
    await runner.tick(clock[0])
    await drain(runner)


def settle(config, env, *, complete=False, **kwargs):
    fn = heartbeat.complete if complete else heartbeat.wait
    return fn(
        config,
        env["ENSO_BEAT"],
        run_id=env["ENSO_BEAT_RUN_ID"],
        message="Receipt checked; progress is recorded",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_quiet_gate_only_updates_metadata(runtime, monkeypatch):
    config, clock = runtime
    beat = make_beat(config, gate="exit 1\n")

    async def forbidden(*args, **kwargs):
        pytest.fail("quiet gate must not invoke a provider")

    monkeypatch.setattr(execution, "execute_turn", forbidden)
    runner = HeartbeatRunner(config)
    await tick(runner, clock)
    first_check = heartbeat.get(config.paths, beat.ref).last_check_at
    await tick(runner, clock, minutes=1)
    assert heartbeat.get(config.paths, beat.ref).last_check_at == first_check
    assert (
        heartbeat.due_reason(config.paths, heartbeat.get(config.paths, beat.ref), clock[0]) is None
    )
    await tick(runner, clock)
    saved = heartbeat.get(config.paths, beat.ref)
    assert saved.last_check_status == "quiet" and saved.last_success_at == db.now()
    assert beat_runs(config.paths, beat.ref) == []
    assert [event.kind for event in heartbeat.history(config.paths, beat.ref)] == [
        "resumed",
        "created",
    ]


@pytest.mark.asyncio
async def test_restart_keeps_workspace_gate_helpers_provider_and_action_receipts(
    runtime, monkeypatch
):
    config, clock = runtime
    workspace = config.paths.workspace("team")
    workspace.mkdir()
    write_workspace(config.paths, "team", {"providers": {"claude": {"args": ["--team"]}}})
    beat = make_beat(config, workspace="team", gate=". ./helper.sh\n")
    directory = config.paths.workspace_heartbeat("team") / beat.ref
    (directory / "helper.sh").write_text('printf "%s\\n%s\\n" "$PWD" "$ENSO_WORKSPACE"\n')
    calls = []

    async def assess(provider, prompt, model, effort, args, **kwargs):
        env = kwargs["env"]
        calls.append(env["ENSO_BEAT_RUN_ID"])
        assert kwargs["cwd"] == workspace and args == ("--team",)
        assert env["ENSO_WORKSPACE"] == "team"
        assert f"{directory}\nteam" in prompt
        run = heartbeat.get_run(config.paths, env["ENSO_BEAT_RUN_ID"])
        assert run.definition.workspace == "team"
        if len(calls) == 1:
            heartbeat.begin_action(config, beat.ref, "notify", "Notify once", run_id=run.id)
            heartbeat.resolve_action(
                config,
                beat.ref,
                "notify",
                "succeeded",
                "Delivered",
                receipt="mail:receipt",
                run_id=run.id,
            )
        else:
            with pytest.raises(heartbeat.HeartbeatError, match="succeeded"):
                heartbeat.begin_action(config, beat.ref, "notify", "Retry", run_id=run.id)
        settle(config, env, complete=len(calls) == 2)
        return execution.ProviderTurn("ok", session_id=f"session-{len(calls)}")

    monkeypatch.setattr(execution, "execute_turn", assess)
    first = HeartbeatRunner(load_config(config.paths))
    await tick(first, clock)
    await first.stop()
    # The new process inherits another workspace context and changed agent defaults.
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
    changed = {**config.raw, "defaults": {"provider": "codex", "model": "sol", "effort": "high"}}
    save_config(config.paths, changed)
    restarted = HeartbeatRunner(load_config(config.paths))
    assert restarted.recover() == 0
    await tick(restarted, clock)
    await tick(restarted, clock)
    saved = heartbeat.get(config.paths, beat.ref)
    assert saved.workspace == "team" and saved.state == "fulfilled"
    assert len(calls) == 2
    assert {run.definition.agent.provider for run in beat_runs(config.paths, beat.ref)} == {
        "claude"
    }
    assert not config.paths.workspace_heartbeat("default").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["workspace", "heartbeat", "missing-workspace"])
async def test_relocated_gate_never_follows_a_linked_parent(runtime, monkeypatch, tmp_path, parent):
    config, clock = runtime
    config.paths.workspace("team").mkdir()
    beat = make_beat(config, workspace="team", gate="echo should-not-run\n")
    root = (
        config.paths.workspace("team")
        if parent in ("workspace", "missing-workspace")
        else config.paths.workspace_heartbeat("team")
    )
    target = tmp_path / "moved"
    root.rename(target)
    if parent != "missing-workspace":
        root.symlink_to(target, target_is_directory=True)

    with pytest.raises(heartbeat.HeartbeatError, match=r"link|missing"):
        heartbeat.validate_gate(config.paths, beat)

    async def forbidden(*args, **kwargs):
        pytest.fail("an unsafe gate must not run or start a provider")

    monkeypatch.setattr(execution, "run_process", forbidden)
    monkeypatch.setattr(execution, "execute_turn", forbidden)
    await tick(HeartbeatRunner(config), clock)
    saved = heartbeat.get(config.paths, beat.ref)
    assert saved.workspace == "team"
    if parent == "workspace":
        # Live config rejects linked workspace identities before admitting any work.
        assert saved.last_check_status is None
    else:
        assert saved.last_check_status == "error"
        assert "link" in saved.last_check_error or "missing" in saved.last_check_error
    assert beat_runs(config.paths, beat.ref) == []


def test_beat_env_replaces_inherited_identity_with_this_beat(runtime, monkeypatch):
    config, _clock = runtime
    beat = make_beat(config)
    for key in ("ENSO_JOB", "ENSO_BEAT", "ENSO_BEAT_RUN_ID", "ENSO_ORIGIN_CHANNEL"):
        monkeypatch.setenv(key, "inherited")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "kept")
    env = module.beat_env(config, beat, run_id="run-1")
    assert env["SLACK_BOT_TOKEN"] == "kept"
    assert "ENSO_JOB" not in env and not any(key.startswith("ENSO_ORIGIN_") for key in env)
    assert env["ENSO_BEAT"] == beat.ref and env["ENSO_BEAT_RUN_ID"] == "run-1"
    assert env["ENSO_HOME"] == str(config.paths.home) and env["ENSO_WORKSPACE"] == "default"
    assert json.loads(env["ENSO_BEAT_CHECKPOINT"]) == {}
    assert "ENSO_BEAT_RUN_ID" not in module.beat_env(config, beat)


@pytest.mark.asyncio
async def test_ready_gate_persists_bounded_evidence_before_compact_provider_prompt(
    runtime, monkeypatch
):
    config, clock = runtime
    # The newline must not hide the fact that the subprocess output was clipped.
    code = "print('x' * 20000)"
    beat = make_beat(config, gate=f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}\n")
    calls = []

    async def assess(provider, prompt, model, effort, args, **kwargs):
        env = kwargs["env"]
        calls.append(prompt)
        assert env["ENSO_HOME"] == str(config.paths.home)
        assert json.loads(env["ENSO_BEAT_CHECKPOINT"]) == {}
        evidence = heartbeat.history(config.paths, beat.ref, unhandled=True)
        assert len(evidence) == 1 and "clipped" in evidence[0].message
        assert len(evidence[0].message.encode()) <= module.GATE_OUTPUT_LIMIT
        assert heartbeat.get(config.paths, beat.ref).checkpoint == {}
        assert "--unhandled --before" in prompt and '"input_cutoff"' in prompt
        assert '"history_count"' in prompt and "untrusted data" in prompt
        assert len(prompt) < 23000
        settle(config, env, checkpoint={"last_id": 7})
        return execution.ProviderTurn("ok", output="Progress recorded", exit_code=0)

    monkeypatch.setattr(execution, "execute_turn", assess)
    await tick(HeartbeatRunner(config), clock)
    saved = heartbeat.get(config.paths, beat.ref)
    assert calls and saved.checkpoint == {"last_id": 7} and saved.state == "active"
    assert heartbeat.history(config.paths, beat.ref, unhandled=True) == []
    assert beat_runs(config.paths, beat.ref)[0].status == "ok"


@pytest.mark.asyncio
async def test_gate_failure_never_falls_back_and_recovery_handles_pending_on_quiet(
    runtime, monkeypatch
):
    config, clock = runtime
    beat = make_beat(
        config, gate="echo 'raw-secret' >&2\necho 'ENSO_ERROR: source unavailable' >&2\nexit 2\n"
    )
    heartbeat.note(config, beat.ref, "New source evidence")
    calls = []

    async def assess(*args, **kwargs):
        calls.append(True)
        settle(config, kwargs["env"])
        return execution.ProviderTurn("ok")

    monkeypatch.setattr(execution, "execute_turn", assess)
    runner = HeartbeatRunner(config)
    await tick(runner, clock)
    first_check = heartbeat.get(config.paths, beat.ref).last_check_at
    await tick(runner, clock, minutes=1)
    assert heartbeat.get(config.paths, beat.ref).last_check_at == first_check
    assert (
        heartbeat.due_reason(config.paths, heartbeat.get(config.paths, beat.ref), clock[0]) is None
    )
    await tick(runner, clock)
    assert calls == []
    events = heartbeat.history(config.paths, beat.ref, kind="check_failed")
    assert len(events) == 1 and events[0].message == "source unavailable"
    assert heartbeat.get(config.paths, beat.ref).attention
    (config.paths.workspace_heartbeat(beat.workspace) / beat.ref / "gate.sh").write_text("exit 1\n")
    await tick(runner, clock)
    assert calls == [True]
    assert len(heartbeat.history(config.paths, beat.ref, kind="check_recovered")) == 1
    assert not heartbeat.get(config.paths, beat.ref).attention


@pytest.mark.asyncio
async def test_provider_exit_does_not_fulfill_or_spin_same_failed_input(runtime, monkeypatch):
    config, clock = runtime
    beat = make_beat(config, gate="echo 'refund pending'\n")
    calls = []

    async def assess(*args, **kwargs):
        calls.append(True)
        return execution.ProviderTurn("ok", output="All done")

    monkeypatch.setattr(execution, "execute_turn", assess)
    runner = HeartbeatRunner(config)
    await tick(runner, clock)
    await tick(runner, clock, minutes=1)
    saved = heartbeat.get(config.paths, beat.ref)
    assert calls == [True] and saved.state == "active" and saved.attention
    assert beat_runs(config.paths, beat.ref)[0].status == "error"
    assert len(heartbeat.history(config.paths, beat.ref, unhandled=True)) == 1
    heartbeat.note(config, beat.ref, "A new receipt became available")
    await tick(runner, clock, minutes=1)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_future_one_shot_waits_despite_notes_and_late_wake_assesses_once(
    runtime, monkeypatch
):
    config, clock = runtime
    planned = clock[0] + timedelta(hours=1)
    beat = make_beat(config, at=planned.isoformat())
    heartbeat.note(config, beat.ref, "Additional context, not permission to run early")
    calls = []

    async def assess(provider, prompt, *args, **kwargs):
        calls.append(prompt)
        assert '"reason": "at"' in prompt and '"lateness_seconds": 600' in prompt
        assert "Check whether the authorized action is still useful and timely" in prompt
        return execution.ProviderTurn("error", error="Needs a user decision")

    monkeypatch.setattr(execution, "execute_turn", assess)
    runner = HeartbeatRunner(config)
    await tick(runner, clock)
    assert calls == []
    clock[0] = planned + timedelta(minutes=10)
    await runner.tick(clock[0])
    await drain(runner)
    await tick(runner, clock, minutes=1)
    assert len(calls) == 1 and heartbeat.get(config.paths, beat.ref).attention


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [False, True])
async def test_one_shot_interruption_on_either_side_of_claim_preserves_a_durable_next_step(
    runtime, monkeypatch, claimed
):
    config, clock = runtime
    config.paths.workspace("team").mkdir()
    beat = make_beat(config, workspace="team", at=clock[0].isoformat())
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
    claim = HeartbeatRunner._claim

    async def interrupt(self, attempt, **kwargs):
        if claimed:
            await claim(self, attempt, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(HeartbeatRunner, "_claim", interrupt)
    runner = HeartbeatRunner(config)
    await runner.tick(clock[0])
    await drain(runner)
    saved = heartbeat.get(config.paths, beat.ref)
    assert saved.workspace == "team"
    assert saved.last_check_at is None
    if claimed:
        assert saved.at_consumed and saved.attention and saved.next_check_at is None
        assert beat_runs(config.paths, beat.ref)[0].status == "cancelled"
        assert heartbeat.due(config, clock[0] + timedelta(minutes=1)) == []
    else:
        assert not saved.at_consumed and saved.next_check_at == beat.at
        assert beat_runs(config.paths, beat.ref) == []
        assert heartbeat.due(config, clock[0] + timedelta(minutes=1))[0].ref == beat.ref


@pytest.mark.asyncio
async def test_explicit_followup_wakes_quiet_gate_and_expiry_closes_without_provider(
    runtime, monkeypatch
):
    config, clock = runtime
    beat = make_beat(
        config, gate="exit 1\n", followup_at=(clock[0] + timedelta(minutes=1)).isoformat()
    )
    expired = make_beat(config, expires_at=(clock[0] + timedelta(minutes=1)).isoformat())
    calls = []

    async def assess(provider, prompt, *args, **kwargs):
        calls.append(kwargs["env"]["ENSO_BEAT"])
        assert '"reason": "followup"' in prompt
        settle(config, kwargs["env"])
        return execution.ProviderTurn("ok")

    monkeypatch.setattr(execution, "execute_turn", assess)
    await tick(HeartbeatRunner(config), clock, minutes=1)
    assert calls == [beat.ref]
    assert heartbeat.get(config.paths, expired.ref).state == "expired"
    assert [event.kind for event in heartbeat.pending_notices(config.paths)] == ["expired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pause", "edit", "disable", "invalid"])
async def test_live_changes_cancel_and_await_active_provider(runtime, monkeypatch, change):
    config, clock = runtime
    beat = make_beat(config)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def assess(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(execution, "execute_turn", assess)
    runner = HeartbeatRunner(config)
    clock[0] += timedelta(minutes=5)
    await runner.tick(clock[0])
    await asyncio.wait_for(started.wait(), 2)
    if change == "pause":
        heartbeat.pause(config, beat.ref)
    elif change == "edit":
        heartbeat.update(config, beat.ref, {"instructions": "Stop and ask before replying"})
    elif change == "disable":
        save_config(config.paths, {**config.raw, "heartbeat": {"enabled": False}})
        await runner.tick(clock[0])
    else:
        config.paths.config.write_text("{")
        await runner.tick(clock[0])
    await drain(runner)
    assert cleaned.is_set() and runner.running() == []
    run = beat_runs(config.paths, beat.ref)[0]
    assert run.status == "cancelled" and run.settlement is None
    assert heartbeat.get(config.paths, beat.ref).claim_run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["gate", "provider"])
async def test_stop_cleans_actual_subprocess_and_retains_pending_actions(
    runtime, monkeypatch, stage
):
    config, clock = runtime
    started = asyncio.get_running_loop().create_future()

    async def connected(reader, writer):
        started.set_result(int(await reader.read()))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    code = (
        "import os,socket,time; "
        f"s=socket.create_connection(('127.0.0.1',{port})); "
        "s.sendall(str(os.getpid()).encode()); s.close(); time.sleep(60)"
    )
    command = [sys.executable, "-c", code]
    gate = "exec " + shlex.join(command) + "\n" if stage == "gate" else None
    beat = make_beat(config, gate=gate)

    async def assess(*args, **kwargs):
        env = kwargs["env"]
        heartbeat.begin_action(
            config, beat.ref, "reply", "Reply reservation", run_id=env["ENSO_BEAT_RUN_ID"]
        )
        await execution.run_process(
            command,
            cwd=config.paths.workspace("default"),
            env=env,
            timeout=60,
            merge_stderr=False,
            label="test heartbeat provider",
        )
        return execution.ProviderTurn("ok")

    monkeypatch.setattr(execution, "execute_turn", assess)
    runner = HeartbeatRunner(config)
    try:
        clock[0] += timedelta(minutes=5)
        await runner.tick(clock[0])
        pid = await asyncio.wait_for(started, 3)
        await runner.stop()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert runner.running() == []
        if stage == "provider":
            assert (
                heartbeat.context(config.paths, beat.ref)["actions"][0]["action_status"]
                == "uncertain"
            )
    finally:
        await runner.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["gate", "provider"])
async def test_hard_expiry_interrupts_active_assessment(runtime, monkeypatch, stage):
    config, clock = runtime
    beat = make_beat(config, expires_at=(clock[0] + timedelta(minutes=6)).isoformat())
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    if stage == "gate":
        monkeypatch.setattr(HeartbeatRunner, "_gate", blocked)
    else:
        monkeypatch.setattr(execution, "execute_turn", blocked)
    runner = HeartbeatRunner(config)
    clock[0] += timedelta(minutes=5)
    await runner.tick(clock[0])
    await asyncio.wait_for(started.wait(), 2)
    clock[0] += timedelta(minutes=2)
    await drain(runner)
    assert cleaned.is_set() and heartbeat.get(config.paths, beat.ref).state == "expired"
    runs = beat_runs(config.paths, beat.ref)
    assert runs == [] if stage == "gate" else runs[0].status == "cancelled"


@pytest.mark.asyncio
async def test_gate_timeout_records_safe_failure(runtime):
    config, clock = runtime
    beat = make_beat(config, gate="sleep 60\n", gate_timeout=1)
    await tick(HeartbeatRunner(config), clock)
    saved = heartbeat.get(config.paths, beat.ref)
    assert (
        saved.last_check_status == "error" and saved.last_check_error == "Gate timed out after 1s"
    )
    assert beat_runs(config.paths, beat.ref) == []


@pytest.mark.asyncio
async def test_real_provider_adapter_uses_fresh_session_and_requires_settlement(fake_config):
    save_config(fake_config.paths, fake_config.raw)
    now = datetime.now(UTC)
    beat = make_beat(fake_config, at=now.isoformat())
    runner = HeartbeatRunner(fake_config)
    await runner.tick(now + timedelta(seconds=1))
    await drain(runner)
    run = beat_runs(fake_config.paths, beat.ref)[0]
    assert run.session_id and run.exit_code == 0 and run.status == "error"
    assert "new " + run.session_id in run.output
    assert "without recording wait or complete" in run.error
    with db.transaction(fake_config.paths) as con:
        assert con.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dispatch", "notices"])
async def test_cancellation_while_acquiring_lock_releases_eventual_lock(
    runtime, monkeypatch, operation
):
    config, clock = runtime
    beat = make_beat(config, notify="slack:C1")
    heartbeat.record_check(config, beat.ref, "error", error="Source unavailable")
    acquired, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    acquire = heartbeat.acquire_lock

    def blocked(paths, ref):
        lock = acquire(paths, ref)
        loop.call_soon_threadsafe(acquired.set)
        assert release.wait(3)
        return lock

    monkeypatch.setattr(module.store, "acquire_lock", blocked)
    runner = HeartbeatRunner(config)
    coroutine = (
        runner._notices(config) if operation == "notices" else runner._dispatch(beat.ref, clock[0])
    )
    task = asyncio.create_task(coroutine)
    await asyncio.wait_for(acquired.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    lock = acquire(config.paths, beat.ref)
    assert lock is not None
    lock.close()


@pytest.mark.asyncio
async def test_notice_retry_repairs_known_outbox_receipt_without_duplicate_send(
    runtime, monkeypatch, caplog
):
    config, _clock = runtime
    beat = make_beat(config, notify="slack:C1", notify_thread="123.456")
    heartbeat.record_check(config, beat.ref, "error", error="Source unavailable")
    transport = FakeTransport()
    runner = HeartbeatRunner(config, {"slack": transport})
    acknowledge = module.store.notice_delivered

    def unavailable(*args):
        raise OSError("simulated acknowledgement failure")

    monkeypatch.setattr(module.store, "notice_delivered", unavailable)
    await runner._notices(config)
    await runner._notices(config)
    assert len(transport.sent) == 1
    assert caplog.text.count("notification was not delivered") == 1
    monkeypatch.setattr(module.store, "notice_delivered", acknowledge)
    await runner._notices(config)
    assert len(transport.sent) == 1 and heartbeat.pending_notices(config.paths) == []
    with db.transaction(config.paths) as con:
        row = con.execute("SELECT * FROM messages WHERE status = 'sent'").fetchone()
    assert row["thread"] == "123.456" and row["consumed_at"] is None
    assert row["workspace"] == beat.workspace
    assert row["source"].startswith(f"beat:{beat.ref}:notice:")


@pytest.mark.asyncio
async def test_slow_notices_do_not_delay_other_due_assessments(runtime, monkeypatch):
    config, clock = runtime
    notice = make_beat(config, notify="slack:C1")
    heartbeat.record_check(config, notice.ref, "error", error="Source unavailable")
    heartbeat.pause(config, notice.ref)
    due = make_beat(config)
    sending, assessed, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowTransport(FakeTransport):
        async def send(self, *args, **kwargs):
            sending.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    async def assess(*args, **kwargs):
        settle(config, kwargs["env"])
        assessed.set()
        return execution.ProviderTurn("ok")

    monkeypatch.setattr(execution, "execute_turn", assess)
    runner = HeartbeatRunner(config, {"slack": SlowTransport()})
    clock[0] += timedelta(minutes=5)
    await asyncio.wait_for(runner.tick(clock[0]), 2)
    await asyncio.wait_for(asyncio.gather(sending.wait(), assessed.wait()), 2)
    await runner.stop()
    assert cancelled.is_set() and beat_runs(config.paths, due.ref)[0].status == "ok"


@pytest.mark.asyncio
async def test_failed_transport_retries_and_failure_recovery_notices_are_transitions(
    runtime, monkeypatch
):
    config, clock = runtime
    beat = make_beat(config, notify="slack:C1")
    calls = []

    class FlakyTransport(FakeTransport):
        failed = False

        async def send(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise OSError("temporary send failure")
            return await super().send(*args, **kwargs)

    async def assess(*args, **kwargs):
        calls.append(True)
        if len(calls) < 3:
            return execution.ProviderTurn("error", error="Provider unavailable")
        settle(config, kwargs["env"])
        return execution.ProviderTurn("ok")

    monkeypatch.setattr(execution, "execute_turn", assess)
    transport = FlakyTransport()
    runner = HeartbeatRunner(config, {"slack": transport})
    await tick(runner, clock)
    assert transport.sent == [] and len(heartbeat.pending_notices(config.paths)) == 1
    await tick(runner, clock)
    assert len(transport.sent) == 1
    await tick(runner, clock)
    assert len(transport.sent) == 2 and "recovered" in transport.sent[-1][1]
    failures = heartbeat.history(config.paths, beat.ref, kind="run_failed")
    assert len(failures) == 2 and sum(event.payload["notify"] for event in failures) == 1


@pytest.mark.asyncio
async def test_lock_overlap_recovery_and_pruning_preserve_owned_boundaries(
    runtime, tmp_path, caplog
):
    config, clock = runtime
    config.paths.workspace("team").mkdir()
    beat = make_beat(config, workspace="team", gate="exit 1\n")
    other = make_beat(config, gate="exit 1\n")
    runner = HeartbeatRunner(config)
    lock = heartbeat.acquire_lock(config.paths, beat.ref)
    assert lock is not None
    with lock:
        run = heartbeat.start_run(config, beat.ref)
        assert runner.recover() == 0
        await tick(runner, clock)
        assert heartbeat.get_run(config.paths, run.id).status == "running"
    assert runner.recover() == 1
    heartbeat.cancel(config, beat.ref)
    for outbox_id, notice in enumerate(heartbeat.pending_notices(config.paths), start=1):
        heartbeat.notice_delivered(config, notice, outbox_id)
    clock[0] += timedelta(days=31)
    await runner.tick(clock[0])
    await drain(runner)
    # A notice can hold the beat lock during tick's prune; retry after its owner finishes.
    assert heartbeat.prune(config, now=clock[0]) == {}
    assert heartbeat.get(config.paths, beat.ref) is None
    assert not (config.paths.workspace_heartbeat(beat.workspace) / beat.ref).exists()
    assert heartbeat.get(config.paths, other.ref).state == "active"
    assert (config.paths.workspace_heartbeat(other.workspace) / other.ref / "gate.sh").is_file()
    # A closed beat's symlink is never followed into somebody else's files.
    unsafe = make_beat(config)
    directory = config.paths.workspace_heartbeat(unsafe.workspace) / unsafe.ref
    directory.symlink_to(tmp_path, target_is_directory=True)
    marker = tmp_path / "keep"
    marker.write_text("user data")
    heartbeat.cancel(config, unsafe.ref)
    clock[0] += timedelta(days=31)
    with caplog.at_level("WARNING", logger=module.log.name):
        for _ in range(2):
            await runner.tick(clock[0])
            await drain(runner)
    assert marker.read_text() == "user data" and directory.is_symlink()
    assert heartbeat.get(config.paths, unsafe.ref).state == "cancelled"
    kept = [record.message for record in caplog.records if "past retention" in record.message]
    assert kept == [
        f"heartbeat {unsafe.ref} is kept past retention: its script path is a symbolic link"
    ]
