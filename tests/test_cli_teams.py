"""Tests for the teams CLI surface: policy check, route explain, audit."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_policy_check_passes_valid_config(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["policy", "check"])
    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output


def test_policy_check_fails_on_missing_policy(tmp_enso):
    config = _teams_config(tmp_enso)
    Path(tmp_enso, "policies", "client", "claude", "settings.json").unlink()
    save_config(config)
    result = runner.invoke(app, ["policy", "check"])
    assert result.exit_code == 1
    assert "claude" in result.output


def test_policy_check_without_teams_mode(tmp_enso):
    config = _teams_config(tmp_enso)
    del config["routes"]
    save_config(config)
    result = runner.invoke(app, ["policy", "check"])
    assert result.exit_code == 0
    assert "not configured" in result.output


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


def test_install_teams_workspaces_keeps_instructions_local(tmp_enso):
    """Teams workspaces get local instructions, not global skill links."""
    config = _teams_config(tmp_enso)
    skills_root = Path(tmp_enso, "skills")
    (skills_root / "docs").mkdir(parents=True)

    Runtime(config).install_teams_workspaces()

    for ws in ("ops", "acme"):
        root = Path(tmp_enso, "workspaces", ws)
        assert (root / "AGENTS.md").is_file()
        assert (root / "CLAUDE.md").is_symlink()
        for cli_dir in (".claude", ".agents"):
            assert not (root / cli_dir / "skills").exists()
