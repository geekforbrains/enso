"""Workspace settings, live snapshots, and explicit/environment context selection."""

from __future__ import annotations

import json
import logging
import os
import shutil

import pytest
from conftest import load_job, write_config, write_job, write_workspace
from typer.testing import CliRunner

from enso import db, heartbeat, messages, runs, workspaces
from enso.cli import app
from enso.config import ConfigError, LiveConfig, load_config, parse_config, resolve_workspace
from enso.routing import resolve_agent


@pytest.mark.parametrize("command", ["job", "message", "heartbeat"])
def test_operational_lists_select_workspace_before_limiting(
    enso_home, raw_config, monkeypatch, command
):
    write_config(enso_home, raw_config)
    enso_home.workspace("team").mkdir()
    config = load_config(enso_home)
    db.initialize(enso_home)
    for owner in ("default", "team"):
        write_job(enso_home, workspace=owner)
        runs.start(
            enso_home, load_job(enso_home, config, f"{owner}:nightly"), "manual", effort="high"
        )
        messages.record(
            enso_home,
            workspace=owner,
            transport="slack",
            target="C1",
            thread=None,
            text=owner,
            source="cli",
            status="sent",
            message_id="1.0",
        )
        heartbeat.create(
            config,
            {
                "title": owner,
                "workspace": owner,
                "instructions": "Send a reminder.",
                "completion": "Reminder sent.",
                "allowed_actions": "Send the reminder.",
                "at": "2030-01-01T10:00:00+00:00",
            },
        )
    # Malformed jobs stay visible only in their owning workspace's list.
    write_job(enso_home, "broken", workspace="team", provider=None)
    cli = CliRunner()
    args = [command, "list", "--json"]
    absent = cli.invoke(app, args)
    assert absent.exit_code == 1 and "select a workspace" in json.loads(absent.stdout)["error"]
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
    for flags, expected in (
        ([], {"default"}),
        (["--workspace", "team"], {"team"}),
        (["--all-workspaces"], {"default", "team"}),
    ):
        result = cli.invoke(app, [*args, *flags])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert {
            row.get("workspace", row.get("ref", "").partition(":")[0]) for row in rows
        } == expected
        if command == "job":
            assert any(row.get("ref") == "team:broken" for row in rows) == ("team" in expected)
    if command != "job":
        limit = "--limit" if command == "heartbeat" else "-n"
        result = cli.invoke(app, [*args, limit, "1"])
        assert [row["workspace"] for row in json.loads(result.stdout)] == ["default"]
    for flags in (["--workspace", "missing"], ["--workspace", "team", "--all-workspaces"]):
        result = cli.invoke(app, [*args, *flags])
        assert result.exit_code == 1 and json.loads(result.stdout)["ok"] is False
    monkeypatch.delenv("ENSO_WORKSPACE")
    assert cli.invoke(app, [*args, "--all-workspaces"]).exit_code == 0


def test_workspace_settings_inherit_and_replace_complete_values(enso_home, raw_config):
    workspaces.create_workspace(enso_home, "team")
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems
    assert resolve_agent(config, "team").source == "defaults"
    assert config.provider_args("team", "claude") == ("--skip",)
    write_workspace(enso_home, "team", {})
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems
    assert resolve_agent(config, "team").source == "defaults"
    path = write_workspace(
        enso_home,
        "team",
        {
            "agent": {"provider": "codex", "model": "sol", "effort": "high"},
            "providers": {"claude": {"args": []}},
        },
    )
    path.write_text(path.read_text() + "\nExplanatory Markdown is not an Enso setting.\n")
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems
    assert resolve_agent(config, "team").provider == "codex"
    assert config.provider_args("team", "claude") == ()
    assert config.provider_args("team", "grok") == ("--always-approve",)
    assert config.provider_args("default", "claude") == ("--skip",)
    assert "workspaces" not in config.raw


@pytest.mark.parametrize(
    ("fields", "fragments"),
    [
        ({"providers": False}, ["providers must be an object"]),
        (
            {"providers": {"claude": {"path": "cli", "models": [], "args": []}}},
            ["path is not a recognized key", "models is not a recognized key"],
        ),
        (
            {"workspace": "other", "bindings": {}, "restricted": False},
            [
                "workspace is not a recognized key",
                "bindings is not a recognized key",
                "restricted is not a recognized key",
            ],
        ),
    ],
)
def test_invalid_workspace_fields_are_reported_together(enso_home, raw_config, fields, fragments):
    path = write_workspace(enso_home, "default", fields)
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None
    assert all(str(path) in problem for problem in problems)
    assert all(any(fragment in problem for problem in problems) for fragment in fragments), problems


