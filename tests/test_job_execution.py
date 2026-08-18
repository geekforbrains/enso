"""Job prerun contract, history, notification, and shared execution tests."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from enso import runs
from enso.core import Runtime
from enso.job_runner import PRERUN_DIAGNOSTIC_LIMIT, PrerunResult
from enso.jobs import Job


class FakeProcess:
    def __init__(self, returncode: int | None = 0):
        self.pid = 42
        self.returncode = returncode


class FakeProvider:
    def __init__(self):
        self.prompts: list[tuple[str, str]] = []
        self.launches = []
        self.instructions = []

    def build_batch_command(
        self, prompt: str, model: str, *, launch=None, instructions=None
    ) -> list[str]:
        self.prompts.append((prompt, model))
        self.launches.append(launch)
        self.instructions.append(instructions)
        return ["fake-provider"]

    @staticmethod
    def parse_batch_output(output: str) -> str:
        return output


class RecordingTransport:
    name = "telegram"
    message_limit = 4096

    def __init__(self):
        self.notifications: list[tuple[str, str | None]] = []

    async def notify(self, text: str, *, destination: str | None = None) -> None:
        self.notifications.append((text, destination))


class FailingTransport(RecordingTransport):
    async def notify(self, text: str, *, destination: str | None = None) -> None:
        raise RuntimeError("transport unavailable")


def make_job(tmp_enso: str, *, prerun: str | None = "prerun.sh", notify: str = "123") -> Job:
    job_dir = Path(tmp_enso, "jobs", "capture")
    job_dir.mkdir(parents=True, exist_ok=True)
    if prerun:
        (job_dir / prerun).touch()
    return Job(
        dir_name="capture",
        name="Capture",
        schedule="*/5 * * * *",
        provider="claude",
        model="sonnet",
        workspace="company",
        prerun=prerun,
        notify=notify,
        prompt="Use this: {{prerun_output}}",
    )


def test_job_session_key_is_stable_and_rotates_with_workspace_policy(
    tmp_enso,
    sample_config,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)

    first, first_error = runtime.jobs._job_execution_context(job)
    repeated, repeated_error = runtime.jobs._job_execution_context(job)
    assert first_error is None and repeated_error is None
    assert first is not None and repeated is not None
    assert first.chat_key == repeated.chat_key

    other_root = Path(tmp_enso, "workspaces", "other")
    other_root.mkdir(parents=True)
    sample_config["workspaces"]["other"] = {
        "policy": "automation",
        "concurrency": 1,
    }
    other, other_error = runtime.jobs._job_execution_context(
        replace(job, workspace="other")
    )
    assert other_error is None and other is not None
    assert other.chat_key != first.chat_key

    sample_config["policies"]["alternate"] = dict(
        sample_config["policies"]["automation"]
    )
    sample_config["workspaces"]["company"]["policy"] = "alternate"
    changed, changed_error = runtime.jobs._job_execution_context(job)
    assert changed_error is None and changed is not None
    assert changed.chat_key not in {first.chat_key, other.chat_key}


@pytest.fixture(autouse=True)
def configured_job_catalog(sample_config, tmp_enso):
    """Give execution tests one valid workspace-owned policy."""
    workspace = Path(tmp_enso, "workspaces", "company")
    workspace.mkdir(parents=True, exist_ok=True)
    sample_config.update(
        {
            "workspaces": {
                "company": {
                    "policy": "automation",
                    "concurrency": 1,
                },
            },
            "policies": {
                "automation": {
                    "unrestricted": True,
                    "providers": ["claude", "codex", "agy"],
                    "default_provider": "claude",
                    "chat_commands": [],
                },
            },
        }
    )
    return workspace


def stub_prerun_process(
    runtime: Runtime,
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
) -> None:
    runtime._spawn_process = AsyncMock(return_value=FakeProcess(returncode))
    runtime._communicate_with_timeout = AsyncMock(return_value=(stdout, stderr, timed_out))


def stub_provider(
    runtime: Runtime, *, returncode: int = 0, output: bytes = b"done"
) -> FakeProvider:
    provider = FakeProvider()
    runtime.make_provider = MagicMock(return_value=provider)
    runtime._spawn_process = AsyncMock(return_value=FakeProcess(returncode))
    runtime._communicate_with_timeout = AsyncMock(return_value=(output, b"", False))
    return provider


async def test_run_history_create_is_offloaded_from_event_loop(
    tmp_enso,
    sample_config,
    monkeypatch,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def fake_create(**_kwargs):
        worker_threads.append(threading.get_ident())
        return "a" * 32

    monkeypatch.setattr(runs, "create", fake_create)

    run_id = await runtime.jobs._create_job_run(
        job,
        "schedule",
        "[job:capture]",
        datetime.now(timezone.utc).isoformat(),
    )

    assert run_id == "a" * 32
    assert worker_threads and worker_threads[0] != event_loop_thread


async def test_run_history_finish_is_offloaded_from_event_loop(
    tmp_enso,
    sample_config,
    monkeypatch,
):
    runtime = Runtime(sample_config)
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    monkeypatch.setattr(runs, "append_output", lambda *_args: None)
    monkeypatch.setattr(
        runs,
        "finish",
        lambda *_args: worker_threads.append(threading.get_ident()),
    )
    monkeypatch.setattr(runs, "prune", lambda **_kwargs: None)

    await runtime.jobs._record_run_finish(
        "b" * 32,
        "output",
        0,
        "ok",
        "[job:capture]",
    )

    assert worker_threads and worker_threads[0] != event_loop_thread


def test_run_history_finish_survives_output_bookkeeping_failure(
    tmp_enso,
    sample_config,
    monkeypatch,
):
    """A log-write failure must not leave an otherwise completed run active."""
    runtime = Runtime(sample_config)
    run_id = "c" * 32
    finish_calls = []

    def fail_append(*_args):
        raise OSError("injected output bookkeeping failure")

    monkeypatch.setattr(runs, "append_output", fail_append)
    monkeypatch.setattr(
        runs,
        "finish",
        lambda *args: finish_calls.append(args),
    )
    monkeypatch.setattr(runs, "prune", lambda **_kwargs: None)

    runtime.jobs._record_run_finish_sync(
        run_id,
        "output",
        0,
        "ok",
        "[job:capture]",
    )

    assert finish_calls == [(run_id, 0, "ok")]


async def test_invalid_job_provider_errors_before_prerun(tmp_enso, sample_config):
    """A job naming a retired/unknown provider errors out without running anything."""
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    job.provider = "gemini"
    runtime._spawn_process = AsyncMock()  # neither prerun nor provider may spawn

    result = await runtime.jobs._execute_job(job)

    assert result.status == "error"
    assert "Unknown provider 'gemini'" in result.output
    row = runs.get(result.run_id)
    assert row["status"] == "error"
    assert row["exit_code"] == -1
    runtime._spawn_process.assert_not_called()
    assert "Unknown provider 'gemini'" in runtime.transport.notifications[0][0]


async def test_invalid_job_model_errors_and_lists_valid_models(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    job.model = "bogus"
    runtime._spawn_process = AsyncMock()

    result = await runtime.jobs._execute_job(job)

    assert result.status == "error"
    assert "Unknown claude model 'bogus'" in result.output
    assert "opus, sonnet" in result.output  # valid models from config
    runtime._spawn_process.assert_not_called()


async def test_manual_run_of_invalid_job_errors_without_notifying(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    job.provider = "gemini"
    runtime._spawn_process = AsyncMock()

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "error"
    assert runs.get(result.run_id)["status"] == "error"
    assert runtime.transport.notifications == []


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, "open"), (1, "no_work"), (2, "error"), (7, "error"), (-15, "error")],
)
async def test_prerun_classifies_exact_exit_contract(
    tmp_enso,
    sample_config,
    returncode,
    expected,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    stub_prerun_process(runtime, returncode=returncode, stdout=b"context")

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert result.outcome == expected
    assert result.exit_code == returncode
    assert result.output == ("context" if returncode == 0 else "")


async def test_prerun_timeout_wins_over_process_exit(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    stub_prerun_process(runtime, returncode=0, timed_out=True)

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert result.outcome == "timeout"
    assert result.exit_code is None
    assert "timed out" in result.diagnostic


async def test_prerun_executes_real_shell_contract(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    Path(job.job_dir, job.prerun).write_text(
        '#!/usr/bin/env bash\necho "raw source"\n'
        'echo "ENSO_ERROR: safe shell failure" >&2\nexit 2\n'
    )

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert result == PrerunResult(
        "error",
        diagnostic="safe shell failure",
        exit_code=2,
    )


async def test_missing_prerun_is_error_without_spawning(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso, prerun=None)
    job.prerun = "missing.sh"
    runtime._spawn_process = AsyncMock()

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert result.outcome == "error"
    assert "not found" in result.diagnostic
    runtime._spawn_process.assert_not_awaited()


async def test_prerun_spawn_error_is_classified(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime._spawn_process = AsyncMock(side_effect=OSError("bash unavailable"))

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert result.outcome == "error"
    assert "Could not start prerun" in result.diagnostic


async def test_prerun_diagnostic_requires_safe_marker_and_redacts(
    tmp_enso,
    sample_config,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    raw = b"private source record\n"
    marked = (
        b"  ENSO_ERROR: request failed token=hunter2 Authorization=Bearer-secret Bearer abc.def\n"
    )
    stub_prerun_process(
        runtime,
        returncode=2,
        stdout=b"raw source output",
        stderr=raw + marked,
    )

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert result.outcome == "error"
    assert "private source" not in result.diagnostic
    assert "raw source" not in result.diagnostic
    assert "hunter2" not in result.diagnostic
    assert "abc.def" not in result.diagnostic
    assert result.diagnostic.count("<redacted>") >= 2


async def test_unmarked_or_embedded_stderr_uses_generic_diagnostic(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    stub_prerun_process(
        runtime,
        returncode=2,
        stderr=b"raw ENSO_ERROR: still untrusted\nprivate record",
    )

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert result.diagnostic == "Prerun exited with status 2"


async def test_prerun_diagnostic_is_truncated(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    stub_prerun_process(
        runtime,
        returncode=2,
        stderr=f"ENSO_ERROR: {'x' * 1000}".encode(),
    )

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert len(result.diagnostic) == PRERUN_DIAGNOSTIC_LIMIT
    assert result.diagnostic.endswith("…")


async def test_failure_history_and_notification_never_include_raw_streams(
    tmp_enso,
    sample_config,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    stub_prerun_process(
        runtime,
        returncode=2,
        stdout=b"private stdout record",
        stderr=b"private traceback\nENSO_ERROR: safe summary\n",
    )

    result = await runtime.jobs._execute_job(job)

    assert "safe summary" in runtime.transport.notifications[0][0]
    assert "private" not in runtime.transport.notifications[0][0]
    history = runs.read_output(result.run_id)
    assert "safe summary" in history
    assert "private" not in history


async def test_scheduled_open_prerun_injects_output_and_runs_provider(
    tmp_enso,
    sample_config,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock(
        return_value=PrerunResult("open", output="captured context", exit_code=0)
    )
    provider = stub_provider(runtime)

    result = await runtime.jobs._execute_job(job)

    assert result.status == "ok"
    assert provider.prompts == [("Use this: captured context", "sonnet")]
    assert provider.instructions[0].content == "# Test shared instructions\n"
    runtime.make_provider.assert_called_once()
    assert runtime.make_provider.call_args.args == ("claude",)
    assert runtime.make_provider.call_args.kwargs["timeout"] == job.timeout
    assert runtime.make_provider.call_args.kwargs["context"].workspace_id == "company"
    assert runtime._spawn_process.await_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    row = runs.get(result.run_id)
    assert row["trigger"] == "schedule"
    assert row["status"] == "ok"


async def test_empty_open_prerun_removes_placeholder(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock(return_value=PrerunResult("open", exit_code=0))
    provider = stub_provider(runtime)

    await runtime.jobs._execute_job(job)

    assert provider.prompts == [("Use this: ", "sonnet")]


async def test_no_work_is_silent_without_history_or_provider(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock(return_value=PrerunResult("no_work", exit_code=1))
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(job)

    assert result.status == "no_work"
    assert result.run_id is None
    assert runs.list_runs() == []
    assert runtime.transport.notifications == []
    runtime.make_provider.assert_not_called()


@pytest.mark.parametrize(
    ("prerun", "expected_status", "expected_exit"),
    [
        (PrerunResult("error", diagnostic="safe failure", exit_code=2), "prerun_error", 2),
        (PrerunResult("timeout", diagnostic="timed out"), "prerun_timeout", -1),
    ],
)
async def test_scheduled_prerun_failure_records_and_notifies_destination(
    tmp_enso,
    sample_config,
    prerun,
    expected_status,
    expected_exit,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso, notify="987")
    runtime.jobs._run_job_prerun = AsyncMock(return_value=prerun)
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(job)

    row = runs.get(result.run_id)
    assert result.status == expected_status
    assert row["status"] == expected_status
    assert row["exit_code"] == expected_exit
    assert row["duration_ms"] >= 0
    assert runtime.transport.notifications[0][1] == "987"
    assert "Capture" in runtime.transport.notifications[0][0]
    runtime.make_provider.assert_not_called()


async def test_missing_prerun_records_failure_and_never_runs_provider(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso, prerun=None)
    job.prerun = "missing.sh"
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(job)

    assert result.status == "prerun_error"
    assert runs.get(result.run_id)["status"] == "prerun_error"
    runtime.make_provider.assert_not_called()


async def test_identical_prerun_failures_are_suppressed_but_recorded(
    tmp_enso,
    sample_config,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    failure = PrerunResult("error", diagnostic="same failure", exit_code=2)
    runtime.jobs._run_job_prerun = AsyncMock(return_value=failure)

    first = await runtime.jobs._execute_job(job)
    runtime.jobs.failure_alerts[job.dir_name]["suppressed"] = "corrupt"
    second = await runtime.jobs._execute_job(job)

    assert len(runtime.transport.notifications) == 1
    assert {runs.get(first.run_id)["status"], runs.get(second.run_id)["status"]} == {"prerun_error"}
    assert runtime.jobs.failure_alerts[job.dir_name]["suppressed"] == 1
    assert "same failure" not in Path(tmp_enso, "state.json").read_text()


async def test_transport_change_alerts_same_failure_again(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    telegram = RecordingTransport()
    runtime.transport = telegram
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock(
        return_value=PrerunResult("error", diagnostic="same", exit_code=2)
    )

    await runtime.jobs._execute_job(job)
    slack = RecordingTransport()
    slack.name = "slack"
    runtime.transport = slack
    await runtime.jobs._execute_job(job)

    assert len(telegram.notifications) == 1
    assert len(slack.notifications) == 1


async def test_changed_exit_or_destination_alerts_immediately(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso, notify="one")
    runtime.jobs._run_job_prerun = AsyncMock(
        side_effect=[
            PrerunResult("error", diagnostic="same", exit_code=2),
            PrerunResult("error", diagnostic="same", exit_code=3),
            PrerunResult("error", diagnostic="same", exit_code=3),
        ]
    )

    await runtime.jobs._execute_job(job)
    await runtime.jobs._execute_job(job)
    job.notify = "two"
    await runtime.jobs._execute_job(job)

    assert [destination for _, destination in runtime.transport.notifications] == [
        "one",
        "one",
        "two",
    ]


async def test_failure_realerts_after_cooldown(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock(
        return_value=PrerunResult("error", diagnostic="same", exit_code=2)
    )

    await runtime.jobs._execute_job(job)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    runtime.jobs.failure_alerts[job.dir_name]["last_notified_at"] = old.isoformat()
    await runtime.jobs._execute_job(job)

    assert len(runtime.transport.notifications) == 2


async def test_terminal_job_runs_apply_retention_config(tmp_enso, sample_config):
    sample_config["runs"] = {"keep": 1, "max_age_days": 30}
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock(
        return_value=PrerunResult("error", diagnostic="same", exit_code=2)
    )

    await runtime.jobs._execute_job(job)
    latest = await runtime.jobs._execute_job(job)

    assert [row["id"] for row in runs.list_runs()] == [latest.run_id]


async def test_dedupe_persists_and_healthy_prerun_sends_one_recovery(
    tmp_enso,
    sample_config,
):
    job = make_job(tmp_enso)
    first_runtime = Runtime(sample_config)
    first_runtime.transport = RecordingTransport()
    failure = PrerunResult("error", diagnostic="same", exit_code=2)
    first_runtime.jobs._run_job_prerun = AsyncMock(return_value=failure)
    await first_runtime.jobs._execute_job(job)

    runtime = Runtime(sample_config)
    runtime.load_state()
    runtime.transport = RecordingTransport()
    runtime.jobs._run_job_prerun = AsyncMock(return_value=failure)
    await runtime.jobs._execute_job(job)
    assert runtime.transport.notifications == []

    runtime.jobs._run_job_prerun = AsyncMock(return_value=PrerunResult("no_work", exit_code=1))
    await runtime.jobs._execute_job(job)
    await runtime.jobs._execute_job(job)

    assert len(runtime.transport.notifications) == 1
    assert "recovered" in runtime.transport.notifications[0][0]
    assert job.dir_name not in runtime.jobs.failure_alerts


async def test_recovery_clears_episode_even_if_notification_fails(
    tmp_enso,
    sample_config,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock(
        return_value=PrerunResult("error", diagnostic="same", exit_code=2)
    )
    await runtime.jobs._execute_job(job)

    runtime.transport = FailingTransport()
    runtime.jobs._run_job_prerun = AsyncMock(return_value=PrerunResult("no_work", exit_code=1))
    await runtime.jobs._execute_job(job)

    assert job.dir_name not in runtime.jobs.failure_alerts


@pytest.mark.parametrize(
    "prerun",
    [
        PrerunResult("error", diagnostic="bad", exit_code=2),
        PrerunResult("timeout", diagnostic="slow"),
    ],
)
async def test_manual_run_uses_prerun_records_failure_without_notifying(
    tmp_enso,
    sample_config,
    monkeypatch,
    prerun,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    monkeypatch.setattr("enso.job_runner.load_jobs", lambda: [job])
    runtime.jobs._run_job_prerun = AsyncMock(return_value=prerun)
    runtime.make_provider = MagicMock()

    result = await runtime.jobs.run_now(job.dir_name)

    assert result.status in {"prerun_error", "prerun_timeout"}
    assert runs.get(result.run_id)["trigger"] == "manual"
    assert runtime.transport.notifications == []
    runtime.make_provider.assert_not_called()


async def test_manual_no_work_has_no_history(tmp_enso, sample_config, monkeypatch):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    monkeypatch.setattr("enso.job_runner.load_jobs", lambda: [job])
    runtime.jobs._run_job_prerun = AsyncMock(return_value=PrerunResult("no_work", exit_code=1))

    result = await runtime.jobs.run_now(job.dir_name)

    assert result.status == "no_work"
    assert result.run_id is None
    assert runs.list_runs() == []
    assert runtime.transport.notifications == []


async def test_concurrent_same_job_is_skipped_via_run_lock(tmp_enso, sample_config):
    """The cross-process run lock rejects overlapping runs of one job."""
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso, prerun=None)

    held = runtime.jobs._acquire_job_lock(job)
    assert held not in (None, "unlocked")
    try:
        assert runtime.jobs.running_elsewhere() == []  # load_jobs sees no JOB.md
        result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)
        assert result.status == "error"
        assert "already running" in result.output
    finally:
        held.close()

    # Lock released — a fresh probe acquires cleanly.
    reacquired = runtime.jobs._acquire_job_lock(job)
    assert reacquired not in (None, "unlocked")
    reacquired.close()


@pytest.mark.asyncio
async def test_running_here_reports_only_live_job_tasks(sample_config):
    """The update busy-check must not be blocked by finished job tasks."""
    runtime = Runtime(sample_config)
    live = asyncio.create_task(asyncio.sleep(10))
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    runtime.jobs._running_tasks["live"] = live
    runtime.jobs._running_tasks["finished"] = finished
    try:
        assert runtime.jobs.running_here() == ["live"]
    finally:
        live.cancel()


# -- Named workspace-policy execution bindings --


async def test_job_runs_in_named_workspace_with_native_launch(
    tmp_enso,
    sample_config,
    configured_job_catalog,
):
    """Jobs reuse the same workspace-policy execution plumbing as routes."""
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso, prerun=None)
    provider = stub_provider(runtime)

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "ok"
    context = runtime.make_provider.call_args.kwargs["context"]
    assert context.path == str(configured_job_catalog)
    assert context.workspace_id == "company"
    assert context.workspace.name == "company"
    assert context.policy.name == "automation"
    spawn_kwargs = runtime._spawn_process.await_args.kwargs
    assert spawn_kwargs["cwd"] == str(configured_job_catalog)
    assert provider.launches[0].mode == "unrestricted"


async def test_job_passes_policy_launch_and_minimal_environment(
    tmp_enso,
    sample_config,
    monkeypatch,
):
    """The workspace policy controls the batch command and child environment."""
    launch = MagicMock(mode="policy", env={"SAFE_ONLY": "1"})
    prepared = []

    def fake_prepare(workspace, execution_policy, provider):
        prepared.append((workspace.name, execution_policy.name, provider))
        return launch

    monkeypatch.setattr("enso.policy.prepare_launch", fake_prepare)
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso, prerun=None)
    provider = stub_provider(runtime)

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "ok"
    assert prepared == [
        ("company", "automation", "claude"),
        ("company", "automation", "claude"),
    ]
    assert provider.launches == [launch]
    assert runtime._spawn_process.await_args.kwargs["env"] == {"SAFE_ONLY": "1"}


async def test_job_policy_preparation_failure_never_falls_back(
    tmp_enso,
    sample_config,
    monkeypatch,
):
    def fail_prepare(_workspace, _access, _provider):
        raise RuntimeError("native policy is invalid")

    monkeypatch.setattr("enso.policy.prepare_launch", fail_prepare)
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock()
    runtime.make_provider = MagicMock()
    runtime._spawn_process = AsyncMock()

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "error"
    assert "native policy is invalid" in result.output
    runtime.jobs._run_job_prerun.assert_not_awaited()
    runtime.make_provider.assert_not_called()
    runtime._spawn_process.assert_not_awaited()


async def test_job_missing_shared_instructions_fails_before_prerun(
    tmp_enso,
    sample_config,
):
    Path(tmp_enso, "AGENTS.md").unlink()
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock()
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(
        job, trigger="manual", notify_failures=False
    )

    assert result.status == "error"
    assert "shared instruction file is missing" in result.output
    runtime.jobs._run_job_prerun.assert_not_awaited()
    runtime.make_provider.assert_not_called()


async def test_job_policy_preflight_failure_happens_before_prerun(
    tmp_enso,
    sample_config,
    monkeypatch,
):
    from enso.policy import PolicyCheck

    monkeypatch.setattr(
        "enso.policy.check_provider",
        lambda _workspace, _access, provider: PolicyCheck(
            provider=provider,
            ok=False,
            problems=("native policy is missing",),
        ),
    )
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock()
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "error"
    assert "native policy is missing" in result.output
    runtime.jobs._run_job_prerun.assert_not_awaited()
    runtime.make_provider.assert_not_called()


async def test_missing_managed_job_workspace_fails_before_prerun(
    tmp_enso,
    sample_config,
):
    workspace = Path(tmp_enso, "workspaces", "company")
    workspace.rmdir()
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock()
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "error"
    assert "workspace path does not exist" in result.output
    runtime.jobs._run_job_prerun.assert_not_awaited()
    runtime.make_provider.assert_not_called()


async def test_bound_jobs_share_process_local_workspace_concurrency(
    tmp_enso,
    sample_config,
):
    runtime = Runtime(sample_config)
    first = make_job(tmp_enso, prerun=None)
    second_dir = Path(tmp_enso, "jobs", "second")
    second_dir.mkdir(parents=True)
    second = replace(first, dir_name="second", name="Second")
    runtime.make_provider = MagicMock(side_effect=[FakeProvider(), FakeProvider()])
    runtime._spawn_process = AsyncMock(side_effect=[FakeProcess(0), FakeProcess(0)])
    runtime.jobs._create_job_run = AsyncMock(return_value=None)
    runtime.jobs._record_run_finish = AsyncMock()
    active = 0
    maximum = 0

    async def communicate(_proc, _label, _timeout):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return b"done", b"", False

    runtime._communicate_with_timeout = AsyncMock(side_effect=communicate)

    results = await asyncio.gather(
        runtime.jobs._execute_job(first, trigger="manual", notify_failures=False),
        runtime.jobs._execute_job(second, trigger="manual", notify_failures=False),
    )

    assert [result.status for result in results] == ["ok", "ok"]
    assert maximum == 1


async def test_job_binding_does_not_move_trusted_prerun(tmp_enso, sample_config):
    """Prerun remains a trusted job-directory script outside native policy."""
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime._spawn_process = AsyncMock(return_value=FakeProcess(1))
    runtime._communicate_with_timeout = AsyncMock(return_value=(b"", b"", False))

    result = await runtime.jobs._run_job_prerun(job, "[job:capture]")

    assert result.outcome == "no_work"
    assert runtime._spawn_process.await_args.kwargs["cwd"] == job.job_dir


async def test_job_policy_must_allow_its_provider_before_prerun(
    tmp_enso,
    sample_config,
):
    sample_config["policies"]["automation"]["providers"] = ["claude"]
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    job.provider = "codex"
    job.model = "gpt-5.3-codex"
    runtime.jobs._run_job_prerun = AsyncMock()
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "error"
    assert "does not allow provider 'codex'" in result.output
    runtime.jobs._run_job_prerun.assert_not_awaited()
    runtime.make_provider.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("workspace", "missing", "Unknown workspace 'missing'"),
        ("workspace", "", "workspace is required"),
    ],
)
async def test_invalid_job_binding_fails_without_global_fallback(
    tmp_enso,
    sample_config,
    field,
    value,
    expected,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    setattr(job, field, value)
    runtime.jobs._run_job_prerun = AsyncMock()
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "error"
    assert expected in result.output
    runtime.jobs._run_job_prerun.assert_not_awaited()
    runtime.make_provider.assert_not_called()


@pytest.mark.parametrize(
    ("section", "field", "value", "expected"),
    [
        ("workspaces", "concurrency", 0, "Invalid workspace 'company'"),
        ("policies", "unrestricted", "yes", "Invalid policy 'automation'"),
    ],
)
async def test_invalid_selected_catalog_entry_fails_before_prerun(
    tmp_enso,
    sample_config,
    section,
    field,
    value,
    expected,
):
    sample_config[section]["company" if section == "workspaces" else "automation"][field] = value
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime.jobs._run_job_prerun = AsyncMock()
    runtime.make_provider = MagicMock()

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "error"
    assert expected in result.output
    runtime.jobs._run_job_prerun.assert_not_awaited()
    runtime.make_provider.assert_not_called()


async def test_job_provider_and_model_override_policy_default(
    tmp_enso,
    sample_config,
):
    """Policy authorizes providers; the JOB.md still chooses provider/model."""
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso, prerun=None)
    job.provider = "codex"
    job.model = "gpt-5.3-codex"
    provider = stub_provider(runtime)

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "ok"
    runtime.make_provider.assert_called_once()
    assert runtime.make_provider.call_args.args == ("codex",)
    assert provider.prompts == [("Use this: ", "gpt-5.3-codex")]


async def test_batch_path_never_retries_a_retryable_provider_error(
    tmp_enso,
    sample_config,
):
    """Retry-once for transient errors is interactive-only; a failing job run
    spawns the provider exactly once even when the provider would call the
    error retryable (grok's lapsed-OAuth signature)."""

    class RetryableProvider(FakeProvider):
        def retryable_error(self, text: str) -> bool:
            return "Not signed in" in text

    sample_config["policies"]["automation"]["providers"].append("grok")
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso, prerun=None)
    job.provider = "grok"
    job.model = "grok-4.6"
    provider = RetryableProvider()
    runtime.make_provider = MagicMock(return_value=provider)
    runtime._spawn_process = AsyncMock(return_value=FakeProcess(1))
    runtime._communicate_with_timeout = AsyncMock(return_value=(b"Not signed in", b"", False))

    result = await runtime.jobs._execute_job(job, trigger="manual", notify_failures=False)

    assert result.status == "error"
    assert "Not signed in" in result.output
    runtime._spawn_process.assert_awaited_once()


async def test_bound_job_failure_does_not_enqueue_chat_context(
    tmp_enso,
    sample_config,
):
    """A job binding does not make the job part of an interactive route."""
    from enso import messages

    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso, prerun=None)
    stub_provider(runtime, returncode=1, output=b"boom")

    await runtime.jobs._execute_job(job, trigger="manual", notify_failures=True)

    assert messages.pending() == []
