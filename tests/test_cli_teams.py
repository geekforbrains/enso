"""Tests for routed configuration checks, route explain, and audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enso import audit
from enso.cli import app
from enso.config import save_config
from enso.core import Runtime

runner = CliRunner()


def _teams_config(tmp_enso: str) -> dict:
    base = Path(tmp_enso)
    ops = base / "workspaces" / "ops"
    acme = base / "workspaces" / "acme"
    policies = base / "policies" / "client" / "claude"
    for d in (ops, acme, policies):
        d.mkdir(parents=True, exist_ok=True)
    settings = policies / "settings.json"
    settings.write_text(json.dumps({"sandbox": {"enabled": True}, "disableAllHooks": True}))
    settings.chmod(0o600)
    return {
        "working_dir": str(base / "workspace"),
        "transport": "slack",
        "transports": {"slack": {"bot_token": "x", "app_token": "x"}},
        "workspaces": {
            "ops": {"path": str(ops)},
            "acme": {"path": str(acme)},
        },
        "access": {
            "admin": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
            "client": {
                "policy_dir": str(base / "policies" / "client"),
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": ["status"],
            },
        },
        "routes": {
            "slack": {
                "account_id": "T1",
                "dms": {
                    "U01ADMIN": {"workspace": "ops", "access": "admin"},
                },
                "channels": {
                    "C1": {
                        "workspace": "acme",
                        "access": "client",
                        "audit": True,
                    },
                },
            }
        },
    }


def test_config_check_passes_valid_config(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output


def test_config_check_fails_on_missing_policy(tmp_enso):
    config = _teams_config(tmp_enso)
    Path(tmp_enso, "policies", "client", "claude", "settings.json").unlink()
    save_config(config)
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 1
    assert "claude" in result.output


def test_config_check_validates_catalog_without_slack_routes(tmp_enso):
    config = _teams_config(tmp_enso)
    del config["routes"]
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": ["123"],
            "notify_channel": "123",
        }
    }
    save_config(config)
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0
    assert "All checks passed" in result.output


@pytest.mark.parametrize(
    "allowed_users",
    [
        [123],
        ["123", "123"],
        ["123", "0"],
        ["123", "invalid"],
    ],
)
def test_config_check_rejects_malformed_telegram_allowlist(
    tmp_enso,
    allowed_users,
):
    config = _teams_config(tmp_enso)
    del config["routes"]
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": allowed_users,
            "notify_channel": "123",
        },
    }
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "allowed_users must be a non-empty" in result.output


def test_config_check_rejects_telegram_alias_with_valid_allowlist(tmp_enso):
    config = _teams_config(tmp_enso)
    del config["routes"]
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": ["123"],
            "allowed_user_ids": [123],
            "notify_channel": "123",
        },
    }
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "allowed_user_ids is no longer supported" in result.output


def test_config_check_validates_inactive_configured_telegram(tmp_enso):
    config = _teams_config(tmp_enso)
    config["transports"]["telegram"] = {
        "bot_token": "x",
        "allowed_users": ["123", "0"],
        "allowed_user_ids": [123],
    }
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "allowed_user_ids is no longer supported" in result.output
    assert "allowed_users must be a non-empty" in result.output


def test_config_check_validates_inactive_configured_slack(tmp_enso):
    config = _teams_config(tmp_enso)
    del config["routes"]
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": ["123"],
            "notify_channel": "123",
        },
        "slack": {
            "bot_token": "x",
            "app_token": "x",
            "allowed_users": ["U01ADMIN"],
        },
    }
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "transports.slack.allowed_users is no longer supported" in result.output
    assert "routes.slack is required" in result.output


def test_config_check_reports_jobs_missing_execution_binding(tmp_enso):
    config = _teams_config(tmp_enso)
    save_config(config)
    job_dir = Path(tmp_enso, "jobs", "old-job")
    job_dir.mkdir(parents=True)
    (job_dir / "JOB.md").write_text(
        "---\n"
        "name: Old job\n"
        'schedule: "0 9 * * *"\n'
        "provider: claude\n"
        "model: opus\n"
        "---\n\n"
        "Do work.\n"
    )

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "jobs.old-job" in result.output
    assert "workspace" in result.output
    assert "access" in result.output


def test_removed_policy_check_is_not_advertised(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["policy", "check"])
    assert result.exit_code != 0


def test_route_explain_authorized(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["route", "explain", "slack", "U02DEV", "C1"])
    assert result.exit_code == 0, result.output
    assert "authorized" in result.output
    assert "slack.channel.C1" in result.output
    assert "Audit: on" in result.output


def test_route_explain_channel_authorizes_every_member(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["route", "explain", "slack", "UNOBODY", "C1"])
    assert result.exit_code == 0
    assert "authorized" in result.output


def test_route_explain_dm_requires_exact_user_id(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["route", "explain", "slack", "UNOBODY"])
    assert result.exit_code == 0
    assert "unconfigured" in result.output


def test_audit_tail_and_export(tmp_enso):
    save_config(_teams_config(tmp_enso))
    audit.create_turn(
        account_id="T1",
        delivery_id="d1",
        route_id="slack.channel.C1",
        channel_id="C1",
        source_message_id="1.1",
        conversation_id="C1:1.1",
        user_id="U02DEV",
        request_text="hello there",
        decision="accepted",
    )
    result = runner.invoke(app, ["audit", "tail"])
    assert result.exit_code == 0
    assert "U02DEV" in result.output
    assert "accepted" in result.output

    result = runner.invoke(app, ["audit", "export"])
    assert result.exit_code == 0
    row = json.loads(result.output.strip().splitlines()[-1])
    assert row["user_id"] == "U02DEV"


def test_install_workspaces_keeps_instructions_local(tmp_enso):
    """Named workspaces get local instructions, not global skill links."""
    config = _teams_config(tmp_enso)
    skills_root = Path(tmp_enso, "skills")
    (skills_root / "docs").mkdir(parents=True)

    Runtime(config).install_workspaces()

    for ws in ("ops", "acme"):
        root = Path(tmp_enso, "workspaces", ws)
        assert (root / "AGENTS.md").is_file()
        instructions = (root / "AGENTS.md").read_text()
        assert "knowledge/" in instructions
        assert "drafts/" in instructions
        assert "uploads/<random-id>/" in instructions
        assert "control files" in instructions
        assert (root / "CLAUDE.md").is_symlink()
        for cli_dir in (".claude", ".agents"):
            assert not (root / cli_dir / "skills").exists()
