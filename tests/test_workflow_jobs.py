"""The runner accepts task handoffs only after external workflow checks finish."""

from __future__ import annotations

import asyncio
import threading

import pytest
from conftest import edit_project, git, load_job, write_config, write_job, write_project

from enso import db, maintenance, runs, tasks, workflows
from enso.config import Config, Paths, load_config, parse_config
from enso.execution import ProviderTurn
from enso.jobs import parse_job
from enso.jobs import runner as runner_module
from enso.jobs.runner import JobRunner, acquire_project_slot


def workflow_config(paths: Paths, raw: dict, stages: list, **project_fields: object) -> Config:
    write_project(paths, "EN", {"name": "Example", "stages": stages, **project_fields})
    config, problems, _ = parse_config(raw, paths)
    assert config is not None, problems
    db.initialize(paths)
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
    job, problems = parse_job(path, config)
    assert job is not None and not problems, problems

    def no_provider(*args: object, **kwargs: object) -> None:
        pytest.fail("a command stage invoked a provider")

    monkeypatch.setattr(runner_module, "make_provider", no_provider)
    task = tasks.create(enso_home, config, "EN", "Generate output", actor="user:test")
    result = await JobRunner(config).run(job, trigger="manual")
    assert result.status == "ok", result.error
    assert tasks.get(enso_home, task.ref).stage == "done"
    assert (enso_home.project("default", "EN") / "output.txt").read_text() == "ready"
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
        (enso_home.project("default", "EN") / "finished.txt").touch()
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
            (enso_home.project("default", "EN") / "fixed.txt").touch()
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


@pytest.mark.parametrize("provider_status", ["ok", "error"])
@pytest.mark.parametrize("submitted", [False, True])
async def test_explicit_block_keeps_reason_and_writer_until_provider_stops(
    enso_home: Paths,
    raw_config: dict,
    monkeypatch: pytest.MonkeyPatch,
    provider_status: str,
    submitted: bool,
) -> None:
    config = workflow_config(
        enso_home,
        raw_config,
        [{"name": "work", "checks": [{"name": "tests", "command": "touch check-ran"}]}],
    )
    task = tasks.create(enso_home, config, "EN", "Needs an operator", actor="user:test")
    reason = "Setup rule needs operator review; test wrapper inherited watch mode."

    async def turn(*args: object, **kwargs: object) -> ProviderTurn:
        env = kwargs["env"]
        assert isinstance(env, dict)
        if submitted:
            submit(enso_home, config, env)
        blocked = tasks.move(
            enso_home,
            config,
            task.ref,
            "block",
            actor="job:work",
            run_id=env["ENSO_RUN_ID"],
            message=reason,
        )
        assert blocked.stage == "blocked" and blocked.claim_run_id == env["ENSO_RUN_ID"]
        assert workflows.history(enso_home, task.ref)[0]["status"] in ("working", "submitted")
        with pytest.raises(tasks.TaskError, match="claimed"):
            tasks.move(enso_home, config, task.ref, "resume", actor="user:test", run_id=None)
        return ProviderTurn(
            provider_status,
            output="Work stopped for operator review",
            error="provider crashed" if provider_status == "error" else "",
            exit_code=1 if provider_status == "error" else 0,
        )

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    result = await JobRunner(config).run(stage_job(enso_home, config), trigger="manual")
    assert result.status == "error"
    assert result.error == (reason if provider_status == "ok" else "provider crashed")
    blocked = tasks.get(enso_home, task.ref)
    assert blocked.stage == "blocked" and blocked.claim_run_id is None
    transaction = workflows.history(enso_home, task.ref)[0]
    assert transaction["status"] == "blocked"
    assert transaction["error"] == transaction["message"] == reason
    assert transaction["move"] == "block" and transaction["to_stage"] == "blocked"
    assert transaction["checks"] == [] and transaction["attempts"] == 0
    assert not (enso_home.project("default", "EN") / "check-ran").exists()
    assert not any(event.kind == "accepted" for event in tasks.events(enso_home, task.ref))
    assert result.run_id is not None
    attempts = runs.attempts(enso_home, result.run_id)
    assert len(attempts) == 1 and attempts[0].status == provider_status
    assert attempts[0].exit_code == (0 if provider_status == "ok" else 1)


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


