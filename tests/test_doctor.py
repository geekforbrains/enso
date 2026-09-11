"""``enso doctor``: every section on a healthy home, a broken one, and a degraded one."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from conftest import write_config, write_job
from typer.testing import CliRunner

from enso import doctor, heartbeat, service, workspaces
from enso.cli import app
from enso.config import Paths, load_config
from enso.providers.codex import CodexProvider

BINARY = "/opt/enso/bin/enso"


@pytest.fixture
def unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A launchd unit on disk running ``BINARY``; ``service.status`` says it is running."""
    path = tmp_path / "com.enso.agent.plist"
    path.write_text(f"<string>{BINARY}</string>")
    monkeypatch.setattr(service, "enso_binary", lambda: BINARY)
    monkeypatch.setattr(
        service,
        "status",
        lambda platform=None, *, definition=service.AGENT: (
            service.Status("launchd", path, True, True, 42)
            if definition == service.AGENT
            else service.Status("launchd", path.with_name("com.enso.web.plist"), False, False, None)
        ),
    )
    return path


def healthy(enso_home: Paths, raw_config: dict) -> None:
    write_config(enso_home, raw_config)
    workspaces.seed_home(enso_home)
    workspaces.ensure_layout(enso_home.workspace("default"))
    (enso_home.workspace("default") / "AGENTS.md").write_text("# default\n")


def test_a_healthy_home_is_ok_everywhere(enso_home: Paths, raw_config: dict, unit: Path) -> None:
    healthy(enso_home, raw_config)
    write_job(enso_home)

    report = doctor.run(enso_home)

    assert report.ok and [s.name for s in report.sections] == list(doctor.SECTIONS)
    assert all(s.status == "ok" and s.summary == "ok" for s in report.sections)
    assert [s.note for s in report.sections] == [
        str(enso_home.config),
        f"Git root at {enso_home.home}",
        "default",
        "claude, codex, grok",
        "slack",
        "launchd, running pid 42",
        "not installed (optional)",
        "1 job, 1 enabled",
        "0 active, 0 paused",
    ]
    payload = json.loads(json.dumps(report.as_dict()))
    assert payload["ok"] and payload["home"] == str(enso_home.home)
    assert payload["sections"][3] == {
        "name": "providers",
        "status": "ok",
        "note": "claude, codex, grok",
        "problems": [],
        "warnings": [],
        "details": {
            name: {"path": sys.executable, "executable": True, "models": models}
            for name, models in (
                ("claude", ["opus", "sonnet", "haiku"]),
                ("codex", ["astra", "sol", "terra", "luna"]),
                ("grok", ["grok-4.6"]),
            )
        },
    }
    assert payload["sections"][4]["details"] == {
        "slack": {"configured": True, "installed": True},
        "telegram": {"configured": False, "installed": True},
    }
    assert payload["sections"][5]["details"] == {
        "platform": "launchd", "unit": str(unit), "installed": True, "loaded": True, "pid": 42,
    }  # fmt: skip
    assert payload["sections"][7]["details"] == {"jobs": ["nightly"], "enabled": ["nightly"]}


def test_codex_astra_is_available_with_ultra_effort(
    enso_home: Paths, raw_config: dict, unit: Path
) -> None:
    raw_config["defaults"] = {"provider": "codex", "model": "astra", "effort": "ultra"}
    healthy(enso_home, raw_config)

    report = doctor.run(enso_home)

    assert report.ok
    assert report.section("providers").details["codex"]["models"] == CodexProvider.models
    provider = CodexProvider(sys.executable)
    for model in ("astra", "gpt-6-astra"):
        assert provider.max_effort(model) == "ultra"
        effort = provider.clamp_effort("ultra", model)
        assert provider.command("hi", model, effort, []) == [
            sys.executable, "exec", "--json", "-m", "gpt-6-astra",
            "-c", 'model_reasoning_effort="ultra"', "--", "hi",
        ]  # fmt: skip


def test_heartbeat_health_is_read_only_and_respects_disabling(enso_home, raw_config, unit):
    healthy(enso_home, raw_config)
    assert doctor.run(enso_home).section("heartbeat").status == "ok"
    assert not enso_home.db.exists()
    config = load_config(enso_home)
    beat = heartbeat.create(
        config,
        {
            "title": "Watch the refund",
            "instructions": "Read refund progress",
            "completion": "Money arrived",
            "allowed_actions": "Read only",
            "workspace": "default",
            "schedule": "0 * * * *",
            "llm_checks": True,
        },
    )
    heartbeat.resume(config, beat.ref)
    heartbeat.record_check(config, beat.ref, "error", error="Mail source unavailable")
    before = len(heartbeat.history(enso_home, beat.ref))
    section = doctor.run(enso_home).section("heartbeat")
    assert section.status == "error" and section.details["attention"] == [beat.ref]
    assert beat.ref in section.problems[0]
    assert len(heartbeat.history(enso_home, beat.ref)) == before
    raw_config["heartbeat"] = {"enabled": False}
    write_config(enso_home, raw_config)
    disabled = doctor.run(enso_home).section("heartbeat")
    assert disabled.status == "ok" and disabled.note == "disabled; 1 saved beats"
    assert disabled.details["attention"] == [beat.ref]


