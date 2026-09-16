"""Workspace settings, live snapshots, and explicit/environment context selection."""

from __future__ import annotations

import logging
import os
import shutil

import pytest
from conftest import write_config, write_workspace
from typer.testing import CliRunner

from enso import initialization, workspaces
from enso.cli import app
from enso.config import ConfigError, LiveConfig, load_config, parse_config, resolve_workspace
from enso.routing import resolve_agent


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
        (
            {"agent": {"provider": "claude"}},
            ["agent.model is required"],
        ),
        ({"agent": None}, ["agent must be an object"]),
        ({"agent": {"provider": "absent", "model": "x", "effort": "high"}}, ["not configured"]),
        (
            {"agent": {"provider": "claude", "model": "wrong", "effort": "wrong"}},
            ["agent.model"],
        ),
        (
            {"agent": {"provider": "claude", "model": "opus"}},
            ["agent.effort is required"],
        ),
        (
            {"agent": {"provider": "claude", "model": "opus", "effort": "wrong"}},
            ["agent.effort"],
        ),
        ({"providers": False}, ["providers must be an object"]),
        ({"providers": {"claude": "--skip"}}, ["providers.claude.args must be a list of strings"]),
        ({"providers": {"claude": {"args": [1]}}}, ["args must be a list of strings"]),
        ({"providers": {"agy": {"args": []}}}, ["provider is not configured"]),
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
        b"---\nproviders: {claude: {args: [], args: []}}\n---\n",
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


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo"])
def test_workspace_settings_must_be_a_regular_file(enso_home, raw_config, tmp_path, kind):
    path = enso_home.workspace_settings("default")
    target = tmp_path / "outside.md"
    if kind == "symlink":
        target.write_text("---\n{}\n---\n")
    if kind in ("symlink", "dangling"):
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and str(path) in problems[0]
    if kind == "symlink":
        assert target.read_text() == "---\n{}\n---\n"


def test_workspace_reload_preserves_linked_config_reads(enso_home, raw_config, tmp_path):
    target = tmp_path / "config.json"
    write_config(enso_home, raw_config)
    enso_home.config.rename(target)
    enso_home.config.symlink_to(target)
    live = LiveConfig(load_config(enso_home))
    assert live.current().defaults.model == "opus"
    write_workspace(enso_home, "default", {"providers": {"claude": {"args": []}}})
    assert live.current().provider_args("default", "claude") == ()
    raw_config["defaults"]["model"] = "sonnet"
    write_config(enso_home, raw_config)
    assert live.current().defaults.model == "sonnet"
    assert enso_home.config.is_symlink()


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
        enso_home.workspace_memory("../team")


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


def test_fresh_scaffold_preserves_settings_and_keeps_optional_paths_absent(enso_home):
    path = write_workspace(enso_home, "default", {"providers": {"claude": {"args": []}}})
    before = path.read_bytes()
    assert initialization.initialize_home(enso_home)["ok"]
    assert path.read_bytes() == before
    root = workspaces.create_workspace(enso_home, "team")
    for name in ("knowledge", "memory", "jobs", "projects", "drafts", "uploads", "skills"):
        assert (root / name).is_dir()
    assert not enso_home.workspace_settings("team").exists()
    assert not enso_home.workspace_heartbeat("team").exists()
    for link in (".claude/skills", ".agents/skills"):
        assert (root / link).resolve() == enso_home.workspace_skills("team")
        assert (enso_home.home / link / "enso-workspace/SKILL.md").is_file()
    assert not (root / "enso.db").exists()
