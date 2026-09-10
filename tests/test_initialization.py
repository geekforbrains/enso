"""Onboarding contracts: offline scaffolding, safe writes, and packaged discovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enso import connection_setup, initialization, workspaces
from enso.cli import app
from enso.config import config_fingerprint, config_lock, load_config, save_config
from enso.providers import PROVIDER_CLASSES


def invoke(*arguments, input=None):
    result = CliRunner().invoke(app, list(arguments), input=input)
    assert result.stderr == "", result.stderr
    return result.exit_code, json.loads(result.stdout)


def test_init_is_offline_with_closed_stdin_and_without_transport_extras(enso_home):
    # Run a clean import: already-imported pytest fixtures must not hide an optional import.
    script = """
import importlib.abc, sys
class NoExtras(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'slack_sdk', 'slack_bolt', 'telegram', 'aiohttp'}:
            raise ImportError('optional transport import attempted')
sys.meta_path.insert(0, NoExtras())
import socket
socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network'))
from enso.cli import main
sys.argv = ['enso', 'init', '--json']
main()
"""
    run = subprocess.run(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=enso_home.home,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["ok"]
    assert not enso_home.config.exists() and not enso_home.db.exists()
    assert enso_home.config_example.is_file()
    assert not list(enso_home.jobs.iterdir())
    assert (enso_home.home / ".git").is_dir()


def test_init_rerun_preserves_active_config_instructions_jobs_and_skills(enso_home, raw_config):
    assert initialization.initialize_home(enso_home)["ok"]
    assert initialization.apply_config(enso_home, raw_config)["ok"]
    personal = [
        enso_home.agents_md,
        enso_home.config_example,
        enso_home.skills / "enso" / "SKILL.md",
        enso_home.workspace("default") / "AGENTS.md",
        enso_home.jobs / "enso-audit" / "JOB.md",
    ]
    for path in personal:
        path.write_text("personal content\n")
    before = enso_home.config.read_bytes()
    result = initialization.initialize_home(enso_home)
    assert result["ok"] and result["changes"] == []
    assert enso_home.config.read_bytes() == before
    assert all(path.read_text() == "personal content\n" for path in personal)


@pytest.mark.parametrize("relative", ["CLAUDE.md", ".claude", "workspaces/default/.agents/skills"])
def test_init_reports_and_preserves_path_conflicts(enso_home, relative):
    conflict = enso_home.home / relative
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text("keep this")
    code, report = invoke("init", "--json")
    assert code == 1 and not report["ok"]
    assert any(str(conflict) in problem for problem in report["problems"])
    assert conflict.read_text() == "keep this"
    assert not enso_home.config_example.exists()


def test_init_rejects_symlink_escape_without_writing_outside_home(enso_home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (enso_home.home / "skills").symlink_to(outside, target_is_directory=True)
    assert not initialization.initialize_home(enso_home)["ok"]
    assert list(outside.iterdir()) == []


def test_init_recovers_after_interrupted_file_seeding(enso_home, monkeypatch):
    original = workspaces.write_missing
    failed = False

    def interrupt(path, text):
        nonlocal failed
        if path.name == "SKILL.md" and not failed:
            failed = True
            raise OSError("disk failure")
        return original(path, text)

    monkeypatch.setattr(workspaces, "write_missing", interrupt)
    assert not initialization.initialize_home(enso_home)["ok"]
    assert initialization.initialize_home(enso_home)["ok"]
    assert len(list(enso_home.skills.glob("*/SKILL.md"))) == len(workspaces.BUNDLED_SKILLS)


def test_init_places_git_root_at_a_relative_enso_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENSO_HOME", "relative-enso-home")
    code, report = invoke("init", "--json")
    assert code == 0 and report["ok"]
    assert (tmp_path / "relative-enso-home" / ".git").is_dir()
    assert not (tmp_path / "relative-enso-home" / "relative-enso-home").exists()


def test_apply_accepts_stdin_and_replaces_whole_document_with_private_permissions(
    enso_home, raw_config
):
    save_config(enso_home, {**raw_config, "logging": {"level": "DEBUG"}})
    revision = config_fingerprint(enso_home)
    code, report = invoke(
        "config",
        "apply",
        "--file",
        "-",
        "--json",
        "--expected-hash",
        revision,
        input=json.dumps(raw_config),
    )
    assert code == 0 and report["ok"] and report["applied"] and report["jobs_complete"]
    assert report["restart_required"]  # logging is read at start, and it changed
    assert "logging" not in json.loads(enso_home.config.read_text())
    assert enso_home.config.stat().st_mode & 0o777 == 0o600
    assert report["config_hash"] == hashlib.sha256(enso_home.config.read_bytes()).hexdigest()
    assert load_config(enso_home).defaults.provider == "claude"


def test_apply_invalid_input_aggregates_and_preserves_active_config(enso_home, raw_config):
    save_config(enso_home, raw_config)
    previous = enso_home.config.read_bytes()
    raw_config["version"] = 9
    raw_config["agent"]["timeout"] = -1
    raw_config["transports"]["slack"]["app_token"] = ""
    code, report = invoke("config", "apply", "--file", "-", "--json", input=json.dumps(raw_config))
    assert code == 1 and not report["applied"] and len(report["problems"]) == 3
    assert enso_home.config.read_bytes() == previous
    assert not enso_home.jobs.exists()


@pytest.mark.parametrize(
    "content", ['{"secret":"xoxb-topsecret",broken}', "[" * 1500, " " * (1024 * 1024 + 1)]
)
def test_apply_malformed_or_oversized_input_never_echoes_content(enso_home, content):
    code, report = invoke("config", "apply", "--file", "-", "--json", input=content)
    assert code == 1 and not report["applied"] and report["restart_required"] is False
    assert "xoxb-topsecret" not in json.dumps(report)
    assert not enso_home.config.exists()


@pytest.mark.parametrize("token", ["xoxb-test", "secret\nvalue\t£"])
def test_apply_and_check_never_echo_tokens_repeated_in_other_invalid_fields(
    enso_home, raw_config, token
):
    raw_config["transports"]["slack"]["bot_token"] = token
    raw_config["defaults"]["model"] = token
    raw_config["providers"]["claude"]["path"] = token
    _, report = invoke("config", "apply", "--file", "-", "--json", input=json.dumps(raw_config))
    assert token not in str(report) and repr(token)[1:-1] not in str(report)
    save_config(enso_home, raw_config)
    _, report = invoke("config", "check", "--json")
    assert token not in str(report) and repr(token)[1:-1] not in str(report)


def test_apply_stale_revision_and_busy_lock_preserve_config(enso_home, raw_config):
    save_config(enso_home, raw_config)
    previous = enso_home.config.read_bytes()
    stale = initialization.apply_config(enso_home, raw_config, expected_hash="missing")
    assert not stale["applied"] and "changed" in stale["problems"][0]
    with config_lock(enso_home):
        busy = initialization.apply_config(enso_home, raw_config)
    assert not busy["applied"] and "busy" in busy["problems"][0]
    assert enso_home.config.read_bytes() == previous


def test_apply_proceeds_while_the_service_holds_the_receiver(enso_home, raw_config):
    with connection_setup.service_receiver(enso_home):
        report = initialization.apply_config(enso_home, raw_config, expected_hash="missing")
    assert report["ok"] and report["restart_required"] is False


def test_apply_refuses_when_the_pairing_state_cannot_be_read(enso_home, raw_config):
    connection_setup._prepare(enso_home)
    (enso_home.connection_dir / "state.json").write_text("{")
    report = initialization.apply_config(enso_home, raw_config)
    assert not report["applied"] and "connection state" in report["problems"][0]
    assert not enso_home.config.exists()


def test_apply_reports_when_only_a_restart_can_apply_the_change(enso_home, raw_config):
    fresh = initialization.apply_config(enso_home, raw_config, expected_hash="missing")
    assert fresh["ok"] and fresh["restart_required"] is False
    raw_config["defaults"]["effort"] = "low"
    assert initialization.apply_config(enso_home, raw_config)["restart_required"] is False
    raw_config["logging"] = {"level": "DEBUG"}
    assert initialization.apply_config(enso_home, raw_config)["restart_required"] is True
    raw_config["transports"]["slack"]["bot_token"] = "xoxb-rotated"
    raw_config["version"] = 9
    rejected = initialization.apply_config(enso_home, raw_config)
    assert not rejected["applied"] and rejected["restart_required"] is False


@pytest.mark.parametrize(
    "model",
    [
        'custom"\nenabled: false\nextra: "model',
        "custom\u0085model",
        "custom\ud800model",
        "custom{{effort}}model",
    ],
)
def test_apply_model_text_cannot_change_bundled_job_frontmatter(enso_home, raw_config, model):
    from enso.jobs import find_job

    raw_config["providers"]["claude"]["models"].append(model)
    raw_config["defaults"]["model"] = model
    code, report = invoke("config", "apply", "--file", "-", "--json", input=json.dumps(raw_config))
    assert code == 0 and report["ok"]
    job, problems = find_job(enso_home, load_config(enso_home), "enso-audit")
    assert not problems and job.model == model and job.enabled


def test_apply_atomic_write_failure_preserves_old_config(enso_home, raw_config, monkeypatch):
    save_config(enso_home, raw_config)
    previous = enso_home.config.read_bytes()

    def fail_replace(*args):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    raw_config["defaults"]["effort"] = "low"
    report = initialization.apply_config(enso_home, raw_config)
    assert not report["ok"] and not report["applied"]
    assert enso_home.config.read_bytes() == previous
    assert list(enso_home.home.glob(".config-*")) == []


def test_apply_job_failure_is_reported_and_retryable_without_partial_job(
    enso_home, raw_config, monkeypatch
):
    original = workspaces.write_missing

    def fail_script(path, text):
        if path.name == "prerun.sh":
            raise OSError("disk full")
        return original(path, text)

    monkeypatch.setattr(workspaces, "write_missing", fail_script)
    report = initialization.apply_config(enso_home, raw_config, expected_hash="missing")
    assert report["applied"] and not report["ok"] and not report["jobs_complete"]
    assert not (enso_home.jobs / "enso-audit").exists()
    monkeypatch.setattr(workspaces, "write_missing", original)
    retried = initialization.apply_config(
        enso_home, raw_config, expected_hash=report["config_hash"]
    )
    assert retried["ok"] and retried["jobs_complete"]
    assert (enso_home.jobs / "enso-audit" / "prerun.sh").is_file()
    before = (enso_home.jobs / "enso-audit" / "JOB.md").read_bytes()
    raw_config["defaults"]["effort"] = "low"
    assert initialization.apply_config(enso_home, raw_config)["ok"]
    assert (enso_home.jobs / "enso-audit" / "JOB.md").read_bytes() == before


def test_check_reports_revision_of_the_validated_snapshot(enso_home, raw_config, monkeypatch):
    save_config(enso_home, raw_config)
    original = Path.read_bytes
    before = enso_home.config.read_bytes()

    def edit_after_read(path):
        content = original(path)
        if path == enso_home.config:
            path.write_text("invalid later edit")
        return content

    monkeypatch.setattr(Path, "read_bytes", edit_after_read)
    code, report = invoke("config", "check", "--json")
    assert code == 0 and report["ok"]
    assert report["config_hash"] == hashlib.sha256(before).hexdigest()


def test_provider_catalog_matches_adapter_caps_without_claiming_dynamic_model_access(enso_home):
    code, catalog = invoke("providers", "--json")
    assert code == 0 and catalog["version"] == 1
    assert {p["id"] for p in catalog["providers"]} == set(PROVIDER_CLASSES)
    for provider in catalog["providers"]:
        cls = PROVIDER_CLASSES[provider["id"]]
        for model in provider["models"]:
            assert model["id"] in cls.models
            if provider["id"] == "opencode":
                assert model["efforts"] == [] and not model["known"]
            else:
                assert model["efforts"]
                assert all(cls.clamp_effort(e, model["id"]) == e for e in model["efforts"])
        if provider["default_effort"] is not None:
            assert provider["default_effort"] in provider["models"][0]["efforts"]
    assert not enso_home.config.exists()


def test_packaged_slack_manifest_matches_repository_example_without_config(enso_home):
    code, manifest = invoke("slack", "manifest")
    example = Path(__file__).resolve().parents[1] / "assets/slack/manifest.json"
    assert code == 0 and manifest == json.loads(example.read_text())
    assert manifest["settings"]["socket_mode_enabled"]
    assert not manifest["features"]["app_home"]["messages_tab_read_only_enabled"]
    assert not enso_home.config.exists() and not enso_home.db.exists()


APPLY_FIELDS = {
    "version",
    "ok",
    "applied",
    "config_hash",
    "jobs_complete",
    "restart_required",
    "changes",
    "problems",
    "warnings",
}
REFUSAL = (
    "workspace meteor is restricted, so a run inside it cannot change workspaces.meteor or "
    "provider arguments; ask a person to make this change from a terminal"
)


def test_set_creates_a_nested_key_and_validates_the_result(enso_home, raw_config):
    save_config(enso_home, raw_config)
    revision = config_fingerprint(enso_home)
    code, report = invoke(
        "config",
        "set",
        "workspaces.meteor.restricted",
        "true",
        "--json",
        "--expected-hash",
        revision,
    )
    assert code == 0 and report["ok"] and report["applied"] and report["jobs_complete"]
    assert set(report) == APPLY_FIELDS and report["version"] == 1
    assert report["restart_required"] is False
    assert report["config_hash"] == config_fingerprint(enso_home) != revision
    assert enso_home.config.stat().st_mode & 0o777 == 0o600
    assert load_config(enso_home).workspaces["meteor"].restricted is True
    assert (enso_home.jobs / "enso-audit" / "JOB.md").is_file()


def test_set_stores_text_that_is_not_json_as_a_string(enso_home, raw_config):
    save_config(enso_home, raw_config)
    code, report = invoke("config", "set", "defaults.model", "sonnet", "--json")
    assert code == 0 and report["ok"] and report["restart_required"] is False
    assert load_config(enso_home).defaults.model == "sonnet"
    code, report = invoke("config", "set", "logging.level", '"DEBUG"', "--json")
    assert code == 0 and report["restart_required"] is True
    assert json.loads(enso_home.config.read_text())["logging"] == {"level": "DEBUG"}


def test_unset_removes_a_key_and_reports_an_absent_one(enso_home, raw_config):
    save_config(enso_home, {**raw_config, "logging": {"level": "DEBUG"}})
    code, report = invoke("config", "unset", "logging.level", "--json")
    assert code == 0 and report["ok"] and report["restart_required"] is True
    assert json.loads(enso_home.config.read_text())["logging"] == {}
    previous = enso_home.config.read_bytes()
    code, report = invoke("config", "unset", "logging.level", "--json")
    assert code == 1 and set(report) == APPLY_FIELDS and not report["applied"]
    assert report["problems"] == ["logging.level is not set"]
    assert enso_home.config.read_bytes() == previous


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        (("set", "agent.timeout", "soon"), "agent.timeout"),
        (("set", "agent.timeout.max", "1"), "agent.timeout is not an object"),
        (("set", "defaults..model", "opus"), "not a dotted key path"),
        (("unset", "agent.timeout.max"), "agent.timeout.max is not set"),
        (("unset", ""), "not a dotted key path"),
    ],
)
def test_invalid_edits_leave_the_file_unchanged(enso_home, raw_config, arguments, problem):
    save_config(enso_home, raw_config)
    previous = enso_home.config.read_bytes()
    code, report = invoke("config", *arguments, "--json")
    assert code == 1 and not report["applied"] and report["restart_required"] is False
    assert len(report["problems"]) == 1 and problem in report["problems"][0]
    assert enso_home.config.read_bytes() == previous


def test_set_never_echoes_a_token_repeated_in_the_new_value(enso_home, raw_config):
    save_config(enso_home, raw_config)
    code, report = invoke("config", "set", "defaults.model", "xoxb-test", "--json")
    assert code == 1 and report["problems"] and "xoxb-test" not in json.dumps(report)


def test_set_and_unset_need_a_readable_document(enso_home):
    code, report = invoke("config", "set", "logging.level", "DEBUG", "--json")
    assert code == 1 and report["config_hash"] == "missing"
    assert report["problems"] == ["config.json is missing; run setup or init and config apply"]
    assert not enso_home.config.exists()
    enso_home.config.write_text("{")
    code, report = invoke("config", "unset", "logging.level", "--json")
    assert code == 1 and "repair it with config apply" in report["problems"][0]
    assert report["config_hash"] == config_fingerprint(enso_home)
    assert enso_home.config.read_text() == "{"


def test_set_with_a_stale_revision_preserves_config(enso_home, raw_config):
    save_config(enso_home, raw_config)
    previous = enso_home.config.read_bytes()
    edit = initialization.ConfigEdit("set", "logging.level", "DEBUG")
    stale = initialization.patch_config(enso_home, [edit], expected_hash="missing")
    assert not stale["applied"] and "changed" in stale["problems"][0]
    assert stale["config_hash"] == config_fingerprint(enso_home)
    assert enso_home.config.read_bytes() == previous


@pytest.mark.parametrize(
    "edit",
    [
        initialization.ConfigEdit("set", "workspaces.meteor.restricted", False),
        initialization.ConfigEdit("unset", "workspaces.meteor"),
        initialization.ConfigEdit("set", "workspaces.meteor.providers.claude.args", []),
        initialization.ConfigEdit("set", "providers.claude.args", []),
        initialization.ConfigEdit("unset", "providers.grok"),
        initialization.ConfigEdit(
            "set", "providers.opencode", {"path": sys.executable, "models": ["m"], "args": []}
        ),
    ],
)
def test_a_restricted_run_cannot_loosen_its_own_restriction(
    enso_home, raw_config, monkeypatch, edit
):
    raw_config["workspaces"]["meteor"] = {"restricted": True}
    save_config(enso_home, raw_config)
    previous = enso_home.config.read_bytes()
    monkeypatch.setenv("ENSO_WORKSPACE", "meteor")
    report = initialization.patch_config(enso_home, [edit])
    assert not report["applied"] and report["problems"] == [REFUSAL]
    assert enso_home.config.read_bytes() == previous


def test_a_restricted_run_cannot_apply_provider_arguments_either(
    enso_home, raw_config, monkeypatch
):
    raw_config["workspaces"]["meteor"] = {"restricted": True}
    save_config(enso_home, raw_config)
    previous = enso_home.config.read_bytes()
    monkeypatch.setenv("ENSO_WORKSPACE", "meteor")
    raw_config["providers"]["claude"]["args"] = ["--skip", "--more"]
    report = initialization.apply_config(enso_home, raw_config)
    assert not report["applied"] and report["problems"] == [REFUSAL]
    raw_config["version"] = 9  # the refusal is reported with the validation problems
    report = initialization.apply_config(enso_home, raw_config)
    assert report["problems"] == ["version must be 1", REFUSAL]
    assert enso_home.config.read_bytes() == previous


def test_a_restricted_run_may_still_change_unrelated_keys(enso_home, raw_config, monkeypatch):
    raw_config["workspaces"]["meteor"] = {"restricted": True}
    save_config(enso_home, raw_config)
    monkeypatch.setenv("ENSO_WORKSPACE", "meteor")
    for edit in (
        initialization.ConfigEdit("set", "logging.level", "DEBUG"),
        initialization.ConfigEdit("set", "workspaces.default.restricted", True),
        initialization.ConfigEdit("set", "providers.claude.models", ["opus", "sonnet"]),
        # A provider added without arguments changes nothing under any provider's args.
        initialization.ConfigEdit(
            "set", "providers.opencode", {"path": sys.executable, "models": ["m"]}
        ),
    ):
        report = initialization.patch_config(enso_home, [edit])
        assert report["ok"], report["problems"]
    config = load_config(enso_home)
    assert config.workspaces["meteor"].restricted and config.workspaces["default"].restricted
    assert config.providers["opencode"].args == ()


@pytest.mark.parametrize("workspace", [None, "default"])
def test_the_guard_needs_a_restricted_calling_workspace(
    enso_home, raw_config, monkeypatch, workspace
):
    raw_config["workspaces"]["meteor"] = {"restricted": True}
    save_config(enso_home, raw_config)
    if workspace is not None:
        monkeypatch.setenv("ENSO_WORKSPACE", workspace)
    edit = initialization.ConfigEdit("set", "workspaces.meteor.restricted", False)
    assert initialization.patch_config(enso_home, [edit])["ok"]
    raw_config["workspaces"]["meteor"]["restricted"] = False
    raw_config["providers"]["claude"]["args"] = []
    assert initialization.apply_config(enso_home, raw_config)["ok"]
    assert not load_config(enso_home).workspaces["meteor"].restricted


def test_an_unparsable_document_is_repaired_without_the_guard(enso_home, raw_config, monkeypatch):
    raw_config["workspaces"]["meteor"] = {"restricted": True}
    save_config(enso_home, {**raw_config, "version": 9})
    monkeypatch.setenv("ENSO_WORKSPACE", "meteor")
    raw_config["providers"]["claude"]["args"] = []
    assert initialization.apply_config(enso_home, raw_config)["ok"]
