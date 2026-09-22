"""Repeated stops cannot release stage ownership before settlement workers finish."""

from __future__ import annotations

import asyncio
import threading

import pytest
from conftest import load_job, write_job

from enso import runs, tasks, workflows, worktrees
from enso.jobs import taskflow


@pytest.mark.parametrize("preparation_fails", [False, True])
async def test_cancel_during_preparation_cleanup_keeps_claim_and_ownership_until_settled(
    enso_home, project_config, monkeypatch, preparation_fails
):
    write_job(enso_home, project="EN", stage="triage", omit=["schedule"])
    job = load_job(enso_home, project_config)
    task = tasks.create(enso_home, project_config, "EN", "Check cancellation", actor="user:test")
    run_id = runs.start(enso_home, job, "ready", effort="high")
    preparing, interrupting = asyncio.Event(), asyncio.Event()
    finish_prepare, finish_interrupt = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original_interrupt = workflows.interrupt

    def prepare(*args):
        loop.call_soon_threadsafe(preparing.set)
        assert finish_prepare.wait(5)
        if preparation_fails:
            raise worktrees.WorktreeError("setup failed")
        return taskflow.StageRun(task, prompt="Ready")

    def interrupt(*args):
        loop.call_soon_threadsafe(interrupting.set)
        assert finish_interrupt.wait(5)
        original_interrupt(*args)

    monkeypatch.setattr(taskflow, "_prepare", prepare)
    monkeypatch.setattr(workflows, "interrupt", interrupt)
    running = asyncio.create_task(taskflow.begin(enso_home, project_config, job, run_id, ""))
    try:
        await asyncio.wait_for(preparing.wait(), 5)
        if not preparation_fails:
            running.cancel()
        finish_prepare.set()
        await asyncio.wait_for(interrupting.wait(), 5)
        for _ in range(3):
            running.cancel()
            await asyncio.sleep(0)
        assert not running.done()
        assert tasks.get(enso_home, task.ref).claim_run_id == run_id
        with (
            pytest.raises(worktrees.WorktreeBusyError),
            worktrees.execution_context(enso_home, task.ref),
        ):
            pass
    finally:
        finish_prepare.set()
        finish_interrupt.set()
        await asyncio.gather(running, return_exceptions=True)
    assert running.cancelled()
    settled = tasks.get(enso_home, task.ref)
    assert settled.claim_run_id is None and settled.stage == "blocked"
    with worktrees.execution_context(enso_home, task.ref):
        pass


def test_project_capacity_does_not_collide_with_user_concurrency_names(enso_home):
    from enso.jobs.concurrency import acquire_group_lock
    from enso.jobs.runner import acquire_project_slot

    group = acquire_group_lock(enso_home, "project:EN:slot:0")
    assert group is not None
    slot = None
    try:
        slot = acquire_project_slot(enso_home, "EN", 1)
        assert slot is not None
        assert acquire_project_slot(enso_home, "EN", 1) is None
    finally:
        if slot is not None:
            slot.close()
        group.close()


@pytest.mark.parametrize("missing_run", [False, True])
def test_recovery_releases_claims_whose_run_is_already_finished_or_missing(
    enso_home, project_config, missing_run
):
    from enso import db
    from enso.jobs.runner import JobRunner

    write_job(enso_home, project="EN", stage="triage", omit=["schedule"])
    job = load_job(enso_home, project_config)
    task = tasks.create(enso_home, project_config, "EN", "Recover claim", actor="user:test")
    run_id = runs.start(enso_home, job, "ready", effort="high")
    tasks.take(enso_home, project_config, "EN", "triage", run_id=run_id, actor=f"job:{job.ref}")
    runs.finish(enso_home, run_id, status="error", error="interrupted before releasing the task")
    if missing_run:
        with db.transaction(enso_home) as connection:
            connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    runner = JobRunner(project_config)
    assert runner.recover() == 0  # no running row needed recovery
    recovered = tasks.get(enso_home, task.ref)
    assert recovered.claim_run_id is None and recovered.stage == "blocked"
    assert recovered.attention
    assert runner.recover() == 0


async def test_repeated_cancel_during_final_settlement_holds_resources_until_worker_finishes(
    enso_home, project_config, monkeypatch
):
    from enso.jobs.concurrency import acquire_group_lock
    from enso.jobs.runner import JobRunner, RunResult, acquire_lock

    write_job(
        enso_home,
        project="EN",
        stage="triage",
        omit=["schedule"],
        concurrency={"group": "settlement", "on_busy": "wait"},
    )
    job = load_job(enso_home, project_config)
    task = tasks.create(enso_home, project_config, "EN", "Finish safely", actor="user:test")
    runner = JobRunner(project_config)
    interrupting = asyncio.Event()
    finish_interrupt = threading.Event()
    loop = asyncio.get_running_loop()
    original_interrupt = workflows.interrupt

    async def execute(job, run_id, *args, **kwargs):
        return RunResult("ok", run_id, output="completed")

    def interrupt(*args):
        loop.call_soon_threadsafe(interrupting.set)
        assert finish_interrupt.wait(5)
        original_interrupt(*args)

    monkeypatch.setattr(runner, "_turns", execute)
    monkeypatch.setattr(workflows, "interrupt", interrupt)
    running = asyncio.create_task(runner.run(job, trigger="manual"))
    try:
        await asyncio.wait_for(interrupting.wait(), 5)
        for _ in range(3):
            running.cancel()
            await asyncio.sleep(0)
        assert not running.done()
        assert tasks.get(enso_home, task.ref).claim_run_id is not None
        assert runs.list_runs(enso_home)[0].status == "running"
        assert acquire_lock(enso_home, job.ref) is None
        assert acquire_group_lock(enso_home, "settlement") is None
        with (
            pytest.raises(worktrees.WorktreeBusyError),
            worktrees.execution_context(enso_home, task.ref),
        ):
            pass
    finally:
        finish_interrupt.set()
        await asyncio.gather(running, return_exceptions=True)
    assert running.cancelled()
    assert tasks.get(enso_home, task.ref).claim_run_id is None
    assert runs.list_runs(enso_home)[0].status == "error"
    with worktrees.execution_context(enso_home, task.ref):
        pass
    for lock in (acquire_lock(enso_home, job.ref), acquire_group_lock(enso_home, "settlement")):
        assert lock is not None
        lock.close()
