"""``enso doctor``: every section on a healthy home, a broken one, and a degraded one."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from conftest import write_config, write_job
from typer.testing import CliRunner

from enso import doctor, heartbeat, knowledge, service, workspaces
from enso.cli import app
from enso.config import Paths, load_config
from enso.transport_registry import TRANSPORTS

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


def test_doctor_and_config_check_fail_without_unbound_default(enso_home, raw_config, unit):
    raw_config["bindings"] = {}
    healthy(enso_home, raw_config)
    enso_home.workspace("default").rename(enso_home.workspace("personal"))

    for command in (("config", "check"), ("doctor",)):
        result = CliRunner().invoke(app, [*command, "--json"])
        report = json.loads(result.stdout)
        assert result.exit_code == 1 and not report["ok"]
        assert "required operator workspace 'default'" in result.stdout


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
        "0 notes, 0 findings",
    ]
    payload = json.loads(json.dumps(report.as_dict()))
    assert payload["ok"] and payload["home"] == str(enso_home.home)
    assert payload["sections"][3] == {
        "name": "providers",
        "status": "ok",
        "note": "claude, codex, grok",
        "problems": [],
        "warnings": [],
        "attention": False,
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
    assert payload["sections"][7]["details"] == {
        "jobs": ["default:nightly"],
        "enabled": ["default:nightly"],
    }


def test_fresh_two_workspace_home_with_bundled_jobs_passes_doctor(
    enso_home: Paths, raw_config: dict, unit: Path
) -> None:
    raw_config["bindings"]["slack:C2"] = "team"
    healthy(enso_home, raw_config)
    workspaces.create_workspace(enso_home, "team")
    (enso_home.workspace("team") / "AGENTS.md").write_text("# team\n")
    config = load_config(enso_home)
    workspaces.seed_jobs(enso_home, config.defaults)

    report = doctor.run(enso_home)

    assert report.ok
    assert report.section("workspaces").status == "ok"
    assert report.section("jobs").details["jobs"] == [
        "default:enso-audit",
        "default:enso-memory",
        "default:enso-update",
    ]


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
        "knowledge": "ok",
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
    assert home.details == {
        "path": str(enso_home.home),
        "git_root": False,
        "git": None,
        "layout": {".migrations.json": "managed", "workspaces": "required"},
    }
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
    monkeypatch.setattr(TRANSPORTS["telegram"], "module", "no_such_module")
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
        "the release must include the telegram extra; "
        "for a checkout run `uv sync --all-extras --locked`"
    ]
    assert report.section("service").status == "warning"
    assert report.section("service").warnings == [
        f"the unit does not run {BINARY}; run `enso service install` after an upgrade"
    ]
    assert report.section("jobs").problems == [
        f"default:broken ({enso_home.workspace_jobs('default') / 'broken' / 'JOB.md'}): "
        "JOB.md.agent.model 'gpt' is not in providers.claude.models"
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


def test_note_audits_report_paths_and_counts_without_writes(enso_home, raw_config, unit):
    healthy(enso_home, raw_config)
    reference = knowledge.create_note(enso_home, "shared", "Reference.md", "Current facts.")
    assert doctor.run(enso_home).ok
    root = enso_home.knowledge
    original = root / reference.path
    # Duplicate identity, unsupported schema, and broken link.
    (root / "Copy.md").write_text(original.read_text() + "\n[missing](Gone.md)\n")
    (root / "Wrong.md").write_text(original.read_text().replace("enso.note/v1", "other.note/v1"))
    (root / "Corrupt.md").write_bytes(b"\xff")
    before = {p: p.read_bytes() for p in enso_home.home.rglob("*") if p.is_file()}
    report = doctor.run(enso_home)
    assert not report.ok and not enso_home.db.exists()
    catalog = knowledge.scan(enso_home)
    section = report.section("knowledge")
    assert section.details["notes"] == len(catalog.notes)
    assert section.details["findings"] == len(catalog.audit())
    assert section.status == "error"
    assert "enso knowledge audit" in section.note
    errors = "\n".join(section.problems)
    assert str(root / "Copy.md") in errors
    assert all(text in errors for text in ("duplicate note id", "missing link"))
    assert "schema must be enso.note/v1" in errors
    assert catalog.get("Reference.md", "shared").body == "Current facts."
    assert before == {p: p.read_bytes() for p in enso_home.home.rglob("*") if p.is_file()}
    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1 and json.loads(result.stdout) == report.as_dict()


def test_doctor_bounds_note_findings_and_scoped_audit_keeps_details(enso_home, raw_config, unit):
    healthy(enso_home, raw_config)
    for index in range(20):
        (enso_home.knowledge / f"{index:02}.md").write_text("Missing metadata.")
    report = doctor.run(enso_home)
    section = report.section("knowledge")
    assert section.details["notes"] == 20 and section.details["findings"] == 20
    assert len(section.problems) == 11 and section.problems[-1].startswith("10 more findings")
    detailed = CliRunner().invoke(app, ["knowledge", "audit", "--shared", "--json"])
    assert detailed.exit_code == 1 and len(json.loads(detailed.stdout)["problems"]) == 20


def test_doctor_reports_invalid_note_roots_without_following_links(
    enso_home, raw_config, unit, tmp_path
):
    healthy(enso_home, raw_config)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secret.md").write_text("This must not be read.")
    root = enso_home.workspace("default") / "knowledge"
    if root.exists():
        root.rmdir()
    root.symlink_to(outside, target_is_directory=True)
    report = doctor.run(enso_home)
    section = report.section("knowledge")
    assert section.status == "error" and section.details["notes"] == 0
    assert "symbolic link" in "\n".join(section.problems)


def test_attention_separates_what_is_worth_reporting_from_what_is_unhealthy(
    enso_home: Paths, raw_config: dict, unit: Path
) -> None:
    """The nightly audit's selection: a healthy home stays quiet unless it needs tidying."""
    healthy(enso_home, raw_config)
    runner = CliRunner()

    quiet = doctor.run(enso_home)
    assert quiet.ok and not quiet.attention
    assert not any(section.attention for section in quiet.sections)

    # A matter of taste: reported, but never enough on its own to wake the job.
    workspaces.create_workspace(enso_home, "lonely")
    taste = doctor.run(enso_home)
    assert taste.ok and not taste.attention
    assert taste.section("workspaces").warnings and not taste.section("workspaces").attention

    # A portable fact about the layout: still healthy, now worth a report.
    (enso_home.home / "leftover.tar.gz").write_bytes(b"")
    report = doctor.run(enso_home)
    assert report.ok and report.attention
    home = report.section("home")
    assert home.status == "warning" and home.attention
    assert home.warnings == (
        [f"leftover.tar.gz is not part of the layout; move it out of {enso_home.home} or remove it"]
    )
    assert home.details["layout"]["leftover.tar.gz"] == "unexpected"

    plain = runner.invoke(app, ["doctor"])
    assert plain.exit_code == 0, plain.output  # ordinary semantics are untouched
    gated = runner.invoke(app, ["doctor", "--json", "--attention"])
    payload = json.loads(gated.stdout)
    assert gated.exit_code == 1 and payload["ok"] and payload["attention"]

    enso_home.config.chmod(0o644)
    loose = runner.invoke(app, ["doctor", "--json", "--attention"])
    assert loose.exit_code == 1 and json.loads(loose.stdout)["ok"]


