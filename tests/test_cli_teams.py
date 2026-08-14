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
        "transport": "slack",
        "transports": {
            "slack": {
                "bot_token": "x",
                "app_token": "x",
                "account_id": "T1",
                "dms": {
                    "U01ADMIN": {"workspace": "ops"},
                },
                "channels": {
                    "C1": {
                        "workspace": "acme",
                        "audit": True,
                    },
                },
            }
        },
        "workspaces": {
            "ops": {"path": str(ops), "policy": "admin"},
            "acme": {"path": str(acme), "policy": "client"},
        },
        "policies": {
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
    }


def test_config_check_passes_valid_config(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output
    assert "Shared instructions" in result.output
    assert not Path(tmp_enso, "runtime").exists()


def test_config_check_fails_when_shared_instructions_are_missing(tmp_enso):
    Path(tmp_enso, "AGENTS.md").unlink()
    save_config(_teams_config(tmp_enso))

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "shared instruction file is missing" in result.output


def test_config_check_fails_on_missing_policy(tmp_enso):
    config = _teams_config(tmp_enso)
    Path(tmp_enso, "policies", "client", "claude", "settings.json").unlink()
    save_config(config)
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 1
    assert "claude" in result.output


def _isolate_secrets(tmp_enso, monkeypatch) -> Path:
    """Point the config-check secrets scan away from the developer's ~/.enso."""
    secrets = Path(tmp_enso) / "secrets"
    monkeypatch.setattr("enso.cli.SECRETS_DIR", str(secrets))
    return secrets


def test_config_check_prints_resolvable_passthrough_names(tmp_enso, monkeypatch):
    _isolate_secrets(tmp_enso, monkeypatch)
    monkeypatch.setenv("CLIENT_METRICS_TOKEN", "tok")
    config = _teams_config(tmp_enso)
    config["policies"]["client"]["env_passthrough"] = ["CLIENT_METRICS_TOKEN"]
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert "env_passthrough:" in plain
    assert "✓ CLIENT_METRICS_TOKEN" in plain
    assert "the service environment may differ" in plain


def test_config_check_marks_unset_passthrough_name_without_failing(tmp_enso, monkeypatch):
    _isolate_secrets(tmp_enso, monkeypatch)
    monkeypatch.delenv("CLIENT_METRICS_TOKEN", raising=False)
    config = _teams_config(tmp_enso)
    config["policies"]["client"]["env_passthrough"] = ["CLIENT_METRICS_TOKEN"]
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert "! CLIENT_METRICS_TOKEN not set" in plain


def test_config_check_resolves_passthrough_from_secrets_files(tmp_enso, monkeypatch):
    secrets = _isolate_secrets(tmp_enso, monkeypatch)
    secrets.mkdir()
    (secrets / "tokens.env").write_text("CLIENT_METRICS_TOKEN=tok\n")
    monkeypatch.delenv("CLIENT_METRICS_TOKEN", raising=False)
    config = _teams_config(tmp_enso)
    config["policies"]["client"]["env_passthrough"] = ["CLIENT_METRICS_TOKEN"]
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert "✓ CLIENT_METRICS_TOKEN" in plain
    assert "not set" not in plain


def test_config_check_lists_mcp_servers_on_native_launch_line(tmp_enso):
    config = _teams_config(tmp_enso)
    policies = Path(tmp_enso) / "policies" / "client" / "claude"
    settings = policies / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "sandbox": {"enabled": True},
                "disableAllHooks": True,
                "permissions": {"allow": ["mcp__metrics__query", "mcp__tickets__list"]},
            }
        )
    )
    settings.chmod(0o600)
    mcp = policies / "mcp.json"
    mcp.write_text(
        json.dumps({"mcpServers": {"tickets": {"type": "http"}, "metrics": {"type": "http"}}})
    )
    mcp.chmod(0o600)
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert "mcp: metrics, tickets" in plain


