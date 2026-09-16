"""Regression checks for workflow trust, finite repair, and retained execution ownership."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import commit_file, edit_project, git

from enso import tasks, workflows, worktrees
from enso.config import Config, Paths, load_config


def configured(paths: Paths, config: Config, stages: list, *, repo=None, hooks=None) -> Config:
    fields = {"stages": stages, "hooks": hooks or {}}
    if repo is not None:
        fields["repo"] = str(repo)
    edit_project(paths, **fields)
    return load_config(paths)


def claim(paths: Paths, config: Config, ref: str, run_id: str) -> None:
    task = tasks.get(paths, ref)
    assert tasks.take(paths, config, "EN", task.stage, run_id=run_id, actor="job:test")
    project = config.projects["EN"]
    if project.repo:
        worktrees.prepare(paths, project, ref)
    workflows.start(paths, config, ref, run_id)


def submission(paths: Paths, config: Config, ref: str, run_id: str, move: str = "advance") -> None:
    tasks.move(paths, config, ref, move, actor="job:test", run_id=run_id, message="Candidate ready")


def release_after_acceptance(paths: Paths, ref: str, run_id: str) -> None:
    if tasks.get(paths, ref).claim_run_id == run_id:
        tasks.release(
            paths, ref, actor=tasks.ENSO_ACTOR, run_id=run_id, message="Execution stopped"
        )


@pytest.mark.asyncio
async def test_dirty_submissions_consume_the_configured_repair_budget(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    config = configured(enso_home, project_config, [{"name": "work", "max_repairs": 1}], repo=repo)
    task = tasks.create(enso_home, config, "EN", "Bound repairs", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    cwd = worktrees.worktree_path(enso_home, config.projects["EN"], task.ref)
    (cwd / "forgotten.txt").write_text("still untracked\n")
    submission(enso_home, config, task.ref, "r1")
    assert (
        await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    ).status == "repair"
    submission(enso_home, config, task.ref, "r1")
    assert (
        await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    ).status == "failed"


@pytest.mark.parametrize("name", ["pytest.ini", "conftest.py", "eslint.config.js"])
@pytest.mark.asyncio
async def test_adding_a_check_harness_input_requires_rule_review(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    name: str,
) -> None:
    config = configured(
        enso_home,
        project_config,
        [{"name": "work", "checks": [{"name": "test", "command": "true"}]}],
        repo=repo,
    )
    task = tasks.create(enso_home, config, "EN", "Check inputs", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    cwd = worktrees.worktree_path(enso_home, config.projects["EN"], task.ref)
    commit_file(cwd, name, "# changes check discovery\n", "chore: change check harness")
    submission(enso_home, config, task.ref, "r1")
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "failed" and "rule" in result.feedback.lower()
    assert workflows.history(enso_home, task.ref)[0]["checks"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "backend/internal/store/store_test.go",
        "test_health.py",
        "pkg/test_health.py",
        "pkg/health_test.py",
    ],
)
@pytest.mark.parametrize("change", ["modify", "delete", "add"])
async def test_conventional_go_and_python_tests_preserve_existing_acceptance_inputs(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    name: str,
    change: str,
) -> None:
    if change != "add":
        commit_file(repo, name, "original acceptance assertion\n", "test: acceptance input")
    config = configured(
        enso_home,
        project_config,
        [{"name": "work", "checks": [{"name": "test", "command": "true"}]}],
        repo=repo,
    )
    task = tasks.create(enso_home, config, "EN", "Preserve existing tests", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    cwd = worktrees.worktree_path(enso_home, config.projects["EN"], task.ref)
    if change == "delete":
        git(cwd, "rm", name)
        git(cwd, "commit", "-qm", "test: remove assertion")
    else:
        commit_file(cwd, name, "new assertion\n", "test: change assertion")
    submission(enso_home, config, task.ref, "r1")
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    transaction = workflows.history(enso_home, task.ref)[0]
    if change == "add":
        assert result.status == "accepted"
        assert len(transaction["checks"]) == 1
    else:
        assert result.status == "failed" and name in result.feedback
        assert transaction["checks"] == []


@pytest.mark.asyncio
async def test_replacing_a_check_input_with_a_symlink_requires_rule_review(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    commit_file(repo, "package.json", "{}\n", "chore: package")
    config = configured(
        enso_home,
        project_config,
        [{"name": "work", "checks": [{"name": "test", "command": "true"}]}],
        repo=repo,
    )
    task = tasks.create(enso_home, config, "EN", "Type changes", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    cwd = worktrees.worktree_path(enso_home, config.projects["EN"], task.ref)
    (cwd / "package.json").unlink()
    (cwd / "package.json").symlink_to("README.md")
    git(cwd, "add", "package.json")
    git(cwd, "commit", "-qm", "chore: replace check input")
    submission(enso_home, config, task.ref, "r1")
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "failed" and "rule" in result.feedback.lower()


@pytest.mark.asyncio
async def test_integration_checks_rule_inputs_after_rebasing(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    config = configured(
        enso_home,
        project_config,
        [{"name": "integrate", "integrate": True, "checks": [{"name": "test", "command": "true"}]}],
        repo=repo,
    )
    task = tasks.create(enso_home, config, "EN", "Rebased evidence", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    cwd = worktrees.worktree_path(enso_home, config.projects["EN"], task.ref)
    commit_file(cwd, "feature.py", "feature = True\n", "feat: candidate")
    target = commit_file(repo, "pytest.ini", "[pytest]\n", "chore: change upstream harness")
    submission(enso_home, config, task.ref, "r1")
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "failed" and "rule" in result.feedback.lower()
    assert git(repo, "rev-parse", "HEAD").strip() == target


def test_manual_progression_waits_for_lifecycle_delivery(
    enso_home: Paths, project_config: Config
) -> None:
    config = configured(
        enso_home, project_config, ["work", "review"], hooks={"after:review": "true"}
    )
    task = tasks.create(enso_home, config, "EN", "Hook barrier", actor="user:test")
    tasks.move(
        enso_home, config, task.ref, "advance", actor="user:test", run_id=None, message="Review"
    )
    with pytest.raises(tasks.TaskError, match="lifecycle"):
        tasks.move(
            enso_home, config, task.ref, "advance", actor="user:test", run_id=None, message="Finish"
        )


@pytest.mark.asyncio
async def test_lifecycle_failure_defers_later_events_for_the_same_task(
    enso_home: Paths,
    project_config: Config,
) -> None:
    config = configured(
        enso_home,
        project_config,
        ["work", "review"],
        hooks={"after_transition": "test -f ready", "after:review": "touch second"},
    )
    task = tasks.create(enso_home, config, "EN", "Ordered hooks", actor="user:test")
    tasks.move(
        enso_home, config, task.ref, "advance", actor="user:test", run_id=None, message="Review"
    )
    await workflows.drain_events(enso_home, config)
    events = workflows.event_history(enso_home, task.ref)
    assert [event["status"] for event in events] == ["failed", "pending"]
    assert not (enso_home.project("default", "EN") / "second").exists()
    (enso_home.project("default", "EN") / "ready").touch()
    await workflows.drain_events(enso_home, config)
    assert [event["status"] for event in workflows.event_history(enso_home, task.ref)] == [
        "delivered",
        "delivered",
    ]


@pytest.mark.asyncio
async def test_operator_reset_replenishes_exhausted_return_budget(
    enso_home: Paths,
    project_config: Config,
) -> None:
    config = configured(enso_home, project_config, ["work", {"name": "review", "max_returns": 1}])
    task = tasks.create(enso_home, config, "EN", "Review loop", actor="user:test")
    for run_id in ("r1", "r2"):
        tasks.move(
            enso_home, config, task.ref, "advance", actor="user:test", run_id=None, message="Review"
        )
        claim(enso_home, config, task.ref, run_id)
        submission(enso_home, config, task.ref, run_id, "return")
        result = await workflows.evaluate(enso_home, config, task.ref, run_id, dict(os.environ))
        if run_id == "r1":
            assert result.status == "accepted"
            release_after_acceptance(enso_home, task.ref, run_id)
        else:
            assert result.status == "failed" and "return limit" in result.feedback
            workflows.interrupt(enso_home, config, task.ref, run_id, result.feedback)
    workflows.reset(enso_home, task.ref, "Reviewed why another revision is needed")
    tasks.move(enso_home, config, task.ref, "resume", actor="user:test", run_id=None)
    claim(enso_home, config, task.ref, "r3")
    submission(enso_home, config, task.ref, "r3", "return")
    result = await workflows.evaluate(enso_home, config, task.ref, "r3", dict(os.environ))
    assert result.status == "accepted"


def test_dependency_completion_does_not_release_a_blocked_tasks_live_writer(
    enso_home: Paths,
    project_config: Config,
) -> None:
    config = configured(enso_home, project_config, ["work"])
    waiting = tasks.create(enso_home, config, "EN", "Active writer", actor="user:test")
    dependency = tasks.create(enso_home, config, "EN", "Dependency", actor="user:test")
    claim(enso_home, config, waiting.ref, "writer")
    tasks.move(
        enso_home,
        config,
        waiting.ref,
        "block",
        actor="job:test",
        run_id="writer",
        message="Wait",
        after=dependency.ref,
    )
    tasks.move(
        enso_home,
        config,
        dependency.ref,
        "advance",
        actor="user:test",
        run_id=None,
        message="Complete",
    )
    current = tasks.get(enso_home, waiting.ref)
    assert current.stage == "blocked" and current.claim_run_id == "writer"


@pytest.mark.parametrize("interruption", ["after_git", "before_acceptance"])
@pytest.mark.asyncio
async def test_interrupted_integration_rechecks_before_accepting_already_landed_work(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    check = {"name": "check", "command": 'echo checked >> "$ENSO_HOME/check-count"'}
    config = configured(
        enso_home,
        project_config,
        [{"name": "integrate", "integrate": True, "checks": [check]}],
        repo=repo,
    )
    task = tasks.create(enso_home, config, "EN", "Recover integration", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    cwd = worktrees.worktree_path(enso_home, config.projects["EN"], task.ref)
    head = commit_file(cwd, "feature.py", "feature = True\n", "feat: candidate")
    submission(enso_home, config, task.ref, "r1")
    original_accept, original_finish = workflows._accept, worktrees.finish_land

    def failed_accept(*args, **kwargs):
        raise RuntimeError("process stopped before SQLite acceptance")

    def failed_finish(*args, **kwargs):
        original_finish(*args, **kwargs)
        raise OSError("process stopped after updating Git")

    if interruption == "before_acceptance":
        monkeypatch.setattr(workflows, "_accept", failed_accept)
        with pytest.raises(RuntimeError, match="SQLite acceptance"):
            await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    else:
        monkeypatch.setattr(worktrees, "finish_land", failed_finish)
        assert (
            await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
        ).status == "failed"
    previous = workflows.history(enso_home, task.ref)[0]
    assert previous["integration"]["phase"] == (
        "applied" if interruption == "before_acceptance" else "applying"
    )
    assert previous["integration"]["candidate"] == head
    assert previous["integration"]["checks"][0]["status"] == "passed"
    assert git(repo, "rev-parse", "HEAD").strip() == head
    assert tasks.get(enso_home, task.ref).stage == "integrate"
    workflows.interrupt(enso_home, config, task.ref, "r1", "Interrupted integration")
    if interruption == "before_acceptance":
        head = commit_file(repo, "later.py", "later = True\n", "feat: later target")
    monkeypatch.setattr(workflows, "_accept", original_accept)
    monkeypatch.setattr(worktrees, "finish_land", original_finish)
    workflows.reset(enso_home, task.ref, "Reviewed interrupted integration")
    tasks.move(enso_home, config, task.ref, "resume", actor="user:test", run_id=None)
    claim(enso_home, config, task.ref, "r2")
    submission(enso_home, config, task.ref, "r2")
    result = await workflows.evaluate(enso_home, config, task.ref, "r2", dict(os.environ))
    assert result.status == "accepted", result.feedback
    latest = workflows.history(enso_home, task.ref)[0]
    assert latest["recovery_of"] == previous["id"]
    assert latest["integration"]["phase"] == "applied"
    assert latest["integration"]["candidate"] == latest["integration"]["target_sha"] == head
    assert latest["checks"][0]["candidate"] == head
    assert (enso_home.home / "check-count").read_text() == "checked\nchecked\n"
    assert tasks.get(enso_home, task.ref).stage == "done"
    assert git(repo, "rev-parse", "HEAD").strip() == head


@pytest.mark.asyncio
async def test_already_landed_candidate_without_checked_intent_is_not_accepted(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    config = configured(
        enso_home, project_config, [{"name": "integrate", "integrate": True}], repo=repo
    )
    task = tasks.create(enso_home, config, "EN", "No invented intent", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    project = config.projects["EN"]
    cwd = worktrees.worktree_path(enso_home, project, task.ref)
    commit_file(cwd, "feature.py", "feature = True\n", "feat: candidate")
    worktrees.land(enso_home, project, task.ref)
    submission(enso_home, config, task.ref, "r1")
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "failed" and "already landed" in result.feedback
    assert tasks.get(enso_home, task.ref).stage == "integrate"


@pytest.mark.parametrize("operation", ["prepare_land", "finish_land"])
@pytest.mark.asyncio
async def test_integration_cancellation_keeps_leases_until_git_worker_stops(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    import asyncio
    import threading

    config = configured(
        enso_home, project_config, [{"name": "integrate", "integrate": True}], repo=repo
    )
    task = tasks.create(enso_home, config, "EN", "Cancel safely", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    project = config.projects["EN"]
    cwd = worktrees.worktree_path(enso_home, project, task.ref)
    commit_file(cwd, "feature.py", "feature = True\n", "feat: candidate")
    submission(enso_home, config, task.ref, "r1")
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = getattr(worktrees, operation)

    def slow_git(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test must release its Git worker"
        return original(*args, **kwargs)

    monkeypatch.setattr(worktrees, operation, slow_git)
    with worktrees.execution_context(enso_home, task.ref):
        pending = asyncio.create_task(
            workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            with (
                pytest.raises(worktrees.WorktreeError, match="another task is integrating"),
                worktrees.landing_context(enso_home, project, task.ref),
            ):
                pass
            # Repeated cancellation must not release the lease either.
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
    if operation == "finish_land":
        assert workflows.history(enso_home, task.ref)[0]["integration"]["phase"] == "applied"
    with worktrees.landing_context(enso_home, project, task.ref):
        assert not list((repo / ".git" / "worktrees").glob("*/rebase-merge"))
    workflows.interrupt(enso_home, config, task.ref, "r1", "Cancelled after Git stopped")


@pytest.mark.asyncio
async def test_operator_retry_recovers_abandoned_manual_claim_but_never_a_live_owner(
    enso_home: Paths,
    project_config: Config,
) -> None:
    config = configured(enso_home, project_config, ["work"])
    task = tasks.create(enso_home, config, "EN", "Recover manual verification", actor="user:test")
    claim(enso_home, config, task.ref, "manual-abandoned")
    with (
        worktrees.execution_context(enso_home, task.ref),
        pytest.raises(tasks.TaskError, match="active execution"),
    ):
        workflows.reset(enso_home, task.ref, "Cannot interrupt a live verifier")
    assert tasks.get(enso_home, task.ref).claim_run_id == "manual-abandoned"
    workflows.reset(enso_home, task.ref, "Recover interrupted manual verification")
    assert tasks.get(enso_home, task.ref).claim_run_id is None
    assert workflows.history(enso_home, task.ref)[0]["status"] == "blocked"
    assert any(event.kind == "workflow_recovered" for event in tasks.events(enso_home, task.ref))
    result = await workflows.verify_manual(enso_home, config, task.ref, "Fresh verification")
    assert result.status == "accepted"


@pytest.mark.parametrize(
    "command,code",
    [("enso-nonexistent-test-tool", 127), ("./not-executable", 126), ("kill -TERM $$", -15)],
)
@pytest.mark.asyncio
async def test_missing_tools_and_signals_are_infrastructure_failures(
    enso_home: Paths,
    project_config: Config,
    command: str,
    code: int,
) -> None:
    config = configured(
        enso_home,
        project_config,
        [{"name": "work", "checks": [{"name": "check", "command": command}]}],
    )
    if code == 126:
        executable = enso_home.project("default", "EN") / "not-executable"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o600)
    task = tasks.create(enso_home, config, "EN", "Bad infrastructure", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    submission(enso_home, config, task.ref, "r1")
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "failed"
    tx = workflows.history(enso_home, task.ref)[0]
    assert tx["repairs"] == 0
    assert tx["checks"][0]["status"] == "error"
    assert tx["checks"][0]["exit_code"] == code
    assert tx["checks"][0]["error"]


@pytest.mark.asyncio
async def test_lifecycle_after_worktree_removal_uses_project_directory_and_keeps_audit_identity(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    from enso import db

    config = configured(
        enso_home,
        project_config,
        ["work"],
        repo=repo,
        hooks={"after:done": 'test -z "$ENSO_TASK_DIR" && pwd'},
    )
    task = tasks.create(enso_home, config, "EN", "Retired worktree", actor="user:test")
    project = config.projects["EN"]
    info = worktrees.prepare(enso_home, project, task.ref)
    tasks.move(
        enso_home, config, task.ref, "drop", actor="user:test", run_id=None, message="Cancel"
    )
    assert worktrees.sweep(enso_home, project) == [task.ref]
    with db.transaction(enso_home) as con:
        workflows.enqueue(con, project, tasks.get(enso_home, task.ref), "done", None)
    event = workflows.event_history(enso_home, task.ref)[0]
    assert event["cwd"] == "" and event["worktree"]["path"] == str(info.path)
    assert event["worktree"]["status"] == "removed"
    await workflows.drain_events(enso_home, config)
    event = workflows.event_history(enso_home, task.ref)[0]
    assert event["status"] == "delivered"
    assert event["output"].strip() == str(enso_home.project(project.workspace, project.key))


@pytest.mark.asyncio
async def test_configuration_changed_during_landing_cannot_accept_old_rules(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = configured(
        enso_home, project_config, [{"name": "integrate", "integrate": True}], repo=repo
    )
    task = tasks.create(enso_home, config, "EN", "Landing definition race", actor="user:test")
    claim(enso_home, config, task.ref, "r1")
    cwd = worktrees.worktree_path(enso_home, config.projects["EN"], task.ref)
    head = commit_file(cwd, "feature.py", "feature = True\n", "feat: candidate")
    submission(enso_home, config, task.ref, "r1")
    original = worktrees.finish_land

    def changed_definition(*args, **kwargs):
        result = original(*args, **kwargs)
        edit_project(enso_home, max_concurrency=2)
        return result

    monkeypatch.setattr(worktrees, "finish_land", changed_definition)
    result = await workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ))
    assert result.status == "failed" and "workflow changed" in result.feedback
    tx = workflows.history(enso_home, task.ref)[0]
    assert tx["integration"]["phase"] == "applied"
    assert tx["status"] == "blocked" and tasks.get(enso_home, task.ref).stage == "integrate"
    assert git(repo, "rev-parse", "HEAD").strip() == head


@pytest.mark.asyncio
async def test_lifecycle_missing_project_records_failure_without_changing_owner(
    enso_home, project_config
):
    config = configured(
        enso_home, project_config, ["work"], hooks={"after:done": "touch misplaced"}
    )
    task = tasks.create(enso_home, config, "EN", "Keep lifecycle owner", actor="user:test")
    tasks.move(
        enso_home, config, task.ref, "advance", actor="user:test", run_id=None, message="Done"
    )
    directory = enso_home.project("default", "EN")
    directory.rename(enso_home.home / "removed-project")
    await workflows.drain_events(enso_home, config)
    event = workflows.event_history(enso_home, task.ref)[0]
    assert event["status"] == "failed" and str(directory) in event["error"]
    assert event["workspace"] == "default" and event["attempts"] == 1
    assert not (enso_home.workspace("default") / "misplaced").exists()
