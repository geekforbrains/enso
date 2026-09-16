"""Workspace jobs turn live conversation fixtures into recallable, nonrecursive memory."""

import json
from dataclasses import replace

from conftest import FakeTransport, load_job, script, write_config, write_job, write_workspace
from typer.testing import CliRunner

from enso import captures, db, execution, harvesting, initialization, memory, runs, workspaces
from enso.cli import app
from enso.config import Agent, load_config


def conversation(paths):
    source, _ = captures.record(
        paths,
        captures.Message(
            "slack",
            "default",
            "slack:C1:1",
            "C1",
            "1",
            "1",
            "U1",
            "Gavin",
            "2026-09-16T12:00:00Z",
            "Could we launch September 25?",
        ),
    )
    captures.reply(
        paths,
        source.id,
        text="It is a proposal until testing finishes.",
        outcome="completed",
        delivery="complete",
        parts=(captures.Part(0, 47, "sent", "2"),),
    )
    captures.record(
        paths,
        captures.Message(
            "slack",
            "default",
            "slack:C1:1",
            "C1",
            "1",
            "3",
            "U2",
            "Alex",
            "2026-09-16T12:01:00Z",
            "Testing is still underway.",
            kind="ambient",
        ),
    )
    selected = harvesting.batch(paths, "default")
    return {
        "batch": selected.id,
        "sources": list(selected.sources),
        "notes": [
            {
                "name": "Launch proposal.md",
                "body": "Gavin proposed September 25. Enso suggested waiting for testing; "
                "Alex said testing was still underway. No launch date was confirmed.",
                "sources": list(selected.sources),
            }
        ],
        "no_memory": [],
    }


async def test_complete_conversation_harvests_with_repair_then_quiet_run_skips_provider(
    enso_home, fake_config, tmp_path, monkeypatch
):
    from enso.jobs.runner import JobRunner

    workspaces.seed_home(enso_home)
    workspaces.seed_jobs(enso_home, fake_config.defaults)
    job = load_job(enso_home, fake_config, "memory")
    value = conversation(enso_home)
    script(tmp_path, monkeypatch, "Not JSON", json.dumps(value))
    log = tmp_path / "launches.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LAUNCHES", str(log))
    transport = FakeTransport("slack")
    runner = JobRunner(fake_config, {"slack": transport})
    before = captures.query(enso_home, "default")

    completed = await runner.run(job, trigger="manual")

    assert completed.status == "ok", completed.error
    attempts = runs.attempts(enso_home, completed.run_id)
    assert [a.postrun_exit_code for a in attempts] == [10, 0]
    launches = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(launches) == 2 and "--resume" in launches[1]["args"]
    assert all(row["cwd"] == str(enso_home.workspace("default")) for row in launches)
    assert transport.sent == [] and captures.query(enso_home, "default") == before
    note = memory.scan(enso_home, "default").notes[0]
    assert note.metadata["sources"] == value["sources"] and "No launch date" in note.body
    found = CliRunner().invoke(
        app, ["memory", "search", "launch testing", "--workspace", "default", "--json"]
    )
    assert found.exit_code == 0 and json.loads(found.output)["notes"][0]["id"] == note.id
    quiet = await runner.run(job, trigger="schedule")
    assert quiet.status == "no_work"
    assert len(log.read_text().splitlines()) == 2
    assert len(memory.scan(enso_home, "default").notes) == 1
    assert captures.query(enso_home, "default") == before


