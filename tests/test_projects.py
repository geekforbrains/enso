"""Workspace PROJECT.md discovery, validation, live reload, and CLI writes."""

from __future__ import annotations

import json
import os

import pytest
from conftest import edit_project, write_config, write_project
from typer.testing import CliRunner

from enso import frontmatter, tasks
from enso.cli import app
from enso.config import LiveConfig, load_config, parse_config, parse_stages

runner = CliRunner()


def invoke(*args):
    return runner.invoke(app, ["project", *args, "--json"])


def test_project_fields_and_external_repo(enso_home, raw_config, repo):
    path = write_project(
        enso_home,
        "EN",
        {
            "name": "Enso",
            "stages": ["work", "approve:human"],
            "repo": str(repo),
            "base": "main",
            "worktree_root": "../trees",
            "setup": "./setup.sh",
            "copy": [".env"],
        },
    )
    path.write_text(path.read_text() + "Project context is prose, not settings.\n")
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None and not problems
    project = config.projects["EN"]
    assert project.workspace == "default" and project.key == "EN"
    assert project.repo == repo and project.worktree_root == "../trees"
    assert project.setup == "./setup.sh" and project.copy == (".env",)
    assert project.agent_stages == ("work",) and project.human_stages == ("approve",)
    assert project.next_stage("work") == "approve" and project.previous_stage("work") is None


def test_project_errors_accumulate_with_source_paths(enso_home, raw_config):
    first = write_project(
        enso_home,
        "EN",
        {
            "name": "",
            "workspace": "default",
            "key": "EN",
            "stages": ["done", "work", "work"],
            "setup": 3,
            "copy": ["../secrets"],
        },
    )
    second = write_project(enso_home, "ZZ", {"name": "Other", "stages": []})
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None
    assert all(str(first) in problem or str(second) in problem for problem in problems)
    for text in (
        "workspace is not",
        "key is not",
        "name must",
        "built-in",
        "listed twice",
        "setup must",
        "copy must",
        "non-empty list",
    ):
        assert any(text in problem for problem in problems), problems


@pytest.mark.parametrize(
    "text",
    ["no frontmatter", "---\nname: [broken\n---\n"],
)
def test_bad_frontmatter_has_one_path_diagnostic(enso_home, raw_config, text):
    path = write_project(enso_home, "EN", {"name": "Enso", "stages": ["work"]})
    path.write_text(text)
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and len(problems) == 1 and str(path) in problems[0]


@pytest.mark.parametrize("location", ["root", "directory", "file", "missing"])
def test_unsafe_and_incomplete_projects_are_reported(enso_home, raw_config, tmp_path, location):
    path = write_project(enso_home, "EN", {"name": "Enso", "stages": ["work"]})
    target = tmp_path / "outside"
    target.mkdir()
    if location == "root":
        path.parent.rename(target / "EN")
        path.parent.parent.rmdir()
        path.parent.parent.symlink_to(target, target_is_directory=True)
    elif location == "directory":
        path.parent.rename(target / "EN")
        path.parent.symlink_to(target / "EN", target_is_directory=True)
    else:
        path.unlink()
        if location == "file":
            path.symlink_to(target / "absent")
        elif location == "fifo":
            os.mkfifo(path)
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and problems
    assert "projects" in problems[0]


def test_duplicate_keys_and_removed_config_block(enso_home, raw_config):
    first = write_project(enso_home, "EN", {"name": "One", "stages": ["work"]})
    second = write_project(enso_home, "EN", {"name": "Two", "stages": ["work"]}, "team")
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and len(problems) == 1
    assert str(first) in problems[0] and str(second) in problems[0]
    second.unlink()
    second.parent.rmdir()
    config, problems, _ = parse_config({**raw_config, "projects": {}}, enso_home)
    assert config is None and problems == ["projects is not a recognized key"]


def test_live_config_tracks_project_add_edit_remove_and_invalid_changes(enso_home, raw_config):
    write_config(enso_home, raw_config)
    live = LiveConfig(load_config(enso_home))
    assert not live.current().projects
    path = write_project(enso_home, "EN", {"name": "Enso", "stages": ["work"]})
    assert live.current().projects["EN"].name == "Enso"
    edit_project(enso_home, name="Renamed")
    assert live.current().projects["EN"].name == "Renamed"
    path.write_text("unfinished")
    assert live.current().projects["EN"].name == "Renamed"
    path.unlink()
    path.parent.rmdir()
    assert not live.current().projects


def test_project_add_preserves_config_and_refuses_overwrite(
    enso_home, raw_config, repo, monkeypatch
):
    write_config(enso_home, raw_config)
    before = enso_home.config.read_bytes()
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
    result = invoke("add", "EN", "--name", "Enso", "--repo", str(repo), "--flow", "basic")
    assert result.exit_code == 0, result.output
    project = json.loads(result.stdout)
    assert project["key"] == "EN" and project["workspace"] == "default"
    assert enso_home.config.read_bytes() == before
    path = enso_home.project("default", "EN") / "PROJECT.md"
    assert frontmatter.read(path).fields == {"name": "Enso", "stages": ["work"], "repo": str(repo)}
    original = path.read_bytes()
    enso_home.workspace("team").mkdir()
    duplicate = invoke("add", "EN", "--name", "Other", "--workspace", "team", "--flow", "basic")
    assert duplicate.exit_code == 1 and "already exists" in duplicate.stdout
    assert path.read_bytes() == original and not enso_home.project("team", "EN").exists()
    assert json.loads(invoke("list").stdout)[0]["key"] == "EN"
    assert json.loads(invoke("list", "--workspace", "team").stdout) == []
    assert len(json.loads(invoke("list", "--all-workspaces").stdout)) == 1


