"""Tests for routed configuration checks, route explain, and audit."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enso import audit
from enso.cli import app
from enso.config import save_config
from enso.repository import EnsoRepository
from enso.scaffolding import ScaffoldService

runner = CliRunner()


def _teams_config(tmp_enso: str) -> dict:
    base = Path(tmp_enso)
    repository = EnsoRepository()
    repository.ensure()
    scaffold = ScaffoldService()
    scaffold.seed_fresh_global()
    for name in ("ops", "acme"):
        if not scaffold.workspace_path(name).exists():
            scaffold.create_workspace(name)
    policies = base / "policies" / "client" / "claude"
    policies.mkdir(parents=True, exist_ok=True)
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
            "ops": {"policy": "admin"},
            "acme": {"policy": "client"},
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


def test_config_check_rejects_malformed_config_without_replacing_it(tmp_enso):
    config_file = Path(tmp_enso, "config.json")
    config_file.write_text("{malformed")
    original = config_file.read_bytes()

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "Could not read" in result.output
    assert config_file.read_bytes() == original


def test_config_check_reports_missing_config_without_creating_it(tmp_enso):
    config_file = Path(tmp_enso, "config.json")
    assert not config_file.exists()

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "config.json is missing" in result.output
    assert not config_file.exists()


def test_config_check_applies_defaults_without_persisting_them(tmp_enso):
    config = _teams_config(tmp_enso)
    config.pop("agent", None)
    config_file = Path(tmp_enso, "config.json")
    original = json.dumps(config, indent=2) + "\n"
    config_file.write_text(original)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 0, result.output
    assert config_file.read_text() == original


def test_config_check_fails_when_shared_instructions_are_missing(tmp_enso):
    config = _teams_config(tmp_enso)
    Path(tmp_enso, "AGENTS.md").unlink()
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "shared instruction file is missing" in result.output


def test_config_check_reports_scaffold_errors_without_repairing_them(tmp_enso):
    config = _teams_config(tmp_enso)
    discovery = Path(tmp_enso, ".agents", "skills")
    discovery.unlink()
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert "required discovery link" in result.output
    assert not discovery.exists()


def test_config_check_fails_on_missing_policy(tmp_enso):
    config = _teams_config(tmp_enso)
    Path(tmp_enso, "policies", "client", "claude", "settings.json").unlink()
    save_config(config)
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 1
    assert "claude" in result.output


# -- grok rule-load verification --
#
# A wrong-shaped grok permission config loads zero rules with no error, so
# `enso config check` stages the policy home and asserts the rule count the
# CLI actually loaded via `grok inspect --json`.


def _grok_teams_config(tmp_enso: str) -> dict:
    """Extend the scaffold with a grok-bound workspace and native policy."""
    base = Path(tmp_enso)
    config = _teams_config(tmp_enso)
    grok_ws = base / "workspaces" / "grok-client"
    grok_policy = base / "policies" / "grok-client" / "grok"
    scaffold = ScaffoldService()
    if not grok_ws.exists():
        scaffold.create_workspace("grok-client")
    grok_policy.mkdir(parents=True, exist_ok=True)
    grok_config = grok_policy / "config.toml"
    grok_config.write_text(
        "[permission]\n"
        'allow = ["run_terminal_command(echo *)"]\n'
        'deny = ["run_terminal_command(enso *)"]\n'
    )
    grok_config.chmod(0o600)
    config["transports"]["slack"]["channels"]["C2"] = {"workspace": "grok-client"}
    config["workspaces"]["grok-client"] = {"policy": "grok-client"}
    config["policies"]["grok-client"] = {
        "policy_dir": str(base / "policies" / "grok-client"),
        "providers": ["grok"],
        "default_provider": "grok",
        "chat_commands": ["status"],
    }
    return config


def _stub_grok_inspect(monkeypatch, tmp_enso, loaded: int) -> list:
    """Stub the `grok inspect --json` subprocess and isolate the user home."""
    monkeypatch.setattr(
        "enso.policy._user_grok_home", lambda: str(Path(tmp_enso) / "grok-user-home")
    )
    calls: list = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "git":
            return real_run(cmd, *args, **kwargs)
        calls.append((list(cmd), kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"permissions": {"loaded": loaded, "sources": []}}),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    return calls


def test_config_check_verifies_grok_policy_rules_load(tmp_enso, monkeypatch):
    config = _grok_teams_config(tmp_enso)
    calls = _stub_grok_inspect(monkeypatch, tmp_enso, loaded=2)
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output
    (cmd, kwargs) = calls[0]
    assert cmd[-2:] == ["inspect", "--json"]
    # The inspection must see exactly what a launch would: the workspace as
    # cwd and the staged revision home as GROK_HOME.
    assert os.path.realpath(kwargs["cwd"]) == os.path.realpath(
        str(Path(tmp_enso) / "workspaces" / "grok-client")
    )
    assert "grok-home" in kwargs["env"]["GROK_HOME"]
    # A scratch HOME keeps the operator's always-trusted home-scope compat
    # rules out of permissions.loaded, so equality stays exact.
    assert kwargs["env"]["HOME"] != os.environ.get("HOME")
    assert "enso-grok-inspect-" in kwargs["env"]["HOME"]


def test_config_check_fails_when_grok_loads_zero_rules(tmp_enso, monkeypatch):
    config = _grok_teams_config(tmp_enso)
    _stub_grok_inspect(monkeypatch, tmp_enso, loaded=0)
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    plain = " ".join(result.output.split())
    assert "dynamic native-policy inspection failed (1 problem)" in plain
    assert "grok-client" in plain


def test_config_check_fails_when_grok_loads_rules_the_policy_never_declared(
    tmp_enso, monkeypatch
):
    """More rules loaded than declared means something outside the policy
    reached the launch — a trusted workspace contributing its own config is
    the case that matters, since that is a policy widening itself."""
    config = _grok_teams_config(tmp_enso)
    monkeypatch.setattr(
        "enso.policy._user_grok_home", lambda: str(Path(tmp_enso) / "grok-user-home")
    )
    planted = str(
        Path(tmp_enso)
        / "workspaces"
        / "grok-client"
        / ".grok"
        / "NATIVE_SOURCE_SENTINEL.toml"
    )
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[0] == "git":
            return real_run(cmd, *args, **kwargs)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {"permissions": {"loaded": 5, "sources": ["staged config.toml", planted]}}
            ),
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    plain = " ".join(result.output.split())
    assert "dynamic native-policy inspection failed (1 problem)" in plain
    assert "NATIVE_SOURCE_SENTINEL" not in plain
    assert ".grok" not in plain


def test_config_check_does_not_echo_grok_inspection_output(tmp_enso, monkeypatch):
    config = _grok_teams_config(tmp_enso)
    monkeypatch.setattr(
        "enso.policy._user_grok_home", lambda: str(Path(tmp_enso) / "grok-user-home")
    )
    real_run = subprocess.run

    def leaking_failure(cmd, *args, **kwargs):
        if cmd[0] == "git":
            return real_run(cmd, *args, **kwargs)
        return subprocess.CompletedProcess(
            cmd,
            17,
            stdout="NATIVE_STDOUT_SENTINEL",
            stderr="NATIVE_STDERR_SENTINEL",
        )

    monkeypatch.setattr("subprocess.run", leaking_failure)
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    plain = " ".join(result.output.split())
    assert "dynamic native-policy inspection failed (1 problem)" in plain
    assert "NATIVE_STDOUT_SENTINEL" not in plain
    assert "NATIVE_STDERR_SENTINEL" not in plain


def test_config_check_fails_when_grok_binary_is_missing(tmp_enso, monkeypatch):
    config = _grok_teams_config(tmp_enso)
    monkeypatch.setattr(
        "enso.policy._user_grok_home", lambda: str(Path(tmp_enso) / "grok-user-home")
    )

    real_run = subprocess.run

    def missing_binary(cmd, *args, **kwargs):
        if cmd[0] == "git":
            return real_run(cmd, *args, **kwargs)
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr("subprocess.run", missing_binary)
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 1
    plain = " ".join(result.output.split())
    assert "dynamic native-policy inspection failed (1 problem)" in plain


def test_config_check_does_not_dynamically_inspect_an_unused_grok_policy(
    tmp_enso,
    monkeypatch,
):
    config = _teams_config(tmp_enso)
    policy_root = Path(tmp_enso, "policies", "unused-grok")
    native = policy_root / "grok" / "config.toml"
    native.parent.mkdir(parents=True)
    native.write_text(
        "[permission]\n"
        'allow = ["run_terminal_command(echo *)"]\n'
        'deny = ["run_terminal_command(enso *)"]\n'
    )
    native.chmod(0o600)
    config["policies"]["unused-grok"] = {
        "policy_dir": str(policy_root),
        "providers": ["grok"],
        "default_provider": "grok",
        "chat_commands": [],
    }
    monkeypatch.setattr(
        "enso.policy.verify_grok_rules",
        lambda *_args, **_kwargs: pytest.fail(
            "dynamic Grok inspection is only valid for a real execution binding"
        ),
    )
    save_config(config)

    result = runner.invoke(app, ["config", "check"])

    assert result.exit_code == 0, result.output
    assert "Policy unused-grok" in result.output
    assert not Path(policy_root, ".runtime").exists()


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
    assert "native source validation reported 2 warnings" in plain
    assert "mcp__ghost__tool" not in plain


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
    assert result.exit_code == 2
    assert "No such command 'check'" in result.output
    help_result = runner.invoke(app, ["policy", "--help"])
    assert help_result.exit_code == 0
    assert "check" not in help_result.output


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