async def test_followups_cannot_expand_the_original_batch(enso_home, fake_config, monkeypatch):
    from enso.jobs.runner import JobRunner

    workspaces.seed_jobs(enso_home, fake_config.defaults)
    job = load_job(enso_home, fake_config, "memory")
    original = conversation(enso_home)
    call = execution.execute_turn
    attempts = 0

    async def answer(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        turn = await call(*args, **kwargs)
        if attempts == 1:
            earlier = captures.query(enso_home, "default")[0]
            captures.record(
                enso_home,
                captures.Message(
                    "slack",
                    "default",
                    earlier.conversation,
                    "C1",
                    "1",
                    "later",
                    "U1",
                    "Gavin",
                    "2026-09-16T12:02:00Z",
                    "A later message",
                    kind="ambient",
                ),
            )
            expanded = harvesting.batch(enso_home, "default")
            value = {
                "batch": expanded.id,
                "sources": list(expanded.sources),
                "notes": [],
                "no_memory": list(expanded.sources),
            }
        else:
            value = {**original, "notes": [], "no_memory": original["sources"]}
        return replace(turn, output=json.dumps(value))

    monkeypatch.setattr(execution, "execute_turn", answer)
    completed = await JobRunner(fake_config, {}).run(job, trigger="manual")
    assert completed.status == "ok", completed.error
    assert (
        attempts == 2
        and "selected batch" in runs.attempts(enso_home, completed.run_id)[0].postrun_output
    )
    remaining = harvesting.batch(enso_home, "default")
    assert len(remaining.sources) == 1 and remaining.captures[0].text == "A later message"
    assert not memory.scan(enso_home, "default").notes


def test_apply_installs_each_workspace_job_with_its_agent_and_preserves_customization(
    enso_home, raw_config
):
    workspaces.create_workspace(enso_home, "team")
    write_workspace(
        enso_home, "default", {"agent": {"provider": "claude", "model": "sonnet", "effort": "low"}}
    )
    write_workspace(
        enso_home, "team", {"agent": {"provider": "claude", "model": "sonnet", "effort": "high"}}
    )
    applied = initialization.apply_config(enso_home, raw_config)
    assert applied["ok"], applied
    config = load_config(enso_home)
    default = load_job(enso_home, config, "memory")
    assert default.model == "sonnet" and default.effort == "low"
    assert load_job(enso_home, config, "enso-audit").model == config.defaults.model
    team = load_job(enso_home, config, "team:memory")
    assert (team.provider, team.model, team.effort) == ("claude", "sonnet", "high")
    assert default.schedule == team.schedule == "*/15 * * * *"
    assert team.enabled and team.prerun == "prerun.sh" and team.postrun == "postrun.sh"
    assert sorted(p.name for p in enso_home.workspace_jobs("team").iterdir()) == ["memory"]
    target = team.job_dir / "JOB.md"
    edited = (
        target.read_text()
        .replace("enabled: true", "enabled: false")
        .replace("*/15 * * * *", "0 9 * * *")
    )
    target.write_text(edited)
    (team.job_dir / "postrun.sh").unlink()
    write_workspace(
        enso_home, "team", {"agent": {"provider": "claude", "model": "opus", "effort": "low"}}
    )
    assert initialization.apply_config(enso_home, raw_config)["ok"]
    workspaces.reconcile_bundles(enso_home, Agent("claude", "opus", "low"))
    assert target.read_text() == edited and not (team.job_dir / "postrun.sh").exists()


def test_new_workspace_and_bundle_refresh_install_memory_in_their_own_locations(
    enso_home, raw_config
):
    raw_config["bindings"]["slack:C2"] = "team"
    write_config(enso_home, raw_config)
    created = CliRunner().invoke(app, ["workspace", "create", "team"])
    assert created.exit_code == 0, created.output
    config = load_config(enso_home)
    assert load_job(enso_home, config, "team:memory").model == config.defaults.model
    workspaces.create_workspace(enso_home, "research")
    workspaces.reconcile_bundles(
        enso_home, config.defaults, workspace_agents={"research": Agent("claude", "sonnet", "high")}
    )
    config = load_config(enso_home)
    assert load_job(enso_home, config, "research:memory").model == "sonnet"
    skill = enso_home.skills / "enso-memory/SKILL.md"
    customized = skill.read_text() + "\nPrefer a brief narrative.\n"
    skill.write_text(customized)
    workspaces.reconcile_bundles(enso_home, config.defaults)
    assert skill.read_text() == customized


def test_conflicting_memory_job_is_reported_and_preserved(enso_home, config):
    original = write_job(enso_home, "memory", prompt="Our custom job")
    before = original.read_bytes()
    changed = workspaces.seed_jobs(enso_home, config.defaults)
    assert any("conflict:" in line for line in changed)
    changed = workspaces.reconcile_bundles(enso_home, config.defaults)
    assert any("conflict:" in line for line in changed)
    assert original.read_bytes() == before
    assert sorted(p.name for p in original.parent.iterdir()) == ["JOB.md"]


def test_hooks_require_current_run_and_reconcile_completed_retry(enso_home, config, monkeypatch):
    db.initialize(enso_home)
    workspaces.seed_jobs(enso_home, config.defaults)
    job = load_job(enso_home, config, "memory")
    runner = CliRunner()
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
    assert runner.invoke(app, ["memory", "job-hook", "prerun"]).exit_code == 2
    run_id = runs.start(enso_home, job, "manual", effort=job.effort)
    monkeypatch.setenv("ENSO_RUN_ID", run_id)
    monkeypatch.setenv("ENSO_RUN_STATUS", "ok")
    value = conversation(enso_home)
    prepared = runner.invoke(app, ["memory", "job-hook", "prerun"])
    assert prepared.exit_code == 0
    complete = captures.complete_receipt

    def interrupted(*args):
        raise OSError("interrupted after file creation")

    monkeypatch.setattr(captures, "complete_receipt", interrupted)
    failed = runner.invoke(app, ["memory", "job-hook", "postrun"], input=json.dumps(value))
    assert failed.exit_code == 2
    monkeypatch.setattr(captures, "complete_receipt", complete)
    retry = runner.invoke(app, ["memory", "job-hook", "postrun"], input=json.dumps(value))
    assert retry.exit_code == 0, retry.output
    assert len(memory.scan(enso_home, "default").notes) == 1
    assert captures.handled(enso_home, "default", tuple(value["sources"]))
