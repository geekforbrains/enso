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
    policies = base / "policies" / "acme" / "claude"
    for d in (ops, acme, policies):
        d.mkdir(parents=True, exist_ok=True)
    settings = policies / "settings.json"
    settings.write_text(json.dumps({"sandbox": {"enabled": True}, "disableAllHooks": True}))
    settings.chmod(0o600)
    return {
        "working_dir": str(base / "workspace"),
        "transport": "slack",
        "transports": {"slack": {"bot_token": "x", "app_token": "x"}},
        "groups": {"team": {"slack": ["U02DEV"]}},
        "workspaces": {
            "ops": {
                "path": str(ops),
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
            "acme": {
                "path": str(acme),
                "policy_dir": str(base / "policies" / "acme"),
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": ["status"],
            },
        },
        "routes": {
            "slack": {
                "account_id": "T1",
                "dms": {},
                "channels": {
                    "C1": {"allow": ["team"], "workspace": "acme", "audit": True},
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
    Path(tmp_enso, "policies", "acme", "claude", "settings.json").unlink()
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


def test_route_explain_unknown_user(tmp_enso):
    save_config(_teams_config(tmp_enso))
    result = runner.invoke(app, ["route", "explain", "slack", "UNOBODY", "C1"])
    assert result.exit_code == 0
    assert "silent" in result.output


def test_audit_tail_and_export(tmp_enso):
    save_config(_teams_config(tmp_enso))
    audit.create_turn(
        account_id="T1", delivery_id="d1", route_id="slack.channel.C1",
        channel_id="C1", source_message_id="1.1", conversation_id="C1:1.1",
        user_id="U02DEV", request_text="hello there", decision="accepted",
    )
    result = runner.invoke(app, ["audit", "tail"])
    assert result.exit_code == 0
    assert "U02DEV" in result.output
    assert "accepted" in result.output

    result = runner.invoke(app, ["audit", "export"])
    assert result.exit_code == 0
    row = json.loads(result.output.strip().splitlines()[-1])
    assert row["user_id"] == "U02DEV"


def test_message_send_refuses_audited_channel(tmp_enso, monkeypatch):
    import pytest
    import typer

    from enso.cli import _refuse_audited_slack_target

    config = _teams_config(tmp_enso)
    with pytest.raises(typer.Exit):
        _refuse_audited_slack_target(config, "C1")
    # Unaudited channels and legacy configs pass through.
    _refuse_audited_slack_target(config, "C2")
    del config["routes"]
    _refuse_audited_slack_target(config, "C1")


def test_message_send_refuses_dm_when_any_dm_route_audited(tmp_enso):
    import pytest
    import typer

    from enso.cli import _refuse_audited_slack_target

    config = _teams_config(tmp_enso)
    config["routes"]["slack"]["dms"] = {
        "pm": {"allow": ["team"], "workspace": "acme", "audit": True},
    }
    with pytest.raises(typer.Exit):
        _refuse_audited_slack_target(config, "D12345")








def test_install_teams_workspaces_links_shared_skill_root(tmp_enso):
    """No allowlist: each workspace gets the shared root where the CLIs look."""
    config = _teams_config(tmp_enso)
    skills_root = Path(tmp_enso, "skills")
    (skills_root / "docs").mkdir(parents=True)

    Runtime(config).install_teams_workspaces()

    for ws in ("ops", "acme"):
        for cli_dir in (".claude", ".agents"):
            link = Path(tmp_enso, "workspaces", ws, cli_dir, "skills")
            assert link.is_symlink(), f"{ws}/{cli_dir} not linked"
            assert (link / "docs").is_dir()
