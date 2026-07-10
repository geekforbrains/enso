"""Job prerun contract, history, notification, and shared execution tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from enso import runs
from enso.core import PRERUN_DIAGNOSTIC_LIMIT, PrerunResult, Runtime
from enso.jobs import Job


class FakeProcess:
    def __init__(self, returncode: int | None = 0):
        self.pid = 42
        self.returncode = returncode


class FakeProvider:
    def __init__(self):
        self.prompts: list[tuple[str, str]] = []

    def build_batch_command(self, prompt: str, model: str) -> list[str]:
        self.prompts.append((prompt, model))
        return ["fake-provider"]

    @staticmethod
    def parse_batch_output(output: str) -> str:
        return output


class RecordingTransport:
    name = "telegram"

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
        prerun=prerun,
        notify=notify,
        prompt="Use this: {{prerun_output}}",
    )


def stub_prerun_process(
    runtime: Runtime,
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
) -> None:
    runtime._spawn_process = AsyncMock(return_value=FakeProcess(returncode))
    runtime._communicate_with_timeout = AsyncMock(
        return_value=(stdout, stderr, timed_out)
    )


def stub_provider(
    runtime: Runtime, *, returncode: int = 0, output: bytes = b"done"
) -> FakeProvider:
    provider = FakeProvider()
    runtime.make_job_provider = MagicMock(return_value=provider)
    runtime._spawn_process = AsyncMock(return_value=FakeProcess(returncode))
    runtime._communicate_with_timeout = AsyncMock(
        return_value=(output, b"", False)
    )
    return provider


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, "open"), (1, "no_work"), (2, "error"), (7, "error"), (-15, "error")],
)
async def test_prerun_classifies_exact_exit_contract(
    tmp_enso, sample_config, returncode, expected,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    stub_prerun_process(runtime, returncode=returncode, stdout=b"context")

    result = await runtime._run_job_prerun(job, "[job:capture]")

    assert result.outcome == expected
    assert result.exit_code == returncode
    assert result.output == ("context" if returncode == 0 else "")


async def test_prerun_timeout_wins_over_process_exit(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    stub_prerun_process(runtime, returncode=0, timed_out=True)

    result = await runtime._run_job_prerun(job, "[job:capture]")

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

    result = await runtime._run_job_prerun(job, "[job:capture]")

    assert result == PrerunResult(
        "error", diagnostic="safe shell failure", exit_code=2,
    )


async def test_missing_prerun_is_error_without_spawning(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso, prerun=None)
    job.prerun = "missing.sh"
    runtime._spawn_process = AsyncMock()

    result = await runtime._run_job_prerun(job, "[job:capture]")

    assert result.outcome == "error"
    assert "not found" in result.diagnostic
    runtime._spawn_process.assert_not_awaited()


async def test_prerun_spawn_error_is_classified(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime._spawn_process = AsyncMock(side_effect=OSError("bash unavailable"))

    result = await runtime._run_job_prerun(job, "[job:capture]")

    assert result.outcome == "error"
    assert "Could not start prerun" in result.diagnostic


async def test_prerun_diagnostic_requires_safe_marker_and_redacts(
    tmp_enso, sample_config,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    raw = b"private source record\n"
    marked = (
        b"  ENSO_ERROR: request failed token=hunter2 "
        b"Authorization=Bearer-secret Bearer abc.def\n"
    )
    stub_prerun_process(
        runtime,
        returncode=2,
        stdout=b"raw source output",
        stderr=raw + marked,
    )

    result = await runtime._run_job_prerun(job, "[job:capture]")

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

    result = await runtime._run_job_prerun(job, "[job:capture]")

    assert result.diagnostic == "Prerun exited with status 2"


async def test_prerun_diagnostic_is_truncated(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    stub_prerun_process(
        runtime,
        returncode=2,
        stderr=f"ENSO_ERROR: {'x' * 1000}".encode(),
    )

    result = await runtime._run_job_prerun(job, "[job:capture]")

    assert len(result.diagnostic) == PRERUN_DIAGNOSTIC_LIMIT
    assert result.diagnostic.endswith("…")


async def test_failure_history_and_notification_never_include_raw_streams(
    tmp_enso, sample_config,
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

    result = await runtime._execute_job(job)

    assert "safe summary" in runtime.transport.notifications[0][0]
    assert "private" not in runtime.transport.notifications[0][0]
    history = runs.read_output(result.run_id)
    assert "safe summary" in history
    assert "private" not in history


async def test_scheduled_open_prerun_injects_output_and_runs_provider(
    tmp_enso, sample_config,
):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("open", output="captured context", exit_code=0)
    )
    provider = stub_provider(runtime)

    result = await runtime._execute_job(job)

    assert result.status == "ok"
    assert provider.prompts == [("Use this: captured context", "sonnet")]
    row = runs.get(result.run_id)
    assert row["trigger"] == "schedule"
    assert row["status"] == "ok"


async def test_empty_open_prerun_removes_placeholder(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime._run_job_prerun = AsyncMock(return_value=PrerunResult("open", exit_code=0))
    provider = stub_provider(runtime)

    await runtime._execute_job(job)

    assert provider.prompts == [("Use this: ", "sonnet")]


async def test_no_work_is_silent_without_history_or_provider(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("no_work", exit_code=1)
    )
    runtime.make_job_provider = MagicMock()

    result = await runtime._execute_job(job)

    assert result.status == "no_work"
    assert result.run_id is None
    assert runs.list_runs() == []
    assert runtime.transport.notifications == []
    runtime.make_job_provider.assert_not_called()


@pytest.mark.parametrize(
    ("prerun", "expected_status", "expected_exit"),
    [
        (PrerunResult("error", diagnostic="safe failure", exit_code=2), "prerun_error", 2),
        (PrerunResult("timeout", diagnostic="timed out"), "prerun_timeout", -1),
    ],
)
async def test_scheduled_prerun_failure_records_and_notifies_destination(
    tmp_enso, sample_config, prerun, expected_status, expected_exit,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso, notify="987")
    runtime._run_job_prerun = AsyncMock(return_value=prerun)
    runtime.make_job_provider = MagicMock()

    result = await runtime._execute_job(job)

    row = runs.get(result.run_id)
    assert result.status == expected_status
    assert row["status"] == expected_status
    assert row["exit_code"] == expected_exit
    assert row["duration_ms"] >= 0
    assert runtime.transport.notifications[0][1] == "987"
    assert "Capture" in runtime.transport.notifications[0][0]
    runtime.make_job_provider.assert_not_called()


async def test_missing_prerun_records_failure_and_never_runs_provider(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso, prerun=None)
    job.prerun = "missing.sh"
    runtime.make_job_provider = MagicMock()

    result = await runtime._execute_job(job)

    assert result.status == "prerun_error"
    assert runs.get(result.run_id)["status"] == "prerun_error"
    runtime.make_job_provider.assert_not_called()


async def test_identical_prerun_failures_are_suppressed_but_recorded(
    tmp_enso, sample_config,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    failure = PrerunResult("error", diagnostic="same failure", exit_code=2)
    runtime._run_job_prerun = AsyncMock(return_value=failure)

    first = await runtime._execute_job(job)
    runtime._job_failure_alerts[job.dir_name]["suppressed"] = "corrupt"
    second = await runtime._execute_job(job)

    assert len(runtime.transport.notifications) == 1
    assert {runs.get(first.run_id)["status"], runs.get(second.run_id)["status"]} == {
        "prerun_error"
    }
    assert runtime._job_failure_alerts[job.dir_name]["suppressed"] == 1
    assert "same failure" not in Path(tmp_enso, "state.json").read_text()


async def test_transport_change_alerts_same_failure_again(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    telegram = RecordingTransport()
    runtime.transport = telegram
    job = make_job(tmp_enso)
    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("error", diagnostic="same", exit_code=2)
    )

    await runtime._execute_job(job)
    slack = RecordingTransport()
    slack.name = "slack"
    runtime.transport = slack
    await runtime._execute_job(job)

    assert len(telegram.notifications) == 1
    assert len(slack.notifications) == 1


async def test_changed_exit_or_destination_alerts_immediately(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso, notify="one")
    runtime._run_job_prerun = AsyncMock(
        side_effect=[
            PrerunResult("error", diagnostic="same", exit_code=2),
            PrerunResult("error", diagnostic="same", exit_code=3),
            PrerunResult("error", diagnostic="same", exit_code=3),
        ]
    )

    await runtime._execute_job(job)
    await runtime._execute_job(job)
    job.notify = "two"
    await runtime._execute_job(job)

    assert [destination for _, destination in runtime.transport.notifications] == [
        "one", "one", "two"
    ]


async def test_failure_realerts_after_cooldown(tmp_enso, sample_config):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("error", diagnostic="same", exit_code=2)
    )

    await runtime._execute_job(job)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    runtime._job_failure_alerts[job.dir_name]["last_notified_at"] = old.isoformat()
    await runtime._execute_job(job)

    assert len(runtime.transport.notifications) == 2


async def test_terminal_job_runs_apply_retention_config(tmp_enso, sample_config):
    sample_config["tasks"] = {"runs_keep": 1, "runs_max_age_days": 30}
    runtime = Runtime(sample_config)
    job = make_job(tmp_enso)
    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("error", diagnostic="same", exit_code=2)
    )

    await runtime._execute_job(job)
    latest = await runtime._execute_job(job)

    assert [row["id"] for row in runs.list_runs()] == [latest.run_id]


async def test_dedupe_persists_and_healthy_prerun_sends_one_recovery(
    tmp_enso, sample_config,
):
    job = make_job(tmp_enso)
    first_runtime = Runtime(sample_config)
    first_runtime.transport = RecordingTransport()
    failure = PrerunResult("error", diagnostic="same", exit_code=2)
    first_runtime._run_job_prerun = AsyncMock(return_value=failure)
    await first_runtime._execute_job(job)

    runtime = Runtime(sample_config)
    runtime.load_state()
    runtime.transport = RecordingTransport()
    runtime._run_job_prerun = AsyncMock(return_value=failure)
    await runtime._execute_job(job)
    assert runtime.transport.notifications == []

    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("no_work", exit_code=1)
    )
    await runtime._execute_job(job)
    await runtime._execute_job(job)

    assert len(runtime.transport.notifications) == 1
    assert "recovered" in runtime.transport.notifications[0][0]
    assert job.dir_name not in runtime._job_failure_alerts


async def test_recovery_clears_episode_even_if_notification_fails(
    tmp_enso, sample_config,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("error", diagnostic="same", exit_code=2)
    )
    await runtime._execute_job(job)

    runtime.transport = FailingTransport()
    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("no_work", exit_code=1)
    )
    await runtime._execute_job(job)

    assert job.dir_name not in runtime._job_failure_alerts


@pytest.mark.parametrize(
    "prerun",
    [
        PrerunResult("error", diagnostic="bad", exit_code=2),
        PrerunResult("timeout", diagnostic="slow"),
    ],
)
async def test_manual_run_uses_prerun_records_failure_without_notifying(
    tmp_enso, sample_config, monkeypatch, prerun,
):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    monkeypatch.setattr("enso.core.load_jobs", lambda: [job])
    runtime._run_job_prerun = AsyncMock(return_value=prerun)
    runtime.make_job_provider = MagicMock()

    result = await runtime.run_job_now(job.dir_name)

    assert result.status in {"prerun_error", "prerun_timeout"}
    assert runs.get(result.run_id)["trigger"] == "manual"
    assert runtime.transport.notifications == []
    runtime.make_job_provider.assert_not_called()


async def test_manual_no_work_has_no_history(tmp_enso, sample_config, monkeypatch):
    runtime = Runtime(sample_config)
    runtime.transport = RecordingTransport()
    job = make_job(tmp_enso)
    monkeypatch.setattr("enso.core.load_jobs", lambda: [job])
    runtime._run_job_prerun = AsyncMock(
        return_value=PrerunResult("no_work", exit_code=1)
    )

    result = await runtime.run_job_now(job.dir_name)

    assert result.status == "no_work"
    assert result.run_id is None
    assert runs.list_runs() == []
    assert runtime.transport.notifications == []
