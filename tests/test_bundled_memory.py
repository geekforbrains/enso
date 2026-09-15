"""The shipped memory job's idle, failure, and bounded-repair behavior."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import load_job, write_config

from enso import workspaces
from enso.jobs import parse_job, validate

BUNDLED = Path(workspaces.__file__).parent / "bundled" / "jobs" / "enso-memory"


def test_memory_bundle_seeds_valid_job_and_preserves_customization(enso_home, config, raw_config):
    raw, problems = parse_job("enso-memory", BUNDLED / "JOB.md")
    assert raw is not None and not problems
    workspaces.seed_home(enso_home)
    workspaces.seed_jobs(enso_home, config.defaults)
    write_config(enso_home, raw_config)
    job = load_job(enso_home, config, "enso-memory")
    assert validate(job, config) == []
    assert (job.provider, job.model, job.effort) == ("claude", "opus", "xhigh")
    assert (job.schedule, job.timeout, job.max_followups) == ("*/15 * * * *", 300, 1)
    assert job.prerun == "prerun.sh" and job.postrun == "postrun.sh"
    assert job.enabled and job.catch_up and job.workspace == "default"
    skill = enso_home.skills / "enso-memory/SKILL.md"
    assert skill.is_file()
    job_file = job.job_dir / "JOB.md"
    customized = job_file.read_text().replace('"*/15 * * * *"', '"0 * * * *"')
    job_file.write_text(customized)
    workspaces.reconcile_bundles(enso_home, config.defaults)
    assert job_file.read_text() == customized


@pytest.mark.parametrize(
    ("script", "run_status", "command_exit", "expected_exit", "calls"),
    [
        ("prerun.sh", "", 0, 0, "memory prepare --batch run-1 --json"),
        ("prerun.sh", "", 1, 1, "memory prepare --batch run-1 --json"),
        ("prerun.sh", "", 2, 2, "memory prepare --batch run-1 --json"),
        ("postrun.sh", "ok", 0, 0, "memory check --batch run-1"),
        ("postrun.sh", "ok", 10, 10, "memory check --batch run-1"),
        ("postrun.sh", "ok", 2, 2, "memory check --batch run-1"),
        ("postrun.sh", "no_work", 10, 0, ""),
        ("postrun.sh", "error", 10, 0, ""),
        ("postrun.sh", "timeout", 10, 0, ""),
    ],
)
def test_hooks_gate_and_check_without_losing_exit_codes(
    enso_home, tmp_path, script, run_status, command_exit, expected_exit, calls
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation = tmp_path / "invocation"
    stub = bin_dir / "enso"
    stub.write_text(
        '#!/bin/bash\nprintf "%s" "$*" > "$MEMORY_TEST_CALL"\n'
        f'printf "result\\n"\nexit {command_exit}\n'
    )
    stub.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(BUNDLED / script)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "ENSO_RUN_ID": "run-1",
            "ENSO_RUN_STATUS": run_status,
            "MEMORY_TEST_CALL": str(invocation),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == expected_exit
    assert (invocation.read_text() if invocation.exists() else "") == calls
    assert result.stdout == ("result\n" if calls else "")
    if script == "prerun.sh" and expected_exit == 2:
        assert "ENSO_ERROR: memory preparation failed" in result.stderr
    else:
        assert result.stderr == ""


@pytest.fixture
async def memory_pipeline(enso_home, fake_config, fake_claude, tmp_path, monkeypatch):
    """Capture through Runtime, then run the shipped job with a CLI-writing fake provider."""
    import copy
    import sys

    from conftest import FakeReply, FakeTransport, make_turn

    from enso.config import load_config
    from enso.runtime import Runtime

    runtime = Runtime(fake_config)
    turn = await runtime.submit(make_turn("Please pause deployment until Friday."), FakeReply())
    assert turn is not None
    await turn

    # Hooks and the provider use the actual CLI in the test interpreter, never an installed home.
    bin_dir = tmp_path / "memory-bin"
    bin_dir.mkdir()
    launcher = bin_dir / "enso"
    launcher.write_text(f"#!{sys.executable}\nfrom enso.cli import main\nmain()\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MEMORY_FAKE_STREAM", fake_claude)
    provider = tmp_path / "memory-provider.py"
    provider.write_text(
        f"#!{sys.executable}\n"
        """import json
