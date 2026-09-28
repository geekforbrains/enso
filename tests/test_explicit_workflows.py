"""Domain-neutral paths, accepted revisions, decisions and preserved legacy execution."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest
from conftest import write_config, write_project

from enso import db, tasks, workflows
from enso.config import load_config


def configure(home, raw, stages, **fields):
    write_project(home, "WF", {"name": "Workflow", "stages": stages, **fields})
    write_config(home, raw)
    db.initialize(home)
    return load_config(home)


def new(paths, config, **kw):
    return tasks.create(
        paths, config, "WF", "Original request", body="Keep this input", actor="user:test", **kw
    )


def move(paths, config, ref, action="advance", **kw):
    return tasks.move(
        paths,
        config,
        ref,
        action,
        actor="user:test",
        run_id=None,
        message="Decision and evidence",
        **kw,
    )


def contract(paths, config, ref):
    return tasks.context(paths, config, ref, env={})["contract"]


def test_minimal_unchecked_result_keeps_original_request(enso_home, raw_config):
    config = configure(enso_home, raw_config, ["work"])
    task = new(enso_home, config)
    move(enso_home, config, task.ref, output={"text": "Delivered", "data": {"count": 3}})
    result = contract(enso_home, config, task.ref)
    assert tasks.get(enso_home, task.ref).body == "Keep this input"
    assert [(r["stage"], r["revision"]) for r in result["outputs"]] == [("request", 1), ("work", 1)]
    tx = workflows.history(enso_home, task.ref)[0]
    assert tx["accepted_output"] == result["outputs"][-1]["id"]
    assert result["outputs"][-1]["inputs"] == {"request": result["outputs"][0]["id"]}


def development(paths, raw):
    from pathlib import Path

    from enso import frontmatter

    example = Path(__file__).parents[1] / "assets/workflows/development/PROJECT.md"
    fields = {**frontmatter.read(example).fields, "enabled": True}
    return configure(paths, raw, fields.pop("stages"), **fields)


def test_development_decision_skips_without_evidence_or_hooks(enso_home, raw_config):
    config = development(enso_home, raw_config)
    task = new(enso_home, config)
    assert task.route is None and task.stage == "classify"
    with pytest.raises(tasks.TaskError, match="choose a path"):
        move(enso_home, config, task.ref)
    with pytest.raises(tasks.TaskError, match="not a declared choice"):
        move(enso_home, config, task.ref, route="invented")
    assert move(enso_home, config, task.ref, route="direct").stage == "build"
    move(enso_home, config, task.ref)
    assert move(enso_home, config, task.ref).stage == "done"
    assert [r["stage"] for r in contract(enso_home, config, task.ref)["outputs"]] == [
        "request",
        "classify",
        "build",
        "review",
    ]
    assert not any(
        e.to_stage in ("plan", "approve", "qa") for e in tasks.events(enso_home, task.ref)
    )


def test_decision_can_return_before_any_path_is_selected(enso_home, raw_config):
    config = configure(
        enso_home,
        raw_config,
        [
            "research",
            {"name": "classify", "routes": ["simple", "complex"], "return_to": "research"},
            "work",
            "review",
        ],
        paths={
            "simple": ["research", "classify", "work"],
            "complex": ["research", "classify", "work", "review"],
        },
    )
    task = new(enso_home, config)
    move(enso_home, config, task.ref)
    returned = move(enso_home, config, task.ref, "return")
    assert returned.stage == "research" and returned.route is None
    assert not next(
        row
        for row in contract(enso_home, config, task.ref)["outputs"]
        if row["stage"] == "research"
    )["valid"]


def test_changed_human_instructions_require_a_fresh_decision_token(enso_home, raw_config):
    config = configure(enso_home, raw_config, ["draft", {"name": "review", "human": True}])
    task = new(enso_home, config)
    move(enso_home, config, task.ref)
    approval = contract(enso_home, config, task.ref)["approval"]
    project = config.projects["WF"]
    stages = (
        project.stages[0],
        replace(project.stages[1], instructions="Review the revised policy"),
    )
    config = replace(config, projects={"WF": replace(project, stages=stages)})
    with pytest.raises(tasks.TaskError, match="approve the current input revisions"):
        move(enso_home, config, task.ref, approve=approval)
    assert (
        move(
            enso_home, config, task.ref, approve=contract(enso_home, config, task.ref)["approval"]
        ).stage
        == "done"
    )


def test_revised_plan_needs_fresh_approval_and_preserves_versions(enso_home, raw_config):
    config = development(enso_home, raw_config)
    task = new(enso_home, config, route="planned")
    move(enso_home, config, task.ref)
    move(enso_home, config, task.ref, output="First plan")
    first = contract(enso_home, config, task.ref)
    token = first["approval"]
    with pytest.raises(tasks.TaskError, match="approve the current input revisions"):
        move(enso_home, config, task.ref)
    move(enso_home, config, task.ref, "return")
    move(enso_home, config, task.ref, output="Corrected plan")
    with pytest.raises(tasks.TaskError, match="approve the current input revisions"):
        move(enso_home, config, task.ref, approve=token)
    current = contract(enso_home, config, task.ref)
    assert [(r["revision"], r["valid"]) for r in current["outputs"] if r["stage"] == "plan"] == [
        (1, False),
        (2, True),
    ]
    assert move(enso_home, config, task.ref, approve=current["approval"]).stage == "build"
    assert tasks.get(enso_home, task.ref).body == "Keep this input"
    assert contract(enso_home, config, task.ref)["inputs"]["plan"]["revision"] == 2


def test_reroute_revisits_new_required_work_and_keeps_budgets(enso_home, raw_config):
    config = development(enso_home, raw_config)
    task = new(enso_home, config, route="direct")
    move(enso_home, config, task.ref)
    move(enso_home, config, task.ref)
    changed = workflows.reroute(enso_home, config, task.ref, "planned", "Scope requires a plan")
    assert changed.stage == "plan" and changed.route == "planned"
    current = contract(enso_home, config, task.ref)
    assert next(r for r in current["outputs"] if r["stage"] == "classify")["valid"]
    assert not next(r for r in current["outputs"] if r["stage"] == "build")["valid"]
    move(enso_home, config, task.ref, "block")
    with pytest.raises(tasks.TaskError, match="interrupted stage"):
        tasks.move(enso_home, config, task.ref, "resume", actor="user:test", run_id=None, to="qa")


@pytest.mark.asyncio
async def test_agent_submission_accepts_deliverable_after_checks(enso_home, raw_config):
    config = configure(
        enso_home,
        raw_config,
        [{"name": "draft", "checks": [{"name": "content", "command": 'test -n "$ENSO_OUTPUT"'}]}],
    )
    task = new(enso_home, config)
    tasks.take(enso_home, config, "WF", "draft", run_id="writer", actor="job:writer")
    workflows.start(enso_home, config, task.ref, "writer")
    tasks.move(
        enso_home,
        config,
        task.ref,
        "advance",
        actor="job:writer",
        run_id="writer",
        message="Draft ready",
        output="Campaign brief",
    )
    assert tasks.get(enso_home, task.ref).stage == "draft"
    assert len(contract(enso_home, config, task.ref)["outputs"]) == 1
    assert (
        await workflows.evaluate(enso_home, config, task.ref, "writer", dict(os.environ))
    ).status == "accepted"
    assert contract(enso_home, config, task.ref)["outputs"][-1]["data"] == {
        "text": "Campaign brief"
    }


@pytest.mark.asyncio
async def test_stale_input_cannot_accept_or_publish_output(enso_home, raw_config):
    config = configure(enso_home, raw_config, ["draft"])
    task = new(enso_home, config)
    tasks.take(enso_home, config, "WF", "draft", run_id="writer", actor="job:writer")
    workflows.start(enso_home, config, task.ref, "writer")
    tasks.move(
        enso_home,
        config,
        task.ref,
        "advance",
        actor="job:writer",
        run_id="writer",
        message="Ready",
        output="Draft",
    )
    tasks.edit(enso_home, task.ref, actor="job:writer", run_id="writer", body="Changed input")
    result = await workflows.evaluate(enso_home, config, task.ref, "writer", {})
    assert result.status == "failed" and "stale" in result.feedback
    assert not any(r["stage"] == "draft" for r in contract(enso_home, config, task.ref)["outputs"])


@pytest.mark.asyncio
async def test_uncertain_lifecycle_requires_receipt_and_is_not_replayed(enso_home, raw_config):
    marker = enso_home.home / "delivered"
    config = configure(enso_home, raw_config, ["work"], hooks={"after:done": f"touch {marker}"})
    task = new(enso_home, config)
    move(enso_home, config, task.ref)
    event = workflows.event_history(enso_home, task.ref)[0]
    event.update(status="running", attempts=3)
    workflows._save_event(enso_home, event)
    await workflows.drain_events(enso_home, config)
    assert not marker.exists()
    assert workflows.event_history(enso_home, task.ref)[0]["status"] == "uncertain"
    workflows.reset(enso_home, task.ref, "Budget reset cannot replay uncertainty")
    await workflows.drain_events(enso_home, config)
    assert not marker.exists()
    workflows.resolve_event(enso_home, task.ref, event["id"], "delivered", "External receipt 123")
    assert workflows.event_history(enso_home, task.ref)[0]["receipt"] == "External receipt 123"


@pytest.mark.asyncio
async def test_uncertain_final_attempt_can_be_explicitly_retried(enso_home, raw_config):
    marker = enso_home.home / "delivered"
    config = configure(enso_home, raw_config, ["work"], hooks={"after:done": f"touch {marker}"})
    task = new(enso_home, config)
    move(enso_home, config, task.ref)
    event = workflows.event_history(enso_home, task.ref)[0]
    event.update(status="uncertain", attempts=3)
    workflows._save_event(enso_home, event)
    workflows.resolve_event(
        enso_home, task.ref, event["id"], "retry", "Verified no external effect"
    )
    await workflows.drain_events(enso_home, config)
    delivered = workflows.event_history(enso_home, task.ref)[0]
    assert marker.exists() and delivered["status"] == "delivered"
    assert delivered["attempts"] == 4 and delivered["max_attempts"] == 4


def test_changed_acceptance_rules_stale_prior_outputs_and_approvals(enso_home, raw_config):
    from enso.config import Check

    config = development(enso_home, raw_config)
    task = new(enso_home, config, route="planned")
    move(enso_home, config, task.ref)
    move(enso_home, config, task.ref, output="Plan")
    move(enso_home, config, task.ref, approve=contract(enso_home, config, task.ref)["approval"])
    project = config.projects["WF"]
    stages = tuple(
        replace(stage, checks=(Check(name="policy", command="true"),))
        if stage.name == "plan"
        else stage
        for stage in project.stages
    )
    config = replace(config, projects={"WF": replace(project, stages=stages)})
    shown = contract(enso_home, config, task.ref)
    assert {r["stage"] for r in shown["outputs"] if not r["valid"]} == {"plan", "approve"}
    assert "stale" in shown["pending"]
    assert not tasks.ready(enso_home, config, "WF", "build")
    assert tasks.list_tasks(enso_home, ready=True, config=config) == []
    assert tasks.take(enso_home, config, "WF", "build", run_id="blocked", actor="job:test") is None
    with pytest.raises(
        (tasks.TaskError, ValueError), match=r"missing accepted inputs|missing or stale"
    ):
        move(enso_home, config, task.ref)
    assert (
        workflows.reroute(enso_home, config, task.ref, "planned", "Changed checks").stage == "plan"
    )


def test_acceptance_rolls_back_output_route_and_events_together(enso_home, raw_config, monkeypatch):
    config = development(enso_home, raw_config)
    task = new(enso_home, config)
    before = tasks.events(enso_home, task.ref)

    def fail(*args, **kwargs):
        raise OSError("simulated delivery queue write failure")

    monkeypatch.setattr(workflows, "enqueue", fail)
    with pytest.raises(OSError, match="simulated"):
        move(enso_home, config, task.ref, route="planned")
    current = tasks.get(enso_home, task.ref)
    assert current.stage == "classify" and current.route is None
    assert len(contract(enso_home, config, task.ref)["outputs"]) == 1
    assert tasks.events(enso_home, task.ref) == before
    assert workflows.history(enso_home, task.ref) == []


def test_request_edits_do_not_replenish_return_budget(enso_home, raw_config):
    config = configure(
        enso_home,
        raw_config,
        ["draft", {"name": "review", "inputs": ["draft"], "return_to": "draft", "max_returns": 1}],
    )
    task = new(enso_home, config)
    move(enso_home, config, task.ref)
    move(enso_home, config, task.ref, "return")
    tasks.edit(enso_home, task.ref, actor="user:test", run_id=None, body="Corrected request")
    move(enso_home, config, task.ref)
    with pytest.raises(tasks.TaskError, match="return limit exhausted"):
        move(enso_home, config, task.ref, "return")


@pytest.mark.asyncio
async def test_legacy_tasks_jobs_hooks_and_cleanup_are_inert(enso_home, raw_config):
    config = configure(enso_home, raw_config, ["work"])
    task = new(enso_home, config)
    with db.transaction(enso_home) as con:
        con.execute("UPDATE _enso_tasks SET workflow_version=1 WHERE ref=?", (task.ref,))
    assert not tasks.ready(enso_home, config, "WF", "work")
    assert tasks.take(enso_home, config, "WF", "work", actor="job:x", run_id="x") is None
    with pytest.raises(tasks.TaskError, match="legacy"):
        move(enso_home, config, task.ref)
    project = config.projects["WF"]
    legacy = replace(config, projects={"WF": replace(project, workflow=1)})
    with pytest.raises(tasks.TaskError, match="legacy"):
        new(enso_home, legacy)
    assert contract(enso_home, config, task.ref)["status"] == "legacy"
    with pytest.raises(tasks.TaskError, match="legacy"):
        workflows.reset(enso_home, task.ref, "Cannot recover a legacy execution")
    with pytest.raises(ValueError, match="legacy"):
        await workflows.approve_rules(enso_home, config, task.ref, "Cannot approve legacy work")
    adopted = workflows.reroute(
        enso_home, config, task.ref, "default", "Reviewed preserved request", adopt=True
    )
    assert adopted.workflow_version == 2 and adopted.stage == "work"
    assert any(e.kind == "adopted" for e in tasks.events(enso_home, task.ref))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("domain", "route"), [("marketing", "campaign"), ("support", "simple"), ("support", "complex")]
)
async def test_domain_examples_use_independent_workers_scripts_and_decisions(
    enso_home, raw_config, monkeypatch, domain, route
):
    from pathlib import Path

    from conftest import load_job, write_job

    from enso import frontmatter
    from enso.execution import ProviderTurn
    from enso.jobs import runner as runner_module
    from enso.jobs.runner import JobRunner

    example = Path(__file__).parents[1] / "assets/workflows" / domain / "PROJECT.md"
    fields = {**frontmatter.read(example).fields, "enabled": True}
    config = configure(enso_home, raw_config, fields.pop("stages"), **fields)
    jobs = {}
    for index, stage in enumerate(config.projects["WF"].stages):
        if stage.human:
            continue
        agent = (
            {"provider": "claude", "model": "sonnet", "effort": "high"}
            if index % 2 == 0 or stage.name == "respond"
            else {"provider": "codex", "model": "sol", "effort": "medium"}
        )
        write_job(
            enso_home,
            stage.name,
            project="WF",
            stage=stage.name,
            omit=["schedule", "agent"] if stage.command else ["schedule"],
            **({} if stage.command else {"agent": agent}),
        )
        jobs[stage.name] = load_job(enso_home, config, stage.name)
    provider_stages = []

    async def turn(*args, **kwargs):
        env = kwargs["env"]
        current = tasks.get(enso_home, env["ENSO_TASK"])
        provider_stages.append(current.stage)
        tasks.move(
            enso_home,
            config,
            current.ref,
            "advance",
            actor="job:" + env["ENSO_JOB"],
            run_id=env["ENSO_RUN_ID"],
            message="Completed " + current.stage,
            output="Deliverable from " + current.stage,
            route=route if current.stage == "classify" else None,
        )
        return ProviderTurn("ok", output="Submitted", exit_code=0)

    monkeypatch.setattr(runner_module.execution, "execute_turn", turn)
    task = new(enso_home, config, route=route if domain == "marketing" else None)
    runner = JobRunner(config)
    returned = False
    while not (current := tasks.get(enso_home, task.ref)).finished:
        stage = config.projects["WF"].stage(current.stage)
        assert stage is not None
        if stage.human:
            if domain == "marketing" and not returned:
                move(enso_home, config, task.ref, "return")
                returned = True
            else:
                move(
                    enso_home,
                    config,
                    task.ref,
                    approve=contract(enso_home, config, task.ref)["approval"],
                )
        else:
            result = await runner.run(jobs[current.stage], trigger="manual")
            assert result.status == "ok", result.error
    assert not {"diagnose", "resolve", "publish"}.intersection(provider_stages)
    history = workflows.history(enso_home, task.ref)
    agent_runs = [tx["executor"]["agent"] for tx in history if tx.get("executor", {}).get("agent")]
    assert {agent["provider"] for agent in agent_runs} == {"claude", "codex"}
    if domain == "marketing":
        drafts = [
            row
            for row in contract(enso_home, config, task.ref)["outputs"]
            if row["stage"] == "draft"
        ]
        assert [(row["revision"], row["valid"]) for row in drafts] == [(1, False), (2, True)]
    elif route == "simple":
        assert not any(tx["stage"] in ("investigate", "approve") for tx in history)
    else:
        assert any(tx["stage"] == "approve" and tx["approval"] for tx in history)


@pytest.mark.asyncio
async def test_manual_replacement_activation_and_legacy_job_admission(enso_home, raw_config):
    from conftest import load_job, write_job

    from enso import workflow_setup
    from enso.jobs.runner import JobRunner

    config = configure(enso_home, raw_config, ["work"])
    task = new(enso_home, config)
    with db.transaction(enso_home) as con:
        con.execute("UPDATE _enso_tasks SET workflow_version=1 WHERE ref=?", (task.ref,))
    marker = enso_home.home / "old-gate"
    write_job(
        enso_home,
        "old",
        project="WF",
        stage="work",
        workflow=1,
        omit=["schedule"],
        gate={"command": f"touch {marker}", "timeout": 10},
    )
    legacy_job = load_job(enso_home, config, "old")
    legacy = configure(enso_home, raw_config, ["work"], workflow=1, enabled=False)
    result = await JobRunner(legacy).run(legacy_job, trigger="manual")
    assert result.status == "skipped" and not marker.exists()
    write_job(enso_home, "standalone", command="printf standalone", omit=["agent"])
    standalone = await JobRunner(legacy).run(
        load_job(enso_home, legacy, "standalone"), trigger="manual"
    )
    assert standalone.status == "ok" and standalone.output == "standalone"

    config = configure(
        enso_home,
        raw_config,
        [{"name": "work", "command": "printf delivered"}],
        enabled=False,
    )
    write_job(enso_home, "new", project="WF", stage="work", workflow=2, omit=["schedule", "agent"])
    replacement = load_job(enso_home, config, "new")
    assert (await JobRunner(config).run(replacement, trigger="manual")).status == "skipped"
    workflow_setup.enable(enso_home, "WF", "default")
    config = load_config(enso_home)
    assert tasks.get(enso_home, task.ref).workflow_version == 1
    assert (await JobRunner(config).run(legacy_job, trigger="manual")).status == "skipped"
    adopted = workflows.reroute(
        enso_home, config, task.ref, "default", "Reviewed legacy request", adopt=True
    )
    assert adopted.ref == task.ref
    result = await JobRunner(config).run(load_job(enso_home, config, "new"), trigger="manual")
    assert result.status == "ok", result.error
    assert tasks.get(enso_home, task.ref).stage == "done" and not marker.exists()