def test_a_fresh_home_reports_and_skips(enso_home: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service,
        "status",
        lambda platform=None, **kwargs: service.Status(
            "systemd", Path("/nowhere/enso.service"), False, False, None
        ),
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None if name == "git" else "/bin/x")

    report = doctor.run(enso_home)

    assert not report.ok
    assert {s.name: s.status for s in report.sections} == {
        "config": "error",
        "home": "error",
        "workspaces": "error",
        "providers": "skipped",
        "transports": "skipped",
        "service": "warning",
        "viewer_service": "ok",
        "jobs": "skipped",
        "heartbeat": "skipped",
    }
    assert report.section("config").problems == [
        f"{enso_home.config} is missing; run `enso setup` first"
    ]
    home = report.section("home")
    assert (
        home.problems[0].startswith("the home is not a Git root")
        and "(git is not on PATH)" in home.problems[0]
    )
    assert home.warnings == ["git is not on PATH; the provider CLIs and `--fix` expect it"]
    assert home.details == {"path": str(enso_home.home), "git_root": False, "git": None}
    assert report.section("workspaces").problems[0] == (
        "default: skills/ is missing (repairable with `enso workspace audit --fix`)"
    )
    assert report.section("providers").summary == "skipped"
    assert report.section("service").note == "systemd, not installed"
    assert report.section("service").warnings[0].startswith("the service is not installed")


def test_a_degraded_home_names_each_problem(
    enso_home: Paths, raw_config_both: dict, unit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_config_both["providers"]["grok"]["path"] = "/nowhere/grok"
    healthy(enso_home, raw_config_both)
    write_job(enso_home, "broken", model="gpt")
    monkeypatch.setattr(doctor, "EXTRAS", {"slack": "slack_bolt", "telegram": "no_such_module"})
    unit.write_text("<string>/somewhere/else/enso</string>")

    report = doctor.run(enso_home)

    assert not report.ok
    assert report.section("config").warnings == [
        "providers.grok.path '/nowhere/grok' is not an executable on this machine"
    ]
    assert report.section("providers").problems == [
        "grok: /nowhere/grok is not an executable on this machine"
    ]
    assert report.section("transports").problems == [
        "telegram is configured but its extra is not installed; "
        "reinstall with `uv tool install -e './enso[telegram]'`"
    ]
    assert report.section("service").status == "warning"
    assert report.section("service").warnings == [
        f"the unit does not run {BINARY}; run `enso service install` after an upgrade"
    ]
    assert report.section("jobs").problems == [
        f"broken ({enso_home.jobs / 'broken' / 'JOB.md'}): "
        "JOB.md.model 'gpt' is not in providers.claude.models"
    ]
    assert report.section("jobs").note == "1 job, 1 enabled"

    monkeypatch.setattr(
        service,
        "status",
        lambda platform=None, **kwargs: service.Status("launchd", unit, True, True, None),
    )
    stopped = doctor.run(enso_home).section("service")
    assert stopped.note == "launchd, loaded but stopped" and stopped.status == "error"
    assert stopped.problems == [
        "the service is installed but not running; `enso service start`, then `enso logs`"
    ]


def test_a_bad_schedule_names_its_file_and_changes_nothing(
    enso_home: Paths, raw_config: dict, unit: Path
) -> None:
    """The report has to be enough to repair the file by hand; doctor never edits it."""
    healthy(enso_home, raw_config)
    path = write_job(enso_home, "hourly", schedule="@hourly")
    before = path.read_bytes()

    section = doctor.run(enso_home).section("jobs")

    assert section.problems == [
        f"hourly ({path}): schedule '@hourly' must be exactly five fields, "
        "minute hour day-of-month month day-of-week; Enso schedules at minute resolution, "
        "so a seconds or year field and aliases such as @daily are not accepted"
    ]
    # The nightly enso-audit agent reads exactly this JSON, so the path must survive it.
    payload = json.loads(json.dumps(doctor.run(enso_home).as_dict()))
    assert payload["sections"][7]["problems"] == section.problems
    assert path.read_bytes() == before


def test_no_service_manager_is_a_warning(enso_home: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    def unsupported(platform: str | None = None, **kwargs) -> service.Status:
        raise service.ServiceError("no service manager on win32; run `enso serve` yourself")

    monkeypatch.setattr(service, "status", unsupported)
    section = doctor.run(enso_home).section("service")
    assert section.status == "warning" and section.details == {}
    assert section.warnings == ["no service manager on win32; run `enso serve` yourself"]


def test_doctor_command(enso_home: Paths, raw_config: dict, unit: Path) -> None:
    healthy(enso_home, raw_config)
    runner = CliRunner()

    fine = runner.invoke(app, ["doctor"])
    assert fine.exit_code == 0, fine.output
    assert fine.stdout.splitlines() == [
        f"config: ok ({enso_home.config})",
        f"home: ok (Git root at {enso_home.home})",
        "workspaces: ok (default)",
        "providers: ok (claude, codex, grok)",
        "transports: ok (slack)",
        "service: ok (launchd, running pid 42)",
        "viewer_service: ok (not installed (optional))",
        "jobs: ok (none yet)",
        "heartbeat: ok (0 active, 0 paused)",
    ]

    shutil.rmtree(enso_home.workspace("default") / "drafts")
    workspaces.create_workspace(enso_home, "lonely")
    broken = runner.invoke(app, ["doctor"])
    assert broken.exit_code == 1
    lines = broken.stdout.splitlines()
    assert lines[2:4] == [
        "workspaces: 1 error, 2 warnings (default, lonely)",
        "  error: default: drafts/ is missing (repairable with `enso workspace audit --fix`)",
    ]
    assert lines[4].startswith("  warning: lonely: AGENTS.md is still the untouched template")
    as_json = runner.invoke(app, ["doctor", "--json"])
    payload = json.loads(as_json.stdout)
    assert as_json.exit_code == 1 and not payload["ok"]
    assert [s["status"] for s in payload["sections"]] == [
        "ok",
        "ok",
        "error",
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
    ]
