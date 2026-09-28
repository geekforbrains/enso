"""Workflow setup validates before mutation and resumes interrupted migrations safely."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import commit_file, edit_project, write_job
from typer.testing import CliRunner

from enso import jobs, maintenance, tasks, workflow_setup, workflows, worktrees
from enso.cli import app
from enso.cli.workflows import NOTIFY
from enso.config import Config, Paths, load_config

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["workflow", *args, "--json"])


def dev_args() -> tuple[str, ...]:
    return (
        "init",
        "EN",
        "--preset",
        "dev",
        "--lint",
        "python -m compileall -q .",
        "--test",
        "python -m unittest",
    )


@pytest.fixture
def repo_config(enso_home: Paths, project_config: Config, repo: Path) -> Config:
    edit_project(enso_home, repo=str(repo))
    return load_config(enso_home)


def test_basic_preset_has_no_checks_or_repository_requirement(
    enso_home: Paths, project_config: Config
) -> None:
    path = enso_home.project("default", "EN") / "PROJECT.md"
    path.write_text(path.read_text() + "Keep this project context.\n")
    installation = enso_home.config.read_bytes()
    result = invoke("init", "EN", "--preset", "basic")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["stages"] == ["work"] and not payload["enabled"]
    configured = load_config(enso_home)
    assert configured.projects["EN"].stages[0].checks == ()
    assert "Keep this project context." in path.read_text()
    assert enso_home.config.read_bytes() == installation
    loaded, faults = jobs.load_jobs(enso_home, configured)
    assert not faults and len(loaded) == 1 and not loaded[0].enabled
    assert not maintenance.paused(enso_home)


def test_development_preset_requires_commands_before_any_project_mutation(
    enso_home: Paths, repo_config: Config
) -> None:
    original = (enso_home.project("default", "EN") / "PROJECT.md").read_bytes()
    result = invoke("init", "EN", "--preset", "dev", "--lint", "true")
    assert result.exit_code == 1 and "--test" in json.loads(result.stdout)["error"]
    assert (enso_home.project("default", "EN") / "PROJECT.md").read_bytes() == original
    assert not enso_home.workspace_jobs("default").exists()
    assert not maintenance.paused(enso_home)


def test_development_preset_creates_disabled_valid_jobs_and_external_worktrees(
    enso_home: Paths, repo_config: Config
) -> None:
    result = invoke(*dev_args(), "--base", "main", "--worktree-root", "../task-worktrees")
    assert result.exit_code == 0, result.output
    configured = load_config(enso_home)
    project = configured.projects["EN"]
    assert project.stage_names == ("build", "review", "qa", "merge")
    assert project.max_concurrency == 3 and project.base == "main"
    assert project.worktree_root == "../task-worktrees"
    build, review, qa, merge = project.stages
    assert [check.name for check in build.checks] == ["lint", "tests"]
    assert review.return_to == "build" and review.max_returns == 1
    assert qa.human and qa.return_to == "build" and merge.integrate
    loaded, faults = jobs.load_jobs(enso_home, configured)
    assert not faults and all(not job.enabled for job in loaded)
    by_stage = {job.stage: job for job in loaded}
    assert set(by_stage) == {"build", "review", "merge"}  # a person moves qa; no job
    assert by_stage["merge"].agent is None and by_stage["merge"].command is None
    assert jobs.execution_kind(by_stage["merge"], configured) == "integration"
    # A second model reviews what the first built, when one is configured.
    assert by_stage["build"].agent.provider == "claude"
    assert by_stage["review"].agent.provider == "codex"
    assert project.hooks == {"after:blocked": "./notify.sh", "after:qa": "./notify.sh"}
    notify = enso_home.project("default", "EN") / "notify.sh"
    assert notify.stat().st_mode & 0o777 == 0o700
    assert "enso message send" in notify.read_text()


def test_development_preset_keeps_the_projects_own_hooks_and_scripts(
    enso_home: Paths, repo_config: Config
) -> None:
    notify = enso_home.project("default", "EN") / "notify.sh"
    notify.write_text("echo mine\n")
    refused = invoke(*dev_args())
    assert refused.exit_code == 1 and "preserving existing" in json.loads(refused.stdout)["error"]
    assert notify.read_text() == "echo mine\n"
    notify.unlink()
    edit_project(enso_home, hooks={"after:blocked": "./page-me.sh"})
    result = invoke(*dev_args())
    assert result.exit_code == 0, result.output
    hooks = load_config(enso_home).projects["EN"].hooks
    assert hooks == {"after:blocked": "./page-me.sh", "after:qa": "./notify.sh"}


@pytest.mark.parametrize(
    ("stage", "kind", "message", "expected"),
    [
        (
            "blocked",
            "decision",
            "Keep the promo code?\nMore context",
            "needs you: Keep the promo code?",
        ),
        (
            "blocked",
            "approval",
            "Changed tests need approval",
            "needs you: Changed tests need approval",
        ),
        ("blocked", "failure", "x" * 300, "stopped: " + "x" * 157 + "..."),
        ("qa", "", "**Changed:** the email", "is ready for qa: Retire the old email"),
    ],
)
def test_the_development_notify_hook_sends_one_short_line(
    tmp_path: Path, stage: str, kind: str, message: str, expected: str
) -> None:
    script = tmp_path / "notify.sh"
    script.write_text(NOTIFY)
    fake = tmp_path / "bin" / "enso"
    fake.parent.mkdir()
    fake.write_text('#!/usr/bin/env bash\nprintf "%s" "$*" > "$SENT"\n')
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake.parent}:{os.environ['PATH']}",
        "SENT": str(tmp_path / "sent"),
        "ENSO_TASK": "EN-001",
        "ENSO_TASK_TITLE": "Retire the old email",
        "ENSO_TO_STAGE": stage,
        "ENSO_BLOCK_KIND": kind,
        "ENSO_MESSAGE": message,
    }
    subprocess.run(["bash", str(script)], env=env, check=True, timeout=10)
    assert (tmp_path / "sent").read_text() == f"message send EN-001 {expected}"


def test_a_notice_that_cannot_be_sent_never_holds_up_the_task(tmp_path: Path) -> None:
    script = tmp_path / "notify.sh"
    script.write_text(NOTIFY)
    fake = tmp_path / "bin" / "enso"
    fake.parent.mkdir()
    fake.write_text("#!/usr/bin/env bash\necho 'no destination' >&2\nexit 1\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}", "ENSO_TASK": "EN-001"}
    done = subprocess.run(
        ["bash", str(script)],
        env={**env, "ENSO_TO_STAGE": "qa"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert done.returncode == 0 and "not sent: EN-001 is ready for qa" in done.stderr


def test_replacement_preserves_scripts_tasks_and_history_and_retires_original_jobs(
    enso_home: Paths, repo_config: Config
) -> None:
    edit_project(enso_home, stages=["build"])
    repo_config = load_config(enso_home)
    task = tasks.create(enso_home, repo_config, "EN", "Existing work", actor="user:test")
    blocked = tasks.move(
        enso_home,
        repo_config,
        task.ref,
        "block",
        actor="user:test",
        run_id=None,
        message="Waiting on clarification",
    )
    old = write_job(enso_home, "old-build", project="EN", stage="build", omit=["schedule"])
    original = old.read_bytes()
    custom = old.parent / "postrun.sh"
    custom.write_text("printf 'custom check'\n")
    history_before = tasks.events(enso_home, task.ref)
    result = invoke(*dev_args(), "--migrate")
    assert result.exit_code == 0, result.output
    assert tasks.get(enso_home, task.ref) == blocked
    assert tasks.events(enso_home, task.ref) == history_before
    assert old.with_name("JOB.md.pre-workflow").read_bytes() == original
    assert custom.read_text() == "printf 'custom check'\n"
    loaded, faults = jobs.load_jobs(enso_home, load_config(enso_home))
    assert not faults
    archived = next(job for job in loaded if job.dir_name == "old-build")
    assert not archived.enabled and archived.project == "EN" and archived.workflow == 1


def test_initialization_refuses_live_claims_and_preserves_config(
    enso_home: Paths, repo_config: Config
) -> None:
    tasks.create(enso_home, repo_config, "EN", "Busy", actor="user:test")
    tasks.take(enso_home, repo_config, "EN", "triage", run_id="busy", actor="job:test")
    original = (enso_home.project("default", "EN") / "PROJECT.md").read_bytes()
    result = invoke(*dev_args(), "--migrate")
    assert result.exit_code == 1 and "active project runs" in json.loads(result.stdout)["error"]
    assert (enso_home.project("default", "EN") / "PROJECT.md").read_bytes() == original
    assert not maintenance.paused(enso_home)


def test_existing_unrelated_target_is_never_overwritten(
    enso_home: Paths, repo_config: Config
) -> None:
    path = write_job(enso_home, "en-build")
    original = path.read_bytes(), enso_home.config.read_bytes()
    result = invoke(*dev_args())
    assert result.exit_code == 1 and "preserving existing" in json.loads(result.stdout)["error"]
    assert (path.read_bytes(), enso_home.config.read_bytes()) == original


def test_partial_io_failure_keeps_admission_closed_and_same_command_resumes(
    enso_home: Paths, repo_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    edit_project(enso_home, stages=["build"])
    repo_config = load_config(enso_home)
    task = tasks.create(enso_home, repo_config, "EN", "Existing", actor="user:test")
    original_write = maintenance.write_bytes
    broken_path = enso_home.workspace_jobs("default") / "en-review" / "JOB.md"

    def broken(path: Path, data: bytes, mode: int = 0o600) -> None:
        if path == broken_path:
            raise OSError("simulated full disk")
        original_write(path, data, mode)

    monkeypatch.setattr(workflow_setup.maintenance, "write_bytes", broken)
    failed = invoke(*dev_args(), "--migrate")
    assert failed.exit_code == 1 and "paused safely" in json.loads(failed.stdout)["error"]
    assert maintenance.paused(enso_home)
    refused = runner.invoke(app, ["task", "advance", task.ref, "--message", "bypass"])
    assert refused.exit_code == 1
    recovery_refused = invoke("retry", task.ref, "--message", "bypass")
    assert recovery_refused.exit_code == 1
    monkeypatch.setattr(workflow_setup.maintenance, "write_bytes", original_write)
    resumed = invoke("init", "EN")
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["resumed"] is True
    assert not maintenance.paused(enso_home)
    assert tasks.get(enso_home, task.ref).stage == "build"
    loaded, faults = jobs.load_jobs(enso_home, load_config(enso_home))
    assert not faults and len(loaded) == 3 and all(not job.enabled for job in loaded)
    notify = enso_home.project("default", "EN") / "notify.sh"
    assert notify.stat().st_mode & 0o777 == 0o700  # the resumed write keeps the mode


def test_workflow_recovery_is_not_available_inside_an_agent_run(
    enso_home: Paths, project_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = tasks.create(enso_home, project_config, "EN", "Work", actor="user:test")
    monkeypatch.setenv("ENSO_RUN_ID", "agent-run")
    result = invoke("retry", task.ref, "--message", "pretend approval")
    assert result.exit_code == 1
    assert "operator" in json.loads(result.stdout)["error"]


def test_verify_executes_checks_and_does_not_fake_a_failed_stage(
    enso_home: Paths, project_config: Config
) -> None:
    edit_project(
        enso_home, stages=[{"name": "work", "checks": [{"name": "lint", "command": "exit 1"}]}]
    )
    config = load_config(enso_home)
    task = tasks.create(enso_home, config, "EN", "Fail validation", actor="user:test")
    result = invoke("verify", task.ref, "--message", "Candidate ready")
    assert result.exit_code == 1
    shown = invoke("show", task.ref)
    assert shown.exit_code == 0, shown.output
    transaction = json.loads(shown.stdout)["transactions"][0]
    assert transaction["checks"][0]["exit_code"] == 1
    assert transaction["status"] != "accepted"
    assert tasks.get(enso_home, task.ref).stage != "done"


@pytest.mark.parametrize("stage,blocked", [("triage", False), ("todo", True)])
def test_replacement_never_converts_legacy_task_stages(enso_home, repo_config, stage, blocked):
    edit_project(enso_home, stages=[stage])
    config = load_config(enso_home)
    task = tasks.create(enso_home, config, "EN", "Retain this stage", actor="user:test")
    if blocked:
        task = tasks.move(
            enso_home,
            config,
            task.ref,
            "block",
            actor="user:test",
            run_id=None,
            message="Keep the return destination",
        )
    history = tasks.events(enso_home, task.ref)
    project = enso_home.project("default", "EN") / "PROJECT.md"
    job = write_job(enso_home, "existing", project="EN", stage=stage, omit=["schedule"])
    before = project.read_bytes(), job.read_bytes()
    result = invoke(*dev_args(), "--migrate")
    assert result.exit_code == 1 and "existing task stages" in result.stdout
    assert tasks.get(enso_home, task.ref) == task and tasks.events(enso_home, task.ref) == history
    assert (project.read_bytes(), job.read_bytes()) == before
    assert not maintenance.paused(enso_home)
    assert not (enso_home.workspace_jobs("default") / "en-build").exists()


def test_approve_rules_continues_the_handoff_it_blocked(
    enso_home: Paths, repo_config: Config, repo: Path
) -> None:
    commit_file(repo, "test_health.py", "original\n", "test: acceptance input")
    checks = [{"name": "test", "command": "true"}]
    edit_project(enso_home, stages=[{"name": "work", "checks": checks}, "review"])
    config = load_config(enso_home)
    task = tasks.create(enso_home, config, "EN", "Change a test", actor="user:test")
    assert tasks.take(enso_home, config, "EN", "work", run_id="r1", actor="job:test")
    info = worktrees.prepare(enso_home, config.projects["EN"], task.ref)
    workflows.start(enso_home, config, task.ref, "r1")
    commit_file(info.path, "test_health.py", "updated\n", "test: update")
    tasks.move(
        enso_home, config, task.ref, "advance", actor="job:test", run_id="r1", message="Built"
    )
    refused = asyncio.run(workflows.evaluate(enso_home, config, task.ref, "r1", dict(os.environ)))
    workflows.interrupt(enso_home, config, task.ref, "r1", refused.feedback)
    result = invoke("approve-rules", task.ref, "--message", "Sound edit")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) | {"ref": task.ref} == {
        "ok": True,
        "ref": task.ref,
        "action": "approve-rules",
        "continued": True,
        "stage": "review",
    }