def test_project_add_requires_context_and_validates_before_writing(
    enso_home, raw_config, tmp_path, monkeypatch
):
    write_config(enso_home, raw_config)
    monkeypatch.chdir(enso_home.workspace("default"))
    args = ("add", "EN", "--name", "Enso", "--flow", "basic")
    assert "select a workspace" in invoke(*args).stdout
    for flags in (("--workspace", "missing"), ("--workspace", "default", "--stages", "work")):
        result = invoke(*args, *flags)
        assert result.exit_code == 1
    assert not enso_home.workspace_projects("default").exists()
    bad_key = invoke("add", "../EN", "--name", "Bad", "--flow", "basic", "--workspace", "default")
    assert bad_key.exit_code == 1


def test_project_add_resolves_relative_repo_once(enso_home, raw_config, repo, monkeypatch):
    write_config(enso_home, raw_config)
    monkeypatch.chdir(repo.parent)
    result = invoke(
        "add",
        "EN",
        "--name",
        "Enso",
        "--repo",
        repo.name,
        "--flow",
        "basic",
        "--workspace",
        "default",
    )
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(enso_home.home)
    assert load_config(enso_home).projects["EN"].repo == repo


@pytest.mark.parametrize("flow", tasks.FLOWS)
def test_flow_presets_remain_valid(flow):
    problems = []
    assert parse_stages(list(tasks.FLOWS[flow]), "flow", problems)
    assert not problems


def test_task_context_and_cross_workspace_dependencies(enso_home, project_config, monkeypatch):
    from enso.config import load_config

    write_project(enso_home, "TEAM", {"name": "Team", "stages": ["work"]}, "team")
    config = load_config(enso_home)
    first = tasks.create(enso_home, config, "EN", "Default work", actor="user:test")
    other = tasks.create(enso_home, config, "TEAM", "Team work", actor="user:test")

    def task_cli(*args):
        return runner.invoke(app, ["task", *args, "--json"])

    assert [row["ref"] for row in json.loads(task_cli("list").stdout)] == [first.ref]
    assert [row["ref"] for row in json.loads(task_cli("list", "--workspace", "team").stdout)] == [
        other.ref
    ]
    assert len(json.loads(task_cli("list", "--all-workspaces").stdout)) == 2
    for args in (
        ("show", other.ref),
        ("note", other.ref, "wrong workspace"),
        ("add", "Wrong", "--project", "TEAM"),
    ):
        result = task_cli(*args)
        assert result.exit_code == 1 and "--workspace" in result.stdout
    assert task_cli("show", other.ref, "--workspace", "team").exit_code == 0
    dependent = task_cli(
        "add", "Waiting", "--project", "EN", "--after", other.ref, "--from", other.ref
    )
    assert dependent.exit_code == 0, dependent.output
    waiting = json.loads(dependent.stdout)
    assert waiting["workspace"] == "default" and waiting["stage"] == "blocked"
    tasks.move(
        enso_home, config, other.ref, "advance", actor="user:test", run_id=None, message="Done"
    )
    assert tasks.get(enso_home, waiting["ref"]).stage == "triage"
    monkeypatch.delenv("ENSO_WORKSPACE")
    monkeypatch.chdir(enso_home.workspace("default"))
    assert "select a workspace" in task_cli("show", first.ref).stdout
    assert task_cli("show", first.ref, "--workspace", "default").exit_code == 0


def test_project_move_never_reassigns_a_task(enso_home, project_config):
    task = tasks.create(enso_home, project_config, "EN", "Keep ownership", actor="user:test")
    enso_home.workspace_projects("team").mkdir(parents=True)
    enso_home.project("default", "EN").rename(enso_home.project("team", "EN"))
    config = load_config(enso_home)
    assert tasks.get(enso_home, task.ref).workspace == "default"
    assert tasks.take(enso_home, config, "EN", "triage", actor="job:team:stage", run_id="r") is None
    with pytest.raises(tasks.TaskError, match="task belongs to default"):
        tasks.context(enso_home, config, task.ref, env={})


def test_config_check_and_doctor_report_project_paths(enso_home, raw_config, monkeypatch):
    from enso import service

    write_config(enso_home, raw_config)
    path = write_project(enso_home, "EN", {"name": "Broken", "stages": []})
    before = path.read_bytes(), enso_home.config.read_bytes()
    monkeypatch.setattr(
        service,
        "status",
        lambda *args, **kwargs: service.Status("unsupported", None, False, False, None),
    )
    for command in (["config", "check"], ["doctor"]):
        result = runner.invoke(app, [*command, "--json"])
        assert result.exit_code == 1 and str(path) in result.stdout, result.output
    assert (path.read_bytes(), enso_home.config.read_bytes()) == before
