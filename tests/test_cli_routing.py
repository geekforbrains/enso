"""Tests for the `enso message send/attach` destination resolver."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer

from enso.cli import (
    _ensure_default_execution_config,
    _install_launchd,
    _install_systemd,
    _resolve_send_targets,
    _resolve_slack_target,
    _scaffold_setup_or_exit,
    _setup_default_workspace,
    _setup_slack,
    _setup_telegram,
    _setup_transport,
    _update_referenced_secrets_with_rollback_or_exit,
    serve,
    setup,
    web,
)
from enso.secret_refs import SecretResolutionError


def test_explicit_to_wins_and_clears_thread(monkeypatch):
    """When --to is given we never leak the origin thread (could be a
    different channel)."""
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C_origin")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "1700.1")
    channel, thread_ts = _resolve_slack_target("#other", "C_notify")
    assert channel == "#other"
    assert thread_ts == ""


def test_origin_env_wins_over_notify_channel(monkeypatch):
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C_origin")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "1700.1")
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "C_origin"
    assert thread_ts == "1700.1"


def test_origin_without_thread(monkeypatch):
    """DM origin: channel set, thread empty."""
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "D_dm")
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "D_dm"
    assert thread_ts == ""


def test_falls_back_to_notify_channel(monkeypatch):
    """No --to and no origin env → notify_channel is the last resort."""
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "C_notify"
    assert thread_ts == ""


def test_nothing_configured(monkeypatch):
    """Fully unconfigured — returns empty so caller can error cleanly."""
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "")
    assert channel == ""
    assert thread_ts == ""


def test_telegram_send_target_resolves_1password_reference(monkeypatch):
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    config = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token_1password": {
                    "item": "Telegram",
                    "field": "TOKEN",
                },
                "allowed_users": ["123"],
                "notify_channel": "456",
            },
        },
    }
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-telegram-token",
    )

    transport, token, targets, thread_ts = _resolve_send_targets(config, "")

    assert (transport, token, targets, thread_ts) == (
        "telegram",
        "resolved-telegram-token",
        ["456"],
        "",
    )


def test_telegram_send_target_does_not_broadcast_to_allowed_users(monkeypatch):
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    config = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "token",
                "allowed_users": ["123", "456"],
            },
        },
    }

    with pytest.raises(typer.Exit):
        _resolve_send_targets(config, "")


def test_default_execution_config_assigns_admin_policy(tmp_enso):
    config = {
        "providers": {
            "claude": {"path": "claude", "models": ["sonnet"]},
            "codex": {"path": "codex", "models": ["terra"]},
        },
    }

    workspace = _ensure_default_execution_config(config)

    assert workspace == "default"
    assert config["workspaces"]["default"] == {
        "policy": "admin",
        "concurrency": 1,
    }
    assert config["policies"]["admin"] == {
        "unrestricted": True,
        "providers": ["claude", "codex"],
        "default_provider": "claude",
        "chat_commands": "*",
    }


def test_default_execution_config_preserves_existing_default_workspace():
    config = {
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {
            "default": {
                "policy": "staff",
                "concurrency": 1,
            }
        },
        "policies": {
            "staff": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            }
        },
    }

    workspace = _ensure_default_execution_config(config)

    assert workspace == "default"
    assert config["workspaces"]["default"] == {
        "policy": "staff",
        "concurrency": 1,
    }
    assert "admin" not in config["policies"]


def test_default_execution_config_adds_default_beside_existing_workspace():
    config = {
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {
            "company": {
                "policy": "staff",
                "concurrency": 2,
            }
        },
        "policies": {
            "staff": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            }
        },
    }

    workspace = _ensure_default_execution_config(config)

    assert workspace == "default"
    assert config["workspaces"]["default"] == {
        "policy": "admin",
        "concurrency": 1,
    }
    assert config["workspaces"]["company"]["policy"] == "staff"
    assert config["policies"]["admin"]["unrestricted"] is True


def test_default_execution_config_replaces_malformed_workspace_block():
    config = {
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {"default": "broken"},
    }

    assert _ensure_default_execution_config(config) == "default"
    assert config["workspaces"]["default"] == {
        "policy": "admin",
        "concurrency": 1,
    }


def test_setup_rejects_legacy_working_dir_before_changes(monkeypatch, capsys):
    config = {"working_dir": "/legacy/workspace"}
    monkeypatch.setattr("enso.cli.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        "enso.cli._setup_providers",
        lambda *_: pytest.fail("setup must stop before mutating legacy config"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 1
    assert config == {"working_dir": "/legacy/workspace"}
    output = " ".join(capsys.readouterr().out.split())
    assert "working_dir is no longer supported" in output
    assert "workspaces" in output


def test_setup_rejects_legacy_workspace_path_before_repository_changes(
    monkeypatch, capsys
):
    config = {
        "workspaces": {
            "default": {
                "path": "/legacy/workspace",
                "policy": "admin",
            }
        }
    }
    monkeypatch.setattr("enso.cli.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        "enso.cli._ensure_repository_or_exit",
        lambda: pytest.fail("setup must stop before repository changes"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 1
    output = " ".join(capsys.readouterr().out.split())
    assert "workspaces.default.path is no longer supported" in output
    assert "v1.3-managed-workspaces.md" in output


def test_setup_default_workspace_only_updates_config(monkeypatch, tmp_enso, capsys):
    monkeypatch.setattr(
        "enso.cli.os.makedirs",
        lambda *_args, **_kwargs: pytest.fail("workspace creation belongs to scaffolding"),
    )
    config = {"providers": {"claude": {"path": "claude", "models": ["sonnet"]}}}

    assert _setup_default_workspace(config) == "default"

    assert config["workspaces"]["default"] == {
        "policy": "admin",
        "concurrency": 1,
    }
    assert "workspaces/default" in capsys.readouterr().out


def test_fresh_setup_scaffold_seeds_complete_canonical_tree(tmp_enso):
    workspace = Path(tmp_enso, "workspaces", "default")
    workspace.rmdir()
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }

    _scaffold_setup_or_exit(config)

    assert Path(tmp_enso, "skills", "workspace", "SKILL.md").is_file()
    assert os.readlink(Path(tmp_enso, "CLAUDE.md")) == "AGENTS.md"
    assert workspace.joinpath("AGENTS.md").is_file()
    assert workspace.joinpath("knowledge", "README.md").is_file()
    assert os.readlink(workspace / ".agents" / "skills") == "../skills"


@pytest.mark.parametrize(
    "setup_block",
    [
        pytest.param({}, id="pre-feature"),
        pytest.param(
            {"setup": {"completed_at": "2026-08-18T12:00:00+00:00"}},
            id="complete",
        ),
    ],
)
def test_nonfresh_setup_repairs_structure_without_reseeding_content(
    setup_block, tmp_enso
):
    from enso.scaffolding import ScaffoldService

    service = ScaffoldService()
    service.seed_fresh_global()
    workspace = Path(tmp_enso, "workspaces", "default")
    workspace.rmdir()
    service.create_workspace("default")
    workspace.joinpath("AGENTS.md").unlink()
    workspace.joinpath("CLAUDE.md").unlink()
    config = {
        **setup_block,
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }

    with pytest.raises(typer.Exit):
        _scaffold_setup_or_exit(config)

    assert not workspace.joinpath("AGENTS.md").exists()
    assert not workspace.joinpath("CLAUDE.md").exists()


def test_setup_rejects_malformed_config_before_scaffolding(monkeypatch, tmp_enso, capsys):
    config_file = Path(tmp_enso, "config.json")
    config_file.write_text("{malformed")
    original = config_file.read_bytes()
    monkeypatch.setattr(
        "enso.cli._setup_providers",
        lambda *_: pytest.fail("setup must stop before mutating the installation"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 1
    assert "Could not read" in capsys.readouterr().out
    assert config_file.read_bytes() == original
    assert not Path(f"{config_file}.lock").exists()


def test_setup_rejects_symlinked_config_root_before_writing(monkeypatch, tmp_path, capsys):
    target = tmp_path / "outside-enso"
    target.mkdir()
    config_root = tmp_path / "enso"
    config_root.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(config_root))
    monkeypatch.setattr("enso.config.CONFIG_FILE", str(config_root / "config.json"))
    monkeypatch.setattr(
        "enso.cli._setup_providers",
        lambda *_: pytest.fail("setup must stop before provider configuration"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 1
    assert "physical directory" in capsys.readouterr().out
    assert list(target.iterdir()) == []


def test_setup_ensures_repository_before_provider_configuration(
    monkeypatch, tmp_enso
):
    events = []
    monkeypatch.setattr(
        "enso.cli._ensure_repository_or_exit",
        lambda: events.append("repository"),
        raising=False,
    )

    def stop_after_repository(_config):
        events.append("providers")
        raise typer.Exit(7)

    monkeypatch.setattr("enso.cli._setup_providers", stop_after_repository)

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 7
    assert events == ["repository", "providers"]


@pytest.mark.parametrize("command", [serve, web])
def test_operational_commands_require_existing_config(
    command, monkeypatch, tmp_enso, capsys
):
    monkeypatch.setattr(
        "enso.core.Runtime",
        lambda *_: pytest.fail("a runtime must not be created without config.json"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        command()

    assert exc_info.value.exit_code == 1
    assert "config.json" in capsys.readouterr().out
    assert not Path(tmp_enso, "config.json").exists()


@pytest.mark.parametrize("command", [serve, web])
def test_operational_startup_validates_before_runtime(command, monkeypatch):
    config = {"transport": "slack"}
    events = []
    monkeypatch.setattr("enso.cli.load_config", lambda: config)

    def stop_at_validation(candidate):
        events.append(candidate)
        raise typer.Exit(9)

    monkeypatch.setattr("enso.cli._validate_installation_or_exit", stop_at_validation)
    monkeypatch.setattr(
        "enso.core.Runtime",
        lambda *_: pytest.fail("runtime construction must follow installation validation"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        command()

    assert exc_info.value.exit_code == 9
    assert events == [config]


@pytest.mark.parametrize("configured_transport", ["", "email", None])
def test_setup_transport_requires_supported_choice(monkeypatch, configured_transport):
    config = {"transport": configured_transport}
    responses = iter(["", "matrix", "slack"])
    entered = []

    def get_input(*_args, **_kwargs):
        response = next(responses)
        entered.append(response)
        return response

    monkeypatch.setattr("enso.cli.Prompt.get_input", get_input)
    monkeypatch.setattr("enso.cli._setup_slack", lambda _: None)
    monkeypatch.setattr(
        "enso.cli._setup_telegram",
        lambda _: pytest.fail("Telegram setup must not run"),
    )

    _setup_transport(config)

    assert entered == ["", "matrix", "slack"]
    assert config["transport"] == "slack"


def test_setup_transport_keeps_supported_existing_choice_as_default(monkeypatch):
    config = {"transport": "telegram"}
    monkeypatch.setattr("enso.cli.Prompt.get_input", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("enso.cli._setup_telegram", lambda _: 123)
    monkeypatch.setattr(
        "enso.cli._setup_slack",
        lambda _: pytest.fail("Slack setup must not run"),
    )

    assert _setup_transport(config) == 123
    assert config["transport"] == "telegram"


def test_launchd_service_has_no_process_working_directory(monkeypatch, tmp_path):
    plist = tmp_path / "enso.plist"
    monkeypatch.setattr("enso.cli._LAUNCHD_PLIST", str(plist))
    monkeypatch.setattr(
        "enso.cli.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert _install_launchd("/venv/bin/enso")

    content = plist.read_text()
    assert "WorkingDirectory" not in content
    assert "<string>/venv/bin/enso</string>" in content


def test_systemd_service_has_no_process_working_directory(monkeypatch, tmp_path):
    service_dir = tmp_path / "systemd"
    original_expanduser = os.path.expanduser
    monkeypatch.setattr(
        "enso.cli.os.path.expanduser",
        lambda path: str(service_dir)
        if path == "~/.config/systemd/user"
        else original_expanduser(path),
    )
    monkeypatch.setattr(
        "enso.cli.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert _install_systemd("/venv/bin/enso")

    content = (service_dir / "enso.service").read_text()
    assert "WorkingDirectory=" not in content
    assert "ExecStart=/venv/bin/enso serve" in content


def test_slack_send_target_resolves_1password_reference(monkeypatch):
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    config = {
        "transport": "slack",
        "transports": {
            "slack": {
                "bot_token_1password": {
                    "item": "Slack",
                    "field": "BOT_TOKEN",
                },
                "notify_channel": "C123",
            },
        },
    }
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-slack-token",
    )

    transport, token, targets, thread_ts = _resolve_send_targets(config, "")

    assert (transport, token, targets, thread_ts) == (
        "slack",
        "resolved-slack-token",
        ["C123"],
        "",
    )


def test_telegram_setup_validates_existing_token_and_binds_default_workspace(
    monkeypatch, tmp_enso
):
    config = {
        "transports": {
            "telegram": {
                "bot_token_1password": {
                    "item": "Telegram",
                    "field": "TOKEN",
                },
                "allowed_users": ["123"],
            },
        },
    }
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-telegram-token",
    )
    monkeypatch.setattr(
        "enso.cli._tg_validate_token",
        lambda token: {"username": "enso_test"} if token == "resolved-telegram-token" else None,
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: False)

    assert _setup_telegram(config) is None
    telegram = config["transports"]["telegram"]
    assert "bot_token" not in telegram
    assert telegram["workspace"] == "default"
    assert "path" not in config["workspaces"]["default"]
    assert config["workspaces"]["default"]["policy"] == "admin"


def test_telegram_setup_adds_canonical_default_beside_existing_workspace(monkeypatch):
    config = {
        "transports": {
            "telegram": {
                "bot_token": "token",
                "allowed_users": ["123"],
            },
        },
        "workspaces": {
            "company": {
                "policy": "staff",
                "concurrency": 1,
            },
        },
        "policies": {
            "staff": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
        },
    }
    monkeypatch.setattr("enso.cli.resolve_config_secret", lambda cfg, key: "token")
    monkeypatch.setattr(
        "enso.cli._tg_validate_token", lambda token: {"username": "enso_test"}
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: False)

    assert _setup_telegram(config) is None

    assert config["transports"]["telegram"]["workspace"] == "default"
    assert config["workspaces"]["default"] == {
        "policy": "admin",
        "concurrency": 1,
    }
    assert config["workspaces"]["company"]["policy"] == "staff"


def test_slack_setup_validates_resolved_existing_token(monkeypatch):
    config = {
        "transports": {
            "slack": {
                "bot_token_1password": {
                    "item": "Slack",
                    "field": "BOT_TOKEN",
                },
                "account_id": "T123",
                "dms": {"U123": {"workspace": "default"}},
                "channels": {},
                "channel_defaults": {"mention_required": False},
            },
        },
    }
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-slack-token",
    )
    monkeypatch.setattr(
        "enso.cli._slack_validate_token",
        lambda token: {"user": "enso", "team_id": "T123"}
        if token == "resolved-slack-token"
        else None,
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: False)
    write_manifest = Mock(return_value="/tmp/slack-manifest.yaml")
    monkeypatch.setattr("enso.cli._write_slack_manifest_copy", write_manifest)

    assert _setup_slack(config) is None
    assert "bot_token" not in config["transports"]["slack"]
    assert config == original
    write_manifest.assert_called_once_with()


def test_slack_setup_rejects_legacy_routes_before_writing(monkeypatch, capsys):
    config = {
        "transports": {
            "slack": {
                "bot_token": "old-bot",
                "app_token": "old-app",
            },
        },
        "routes": {
            "slack": {
                "account_id": "T1",
                "dms": {"UOLD": {"workspace": "default"}},
                "channels": {},
            },
        },
    }
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: pytest.fail("setup must not write before legacy config is migrated"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        _setup_slack(config)

    assert exc_info.value.exit_code == 1
    assert config == original
    output = " ".join(capsys.readouterr().out.split())
    assert "move routes.slack fields into transports.slack" in output


def test_telegram_setup_reconfiguration_updates_reference_without_plaintext(
    monkeypatch,
):
    reference = {"item": "Telegram", "field": "TOKEN"}
    config = {
        "transports": {
            "telegram": {
                "bot_token_1password": reference,
                "bot_token": "stale-literal",
                "allowed_users": ["123"],
                "allowed_user_ids": [999],
            },
        },
    }
    updates = []
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "old-token",
    )
    monkeypatch.setattr(
        "enso.cli._tg_validate_token",
        lambda token: {
            "username": "old_bot" if token == "old-token" else "new_bot",
        },
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "enso.cli.Prompt.ask",
        lambda *args, **kwargs: "new-token",
    )
    monkeypatch.setattr(
        "enso.cli.update_config_secret_reference",
        lambda cfg, key, value: updates.append((cfg, key, value)) or True,
    )
    monkeypatch.setattr(
        "enso.cli._tg_wait_for_message",
        lambda token, timeout: {
            "user_id": 456,
            "first_name": "Tester",
            "chat_id": 456,
        },
    )

    assert _setup_telegram(config) == 456
    telegram = config["transports"]["telegram"]
    assert telegram["bot_token_1password"] is reference
    assert "bot_token" not in telegram
    assert "allowed_user_ids" not in telegram
    assert telegram["allowed_users"] == ["456"]
    assert telegram["notify_channel"] == "456"
    assert telegram["workspace"] == "default"
    assert updates == [
        (
            {
                "bot_token_1password": reference,
                "bot_token": "stale-literal",
                "allowed_users": ["123"],
                "allowed_user_ids": [999],
            },
            "bot_token",
            "new-token",
        ),
    ]


def test_slack_setup_reconfiguration_updates_references_without_plaintext(
    monkeypatch,
):
    bot_reference = {"item": "Slack", "field": "BOT_TOKEN"}
    app_reference = {"item": "Slack", "field": "APP_TOKEN"}
    config = {
        "transports": {
            "slack": {
                "bot_token_1password": bot_reference,
                "app_token_1password": app_reference,
                "bot_token": "stale-bot-literal",
                "app_token": "stale-app-literal",
                "notify_channel": "COLD",
                "channel_context_messages": 12,
                "rich_messages": False,
                "persistent_surfaces": False,
                "account_id": "T1",
                "channel_defaults": {"mention_required": False},
                "dms": {"UOLD": {"workspace": "company"}},
                "channels": {"CSTAFF": {"workspace": "company"}},
            },
        },
        "workspaces": {
            "company": {"policy": "admin", "concurrency": 1},
        },
        "policies": {
            "admin": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
        },
    }
    updates = []
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "old-bot-token",
    )

    def validate(token):
        if token == "old-bot-token":
            return {"user": "old-bot", "user_id": "UOLD", "team_id": "T1"}
        return {"user": "new-bot", "user_id": "UNEWBOT", "team_id": "T1"}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot-token"
        if "App Token" in label:
            return "new-app-token"
        if "Notify channel" in label:
            return "CNEW"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli._slack_validate_token", validate)
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )
    monkeypatch.setattr(
        "enso.cli.update_config_secret_reference",
        lambda cfg, key, value: updates.append((key, value)) or True,
    )

    assert _setup_slack(config) is None
    slack = config["transports"]["slack"]
    assert slack["bot_token_1password"] is bot_reference
    assert slack["app_token_1password"] is app_reference
    assert "bot_token" not in slack
    assert "app_token" not in slack
    assert slack["bot_user_id"] == "UNEWBOT"
    assert "allowed_users" not in slack
    assert slack["notify_channel"] == "CNEW"
    assert slack["channel_context_messages"] == 12
    assert slack["rich_messages"] is False
    assert slack["persistent_surfaces"] is False
    assert slack["account_id"] == "T1"
    assert slack["channel_defaults"] == {"mention_required": False}
    assert slack["dms"] == {"UOLD": {"workspace": "company"}}
    assert slack["channels"] == {"CSTAFF": {"workspace": "company"}}
    assert "routes" not in config
    assert updates == [
        ("bot_token", "new-bot-token"),
        ("app_token", "new-app-token"),
    ]


@pytest.mark.parametrize(
    ("route_key", "route_value"),
    [
        ("dms", {"UOLD": {"workspace": "company"}}),
        ("channels", {"CSTAFF": {"workspace": "company"}}),
    ],
)
def test_slack_setup_preserves_routes_when_other_map_is_omitted(
    monkeypatch,
    route_key,
    route_value,
):
    config = {
        "transports": {
            "slack": {
                "bot_token": "old-bot",
                "app_token": "old-app",
                "account_id": "T1",
                route_key: route_value,
            },
        },
    }

    def validate(token):
        return {"user": "enso", "user_id": "UBOT", "team_id": "T1"}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot"
        if "App Token" in label:
            return "new-app"
        if "Notify channel" in label:
            return ""
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli._slack_validate_token", validate)
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )

    _setup_slack(config)

    slack = config["transports"]["slack"]
    assert slack[route_key] == route_value
    assert slack["dms"] == (route_value if route_key == "dms" else {})
    assert slack["channels"] == (route_value if route_key == "channels" else {})


def test_slack_setup_replaces_only_routing_for_a_different_account(monkeypatch):
    config = {
        "transports": {
            "slack": {
                "bot_token": "old-bot",
                "app_token": "old-app",
                "channel_context_messages": 7,
                "rich_messages": False,
                "account_id": "T1",
                "channel_defaults": {"mention_required": False},
                "dms": {"UOLD": {"workspace": "default"}},
                "channels": {"COLD": {"workspace": "default"}},
            }
        },
    }
    confirmations = iter([True, True])

    def validate(token):
        team = "T1" if token == "old-bot" else "T2"
        return {"user": "enso", "user_id": "UBOT2", "team_id": team}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot"
        if "App Token" in label:
            return "new-app"
        if "Owner Slack user ID" in label:
            return "UNEW"
        if "Notify channel" in label:
            return "CNEW"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli._slack_validate_token", validate)
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: next(confirmations))
    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )

    _setup_slack(config)

    slack = config["transports"]["slack"]
    assert slack["account_id"] == "T2"
    assert slack["dms"] == {"UNEW": {"workspace": "default"}}
    assert slack["channels"] == {}
    assert "channel_defaults" not in slack
    assert slack["channel_context_messages"] == 7
    assert slack["rich_messages"] is False


def test_slack_setup_account_change_cancel_preserves_config(monkeypatch):
    config = {
        "transports": {
            "slack": {
                "bot_token": "old-bot",
                "app_token": "old-app",
                "account_id": "T1",
                "dms": {"UOLD": {"workspace": "default"}},
                "channels": {},
            }
        }
    }
    original = copy.deepcopy(config)
    confirmations = iter([True, False])

    def validate(token):
        team = "T1" if token == "old-bot" else "T2"
        return {"user": "enso", "user_id": "UBOT2", "team_id": team}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot"
        if "App Token" in label:
            return "new-app"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli._slack_validate_token", validate)
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: next(confirmations))
    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )
    monkeypatch.setattr(
        "enso.cli._update_referenced_secrets_with_rollback_or_exit",
        lambda *args, **kwargs: pytest.fail("credential writes must not run after cancel"),
    )

    _setup_slack(config)

    assert config == original


def test_reconfiguration_write_failure_keeps_config_and_exits_clearly(
    monkeypatch, capsys,
):
    config = {
        "transports": {
            "telegram": {
                "bot_token_1password": {
                    "item": "Telegram",
                    "field": "TOKEN",
                },
                "allowed_users": ["123"],
            },
        },
    }
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "old-token",
    )
    monkeypatch.setattr(
        "enso.cli._tg_validate_token",
        lambda token: {"username": "enso_bot"},
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "enso.cli.Prompt.ask",
        lambda *args, **kwargs: "new-token",
    )

    def fail(*args, **kwargs):
        raise SecretResolutionError("helper exit 9")

    monkeypatch.setattr("enso.cli.update_config_secret_reference", fail)

    with pytest.raises(typer.Exit):
        _setup_telegram(config)

    assert config == original
    assert "Could not save Telegram bot token" in capsys.readouterr().out


def test_slack_reference_updates_prevalidate_every_old_value(monkeypatch, capsys):
    config = {
        "bot_token_1password": {"item": "Slack", "field": "BOT"},
        "app_token_1password": {"item": "Slack", "field": "APP"},
    }
    writes = []

    def resolve(_config, key):
        if key == "app_token":
            raise SecretResolutionError("sensitive helper output")
        return "old-bot-secret"

    monkeypatch.setattr("enso.cli.resolve_config_secret", resolve)
    monkeypatch.setattr(
        "enso.cli.update_config_secret_reference",
        lambda *args: writes.append(args) or True,
    )

    with pytest.raises(typer.Exit):
        _update_referenced_secrets_with_rollback_or_exit(
            config,
            [
                ("bot_token", "new-bot-secret", "Slack bot token"),
                ("app_token", "new-app-secret", "Slack app token"),
            ],
        )

    output = " ".join(capsys.readouterr().out.split())
    assert writes == []
    assert "existing Slack app token could not be loaded" in output
    assert "sensitive helper output" not in output
    assert "old-bot-secret" not in output


def test_slack_reference_update_rolls_back_earlier_write(monkeypatch, capsys):
    config = {
        "bot_token_1password": {"item": "Slack", "field": "BOT"},
        "app_token_1password": {"item": "Slack", "field": "APP"},
    }
    old_values = {
        "bot_token": "old-bot-secret",
        "app_token": "old-app-secret",
    }
    writes = []

    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda _config, key: old_values[key],
    )

    def update(_config, key, value):
        writes.append((key, value))
        if key == "app_token":
            raise SecretResolutionError("new-app-secret must not leak")
        return True

    monkeypatch.setattr("enso.cli.update_config_secret_reference", update)

    with pytest.raises(typer.Exit):
        _update_referenced_secrets_with_rollback_or_exit(
            config,
            [
                ("bot_token", "new-bot-secret", "Slack bot token"),
                ("app_token", "new-app-secret", "Slack app token"),
            ],
        )

    output = " ".join(capsys.readouterr().out.split())
    assert writes == [
        ("bot_token", "new-bot-secret"),
        ("app_token", "new-app-secret"),
        ("bot_token", "old-bot-secret"),
    ]
    assert "Earlier referenced credential updates were restored" in output
    assert "new-app-secret" not in output
    assert "old-bot-secret" not in output


def test_slack_reference_update_reports_rollback_failure_without_secrets(
    monkeypatch, capsys,
):
    config = {
        "bot_token_1password": {"item": "Slack", "field": "BOT"},
        "app_token_1password": {"item": "Slack", "field": "APP"},
    }
    old_values = {
        "bot_token": "old-bot-secret",
        "app_token": "old-app-secret",
    }
    writes = []

    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda _config, key: old_values[key],
    )

    def update(_config, key, value):
        writes.append((key, value))
        if key == "app_token":
            raise SecretResolutionError("new-app-secret must not leak")
        if value == "old-bot-secret":
            raise SecretResolutionError("old-bot-secret must not leak")
        return True

    monkeypatch.setattr("enso.cli.update_config_secret_reference", update)

    with pytest.raises(typer.Exit):
        _update_referenced_secrets_with_rollback_or_exit(
            config,
            [
                ("bot_token", "new-bot-secret", "Slack bot token"),
                ("app_token", "new-app-secret", "Slack app token"),
            ],
        )

    output = " ".join(capsys.readouterr().out.split())
    assert writes[-1] == ("bot_token", "old-bot-secret")
    assert "Rollback also failed for: Slack bot token" in output
    assert "Referenced credentials may be inconsistent" in output
    for secret in (*old_values.values(), "new-bot-secret", "new-app-secret"):
        assert secret not in output


def test_slack_setup_reprompts_until_app_token_provided(monkeypatch, capsys):
    """A blank app token silently breaks Socket Mode later (or aborts a
    referenced update with a misleading 1Password error), so setup must
    insist on one just like it does for the bot token."""
    config: dict = {}
    app_prompts = 0

    def prompt(label, **kwargs):
        nonlocal app_prompts
        if "Bot Token" in label:
            return "xoxb-new"
        if "App Token" in label:
            app_prompts += 1
            return "" if app_prompts == 1 else "xapp-new"
        if "Owner Slack user ID" in label:
            return "UOWNER"
        if "Notify channel" in label:
            return "C123"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._slack_validate_token",
        lambda token: {"user": "enso", "user_id": "UBOT", "team_id": "T1"},
    )
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )

    _setup_slack(config)

    slack = config["transports"]["slack"]
    assert app_prompts == 2
    assert slack["app_token"] == "xapp-new"
    assert "allowed_users" not in slack
    assert slack["account_id"] == "T1"
    assert slack["dms"] == {"UOWNER": {"workspace": "default"}}
    assert slack["channels"] == {}
    assert "routes" not in config
    assert "Token is required" in capsys.readouterr().out


def test_serve_reports_secret_resolution_failure_cleanly(
    monkeypatch, tmp_path, capsys,
):
    """`enso serve` must exit with a one-line credential error like every
    other command instead of surfacing a raw traceback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "enso.cli.load_config",
        lambda: {"transport": "slack"},
    )
    monkeypatch.setattr("enso.cli.configure_logging", lambda *a, **k: {})
    monkeypatch.setattr("enso.cli._load_secret_env", lambda: [])
    monkeypatch.setattr("enso.cli._validate_installation_or_exit", lambda _config: None)

    class FakeRuntime:
        def __init__(self, config):
            pass

        def install_system_prompts(self):
            pytest.fail("serve must not install or upgrade user-owned content")

        def install_workspaces(self):
            pytest.fail("serve must not create or repair workspace content")

        def load_state(self):
            pass

    monkeypatch.setattr("enso.core.Runtime", FakeRuntime)

    def fail(name, runtime):
        raise SecretResolutionError(
            "Could not resolve bot_token from 1Password (helper exit 1)"
        )

    monkeypatch.setattr("enso.cli._load_transport", fail)

    with pytest.raises(typer.Exit) as excinfo:
        serve(transport=None)

    assert excinfo.value.exit_code == 1
    out = capsys.readouterr().out
    assert "Could not load transport credentials" in out
    assert "helper exit 1" in out


