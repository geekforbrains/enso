"""Skill commands work before configuration and emit one documented JSON result."""

import json

from typer.testing import CliRunner

from enso import skill_catalog
from enso.cli import app


def test_skill_list_is_offline_without_configuration_or_database(enso_home, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("installed listing must remain offline")

    monkeypatch.setattr(skill_catalog, "_download", no_network)
    runner = CliRunner()
    result = runner.invoke(app, ["skill", "list", "--json"])
    assert result.exit_code == 0 and json.loads(result.stdout) == []
    assert result.stderr == ""
    assert "no home skills" in runner.invoke(app, ["skill", "list"]).stdout
    assert not enso_home.config.exists() and not enso_home.db.exists()


def test_skill_available_and_show_json(enso_home, monkeypatch):
    item = {
        "name": "enso-example",
        "description": "Use for examples.",
        "files": ["SKILL.md"],
        "requires": [],
    }
    source = {"source": skill_catalog.SOURCE, "commit": "a" * 40}
    available_item = {**item, "installed": False}
    monkeypatch.setattr(
        skill_catalog, "available", lambda paths: {**source, "skills": [available_item]}
    )
    monkeypatch.setattr(skill_catalog, "show", lambda name: {**source, **item})
    runner = CliRunner()
    listed = runner.invoke(app, ["skill", "list", "--available", "--json"])
    shown = runner.invoke(app, ["skill", "show", "enso-example", "--json"])
    assert listed.exit_code == shown.exit_code == 0
    assert json.loads(listed.stdout) == {**source, "skills": [available_item]}
    assert json.loads(shown.stdout) == {**source, **item}
    assert listed.stderr == shown.stderr == ""


def test_skill_install_success_and_expected_errors_are_one_json_result(enso_home, monkeypatch):
    result = {
        "ok": True,
        "name": "enso-example",
        "path": str(enso_home.skills / "enso-example"),
        "source": skill_catalog.SOURCE,
        "commit": "a" * 40,
        "files": ["SKILL.md"],
        "requires": [],
    }
    monkeypatch.setattr(skill_catalog, "install", lambda paths, name: result)
    runner = CliRunner()
    written = runner.invoke(app, ["skill", "install", "enso-example", "--json"])
    assert written.exit_code == 0 and json.loads(written.stdout) == result

    def failure(*args):
        raise skill_catalog.SkillError("the official catalog is unavailable")

    monkeypatch.setattr(skill_catalog, "install", failure)
    monkeypatch.setattr(skill_catalog, "available", failure)
    monkeypatch.setattr(skill_catalog, "show", failure)
    for command in (["install", "enso-example"], ["list", "--available"], ["show", "enso-example"]):
        failed = runner.invoke(app, ["skill", *command, "--json"])
        assert failed.exit_code == 1
        assert json.loads(failed.stdout) == {
            "ok": False,
            "error": "the official catalog is unavailable",
        }
        assert failed.stderr == ""


def test_skill_cli_has_no_arbitrary_source_option(enso_home):
    runner = CliRunner()
    for option in ("--repo", "--url", "--ref", "--force"):
        result = runner.invoke(app, ["skill", "install", "enso-example", option, "elsewhere"])
        assert result.exit_code == 2 and "No such option" in result.stderr
