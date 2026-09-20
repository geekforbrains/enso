"""JOB.md frontmatter parsing, validation against config, loading, and scaffolding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import write_config, write_job
from typer.testing import CliRunner

from enso.cli import app
from enso.config import Config, Paths
from enso.jobs import (
    FIELDS,
    PLACEHOLDER_PROMPT,
    REQUIRED,
    create_job,
    find_job,
    load_jobs,
    parse_job,
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
        ({}, ["effort"], "x", ["effort is required"]),
        ({}, list(FIELDS), "x", [f"{key} is required" for key in REQUIRED]),
        ({"effort": ""}, [], "x", ["effort must be non-empty text"]),
        ({"name": 4.5}, [], "x", ["name must be non-empty text"]),
        ({"enabled": "yes"}, [], "x", ["enabled must be true or false"]),
        ({"timeout": 0}, [], "x", ["timeout must be a positive integer"]),
        # A quoted number stays the string it was written as; nothing coerces it back.
        ({"timeout": "60"}, [], "x", ["timeout must be a positive integer"]),
        ({"max_followups": -1}, [], "x", ["max_followups must be a nonnegative integer"]),
        # A bool is not an integer, however much Python would like it to be.
        ({"max_followups": True}, [], "x", ["max_followups must be a nonnegative integer"]),
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
    path = write_job(enso_home, prompt=prompt, omit=omit, **fields)
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
    assert (job.prerun, job.catch_up, job.notify, job.timeout) == ("prerun.sh", True, "C9", 60)
    assert (job.prerun_timeout, job.misfire_grace_seconds, job.enabled) == (45, 60, True)
    assert (job.postrun, job.postrun_timeout) == ("postrun.sh", 30)
    assert job.max_followups == 4
    assert job.concurrency_group == "dev-EN"

    bare = parse_job(write_job(enso_home))[0]
    assert bare is not None and (bare.prerun_timeout, bare.postrun_timeout) == (120, 120)
    assert (bare.timeout, bare.misfire_grace_seconds) == (900, 300)
    assert (bare.prerun, bare.postrun, bare.notify, bare.catch_up) == (None, None, None, False)
    assert bare.concurrency_group is None
    assert bare.max_followups == 2
    assert (
        bare.prompt == "Say hi." and bare.job_dir == enso_home.workspace_jobs("default") / "nightly"
    )


def test_zero_followups_allows_validation_without_extra_turns(enso_home: Paths) -> None:
    job, problems = parse_job(write_job(enso_home, max_followups=0))
    assert problems == [] and job is not None and job.max_followups == 0


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
    assert problems[3] == "JOB.md.model 'gpt' is not in providers.claude.models"
    assert problems[4] == "notify: transport telegram is not configured"


def test_agent_problems_are_reported_field_by_field(enso_home: Paths, config: Config) -> None:
    """Provider, model, and effort answer for themselves, so one never hides another."""
    both = write_job(enso_home, model="gpt", effort="ultra")
    assert parse_job(both, config)[1] == [
        "JOB.md.model 'gpt' is not in providers.claude.models",
        "JOB.md.effort must be one of low, medium, high, xhigh, max",
    ]

    # A missing effort is its own problem and leaves the model to be judged as usual.
    without_effort = write_job(enso_home, model="gpt", omit=["effort"])
    assert parse_job(without_effort, config)[1] == [
        "effort is required",
        "JOB.md.model 'gpt' is not in providers.claude.models",
    ]

    # The provider names the lists a model and an effort are judged against, so an
    # unconfigured one leaves nothing to judge them by.
    unconfigured = write_job(enso_home, provider="gemini", model="gpt", effort="ultra")
    assert parse_job(unconfigured, config)[1] == ["JOB.md.provider 'gemini' is not configured"]


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
        "provider": "claude",
        "model": "4.5",
        "effort": "high",
        "workspace": "default",
        "enabled": True,
        "notify": "12345",
        "timeout": 60,
        "catch_up": False,
    }

    path = write_job(enso_home, **awkward)
    job, problems = parse_job(path)

    assert job is not None and problems == []
    assert {key: getattr(job, key) for key in awkward} == awkward


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