def test_attention_is_true_for_every_health_problem(enso_home: Paths, raw_config: dict) -> None:
    """A health problem is always selected for reporting."""
    healthy(enso_home, raw_config)
    shutil.rmtree(enso_home.workspace("default") / "uploads")

    report = doctor.run(enso_home)

    assert not report.ok and report.attention
    assert CliRunner().invoke(app, ["doctor", "--attention"]).exit_code == 1


@pytest.fixture
def notification_transport(monkeypatch):
    from conftest import FakeTransport

    transport = FakeTransport()
    monkeypatch.setattr(TRANSPORTS["slack"], "build", lambda config: transport)
    return transport


def test_notify_stays_quiet_for_healthy_and_benign_warnings(
    enso_home, raw_config, unit, notification_transport
):
    healthy(enso_home, raw_config)
    workspaces.create_workspace(enso_home, "lonely")
    result = CliRunner().invoke(app, ["doctor", "--attention", "--notify", "--quiet"])
    assert result.exit_code == 0 and result.output == ""
    assert notification_transport.sent == [] and not enso_home.db.exists()
    encoded = CliRunner().invoke(app, ["doctor", "--attention", "--notify", "--quiet", "--json"])
    payload = json.loads(encoded.stdout)
    assert encoded.exit_code == 0 and payload["ok"] and not payload["notified"]
    assert not payload["attention"]


