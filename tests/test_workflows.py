"""Acceptance is externally verified, durable, and never implied by provider prose."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from conftest import commit_file, write_config

from enso import tasks, workflows, worktrees
from enso.config import Config, Paths, load_config


@pytest.fixture
def repo_config(enso_home, project_config, repo):
    raw = project_config.raw.copy()
    raw["projects"] = {**raw["projects"], "EN": {**raw["projects"]["EN"], "repo": str(repo)}}
    write_config(enso_home, raw)
    return load_config(enso_home)


def configure(paths: Paths, config: Config, *, checks=(), max_repairs=2, hooks=None, stages=None):
    raw = config.raw.copy()
    raw["projects"] = {**raw["projects"]}
    raw["projects"]["EN"] = {
        **raw["projects"]["EN"],
        "stages": stages or [{"name": "work", "checks": list(checks), "max_repairs": max_repairs}],
        "hooks": hooks or {},
    }
    write_config(paths, raw)
    return load_config(paths)


def held(paths, config, run="r1"):
    task = tasks.create(paths, config, "EN", "Build a feature", actor="user:test")
    assert tasks.take(paths, config, "EN", task.stage, run_id=run, actor="job:test")
    project = config.projects["EN"]
    stage = project.stage(task.stage)
    if project.repo and stage.worktree is not False:
        worktrees.prepare(paths, project, task.ref)
    workflows.start(paths, config, task.ref, run)
    return task


def submit(paths, config, ref, run="r1"):
    return tasks.move(
        paths, config, ref, "advance", actor="job:test", run_id=run, message="Candidate ready"
    )


@pytest.mark.asyncio
async def test_simple_non_git_submission_waits_for_acceptance(enso_home, project_config):
    config = configure(enso_home, project_config)
    task = held(enso_home, config)
    submitted = submit(enso_home, config, task.ref)
    assert (submitted.stage, submitted.claim_run_id) == ("work", "r1")
    assert not tasks.ready(enso_home, config, "EN", "work")
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "accepted"
    assert tasks.get(enso_home, task.ref).stage == "done"
    assert workflows.history(enso_home, task.ref)[0]["checks"] == []


@pytest.mark.asyncio
async def test_real_failure_repairs_and_records_each_candidate(enso_home, repo_config):
    config = configure(
        enso_home, repo_config, checks=[{"name": "unit", "command": "test -f feature.py"}]
    )
    task = held(enso_home, config)
    submit(enso_home, config, task.ref)
    first = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert first.status == "repair" and "Required check unit failed" in first.feedback
    assert tasks.get(enso_home, task.ref).stage == "work"
    cwd = Path(worktrees.lookup(enso_home, task.ref)["path"])
    head = commit_file(cwd, "feature.py", "value = 1\n", "feat: implement")
    submit(enso_home, config, task.ref)
    assert (
        await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    ).status == "accepted"
    tx = workflows.history(enso_home, task.ref)[0]
    assert [c["status"] for c in tx["checks"]] == ["failed", "passed"]
    assert tx["checks"][0]["candidate"] != head == tx["checks"][1]["candidate"]
    assert tx["repairs"] == 1


@pytest.mark.asyncio
async def test_explicit_block_during_repair_preserves_failed_check_evidence(
    enso_home, project_config
):
    config = configure(
        enso_home, project_config, checks=[{"name": "unit", "command": "echo failed; exit 1"}]
    )
    task = held(enso_home, config)
    submit(enso_home, config, task.ref)
    assert (await workflows.evaluate(enso_home, config, task.ref, "r1", {})).status == "repair"
    before = workflows.history(enso_home, task.ref)[0]
    reason = "The failing test needs a product decision before another repair."
    tasks.move(
        enso_home,
        config,
        task.ref,
        "block",
        actor="job:test",
        run_id="r1",
        message=reason,
    )
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", {})
    assert result.status == "failed" and result.feedback == reason
    transaction = workflows.history(enso_home, task.ref)[0]
    assert transaction["status"] == "blocked" and transaction["message"] == reason
    assert transaction["checks"] == before["checks"]
    assert transaction["attempts"] == before["attempts"] == 1
    assert transaction["repairs"] == before["repairs"] == 1


@pytest.mark.asyncio
async def test_previous_block_is_not_a_handoff_for_a_new_run(enso_home, project_config):
    config = configure(enso_home, project_config)
    task = held(enso_home, config)
    tasks.move(
        enso_home,
        config,
        task.ref,
        "block",
        actor="job:test",
        run_id="r1",
        message="Original decision",
    )
    workflows.interrupt(enso_home, config, task.ref, "r1", "stopped")
    tasks.release(
        enso_home, task.ref, actor="enso", run_id="r1", message="ended", reason="run_ended"
    )
    tasks.move(enso_home, config, task.ref, "resume", actor="user:test", run_id=None)
    tasks.take(enso_home, config, "EN", "work", run_id="r2", actor="job:test")
    workflows.start(enso_home, config, task.ref, "r2")
    result = await workflows.evaluate(enso_home, config, task.ref, "r2", {})
    assert result.status == "no_submission"
    assert "Original decision" not in result.feedback
    assert workflows.history(enso_home, task.ref)[0]["status"] == "working"


@pytest.mark.asyncio
async def test_failed_budget_survives_run_restart_and_manual_resume(enso_home, project_config):
    config = configure(
        enso_home, project_config, checks=[{"name": "unit", "command": "exit 1"}], max_repairs=0
    )
    task = held(enso_home, config)
    submit(enso_home, config, task.ref)
    assert (await workflows.evaluate(enso_home, config, task.ref, "r1", {})).status == "failed"
    workflows.interrupt(enso_home, config, task.ref, "r1", "stopped")
    tasks.move(enso_home, config, task.ref, "resume", actor="user:test", run_id=None)
    tasks.take(enso_home, config, "EN", "work", run_id="r2", actor="job:test")
    workflows.start(enso_home, config, task.ref, "r2")
    submit(enso_home, config, task.ref, "r2")
    result = await workflows.evaluate(enso_home, config, task.ref, "r2", {})
    assert result.status == "failed" and "budget exhausted" in result.feedback
    assert workflows.history(enso_home, task.ref)[0]["checks"] == []


@pytest.mark.asyncio
async def test_changed_candidate_during_check_is_not_accepted(enso_home, repo_config):
    config = configure(
        enso_home, repo_config, checks=[{"name": "mutator", "command": "echo changed >> README.md"}]
    )
    task = held(enso_home, config)
    submit(enso_home, config, task.ref)
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "failed" and "evidence is stale" in result.feedback
    assert tasks.get(enso_home, task.ref).stage == "work"


def test_manual_moves_and_resume_cannot_skip_gates(enso_home, project_config):
    config = configure(
        enso_home,
        project_config,
        stages=[{"name": "work", "checks": [{"name": "test", "command": "exit 0"}]}, "review"],
    )
    task = tasks.create(enso_home, config, "EN", "T", actor="user:test")
    with pytest.raises(tasks.TaskError, match="required stage checks"):
        tasks.move(
            enso_home,
            config,
            task.ref,
            "advance",
            actor="user:test",
            run_id=None,
            message="pretend passed",
        )
    tasks.move(enso_home, config, task.ref, "block", actor="user:test", run_id=None, message="wait")
    with pytest.raises(tasks.TaskError, match="cannot skip"):
        tasks.move(
            enso_home, config, task.ref, "resume", actor="user:test", run_id=None, to="review"
        )


@pytest.mark.asyncio
async def test_indirect_test_rule_change_requires_review(enso_home, repo_config, repo):
    commit_file(repo, "package.json", '{"scripts":{"test":"false"}}', "test: rules")
    config = configure(enso_home, repo_config, checks=[{"name": "test", "command": "true"}])
    task = held(enso_home, config)
    cwd = Path(worktrees.lookup(enso_home, task.ref)["path"])
    commit_file(cwd, "package.json", '{"scripts":{"test":"true"}}', "weaken tests")
    submit(enso_home, config, task.ref)
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "failed" and "Acceptance rule inputs changed" in result.feedback
    assert workflows.history(enso_home, task.ref)[0]["checks"] == []


@pytest.mark.asyncio
async def test_config_change_invalidates_snapshot(enso_home, project_config):
    config = configure(enso_home, project_config, checks=[{"name": "test", "command": "false"}])
    task = held(enso_home, config)
    submit(enso_home, config, task.ref)
    configure(enso_home, config, checks=[{"name": "test", "command": "true"}])
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", {})
    assert result.status == "failed" and "workflow changed" in result.feedback


@pytest.mark.asyncio
async def test_lifecycle_is_durable_blocks_next_stage_and_has_stable_event_id(
    enso_home, project_config
):
    config = configure(
        enso_home,
        project_config,
        hooks={"after:review": 'printf "%s" "$ENSO_EVENT_ID"; test -f ready'},
        stages=["work", "review"],
    )
    task = tasks.create(enso_home, config, "EN", "T", actor="user:test")
    tasks.move(
        enso_home,
        config,
        task.ref,
        "advance",
        actor="user:test",
        run_id=None,
        message="manual handoff",
    )
    assert not tasks.ready(enso_home, config, "EN", "review")
    before = workflows.event_history(enso_home, task.ref)[0]
    await workflows.drain_events(enso_home, config)
    failed = workflows.event_history(enso_home, task.ref)[0]
    assert failed["status"] == "failed" and failed["output"] == before["event_id"]
    assert tasks.get(enso_home, task.ref).stage == "review"
    (enso_home.workspace("default") / "ready").touch()
    await workflows.drain_events(enso_home, config)
    done = workflows.event_history(enso_home, task.ref)[0]
    assert done["event_id"] == before["event_id"] and done["status"] == "delivered"
    assert len(done["deliveries"]) == 2 and tasks.ready(enso_home, config, "EN", "review")


@pytest.mark.asyncio
async def test_timeout_and_interruption_never_become_passing_checks(
    enso_home, project_config, monkeypatch
):
    config = configure(
        enso_home, project_config, checks=[{"name": "wait", "command": "sleep 9", "timeout": 1}]
    )
    task = held(enso_home, config)
    submit(enso_home, config, task.ref)
    entered = asyncio.Event()

    async def interrupted(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(workflows, "command", interrupted)
    pending = asyncio.create_task(workflows.evaluate(enso_home, config, task.ref, "r1", {}))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    workflows.interrupt(enso_home, config, task.ref, "r1", "daemon restarted")
    tx = workflows.history(enso_home, task.ref)[0]
    assert tx["checks"][0]["status"] == "interrupted" and tx["attempts"] == 1
    assert tasks.get(enso_home, task.ref).stage == "blocked"


@pytest.mark.asyncio
async def test_operator_verify_checks_a_human_checkpoint(enso_home, project_config):
    config = configure(
        enso_home,
        project_config,
        stages=[
            {"name": "approve", "human": True, "checks": [{"name": "rule", "command": "true"}]}
        ],
    )
    task = tasks.create(enso_home, config, "EN", "T", actor="user:test")
    result = await workflows.verify_manual(enso_home, config, task.ref, "Reviewed actual output")
    assert result.status == "accepted" and tasks.get(enso_home, task.ref).finished
