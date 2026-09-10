"""The ``projects`` config section, the flow presets, and ``enso project``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import write_config
from typer.testing import CliRunner

from enso import tasks
from enso.cli import app
from enso.config import Paths, ProjectConfig, Stage, load_config, parse_config, parse_stages


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=30)
    return path


def test_projects_parse_with_helpers(
    enso_home: Paths, raw_config_projects: dict, tmp_path: Path
) -> None:
    repo = git_repo(tmp_path / "repo")
    raw_config_projects["projects"]["EN"] |= {
        "repo": str(repo),
        "setup": ".dev/prepare",
        "copy": [".env"],
    }
    config, problems, _ = parse_config(raw_config_projects, enso_home)
    assert config is not None and not problems
    dd, mkt = config.projects["EN"], config.projects["MKT"]
    assert dd == ProjectConfig(
        "EN",
        "Enso",
        "default",
        repo,
        (Stage("triage"), Stage("todo"), Stage("review")),
        ".dev/prepare",
        (".env",),
    )
    assert mkt.stages == (Stage("draft"), Stage("approve", True), Stage("release"))
    assert (mkt.agent_stages, mkt.human_stages) == (("draft", "release"), ("approve",))
    assert (dd.next_stage("triage"), dd.next_stage("review")) == ("todo", "done")
    assert (dd.previous_stage("triage"), dd.previous_stage("review")) == (None, "todo")
    assert dd.index("todo") == 1 and dd.stage("todo") == Stage("todo") and dd.stage("x") is None
    with pytest.raises(ValueError):
        dd.index("blocked")
    assert mkt.as_dict()["stages"][1] == {"name": "approve", "human": True}
    assert dd.as_dict()["repo"] == str(repo) and mkt.as_dict()["repo"] is None
    bare, _, _ = parse_config({**raw_config_projects, "projects": None}, enso_home)
    assert bare is not None and bare.projects == {}


def test_project_problems_are_reported_together(
    enso_home: Paths, raw_config_projects: dict, tmp_path: Path
) -> None:
    raw_config_projects["projects"] = {
        "dd": {"name": "x", "workspace": "default", "stages": ["a"]},
        "A": {"name": "x", "workspace": "default", "stages": ["work"]},
        "LIST": ["work"],
        "P1": {
            "name": "",
            "workspace": "missing",
            "repo": str(tmp_path / "not-a-repo"),
            "stages": ["Work", "x", "done", "todo", "todo", "later:soon", "approve:human"],
            "setup": 3,
            "copy": ["../secrets", ".env"],
            "colour": "red",
        },
        "P2": {"name": "x", "workspace": "Bad Name", "stages": []},
        "P3": {"name": "x", "workspace": "default", "stages": ["approve:human"], "repo": ""},
    }
    config, problems, _ = parse_config(raw_config_projects, enso_home)
    assert config is None
    assert problems == [
        "projects.dd: keys are 2-10 uppercase letters or digits, starting with a letter",
        "projects.A: keys are 2-10 uppercase letters or digits, starting with a letter",
        "projects.LIST must be an object",
        "projects.P1.colour is not a recognized key",
        "projects.P1.name must be non-empty text",
        f"projects.P1.workspace: directory {enso_home.workspace('missing')} missing",
        f"projects.P1.repo {tmp_path / 'not-a-repo'} is not a directory holding a Git repository",
        "projects.P1.stages: 'Work' is not a stage name (lowercase, digits, hyphens, 2-24 chars)",
        "projects.P1.stages: 'x' is not a stage name (lowercase, digits, hyphens, 2-24 chars)",
        "projects.P1.stages: done is a built-in stage",
        "projects.P1.stages: todo is listed twice",
        "projects.P1.stages: 'later:soon' is not a stage; use NAME or NAME:human",
        "projects.P1.setup must be a command string",
        "projects.P1.copy must be a list of relative paths inside the repository",
        "projects.P2.workspace must be a workspace name (lowercase kebab-case)",
        "projects.P2.stages must be a non-empty list of stage names",
        "projects.P3.repo must be a path",
        "projects.P3.stages needs at least one agent stage",
    ]
    raw_config_projects["projects"] = "EN"
    _, problems, _ = parse_config(raw_config_projects, enso_home)
    assert problems == ["projects must be an object"]


@pytest.mark.parametrize("flow", list(tasks.FLOWS))
def test_flow_presets_are_valid_stage_lists(flow: str) -> None:
    problems: list[str] = []
    stages = parse_stages(list(tasks.FLOWS[flow]), "flow", problems)
    assert not problems and stages and any(not stage.human for stage in stages)


def test_project_add_writes_config_atomically(
    enso_home: Paths, raw_config: dict, tmp_path: Path
) -> None:
    write_config(enso_home, raw_config)
    repo = git_repo(tmp_path / "repo")
    runner = CliRunner()
    both = runner.invoke(
        app,
        [
            "project",
            "add",
            "EN",
            "--name",
            "Enso",
            "--workspace",
            "default",
            "--flow",
            "dev",
            "--stages",
            "a",
        ],
    )
    assert both.exit_code == 1 and "give --stages or --flow" in both.stderr
    neither = runner.invoke(
        app, ["project", "add", "EN", "--name", "Enso", "--workspace", "default"]
    )
    assert neither.exit_code == 1
    bad_flow = runner.invoke(
        app,
        [
            "project",
            "add",
            "EN",
            "--name",
            "Enso",
            "--workspace",
            "default",
            "--flow",
            "agile",
            "--json",
        ],
    )
    assert bad_flow.exit_code == 1 and json.loads(bad_flow.stdout)["ok"] is False
    bad_stages = runner.invoke(
        app,
        [
            "project",
            "add",
            "EN",
            "--name",
            "Enso",
            "--workspace",
            "default",
            "--stages",
            "todo,done,x",
        ],
    )
    assert bad_stages.exit_code == 1 and bad_stages.stderr.count("error:") == 2
    assert not enso_home.config.with_suffix(".json.tmp").exists()
    assert "projects" not in json.loads(enso_home.config.read_text())

    added = runner.invoke(
        app,
        [
            "project",
            "add",
            "EN",
            "--name",
            "Enso",
            "--workspace",
            "default",
            "--repo",
            str(repo),
            "--flow",
            "dev",
            "--setup",
            ".dev/prepare",
            "--copy",
            ".env",
            "--copy",
            ".envrc",
            "--json",
        ],
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.stdout) == {
        "key": "EN",
        "name": "Enso",
        "workspace": "default",
        "repo": str(repo),
        "stages": [
            {"name": "triage", "human": False},
            {"name": "todo", "human": False},
            {"name": "review", "human": False},
        ],
        "setup": ".dev/prepare",
        "copy": [".env", ".envrc"],
    }
    written = json.loads(enso_home.config.read_text())
    assert written["projects"] == {
        "EN": {
            "name": "Enso",
            "workspace": "default",
            "stages": ["triage", "todo", "review"],
            "repo": str(repo),
            "setup": ".dev/prepare",
            "copy": [".env", ".envrc"],
        }
    }
    assert written["transports"] == raw_config["transports"]  # the rest is untouched
    assert oct(enso_home.config.stat().st_mode & 0o777) == "0o600"

    again = runner.invoke(
        app, ["project", "add", "EN", "--name", "Enso", "--workspace", "default", "--flow", "dev"]
    )
    assert again.exit_code == 1 and "project EN already exists" in again.stderr
    custom = runner.invoke(
        app,
        [
            "project",
            "add",
            "MKT",
            "--name",
            "Marketing",
            "--workspace",
            "default",
            "--stages",
            "draft, approve:human ,release",
        ],
    )
    assert (
        custom.exit_code == 0
        and custom.stdout == f"added project MKT (draft, approve, release) to {enso_home.config}\n"
    )
    missing_ws = runner.invoke(
        app, ["project", "add", "OPS", "--name", "Ops", "--workspace", "nope", "--flow", "basic"]
    )
    assert missing_ws.exit_code == 1 and "projects.OPS.workspace: directory" in missing_ws.stderr
    assert list(load_config(enso_home).projects) == ["EN", "MKT"]

    listed = runner.invoke(app, ["project", "list"])
    assert listed.exit_code == 0 and listed.stdout.splitlines() == [
        "KEY  NAME       WORKSPACE  REPO" + " " * (len(str(repo)) - 2) + "STAGES",
        f"EN   Enso       default    {repo}  triage, todo, review",
        "MKT  Marketing  default    -"
        + " " * (len(str(repo)) + 1)
        + "draft, approve:human, release",
    ]
    as_json = runner.invoke(app, ["project", "list", "--json"])
    assert [p["key"] for p in json.loads(as_json.stdout)] == ["EN", "MKT"]


def test_project_add_stores_a_relative_repo_absolutely(
    enso_home: Paths, raw_config: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``--repo`` is resolved once; config.json must load from any directory."""
    write_config(enso_home, raw_config)
    repo = git_repo(tmp_path / "relrepo")
    monkeypatch.chdir(tmp_path)
    added = CliRunner().invoke(
        app,
        [
            "project",
            "add",
            "RL",
            "--name",
            "Rel",
            "--workspace",
            "default",
            "--repo",
            "relrepo",
            "--flow",
            "basic",
        ],
    )
    assert added.exit_code == 0, added.output
    assert json.loads(enso_home.config.read_text())["projects"]["RL"]["repo"] == str(repo)
    monkeypatch.chdir(tmp_path / "enso")
    assert load_config(enso_home).projects["RL"].repo == repo
    listed = CliRunner().invoke(app, ["project", "list", "--json"])
    assert listed.exit_code == 0 and json.loads(listed.stdout)[0]["repo"] == str(repo)


def test_project_list_without_projects(enso_home: Paths, raw_config: dict) -> None:
    write_config(enso_home, raw_config)
    result = CliRunner().invoke(app, ["project", "list"])
    assert result.exit_code == 0 and result.stdout == "no projects yet; run `enso project add`\n"