import os
import runpy
import subprocess
import sys

mode = os.environ["MEMORY_FAKE_MODE"]
if mode != "omit":
    prompt = sys.argv[sys.argv.index("--") + 1]
    batch = json.loads(prompt.split("```json\\n", 1)[1].split("\\n```", 1)[0])
    assert batch["id"] == os.environ["ENSO_RUN_ID"]
    assert batch["turns"][0]["request"] == "Please pause deployment until Friday."
    entries = [] if mode == "empty" else [{
        "summary": "The user requested deployment stay paused until Friday.",
        "source_ids": [batch["turns"][0]["id"]],
    }]
    receipt = subprocess.run(
        ["enso", "memory", "record", "--batch", batch["id"], "--file", "-", "--json"],
        input=json.dumps(entries), capture_output=True, text=True, timeout=15,
    )
    if receipt.returncode:
        sys.stderr.write(receipt.stdout + receipt.stderr)
        sys.exit(receipt.returncode)
runpy.run_path(os.environ["MEMORY_FAKE_STREAM"], run_name="__main__")
"""
    )
    provider.chmod(0o755)
    raw = copy.deepcopy(fake_config.raw)
    raw["providers"]["claude"]["path"] = str(provider)
    write_config(enso_home, raw)
    config = load_config(enso_home)
    workspaces.seed_jobs(enso_home, config.defaults)
    return config, load_job(enso_home, config, "enso-memory"), FakeTransport()


@pytest.mark.parametrize(("mode", "count"), [("save", 1), ("empty", 0)])
async def test_bundled_pipeline_records_then_skips_idle_run(
    enso_home, memory_pipeline, monkeypatch, mode, count
):
    from enso import memory, runs
    from enso.jobs.runner import JobRunner

    config, job, transport = memory_pipeline
    monkeypatch.setenv("MEMORY_FAKE_MODE", mode)
    assert memory.status(enso_home)["pending_turns"] == 1
    runner = JobRunner(config, {"slack": transport})
    completed = await runner.run(job, trigger="manual")
    assert completed.status == "ok", completed
    assert completed.run_id
    assert len(runs.attempts(enso_home, completed.run_id)) == 1
    assert memory.batch_status(enso_home, completed.run_id)["status"] == "recorded"
    status = memory.status(enso_home)
    assert status["entries"] == count and status["pending_turns"] == 0
    if count:
        entry = memory.list_entries(enso_home).entries[0]
        assert memory.sources(enso_home, entry.ref)[0].request == (
            "Please pause deployment until Friday."
        )
    idle = await runner.run(job, trigger="manual")
    assert idle.status == "no_work"
    assert idle.run_id and memory.status(enso_home) == status
    assert transport.sent == []


async def test_bundled_pipeline_cannot_succeed_without_recording(
    enso_home, memory_pipeline, monkeypatch
):
    from enso import memory, runs
    from enso.jobs.runner import JobRunner

    config, job, transport = memory_pipeline
    monkeypatch.setenv("MEMORY_FAKE_MODE", "omit")
    completed = await JobRunner(config, {"slack": transport}).run(job, trigger="manual")
    assert completed.status == "error" and completed.run_id
    attempts = runs.attempts(enso_home, completed.run_id)
    assert len(attempts) == 2
    assert [attempt.postrun_exit_code for attempt in attempts] == [10, 10]
    assert attempts[0].session_id == attempts[1].session_id
    assert "max_followups=1" in (completed.postrun_error or "")
    assert memory.batch_status(enso_home, completed.run_id)["status"] == "pending"
    assert memory.status(enso_home)["pending_turns"] == 1
    assert memory.status(enso_home)["entries"] == 0
    assert transport.sent == []
