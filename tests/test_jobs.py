"""JOB.md frontmatter parsing, validation against config, loading, and scaffolding."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from conftest import JOB_FIELDS, write_config, write_job
from typer.testing import CliRunner

from enso.cli import app
from enso.config import Config, Paths
from enso.jobs import (
    FIELDS,
    PLACEHOLDER_PROMPT,
    REQUIRED,
    JobAgent,
    JobConcurrency,
    JobHook,
    create_job,
    execution_kind,
    find_job,
    load_jobs,
    parse_job,
    render,
    schedule_problem,
    validate,
)

FIVE_FIELDS = "must be exactly five fields, minute hour day-of-month month day-of-week"


def write_raw(enso_home: Paths, front: str, dir_name: str = "nightly") -> Path:
    """A JOB.md whose frontmatter is written verbatim, however malformed."""
    path = enso_home.workspace_jobs("default") / dir_name / "JOB.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n\nSay hi.\n")
    return path


@pytest.mark.parametrize(
    ("front", "problem"),
    [
        # A colon inside an unquoted value is the mistake this format invites most.
        ("name: Daily: Review", "JOB.md frontmatter is not valid YAML at line 2, column 12"),
        ("name: [unclosed", "JOB.md frontmatter is not valid YAML at line 2, column 16"),
        ("\tname: Nightly", "JOB.md frontmatter is not valid YAML at line 2, column 1"),
        ("- name\n- schedule", "JOB.md frontmatter must be a block of key: value fields"),
        ("just a scalar", "JOB.md frontmatter must be a block of key: value fields"),
        ("", "JOB.md frontmatter must be a block of key: value fields"),
        ("1: Nightly", "JOB.md frontmatter keys must be text at line 2, column 1"),
        ("[a]: Nightly", "JOB.md frontmatter keys must be text at line 2, column 1"),
        # The last value silently winning is precisely what a strict format must not do.
        (
            "name: One\nschedule: 0 9 * * *\nname: Two",
            "JOB.md frontmatter answers 'name' twice at line 4, column 1",
        ),
    ],
)
def test_malformed_frontmatter_is_never_partly_recovered(
    enso_home: Paths, front: str, problem: str
) -> None:
    """Nothing is read out of a block with no single meaning: one problem, and no job.

    The whole message is Enso's own. A syntax fault is reported as its location and
    nothing else, so PyYAML's wording can change without changing what a user reads.
    """
    job, problems = parse_job(write_raw(enso_home, front))
    assert job is None and problems == [problem]


@pytest.mark.parametrize(
    "front",
    [
        "name: {secret}: leaked\nnotify: {secret}",  # a colon PyYAML points at
        "name: x\nschedule: [0, 9,\nnotify: {secret}",  # an unclosed flow sequence
    ],
)
def test_frontmatter_problems_quote_no_document_content(enso_home: Paths, front: str) -> None:
    """A diagnostic carries a location, not the file: prompts and values are untrusted.

    PyYAML interpolates what it just read into several of its own messages, so none of its
    text is repeated; a secret written anywhere in the block cannot come back out.
    """
    secret = "sk-live-must-never-be-echoed"
    job, problems = parse_job(write_raw(enso_home, front.format(secret=secret)))
    assert job is None and secret not in " ".join(problems)


def test_frontmatter_problems_stay_one_printable_line(enso_home: Paths) -> None:
    """A key is named, but escaped and bounded, so it cannot forge a line of output."""
    forged = 'name: "x"\n"a\\nenabled: true": 1'  # a newline smuggled into a key
    job, problems = parse_job(write_raw(enso_home, forged))
    assert job is None and "\n" not in "".join(problems)


@pytest.mark.parametrize(
    ("fields", "omit", "prompt", "problems"),
    [
        ({"agent": {"provider": "claude", "model": "opus"}}, [], "x", ["agent.effort is required"]),
        (
            {},
            list(FIELDS),
            "x",
            [*[f"{key} is required" for key in REQUIRED], "give exactly one of agent or command"],
        ),
        (
            {"agent": {"provider": "claude", "model": "opus", "effort": ""}},
            [],
            "x",
            ["agent.effort must be non-empty text"],
        ),
        ({"name": 4.5}, [], "x", ["name must be non-empty text"]),
        ({"enabled": "yes"}, [], "x", ["enabled must be true or false"]),
        ({"timeout": 0}, [], "x", ["timeout must be a positive integer"]),
        # A quoted number stays the string it was written as; nothing coerces it back.
        ({"timeout": "60"}, [], "x", ["timeout must be a positive integer"]),
        (
            {
                "agent": {
                    "provider": "claude",
                    "model": "opus",
                    "effort": "high",
                    "max_followups": -1,
                }
            },
            [],
            "x",
            ["agent.max_followups must be a nonnegative integer"],
        ),
        # A bool is not an integer, however much Python would like it to be.
        (
            {
                "agent": {
                    "provider": "claude",
                    "model": "opus",
                    "effort": "high",
                    "max_followups": True,
                }
            },
            [],
            "x",
            ["agent.max_followups must be a nonnegative integer"],
        ),
        ({"retries": 3}, [], "x", ["'retries' is not a recognized field"]),
        ({}, [], "", ["the prompt body is empty"]),
        # Independent problems are collected together, in the order docs/jobs.md lists them.
        (
            {"extra": 1, "timeout": 0, "enabled": "yes"},
            ["name", "workspace"],
            "",
            [
                "'extra' is not a recognized field",
                "name is required",
                "enabled must be true or false",
                "timeout must be a positive integer",
                "the prompt body is empty",
            ],
        ),
    ],
)
def test_parse_job_reports_problems(
    enso_home: Paths, fields: dict, omit: list[str], prompt: str, problems: list[str]
) -> None:
    path = write_job(enso_home)
    given = {**copy.deepcopy(JOB_FIELDS), **fields}
    for key in omit:
        given.pop(key, None)
    path.write_text(render(given, prompt))
    job, found = parse_job(path)
    assert job is None and found == problems


def test_parse_job_without_frontmatter(enso_home: Paths) -> None:
    path = write_job(enso_home)
    path.write_text("Just a prompt.\n")
    assert parse_job(path) == (None, ["JOB.md needs a leading --- frontmatter block"])


def test_parse_job_defaults_and_options(enso_home: Paths) -> None:
    path = write_job(
        enso_home,
        prerun="prerun.sh",
        postrun="postrun.sh",
        prerun_timeout=45,
        postrun_timeout=30,
        max_followups=4,
        misfire_grace_seconds=60,
        catch_up=True,
        concurrency_group="dev-EN",
        notify="C9",
        timeout=60,
    )
    job, problems = parse_job(path)
    assert job is not None and problems == []  # every optional field, so the schema is covered
    assert (job.gate, job.catch_up, job.notify, job.timeout) == (
        JobHook("bash prerun.sh", 45),
        True,
        "C9",
        60,
    )
    assert (job.misfire_grace_seconds, job.enabled) == (60, True)
    assert job.postrun == JobHook("bash postrun.sh", 30)
    assert job.agent == JobAgent("claude", "opus", "high", 4)
    assert job.concurrency == JobConcurrency("dev-EN", "skip")

    bare = parse_job(write_job(enso_home))[0]
    assert bare is not None
    assert (bare.timeout, bare.misfire_grace_seconds) == (900, 300)
    assert (bare.gate, bare.postrun, bare.notify, bare.catch_up) == (None, None, None, False)
    assert bare.concurrency is None
    assert bare.agent == JobAgent("claude", "opus", "high", 2)
    assert (
        bare.prompt == "Say hi." and bare.job_dir == enso_home.workspace_jobs("default") / "nightly"
    )


def test_zero_followups_allows_validation_without_extra_turns(enso_home: Paths) -> None:
    job, problems = parse_job(write_job(enso_home, max_followups=0))
    assert (
        problems == []
        and job is not None
        and job.agent is not None
        and job.agent.max_followups == 0
    )


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("notify", "telegram:1", "notify: transport telegram is not configured"),
        (
            "notify",
            "slack:not-a-channel",
            "notify: 'not-a-channel' is not a Slack conversation id",
        ),
        ("notify", "C1", None),
    ],
)
def test_validate_against_config(
    enso_home: Paths, config: Config, field: str, value: str, problem: str | None
) -> None:
    job, _ = parse_job(write_job(enso_home, **{field: value}))
    assert job is not None
    problems = validate(job, config)
    assert problems == [] if problem is None else problems[0].startswith(problem)


def test_schema_and_config_problems_are_reported_together(enso_home: Paths, config: Config) -> None:
    """One pass names everything wrong, so a broken field never hides an unrunnable one."""
    path = write_job(
        enso_home,
        retries=3,
        timeout="60",
        schedule="* * * * * *",
        model="gpt",
        notify="telegram:1",
    )
    job, problems = parse_job(path, config)

    assert job is None and len(problems) == 5
    assert problems[0] == "'retries' is not a recognized field"
    assert problems[1] == "timeout must be a positive integer"
    assert problems[2].startswith(f"schedule '* * * * * *' {FIVE_FIELDS}")
    assert problems[3] == "JOB.md.agent.model 'gpt' is not in providers.claude.models"
    assert problems[4] == "notify: transport telegram is not configured"


def test_agent_problems_are_reported_field_by_field(enso_home: Paths, config: Config) -> None:
    """Provider, model, and effort answer for themselves, so one never hides another."""
    both = write_job(enso_home, model="gpt", effort="ultra")
    assert parse_job(both, config)[1] == [
        "JOB.md.agent.model 'gpt' is not in providers.claude.models",
        "JOB.md.agent.effort must be one of low, medium, high, xhigh, max",
    ]

    # A missing effort is its own problem and leaves the model to be judged as usual.
    without_effort = write_job(enso_home, model="gpt", omit=["effort"])
    assert parse_job(without_effort, config)[1] == [
        "agent.effort is required",
        "JOB.md.agent.model 'gpt' is not in providers.claude.models",
    ]

    # The provider names the lists a model and an effort are judged against, so an
    # unconfigured one leaves nothing to judge them by.
    unconfigured = write_job(enso_home, provider="gemini", model="gpt", effort="ultra")
    assert parse_job(unconfigured, config)[1] == [
        "JOB.md.agent.provider 'gemini' is not configured"
    ]


@pytest.mark.parametrize(
    ("schedule", "problem"),
    [
        ("*/15 * * * *", None),
        ("30 6 * * 1-5", None),
        (" 0  9 * * 1 ", None),  # padding is not a sixth field
        ("* * * * * *", FIVE_FIELDS),  # croniter would read the sixth column as seconds
        ("@daily", FIVE_FIELDS),  # an alias hides which minute it means
        ("0 9 * *", FIVE_FIELDS),
        ("0 99 * * *", "is not a cron expression"),
    ],
)
def test_schedule_is_five_field_cron(schedule: str, problem: str | None) -> None:
    """Enso checks jobs once a minute, so the accepted form is exactly the five fields."""
    found = schedule_problem(schedule)
    assert found is None if problem is None else found is not None and problem in found


def test_create_and_load_round_trip(enso_home: Paths, config: Config) -> None:
    kwargs = dict(provider="claude", model="opus", effort="high", schedule="0 9 * * *")
    job = create_job(enso_home, config, name="Daily: Review", workspace="default", **kwargs)
    assert job.dir_name == "daily-review" and job.enabled is False
    assert job.prompt == PLACEHOLDER_PROMPT
    assert load_jobs(enso_home, config) == ([job], {})
    with pytest.raises(FileExistsError):
        create_job(enso_home, config, name="Daily Review", workspace="default", **kwargs)
    with pytest.raises(ValueError, match="model 'gpt'"):
        create_job(
            enso_home, config, name="Other", workspace="default", **{**kwargs, "model": "gpt"}
        )
    assert not (enso_home.workspace_jobs("default") / "other").exists()

    write_job(enso_home, "broken", model="gpt")
    write_job(enso_home, "unparsed", enabled="maybe")
    jobs, problems = load_jobs(enso_home, config)
    assert [j.dir_name for j in jobs] == ["broken", "daily-review"]
    assert set(problems) == {"default:broken", "default:unparsed"}
    assert find_job(enso_home, config, "default:broken")[1] == problems["default:broken"]
    assert find_job(enso_home, config, "default:nope") == (None, ["no job named default:nope"])


def test_generated_jobs_round_trip_through_the_strict_parser(enso_home: Paths) -> None:
    """Whatever ``render`` writes must parse back unchanged, quoting whatever YAML needs.

    ``create_job`` and every test fixture go through it, so a value that reads as a cron
    alias, a number, or a second mapping has to come back as the text it went in as.
    """
    awkward = {
        "name": "Daily: Review",
        "schedule": "*/15 * * * *",
        "agent": {"provider": "claude", "model": "4.5", "effort": "high"},
        "workspace": "default",
        "enabled": True,
        "notify": "12345",
        "timeout": 60,
        "catch_up": False,
    }

    path = write_job(enso_home, **awkward)
    job, problems = parse_job(path)

    assert job is not None and problems == []
    assert {key: job.as_dict()[key] for key in awkward} == {
        **awkward,
        "agent": {**awkward["agent"], "max_followups": 2},
    }


# -- Stage jobs ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "omit", "problems"),
    [
        ({"project": "EN"}, [], ["project and stage go together; give both or neither"]),
        ({}, ["schedule"], ["schedule is required"]),
        ({"project": "EN", "stage": "triage"}, ["schedule"], []),
        ({"project": "ZZ", "stage": "triage"}, [], ["JOB.md.project 'ZZ' is not configured"]),
        (
            {"project": "MKT", "stage": "approve"},
            [],
            ["approve is a human stage; a job cannot serve it"],
        ),
        (
            {"project": "EN", "stage": "nope"},
            [],
            ["JOB.md.stage 'nope' is not a stage of EN; use one of triage, todo, review"],
        ),
        # A schedule given beside a stage is still a schedule, and still judged as one.
        ({"project": "EN", "stage": "todo", "schedule": "@daily"}, [], ["schedule '@daily' must"]),
    ],
)
def test_stage_job_validation(
    enso_home: Paths,
    project_config: Config,
    fields: dict,
    omit: list[str],
    problems: list[str],
) -> None:
    _, found = parse_job(write_job(enso_home, omit=omit, **fields), project_config)
    assert len(found) == len(problems)
    assert all(got.startswith(want) for got, want in zip(found, problems, strict=True))


def test_create_job_for_a_stage_needs_no_schedule(enso_home: Paths, project_config: Config) -> None:
    kwargs = dict(provider="claude", model="opus", effort="high", workspace="default")
    job = create_job(
        enso_home,
        project_config,
        name="Dev Todo",
        schedule=None,
        project="EN",
        stage="todo",
        **kwargs,
    )
    assert (job.schedule, job.project, job.stage) == (None, "EN", "todo")
    loaded, problems = find_job(enso_home, project_config, "default:dev-todo")
    assert loaded == job and problems == []
    with pytest.raises(ValueError, match="--schedule is required unless"):
        create_job(enso_home, project_config, name="Plain", schedule=None, **kwargs)
    with pytest.raises(ValueError, match="go together"):
        create_job(enso_home, project_config, name="Half", schedule=None, project="EN", **kwargs)
    with pytest.raises(ValueError, match="human stage"):
        create_job(
            enso_home,
            project_config,
            name="Approve",
            schedule=None,
            project="MKT",
            stage="approve",
            **kwargs,
        )
    assert not (enso_home.workspace_jobs("default") / "approve").exists()


def test_workspace_job_creation_and_explicit_references(enso_home, config, raw_config, monkeypatch):
    write_config(enso_home, raw_config)
    enso_home.workspace("team").mkdir()
    monkeypatch.setenv("ENSO_WORKSPACE", "team")
    cli = CliRunner()
    arguments = [
        "job",
        "create",
        "--name",
        "Digest",
        "--provider",
        "claude",
        "--model",
        "opus",
        "--effort",
        "high",
        "--schedule",
        "0 9 * * *",
        "--json",
    ]
    created = cli.invoke(app, arguments)
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout)["ref"] == "team:digest"
    created = cli.invoke(app, [*arguments, "--workspace", "default"])
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout)["ref"] == "default:digest"
    assert "workspace:" not in enso_home.job("team:digest").read_text()
    assert not (enso_home.home / "jobs").exists()
    shown = cli.invoke(app, ["job", "show", "default:digest", "--json"])
    assert json.loads(shown.stdout)["workspace"] == "default"
    assert cli.invoke(app, ["job", "show", "digest"]).exit_code == 1
    monkeypatch.delenv("ENSO_WORKSPACE")
    assert cli.invoke(app, arguments).exit_code == 1
    found, faults = load_jobs(enso_home, config)
    assert not faults and {job.ref for job in found} == {"default:digest", "team:digest"}


@pytest.mark.parametrize(
    "reference", ["digest", "../default:digest", "default:../digest", "default:x:y"]
)
def test_job_references_cannot_escape_the_workspace(enso_home, config, reference):
    job, problems = find_job(enso_home, config, reference)
    assert job is None and "<workspace>:<job>" in problems[0]


def test_job_frontmatter_cannot_reassign_ownership(enso_home, config):
    path = write_job(enso_home)
    path.write_text(path.read_text().replace("enabled: true", "workspace: team\nenabled: true"))
    job, problems = parse_job(path, config)
    assert job is None and problems == ["'workspace' is not a recognized field"]


def test_job_creation_and_reading_refuse_linked_directories(enso_home, config, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    enso_home.workspace_jobs("default").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        create_job(
            enso_home,
            config,
            name="Digest",
            workspace="default",
            provider="claude",
            model="opus",
            effort="high",
            schedule="0 9 * * *",
        )
    assert not list(outside.iterdir())
    (outside / "digest").mkdir()
    (outside / "digest/JOB.md").write_text("must not be read")
    assert "symbolic links" in find_job(enso_home, config, "default:digest")[1][0]


@pytest.mark.parametrize("body", ["", "Refresh a cache."])
def test_command_jobs_need_no_agent_or_prompt(enso_home, config, body):
    path = write_job(enso_home, command="bash refresh.sh", prompt=body)
    job, problems = parse_job(path, config)
    assert job is not None and problems == []
    assert job.agent is None and job.command == "bash refresh.sh" and job.prompt == body
    assert execution_kind(job, config) == "command"
    assert "provider" not in job.as_dict() and "prerun" not in job.as_dict()


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ({"command": "true"}, "give exactly one of agent or command"),
        ({"provider": "claude"}, "'provider' is not a recognized field"),
        ({"prerun": "gate.sh"}, "'prerun' is not a recognized field"),
        ({"concurrency_group": "reporting"}, "'concurrency_group' is not a recognized field"),
        ({"max_followups": 3}, "'max_followups' is not a recognized field"),
        ({"gate": "gate.sh"}, "gate must be a block of key: value fields"),
        ({"gate": {}}, "gate.command is required"),
        (
            {"postrun": {"command": "true", "timeout": False}},
            "postrun.timeout must be a positive integer",
        ),
        (
            {"agent": {"provider": "claude", "model": "opus", "effort": "high", "typo": 1}},
            "'agent.typo' is not a recognized field",
        ),
        ({"concurrency": {"group": "reporting"}}, "concurrency.on_busy is required"),
        (
            {"concurrency": {"group": "reporting", "on_busy": "waiting"}},
            "concurrency.on_busy must be wait or skip",
        ),
        (
            {"concurrency": {"group": "reporting", "on_busy": "skip", "max_wait": 1}},
            "concurrency.max_wait is only allowed with on_busy: wait",
        ),
        (
            {"concurrency": {"group": "reporting", "on_busy": "wait", "max_wait": 0}},
            "concurrency.max_wait must be a positive integer",
        ),
        (
            {"concurrency": {"group": "reporting", "on_busy": "wait", "max_wait": True}},
            "concurrency.max_wait must be a positive integer",
        ),
    ],
)
def test_nested_schema_is_explicit_and_closed(enso_home, changed, expected):
    path = write_job(enso_home)
    path.write_text(render({**copy.deepcopy(JOB_FIELDS), **changed}, "Prompt."))
    job, problems = parse_job(path)
    assert job is None and problems == [expected]


@pytest.mark.parametrize("policy, maximum", [("wait", None), ("wait", 300), ("skip", None)])
def test_explicit_concurrency_round_trip(enso_home, config, policy, maximum):
    concurrency = JobConcurrency("reporting", policy, maximum)
    created = create_job(
        enso_home,
        config,
        name="Refresh",
        workspace="default",
        schedule="*/15 * * * *",
        command="bash refresh.sh",
        concurrency=concurrency,
    )
    found, problems = parse_job(created.path, config)
    assert found == created and problems == []
    assert found.concurrency == concurrency


def test_cli_creates_command_job_and_requires_explicit_group_policy(enso_home, raw_config):
    write_config(enso_home, raw_config)
    cli = CliRunner()
    arguments = [
        "job",
        "create",
        "--name",
        "Refresh",
        "--workspace",
        "default",
        "--schedule",
        "*/15 * * * *",
        "--command",
        "bash refresh.sh",
        "--json",
    ]
    missing_policy = cli.invoke(app, [*arguments, "--concurrency-group", "reporting"])
    assert missing_policy.exit_code == 1
    assert "required together" in missing_policy.output
    incompatible = cli.invoke(
        app,
        [*arguments, "--concurrency-group", "reporting", "--on-busy", "skip", "--max-wait", "30"],
    )
    assert incompatible.exit_code == 1
    assert "only allowed" in incompatible.output
    created = cli.invoke(app, [*arguments, "--concurrency-group", "reporting", "--on-busy", "wait"])
    assert created.exit_code == 0, created.output
    data = json.loads(created.stdout)
    assert data["agent"] is None and data["command"] == "bash refresh.sh"
    assert data["concurrency"] == {"group": "reporting", "on_busy": "wait", "max_wait": None}


@pytest.mark.parametrize("integrate", [False, True])
def test_project_owns_command_and_integration_stage_executors(enso_home, project_config, integrate):
    from dataclasses import replace

    from enso.config import Stage

    stage = Stage("execute", integrate=integrate, command=None if integrate else "bash deploy.sh")
    project = replace(project_config.projects["EN"], stages=(stage,))
    config = replace(project_config, projects={"EN": project})
    path = write_job(enso_home, project="EN", stage="execute", omit=["agent"], prompt="")
    job, problems = parse_job(path, config)
    assert job is not None and problems == []
    assert job.agent is None and job.command is None
    assert execution_kind(job, config) == ("integration" if integrate else "command")
    assert validate(job, config) == []

    path = write_job(enso_home, project="EN", stage="execute")
    job, problems = parse_job(path, config)
    assert job is None and problems == ["a command or integration stage cannot configure an agent"]
    path = write_job(enso_home, project="EN", stage="execute", command="bash override.sh")
    job, problems = parse_job(path, config)
    assert job is None and problems == [
        "stage commands belong in PROJECT.md; a job cannot override them"
    ]


def test_agent_stage_requires_an_agent(enso_home, project_config):
    path = write_job(enso_home, project="EN", stage="todo", omit=["agent"])
    job, problems = parse_job(path, project_config)
    assert job is None and problems == ["agent is required for an agent stage"]


def test_hook_defaults_and_nested_json(enso_home, config):
    path = write_job(
        enso_home, gate={"command": "bash collect.sh"}, postrun={"command": "bash check.sh"}
    )
    job, problems = parse_job(path, config)
    assert job is not None and problems == []
    assert job.gate == JobHook("bash collect.sh", 120)
    assert job.postrun == JobHook("bash check.sh", 120)
    assert job.as_dict()["agent"] == {
        "provider": "claude",
        "model": "opus",
        "effort": "high",
        "max_followups": 2,
    }
    assert job.as_dict()["gate"] == {"command": "bash collect.sh", "timeout": 120}