def test_config_check_surfaces_mcp_cross_check_warnings(tmp_enso):
    config = _teams_config(tmp_enso)
    policies = Path(tmp_enso) / "policies" / "client" / "claude"
    settings = policies / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "sandbox": {"enabled": True},
                "disableAllHooks": True,
                "permissions": {"allow": ["mcp__ghost__tool"]},
            }
        )
    )
    settings.chmod(0o600)
    mcp = policies / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"metrics": {"type": "http"}}}))
    mcp.chmod(0o600)
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert 'permission rule "mcp__ghost__tool" matches no MCP server' in plain
    assert 'no allow rule references MCP server "metrics"' in plain


def test_config_check_validates_catalog_without_slack_routes(tmp_enso):
    config = _teams_config(tmp_enso)
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": ["123"],
            "notify_channel": "123",
            "workspace": "ops",
        }
    }
    save_config(config)
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0
    assert "All checks passed" in result.output
    assert "ops → admin" in result.output


@pytest.mark.parametrize("workspace", [None, "missing"])
def test_config_check_rejects_invalid_telegram_workspace(tmp_enso, workspace):
    config = _teams_config(tmp_enso)
    config["transport"] = "telegram"
    telegram = {
        "bot_token": "x",
        "allowed_users": ["123"],
    }
    if workspace is not None:
        telegram["workspace"] = workspace
    config["transports"] = {"telegram": telegram}
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "transports.telegram.workspace" in result.output


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
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": allowed_users,
            "notify_channel": "123",
            "workspace": "ops",
        },
    }
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "allowed_users must be a non-empty" in result.output


def test_config_check_rejects_telegram_alias_with_valid_allowlist(tmp_enso):
    config = _teams_config(tmp_enso)
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": ["123"],
            "allowed_user_ids": [123],
            "notify_channel": "123",
            "workspace": "ops",
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
        "workspace": "ops",
    }
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "allowed_user_ids is no longer supported" in result.output
    assert "allowed_users must be a non-empty" in result.output


def test_config_check_validates_inactive_configured_slack(tmp_enso):
    config = _teams_config(tmp_enso)
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": ["123"],
            "notify_channel": "123",
            "workspace": "ops",
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
    assert "transports.slack.account_id is required" in result.output


def test_config_check_rejects_legacy_top_level_routes_when_slack_is_inactive(tmp_enso):
    config = _teams_config(tmp_enso)
    config["transport"] = "telegram"
    config["transports"] = {
        "telegram": {
            "bot_token": "x",
            "allowed_users": ["123"],
            "notify_channel": "123",
            "workspace": "ops",
        }
    }
    config["routes"] = {"slack": {"account_id": "T1"}}
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "routes is no longer supported" in result.output
    assert "transports.slack" in result.output


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


def test_route_explain_shows_effective_response_triggers(tmp_enso):
    config = _teams_config(tmp_enso)
    config["transports"]["slack"]["channels"]["C1"]["mention_required"] = False
    save_config(config)
    result = runner.invoke(app, ["route", "explain", "slack", "U02DEV", "C1"])
    assert result.exit_code == 0, result.output
    assert "Mention required: no" in result.output
    assert "Thread mention required: yes" in result.output


def test_route_explain_omits_response_triggers_for_dms(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["route", "explain", "slack", "U01ADMIN"])
    assert result.exit_code == 0, result.output
    assert "authorized" in result.output
    assert "Mention required" not in result.output


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


def test_install_workspaces_writes_nothing_when_catalog_topology_is_invalid(tmp_enso):
    config = _teams_config(tmp_enso)
    policy_root = Path(config["policies"]["client"]["policy_dir"])
    config["workspaces"]["acme"]["path"] = str(policy_root)
    ops_root = Path(config["workspaces"]["ops"]["path"])

    Runtime(config).install_workspaces()

    assert not (policy_root / "AGENTS.md").exists()
    assert not (policy_root / "CLAUDE.md").exists()
    assert not (policy_root / "uploads").exists()
    assert not (ops_root / "AGENTS.md").exists()