@pytest.mark.parametrize("separate_workspaces", [False, True])
async def test_stages_keep_task_ownership_during_simultaneous_work(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch, separate_workspaces
) -> None:
    config = workflow_config(enso_home, raw_config, ["work", "review"], max_concurrency=2)
    first = tasks.create(enso_home, config, "EN", "First", actor="user:test")
    if separate_workspaces:
        write_project(enso_home, "TEAM", {"name": "Team", "stages": ["work"]}, "team")
        config, problems, _ = parse_config(raw_config, enso_home)
        assert config is not None, problems
        second = tasks.create(enso_home, config, "TEAM", "Second", actor="user:test")
        write_job(
            enso_home, "work", workspace="team", project="TEAM", stage="work", omit=["schedule"]
        )
        jobs = [stage_job(enso_home, config), load_job(enso_home, config, "team:work")]
    else:
        tasks.move(
            enso_home,
            config,
            first.ref,
            "advance",
            actor="user:test",
            run_id=None,
            message="Review",
        )
        second = tasks.create(enso_home, config, "EN", "Second", actor="user:test")
        jobs = [stage_job(enso_home, config), stage_job(enso_home, config, name="review")]
    entered: set[str] = set()
    together = asyncio.Event()

    async def turn(*args: object, **kwargs: object) -> ProviderTurn:
        env = kwargs["env"]
        assert isinstance(env, dict)
        task = tasks.get(enso_home, env["ENSO_TASK"])
        assert env["ENSO_WORKSPACE"] == task.workspace
        assert kwargs["cwd"] == enso_home.workspace(task.workspace)
        entered.add(env["ENSO_TASK"])
        if len(entered) == 2:
            together.set()
        await asyncio.wait_for(together.wait(), 2)
        tasks.move(
            enso_home,
            config,
            task.ref,
            "advance",
            actor=f"job:{env['ENSO_JOB']}",
            run_id=env["ENSO_RUN_ID"],
            message="Candidate ready",
        )
        return ProviderTurn("ok", output="finished", exit_code=0)

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    runner = JobRunner(config)
    results = await asyncio.gather(*(runner.run(job, trigger="manual") for job in jobs))
    assert [r.status for r in results] == ["ok", "ok"]
    assert entered == {first.ref, second.ref}
    for task in (first, second):
        saved = tasks.get(enso_home, task.ref)
        assert saved.workspace == task.workspace and saved.claim_run_id is None
        assert workflows.history(enso_home, task.ref)[0]["status"] == "accepted"


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


async def test_queued_stage_cannot_use_configuration_from_before_project_edit(
    enso_home: Paths, raw_config: dict
) -> None:
    workflow_config(enso_home, raw_config, ["work"])
    write_config(enso_home, raw_config)
    config = load_config(enso_home)
    tasks.create(enso_home, config, "EN", "Use current config", actor="user:test")
    queued = stage_job(enso_home, config)
    edit_project(enso_home, max_concurrency=2)
    result = await JobRunner(config).run(queued, trigger="ready")
    assert result.status == "skipped" and "configuration changed" in result.error
    assert runs.list_runs(enso_home) == []


async def test_project_scripts_and_stage_run_keep_their_workspace(enso_home, raw_config, repo):
    """Real shell scripts, a local Git candidate, and lifecycle cleanup; no provider/network."""
    fields = {
        "name": "Team project",
        "repo": str(repo),
        "setup": "bash setup.sh",
        "stages": [
            {
                "name": "work",
                "command": "bash work.sh",
                "checks": [{"name": "check", "command": "bash check.sh"}],
            }
        ],
        "hooks": {"after:done": "bash done.sh", "teardown": "bash teardown.sh"},
    }
    definition = write_project(enso_home, "TEAM", fields, "team")
    directory = definition.parent
    for name, command in {
        "setup": 'test -d "$ENSO_TASK_DIR/.git" || test -f "$ENSO_TASK_DIR/.git"',
        "work": (
            'cd "$ENSO_TASK_DIR"; echo candidate > feature.py; '
            "git add feature.py; git commit -qm candidate"
        ),
        "check": 'test -f "$ENSO_TASK_DIR/feature.py"',
        "done": 'test -f "$ENSO_TASK_DIR/feature.py"',
        "teardown": 'test -d "$ENSO_TASK_DIR"',
    }.items():
        (directory / f"{name}.sh").write_text(
            'set -eu\ntest "$ENSO_WORKSPACE" = team\ntest "$PWD" = "'
            + str(directory)
            + '"\n'
            + f'printf "{name}\\n" >> trace\n'
            + command
            + "\n"
        )
    write_config(enso_home, raw_config)
    config = load_config(enso_home)
    db.initialize(enso_home)
    task = tasks.create(enso_home, config, "TEAM", "Run in team", actor="user:test")
    write_job(enso_home, "work", workspace="team", project="TEAM", stage="work", omit=["schedule"])
    good = load_job(enso_home, config, "team:work")
    bad_path = write_job(enso_home, "work", project="TEAM", stage="work", omit=["schedule"])
    _, problems = parse_job(bad_path, config)
    assert any("stage jobs must share its workspace" in problem for problem in problems)
    result = await JobRunner(config).run(good, trigger="manual")
    assert result.status == "ok", result.error
    assert tasks.get(enso_home, task.ref).workspace == "team"
    assert tasks.get(enso_home, task.ref).stage == "done"
    tx = workflows.history(enso_home, task.ref)[0]
    assert tx["status"] == "accepted" and tx["checks"][0]["status"] == "passed"
    assert workflows.event_history(enso_home, task.ref)[0]["status"] == "delivered"
    # The unmerged candidate is retained; teardown still runs from its owning project.
    from enso import worktrees

    record = worktrees.lookup(enso_home, task.ref)
    assert (
        record is not None
        and worktrees.worktree_path(enso_home, config.projects["TEAM"], task.ref).exists()
    )
    assert (directory / "trace").read_text().splitlines()[:4] == ["setup", "work", "check", "done"]
    git(repo, "merge", "--ff-only", f"enso/{task.ref}")
    assert worktrees.sweep(enso_home, config.projects["TEAM"]) == [task.ref]
    assert (directory / "trace").read_text().splitlines()[-1] == "teardown"
    assert not (enso_home.workspace("default") / "trace").exists()