@pytest.mark.parametrize(
    "content",
    [
        b"without frontmatter",
        b"---\n---\n",
        b"---\n[]\n---\n",
        b"---\nproviders: [\n---\n",
        b"---\nagent: {}\nagent: {}\n---\n",
        b"\xff",
    ],
)
def test_bad_workspace_documents_fail_closed_at_the_cli(enso_home, raw_config, content):
    write_config(enso_home, raw_config)
    path = enso_home.workspace_settings("default")
    path.write_bytes(content)
    result = CliRunner().invoke(app, ["config", "check", "--json"])
    assert result.exit_code == 1
    assert str(path) in result.stdout and "Traceback" not in result.stdout + result.stderr
    assert path.read_bytes() == content


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_workspace_settings_must_be_a_regular_file(enso_home, raw_config, tmp_path, kind):
    path = enso_home.workspace_settings("default")
    target = tmp_path / "outside.md"
    if kind == "symlink":
        target.write_text("---\n{}\n---\n")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and str(path) in problems[0]
    if kind == "symlink":
        assert target.read_text() == "---\n{}\n---\n"


def test_workspace_live_reload_tracks_creation_edits_removal_and_invalid_revisions(
    enso_home,
    raw_config,
    caplog,
):
    write_config(enso_home, raw_config)
    live = LiveConfig(load_config(enso_home))
    original = live.current()
    workspaces.create_workspace(enso_home, "team")
    path = write_workspace(enso_home, "team", {"providers": {"claude": {"args": []}}})
    first = live.current()
    assert "team" in first.workspaces and first.provider_args("team", "claude") == ()
    assert "team" not in original.workspaces
    replacement = path.with_suffix(".tmp")
    replacement.write_text("---\nagent: {provider: claude, model: sonnet, effort: high}\n---\n")
    replacement.replace(path)
    second = live.current()
    assert resolve_agent(second, "team").model == "sonnet"
    assert first.provider_args("team", "claude") == ()
    path.write_text("---\nagent: {}\n---\n")
    with caplog.at_level(logging.WARNING, logger="enso.config"):
        assert live.current() is second
        assert live.current() is second
    assert caplog.text.count("keeping the last good configuration") == 1
    assert str(path) in caplog.text
    with pytest.raises(ConfigError):
        load_config(enso_home)
    path.unlink()
    assert resolve_agent(live.current(), "team").source == "defaults"
    # An empty, unbound workspace can disappear; discovery must not retain its settings.
    shutil.rmtree(enso_home.workspace("team"))
    assert "team" not in live.current().workspaces


def test_context_precedence_requires_an_existing_workspace_and_never_infers_cwd(
    enso_home,
    monkeypatch,
):
    workspaces.create_workspace(enso_home, "team")
    monkeypatch.chdir(enso_home.workspace("team"))
    with pytest.raises(ValueError, match="--workspace or ENSO_WORKSPACE"):
        resolve_workspace(enso_home)
    monkeypatch.setenv("ENSO_WORKSPACE", "team")
    assert resolve_workspace(enso_home) == "team"
    assert resolve_workspace(enso_home, "default") == "default"
    assert os.environ["ENSO_WORKSPACE"] == "team"
    for bad in ("", "../team", "/tmp", "Bad Name", "a" * 65, "missing"):
        with pytest.raises(ValueError):
            resolve_workspace(enso_home, bad)
    monkeypatch.setenv("ENSO_WORKSPACE", "missing")
    assert resolve_workspace(enso_home, "default") == "default"
    with pytest.raises(ValueError, match="missing"):
        resolve_workspace(enso_home)
    with pytest.raises(ValueError):
        enso_home.workspace_knowledge("../team")


def test_linked_workspace_has_no_second_identity(enso_home, raw_config, tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    (target / "WORKSPACE.md").write_text("---\n{}\n---\n")
    root = enso_home.workspace("alias")
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        resolve_workspace(enso_home, "alias")
    raw_config["bindings"]["slack:C2"] = "alias"
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and any("symbolic link" in problem for problem in problems)
    assert workspaces.list_workspaces(enso_home) == ["default"]
