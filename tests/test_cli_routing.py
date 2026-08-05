"""Tests for the `enso message send/attach` destination resolver."""

from __future__ import annotations

import copy

import pytest
import typer

from enso.cli import (
    _resolve_send_targets,
    _resolve_slack_target,
    _setup_slack,
    _setup_telegram,
    _update_referenced_secrets_with_rollback_or_exit,
    serve,
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
        ["123"],
        "",
    )


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


def test_telegram_setup_validates_resolved_existing_token(monkeypatch):
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
    assert "bot_token" not in config["transports"]["telegram"]


def test_slack_setup_validates_resolved_existing_token(monkeypatch):
    config = {
        "transports": {
            "slack": {
                "bot_token_1password": {
                    "item": "Slack",
                    "field": "BOT_TOKEN",
                },
                "allowed_users": ["U123"],
            },
        },
    }
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-slack-token",
    )
    monkeypatch.setattr(
        "enso.cli._slack_validate_token",
        lambda token: {"user": "enso"} if token == "resolved-slack-token" else None,
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: False)

    assert _setup_slack(config) is None
    assert "bot_token" not in config["transports"]["slack"]


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
    assert telegram["allowed_users"] == ["456"]
    assert updates == [
        (
            {
                "bot_token_1password": reference,
                "bot_token": "stale-literal",
                "allowed_users": ["123"],
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
                "allowed_users": ["UOLD"],
                "notify_channel": "COLD",
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
            return {"user": "old-bot", "user_id": "UOLD"}
        return {"user": "new-bot", "user_id": "UNEWBOT"}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot-token"
        if "App Token" in label:
            return "new-app-token"
        if "Allowed users" in label:
            return "UNEW"
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
    assert slack["allowed_users"] == ["UNEW"]
    assert slack["notify_channel"] == "CNEW"
    assert updates == [
        ("bot_token", "new-bot-token"),
        ("app_token", "new-app-token"),
    ]


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
        if "Allowed users" in label:
            return "*"
        if "Notify channel" in label:
            return "C123"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._slack_validate_token",
        lambda token: {"user": "enso", "user_id": "UBOT"},
    )
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )

    _setup_slack(config)

    slack = config["transports"]["slack"]
    assert app_prompts == 2
    assert slack["app_token"] == "xapp-new"
    assert "Token is required" in capsys.readouterr().out


def test_serve_reports_secret_resolution_failure_cleanly(
    monkeypatch, tmp_path, capsys,
):
    """`enso serve` must exit with a one-line credential error like every
    other command instead of surfacing a raw traceback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "enso.cli.load_config",
        lambda: {"transport": "slack", "working_dir": str(tmp_path)},
    )
    monkeypatch.setattr("enso.cli.configure_logging", lambda *a, **k: {})
    monkeypatch.setattr("enso.cli._load_secret_env", lambda: [])

    class FakeRuntime:
        def __init__(self, config):
            pass

        def install_system_prompts(self):
            pass

        def load_state(self):
            pass

    monkeypatch.setattr("enso.core.Runtime", FakeRuntime)

    def fail(name, runtime):
        raise SecretResolutionError(
            "Could not resolve bot_token from 1Password (helper exit 1)"
        )

    monkeypatch.setattr("enso.cli._load_transport", fail)

    with pytest.raises(typer.Exit) as excinfo:
        serve(working_dir=None, transport=None)

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