# ---------------------------------------------------------------------------
# Slack helper payloads include thread_ts when set
# ---------------------------------------------------------------------------


class _FakeResp:
    status = 200

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return b'{"ok": true}'


def test_slack_send_message_includes_thread_ts(monkeypatch):
    """_slack_send_message adds thread_ts to chat.postMessage payload."""
    import json

    from enso import cli as cli_mod

    captured: dict = {}

    def _fake_urlopen(req, timeout=10):
        captured["data"] = json.loads(req.data)
        captured["url"] = req.full_url
        return _FakeResp()

    monkeypatch.setattr(cli_mod.urllib.request, "urlopen", _fake_urlopen)

    ok = cli_mod._slack_send_message(
        "xoxb-fake", "C012345", "hi", thread_ts="1700000000.123",
    )
    assert ok is True
    assert captured["data"] == {
        "channel": "C012345",
        "text": "hi",
        "thread_ts": "1700000000.123",
    }


def test_slack_send_message_no_thread(monkeypatch):
    """Without thread_ts the payload stays clean."""
    import json

    from enso import cli as cli_mod

    captured: dict = {}

    def _fake_urlopen(req, timeout=10):
        captured["data"] = json.loads(req.data)
        return _FakeResp()

    monkeypatch.setattr(cli_mod.urllib.request, "urlopen", _fake_urlopen)

    cli_mod._slack_send_message("xoxb-fake", "C012345", "hi")
    assert "thread_ts" not in captured["data"]


# ---------------------------------------------------------------------------
# Service control
# ---------------------------------------------------------------------------


def test_service_restart_unknown_platform_returns_false(monkeypatch):
    """On a platform with no service manager (and no os.getuid), restart
    returns False instead of raising."""
    from enso import cli as cli_mod

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delattr(cli_mod.os, "getuid", raising=False)
    assert cli_mod._service_restart() is False
