"""The runner accepts task handoffs only after external workflow checks finish."""

from __future__ import annotations

import asyncio
import threading

import pytest
from conftest import load_job, write_config, write_job

from enso import db, maintenance, runs, tasks, workflows
from enso.config import Config, Paths, load_config, parse_config
from enso.execution import ProviderTurn
from enso.jobs import parse_job
from enso.jobs import runner as runner_module
from enso.jobs.runner import JobRunner, acquire_project_slot


def workflow_config(paths: Paths, raw: dict, stages: list, **project_fields: object) -> Config:
    raw["projects"] = {
        "EN": {
            "name": "Example",
            "workspace": "default",
            "stages": stages,
            **project_fields,
        }
    }
    config, problems, _ = parse_config(raw, paths)
    assert config is not None, problems
    db.migrate(paths)
    return config


def stage_job(paths: Paths, config: Config, *, name: str = "work", **fields: object):
    write_job(paths, name, omit=["schedule"], project="EN", stage=name, **fields)
    return load_job(paths, config, name)


def submit(paths: Paths, config: Config, env: dict[str, str]) -> None:
    tasks.move(
        paths,
        config,
        env["ENSO_TASK"],
        "advance",
        actor="job:work",
        run_id=env["ENSO_RUN_ID"],
        message="Candidate ready",
    )


