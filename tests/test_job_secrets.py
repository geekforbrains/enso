"""Job secrets are resolved before execution and remain stable for the complete run."""

import asyncio
import os

import pytest
from conftest import FakeTransport, load_job, write_job

from enso import db, runs, secrets
from enso.execution import ProviderTurn
from enso.jobs import parse_job
from enso.jobs import runner as runner_module
from enso.jobs.runner import JobRunner


@pytest.mark.parametrize(
    "names", ["TOKEN", None, [1], ["TOKEN", "TOKEN"], ["bad"], ["ENSO_HOME"], ["PATH"]]
)
def test_job_rejects_invalid_declarations(enso_home, names):
    job, problems = parse_job(write_job(enso_home, secrets=names))
    assert job is None
    assert problems == ["secrets must be a list of unique, unreserved secret names"]


async def test_shared_snapshot_across_gate_agent_followups_and_postrun(
    enso_home, fake_config, monkeypatch
):
    secrets.add(enso_home, "TOKEN", "original")
    secrets.add(enso_home, "UNLISTED_TOKEN", "never-injected")
    monkeypatch.setenv("TOKEN", "inherited")
    write_job(enso_home, secrets=["TOKEN"], prerun="prerun.sh", postrun="postrun.sh")
    job = load_job(enso_home, fake_config)
    (job.job_dir / "prerun.sh").write_text('test "$TOKEN" = original || exit 2\nprintf gate-ok\n')
    (job.job_dir / "postrun.sh").write_text(
        'cat >/dev/null\ntest "$TOKEN" = original || exit 2\n'
        'if [ "$ENSO_RUN_ATTEMPT" = 1 ]; then printf continue; exit 10; fi\n'
    )
    seen = []

    async def turn(*args, **kwargs):
        env = kwargs["env"]
        seen.append(env["TOKEN"])
        assert "UNLISTED_TOKEN" not in env
        assert env["ENSO_HOME"] == str(enso_home.home)
        assert env["ENSO_JOB"] == job.ref
        assert "original" not in args[1]  # the generated prompt carries no secret
        if len(seen) == 1:
            secrets.delete(enso_home, "TOKEN")
            secrets.add(enso_home, "TOKEN", "replacement")
            env["TOKEN"] = "child-mutated-copy"
        return ProviderTurn("ok", output="done", exit_code=0, session_id="same-session")

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    runner = JobRunner(fake_config)
    result = await runner.run(job, trigger="manual")
    assert result.status == "ok", result.error
    assert seen == ["original", "original"]
    assert runner._run_env == {}

    assert os.environ["TOKEN"] == "inherited"
    assert secrets.resolve(enso_home, ["TOKEN"]) == {"TOKEN": "replacement"}
    # A subsequent run resolves again, including for a job without hooks.
    write_job(enso_home, secrets=["TOKEN"])

    async def batch(*args, **kwargs):
        assert kwargs["env"]["TOKEN"] == "replacement"
        return ProviderTurn("ok", output="done", exit_code=0)

    monkeypatch.setattr(runner_module.execution, "execute_batch", batch)
    assert (await runner.run(load_job(enso_home, fake_config), trigger="manual")).status == "ok"


async def test_concurrent_runs_keep_their_own_snapshot(enso_home, fake_config, monkeypatch):
    secrets.add(enso_home, "TOKEN", "first")
    for name in ("first", "second"):
        write_job(enso_home, name, secrets=["TOKEN"])
    started, release = asyncio.Event(), asyncio.Event()

    async def batch(*args, **kwargs):
        env = kwargs["env"]
        if env["ENSO_JOB"] == "default:first":
            started.set()
            await release.wait()
            assert env["TOKEN"] == "first"
        else:
            assert env["TOKEN"] == "second"
        return ProviderTurn("ok", exit_code=0)

    monkeypatch.setattr(runner_module.execution, "execute_batch", batch)
    runner = JobRunner(fake_config)
    first = asyncio.create_task(
        runner.run(load_job(enso_home, fake_config, "first"), trigger="manual")
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        secrets.delete(enso_home, "TOKEN")
        secrets.add(enso_home, "TOKEN", "second")
        second = await runner.run(load_job(enso_home, fake_config, "second"), trigger="manual")
        assert second.status == "ok"
    finally:
        release.set()
    assert (await first).status == "ok"
    assert runner._run_env == {}


async def test_secret_failure_is_reported_without_claiming_gate_recovery(enso_home, fake_config):
    secrets.add(enso_home, "PRESENT", "value")
    write_job(enso_home, secrets=["PRESENT", "MISSING"])
    job = load_job(enso_home, fake_config)
    with db.transaction(enso_home) as con:
        con.execute(
            "INSERT INTO job_state (workspace, job, failure_fingerprint, failure_alerted_at) "
            "VALUES ('default', 'nightly', 'previous-gate-error', ?)",
            (db.now(),),
        )
    transport = FakeTransport("slack")
    result = await JobRunner(fake_config, {"slack": transport}).run(job, trigger="schedule")
    assert result.status == "error"
    assert db.job_state(enso_home, job.ref).failure_fingerprint == "previous-gate-error"
    assert len(transport.sent) == 1


@pytest.mark.parametrize("failure", ["missing", "wrong_key"])
async def test_resolution_failure_starts_no_gate_agent_or_postrun(
    enso_home, fake_config, monkeypatch, failure
):
    from enso.config import secret_key_file

    if failure == "wrong_key":
        secrets.add(enso_home, "TOKEN", "value")
        secret_key_file(enso_home).unlink()
    write_job(enso_home, secrets=["TOKEN"], prerun="prerun.sh", postrun="postrun.sh")

    async def forbidden(*args, **kwargs):
        pytest.fail("a process started before all secrets were resolved")

    monkeypatch.setattr(runner_module.execution, "run_process", forbidden)
    monkeypatch.setattr(runner_module.execution, "execute_turn", forbidden)
    runner = JobRunner(fake_config)
    result = await runner.run(load_job(enso_home, fake_config), trigger="manual")
    assert result.status == "error" and result.error
    assert runs.get(enso_home, result.run_id).status == "error"
    assert runner._run_env == {}