def test_notify_selects_exact_attention_warnings_and_repeats_without_an_agent(
    enso_home, raw_config, unit, notification_transport
):
    healthy(enso_home, raw_config)
    workspaces.create_workspace(enso_home, "lonely")
    (enso_home.home / "leftover.tar.gz").write_bytes(b"")
    # Without --attention, warnings remain informational and nothing is sent.
    ordinary = CliRunner().invoke(app, ["doctor", "--notify", "--quiet"])
    assert ordinary.exit_code == 0 and notification_transport.sent == []
    for _ in range(2):
        result = CliRunner().invoke(app, ["doctor", "--attention", "--notify", "--quiet"])
        assert result.exit_code == 0 and result.output == ""
    assert len(notification_transport.sent) == 2
    target, text = notification_transport.sent[0]
    assert target == "C1"
    assert text.startswith("**Enso audit**\n**Status:** Healthy; 1 warning.")
    assert "leftover.tar.gz" in text and "lonely" not in text
    assert "**Details**" in text and "**Action:** Run `enso doctor --attention`" in text


def test_notify_reports_errors_to_configured_target_and_records_outbox(
    enso_home, raw_config, unit, notification_transport, monkeypatch
):
    from enso import messages

    healthy(enso_home, raw_config)
    workspaces.create_workspace(enso_home, "team")
    (enso_home.workspace("team") / "AGENTS.md").write_text("# Team\n")
    shutil.rmtree(enso_home.workspace("default") / "uploads")
    monkeypatch.setenv("ENSO_ORIGIN_TRANSPORT", "slack")
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "Cother")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "1.2")
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
    monkeypatch.setenv("ENSO_JOB", "default:enso-audit")
    result = CliRunner().invoke(
        app, ["doctor", "--notify", "--quiet", "--json", "--workspace", "team"]
    )
    payload = json.loads(result.stdout)
    assert result.exit_code == 0 and not payload["ok"] and payload["notified"]
    target, text = notification_transport.sent[0]
    assert target == "C1" and "**Status:** 1 error" in text
    assert doctor.FIXABLE_MARK in text
    message = messages.list_messages(enso_home, 1)[0]
    assert (message.workspace, message.target, message.thread, message.status) == (
        "team",
        "C1",
        None,
        "sent",
    )
    assert message.source == "job:default:enso-audit" and message.text == text


def test_notify_delivery_failure_stays_nonzero_when_quiet_and_retry_is_possible(
    enso_home, raw_config, unit, notification_transport, monkeypatch
):
    from enso import messages

    healthy(enso_home, raw_config)
    shutil.rmtree(enso_home.workspace("default") / "uploads")
    original = notification_transport.send

    async def failed(*args, **kwargs):
        raise RuntimeError("delivery unavailable")

    monkeypatch.setattr(notification_transport, "send", failed)
    result = CliRunner().invoke(app, ["doctor", "--notify", "--quiet"])
    assert result.exit_code == 1 and result.stdout == ""
    assert "delivery unavailable" in result.stderr
    assert messages.list_messages(enso_home, 1)[0].status == "failed"
    monkeypatch.setattr(notification_transport, "send", original)
    retry = CliRunner().invoke(app, ["doctor", "--notify", "--quiet"])
    assert retry.exit_code == 0 and retry.output == ""
    assert messages.list_messages(enso_home, 1)[0].status == "sent"


def test_notify_requires_a_target_only_when_there_are_findings(enso_home, raw_config, unit):
    raw_config["transports"]["slack"].pop("notify")
    healthy(enso_home, raw_config)
    assert CliRunner().invoke(app, ["doctor", "--notify", "--quiet"]).exit_code == 0
    shutil.rmtree(enso_home.workspace("default") / "uploads")
    failed = CliRunner().invoke(app, ["doctor", "--notify", "--quiet"])
    assert failed.exit_code == 1 and "no notification target configured" in failed.stderr


def test_notify_inspection_failure_is_not_a_successful_quiet_check(enso_home, monkeypatch):
    def failed(paths):
        raise OSError("could not read home")

    monkeypatch.setattr(doctor, "run", failed)
    result = CliRunner().invoke(app, ["doctor", "--notify", "--quiet", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "ok": False,
        "error": "could not complete health audit: could not read home",
    }


def test_notification_bounds_findings_and_shows_errors_before_warnings():
    report = doctor.Report(
        Path("/home/.enso"),
        [
            doctor.Section("home", attention_warnings=["tidy this"]),
            doctor.Section("jobs", problems=[f"{index}: " + "x" * 1000 for index in range(12)]),
        ],
    )
    text = doctor.notification_message(report, attention=True)
    assert "**Status:** 12 errors, 1 warning" in text
    assert text.count("- jobs:") == 8 and "tidy this" not in text
    assert "5 more findings in the full report." in text
    assert len(text) < 3200
    assert all(len(line) <= 352 for line in text.splitlines() if line.startswith("- jobs:"))