async def test_command_stage_needs_no_provider(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = workflow_config(
        enso_home,
        raw_config,
        [{"name": "work", "command": "printf 'ready' > output.txt"}],
    )
    path = write_job(
        enso_home,
        "work",
        omit=["schedule", "provider", "model", "effort"],
        project="EN",
        stage="work",
        prompt="",
    )
    job, problems = parse_job("work", path, config)
    assert job is not None and not problems, problems

    def no_provider(*args: object, **kwargs: object) -> None:
        pytest.fail("a command stage invoked a provider")

    monkeypatch.setattr(runner_module, "make_provider", no_provider)
    task = tasks.create(enso_home, config, "EN", "Generate output", actor="user:test")
    result = await JobRunner(config).run(job, trigger="manual")
    assert result.status == "ok", result.error
    assert tasks.get(enso_home, task.ref).stage == "done"
    assert (enso_home.workspace("default") / "output.txt").read_text() == "ready"
    assert workflows.history(enso_home, task.ref)[0]["status"] == "accepted"


async def test_submission_waits_for_provider_and_external_check(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = workflow_config(
        enso_home,
        raw_config,
        [{"name": "work", "checks": [{"name": "finished", "command": "test -f finished.txt"}]}],
    )
    task = tasks.create(enso_home, config, "EN", "Finish work", actor="user:test")

    async def turn(*args: object, **kwargs: object) -> ProviderTurn:
        env = kwargs["env"]
        assert isinstance(env, dict)
        submit(enso_home, config, env)
        pending = tasks.get(enso_home, task.ref)
        assert pending.stage == "work" and pending.claim_run_id == env["ENSO_RUN_ID"]
        assert not tasks.ready(enso_home, config, "EN", "work")
        (enso_home.workspace("default") / "finished.txt").touch()
        return ProviderTurn("ok", output="I passed everything", exit_code=0)

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    result = await JobRunner(config).run(stage_job(enso_home, config), trigger="manual")
    assert result.status == "ok", result.error
    assert tasks.get(enso_home, task.ref).stage == "done"
    transaction = workflows.history(enso_home, task.ref)[0]
    assert transaction["status"] == "accepted"
    assert transaction["checks"][0]["exit_code"] == 0


async def test_failed_check_repairs_with_actual_feedback_before_acceptance(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = workflow_config(
        enso_home,
        raw_config,
        [
            {
                "name": "work",
                "checks": [
                    {
                        "name": "tests",
                        "command": "test -f fixed.txt || { echo repair-required; exit 1; }",
                    }
                ],
            }
        ],
    )
    task = tasks.create(enso_home, config, "EN", "Repair work", actor="user:test")
    prompts: list[str] = []

    async def turn(*args: object, **kwargs: object) -> ProviderTurn:
        prompt = str(args[1])
        prompts.append(prompt)
        if len(prompts) == 2:
            assert "repair-required" in prompt
            assert "Task: EN-001" in prompt
            assert tasks.get(enso_home, task.ref).stage == "work"
            (enso_home.workspace("default") / "fixed.txt").touch()
        env = kwargs["env"]
        assert isinstance(env, dict)
        submit(enso_home, config, env)
        return ProviderTurn("ok", output="success", exit_code=0)

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    result = await JobRunner(config).run(stage_job(enso_home, config), trigger="manual")
    assert result.status == "ok", result.error
    assert len(prompts) == 2
    transaction = workflows.history(enso_home, task.ref)[0]
    assert [check["exit_code"] for check in transaction["checks"]] == [1, 0]
    assert tasks.get(enso_home, task.ref).stage == "done"


async def test_provider_error_after_submission_never_accepts(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = workflow_config(enso_home, raw_config, ["work"])
    task = tasks.create(enso_home, config, "EN", "Fail after handoff", actor="user:test")

    async def turn(*args: object, **kwargs: object) -> ProviderTurn:
        env = kwargs["env"]
        assert isinstance(env, dict)
        submit(enso_home, config, env)
        return ProviderTurn("error", error="provider crashed", exit_code=1)

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    result = await JobRunner(config).run(stage_job(enso_home, config), trigger="manual")
    assert result.status == "error"
    blocked = tasks.get(enso_home, task.ref)
    assert blocked.stage == "blocked" and blocked.attention
    assert workflows.history(enso_home, task.ref)[0]["status"] == "blocked"


def test_project_slots_enforce_capacity_without_serializing_every_stage(enso_home: Paths) -> None:
    first = acquire_project_slot(enso_home, "EN", 2)
    second = acquire_project_slot(enso_home, "EN", 2)
    assert first is not None and second is not None
    try:
        assert acquire_project_slot(enso_home, "EN", 2) is None
        first.close()
        replacement = acquire_project_slot(enso_home, "EN", 2)
        assert replacement is not None
        replacement.close()
    finally:
        first.close()
        second.close()


async def test_two_stages_can_run_different_tasks_concurrently(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = workflow_config(enso_home, raw_config, ["work", "review"], max_concurrency=2)
    first = tasks.create(enso_home, config, "EN", "First", actor="user:test")
    tasks.move(
        enso_home, config, first.ref, "advance", actor="user:test", run_id=None, message="Review"
    )
    second = tasks.create(enso_home, config, "EN", "Second", actor="user:test")
    entered: set[str] = set()
    together = asyncio.Event()

    async def turn(*args: object, **kwargs: object) -> ProviderTurn:
        env = kwargs["env"]
        assert isinstance(env, dict)
        entered.add(env["ENSO_TASK"])
        if len(entered) == 2:
            together.set()
        await asyncio.wait_for(together.wait(), 2)
        submit(enso_home, config, env)
        return ProviderTurn("ok", output="finished", exit_code=0)

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    runner = JobRunner(config)
    results = await asyncio.gather(
        runner.run(stage_job(enso_home, config), trigger="manual"),
        runner.run(stage_job(enso_home, config, name="review"), trigger="manual"),
    )
    assert [r.status for r in results] == ["ok", "ok"]
    assert entered == {first.ref, second.ref}


async def test_next_stage_defers_when_previous_accepted_run_still_owns_worktree(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = workflow_config(enso_home, raw_config, ["work", "review"], max_concurrency=2)
    task = tasks.create(enso_home, config, "EN", "Handoff without hooks", actor="user:test")
    review = stage_job(enso_home, config, name="review")
    runner = JobRunner(config)
    original_settle = runner._settle
    deferred = []

    async def turn(*args: object, **kwargs: object) -> ProviderTurn:
        env = kwargs["env"]
        assert isinstance(env, dict)
        submit(enso_home, config, env)
        return ProviderTurn("ok", output="finished", exit_code=0)

    async def settle(stage, run_id, status, **kwargs):
        await original_settle(stage, run_id, status, **kwargs)
        if stage.task.stage == "work":
            # The stage changed atomically, but the previous run has not released its
            # filesystem execution lock. A new scheduler must treat this as admission.
            deferred.append(await runner.run(review, trigger="manual"))

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    monkeypatch.setattr(runner, "_settle", settle)
    first = await runner.run(stage_job(enso_home, config), trigger="manual")
    assert first.status == "ok"
    assert len(deferred) == 1 and deferred[0].status == "no_work"
    waiting = tasks.get(enso_home, task.ref)
    assert waiting.stage == "review" and not waiting.attention and waiting.claim_run_id is None
    assert len(workflows.history(enso_home, task.ref)) == 1
    assert tasks.ready(enso_home, config, "EN", "review")
    second = await runner.run(review, trigger="manual")
    assert second.status == "ok" and tasks.get(enso_home, task.ref).stage == "done"


async def test_cancel_during_reservation_does_not_orphan_the_late_claim(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = workflow_config(enso_home, raw_config, ["work"])
    task = tasks.create(enso_home, config, "EN", "Reserve safely", actor="user:test")
    claimed, release = threading.Event(), threading.Event()
    original_take = tasks.take

    def slow_take(*args, **kwargs):
        result = original_take(*args, **kwargs)
        claimed.set()
        release.wait(5)
        return result

    monkeypatch.setattr(tasks, "take", slow_take)
    runner = JobRunner(config)
    running = runner.start(stage_job(enso_home, config), trigger="manual")
    stopping = None
    try:
        for _ in range(100):
            if claimed.is_set():
                break
            await asyncio.sleep(0.01)
        assert claimed.is_set()
        stopping = asyncio.create_task(runner.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done()
        assert tasks.get(enso_home, task.ref).claim_run_id
    finally:
        release.set()
        if stopping is not None:
            await stopping
        else:
            await runner.stop()
    assert running.cancelled()
    stopped = tasks.get(enso_home, task.ref)
    assert stopped.claim_run_id is None and stopped.stage == "blocked"


async def test_queued_stage_cannot_start_behind_maintenance_gate(
    enso_home: Paths, raw_config: dict
) -> None:
    config = workflow_config(enso_home, raw_config, ["work"])
    task = tasks.create(enso_home, config, "EN", "Wait for maintenance", actor="user:test")
    queued = stage_job(enso_home, config)
    maintenance.write_json(enso_home.maintenance, {"kind": "workflow-init"})
    result = await JobRunner(config).run(queued, trigger="ready")
    assert result.status == "skipped" and "maintenance" in result.error
    assert runs.list_runs(enso_home) == []
    assert tasks.get(enso_home, task.ref).claim_run_id is None


async def test_queued_stage_cannot_run_a_definition_retired_by_migration(
    enso_home: Paths, raw_config: dict
) -> None:
    config = workflow_config(enso_home, raw_config, ["work"])
    task = tasks.create(enso_home, config, "EN", "Use current workflow", actor="user:test")
    queued = stage_job(enso_home, config)
    queued.path.write_text(queued.path.read_text().replace("enabled: true", "enabled: false"))
    result = await JobRunner(config).run(queued, trigger="ready")
    assert result.status == "skipped" and "definition changed" in result.error
    assert runs.list_runs(enso_home) == []
    assert tasks.get(enso_home, task.ref).claim_run_id is None


async def test_queued_stage_cannot_use_configuration_from_before_migration(
    enso_home: Paths, raw_config: dict
) -> None:
    workflow_config(enso_home, raw_config, ["work"])
    write_config(enso_home, raw_config)
    config = load_config(enso_home)
    tasks.create(enso_home, config, "EN", "Use current config", actor="user:test")
    queued = stage_job(enso_home, config)
    raw_config["projects"]["EN"]["max_concurrency"] = 2
    write_config(enso_home, raw_config)
    result = await JobRunner(config).run(queued, trigger="ready")
    assert result.status == "skipped" and "configuration changed" in result.error
    assert runs.list_runs(enso_home) == []
