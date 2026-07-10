"""CLI behavior for the shared manual job execution pipeline."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from enso import cli as cli_mod
from enso import core as core_mod
from enso.core import JobRunResult

runner = CliRunner()


def stub_runtime(monkeypatch, result: JobRunResult | Exception) -> None:
    class FakeRuntime:
        def __init__(self, _config):
            pass

        async def run_job_now(self, _name: str) -> JobRunResult:
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(core_mod, "Runtime", FakeRuntime)
    monkeypatch.setattr(cli_mod, "load_config", lambda: {})


@pytest.mark.parametrize(
    ("job_result", "expected_exit", "expected_text"),
    [
        (JobRunResult("ok", output="finished"), 0, "finished"),
        (JobRunResult("no_work", exit_code=1), 0, "No work (prerun exit 1)"),
        (
            JobRunResult("prerun_error", output="Prerun Error: safe", exit_code=2),
            1,
            "Prerun Error: safe",
        ),
        (
            JobRunResult("prerun_timeout", output="Prerun Timeout: slow"),
            1,
            "Prerun Timeout: slow",
        ),
        (JobRunResult("error", output="provider failed", exit_code=3), 1, "provider failed"),
        (JobRunResult("timeout", output="provider timed out"), 1, "provider timed out"),
    ],
)
def test_job_run_renders_distinct_outcomes(
    monkeypatch, job_result, expected_exit, expected_text,
):
    stub_runtime(monkeypatch, job_result)

    result = runner.invoke(cli_mod.app, ["job", "run", "capture"])

    assert result.exit_code == expected_exit
    assert expected_text in result.output


def test_job_run_reports_missing_job(monkeypatch):
    stub_runtime(monkeypatch, ValueError("No such job: absent"))

    result = runner.invoke(cli_mod.app, ["job", "run", "absent"])

    assert result.exit_code == 1
    assert "Job 'absent' not found" in result.output
