"""Tests for opt-in 1Password-backed config values."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from enso.secret_refs import (
    SecretResolutionError,
    resolve_config_secret,
    update_config_secret_reference,
)


def test_legacy_literal_value_is_unchanged():
    assert resolve_config_secret({"bot_token": "legacy-token"}, "bot_token") == "legacy-token"


@pytest.mark.parametrize("value", [None, True, 123, {}, ["token"]])
def test_malformed_legacy_literal_fails_closed(value):
    with pytest.raises(SecretResolutionError, match="bot_token must be a string"):
        resolve_config_secret({"bot_token": value}, "bot_token")


def test_reference_is_resolved_through_helper(tmp_path):
    helper = tmp_path / "1password.sh"
    helper.write_text(
        'op_secret() { printf "resolved:%s:%s\\n" "$1" "$2"; }\n',
    )

    value = resolve_config_secret(
        {
            "bot_token": "stale-legacy-token",
            "bot_token_1password": {
                "item": 'Enso"; exit 99; #',
                "field": "TELEGRAM_BOT_TOKEN",
            },
        },
        "bot_token",
        helper_path=str(helper),
    )

    assert value == 'resolved:Enso"; exit 99; #:TELEGRAM_BOT_TOKEN'


@pytest.mark.parametrize(
    "reference",
    [
        None,
        {},
        {"item": "", "field": "TOKEN"},
        {"item": "Transport", "field": ""},
        {"item": 123, "field": "TOKEN"},
    ],
)
def test_malformed_reference_fails_closed(reference):
    with pytest.raises(SecretResolutionError):
        resolve_config_secret(
            {"bot_token": "legacy", "bot_token_1password": reference},
            "bot_token",
        )


def test_missing_helper_has_clear_error(tmp_path):
    with pytest.raises(SecretResolutionError, match="1Password helper not found"):
        resolve_config_secret(
            {
                "bot_token_1password": {
                    "item": "Transport",
                    "field": "TOKEN",
                },
            },
            "bot_token",
            helper_path=str(tmp_path / "missing.sh"),
        )


def test_helper_failure_does_not_include_output(tmp_path, monkeypatch):
    helper = tmp_path / "1password.sh"
    helper.write_text("# test helper\n")

    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=23,
            stdout="secret-that-must-not-leak",
            stderr="sensitive diagnostic",
        )

    monkeypatch.setattr("enso.secret_refs._run_helper", fail)

    with pytest.raises(SecretResolutionError) as exc_info:
        resolve_config_secret(
            {
                "bot_token_1password": {
                    "item": "Transport",
                    "field": "TOKEN",
                },
            },
            "bot_token",
            helper_path=str(helper),
        )

    message = str(exc_info.value)
    assert "helper exit 23" in message
    assert "secret-that-must-not-leak" not in message
    assert "sensitive diagnostic" not in message


def test_empty_helper_output_is_rejected(tmp_path):
    helper = tmp_path / "1password.sh"
    helper.write_text("op_secret() { :; }\n")

    with pytest.raises(SecretResolutionError, match="empty value"):
        resolve_config_secret(
            {
                "app_token_1password": {
                    "item": "Transport",
                    "field": "APP_TOKEN",
                },
            },
            "app_token",
            helper_path=str(helper),
        )


def test_reference_update_sends_secret_over_stdin_not_argv(
    tmp_path, monkeypatch,
):
    helper = tmp_path / "1password.sh"
    helper.write_text("# test helper\n")
    captured = {}

    def succeed(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("enso.secret_refs._run_helper", succeed)
    updated = update_config_secret_reference(
        {
            "bot_token_1password": {
                "item": "Enso - Transport - Telegram",
                "field": "TELEGRAM_BOT_TOKEN",
            },
        },
        "bot_token",
        "new-secret-value",
        helper_path=str(helper),
    )

    assert updated is True
    assert "new-secret-value" not in captured["args"]
    assert "new-secret-value" not in " ".join(captured["args"])
    assert captured["kwargs"]["input_text"] == "new-secret-value\n"
    assert "op_set_secret" in captured["args"][0]


def test_reference_update_delivers_stdin_value_to_helper(tmp_path):
    helper = tmp_path / "1password.sh"
    helper.write_text(
        """
op_set_secret() {
  [[ "$1" == "Item Name" ]] &&
    [[ "$2" == "TOKEN" ]] &&
    [[ "$3" == 'new value; $HOME "quoted"' ]]
}
""",
    )

    assert update_config_secret_reference(
        {
            "bot_token_1password": {
                "item": "Item Name",
                "field": "TOKEN",
            },
        },
        "bot_token",
        'new value; $HOME "quoted"',
        helper_path=str(helper),
    )


def test_reference_update_failure_does_not_leak_secret(
    tmp_path, monkeypatch,
):
    helper = tmp_path / "1password.sh"
    helper.write_text("# test helper\n")

    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=9,
            stdout="new-secret-value",
            stderr="sensitive helper output",
        )

    monkeypatch.setattr("enso.secret_refs._run_helper", fail)

    with pytest.raises(SecretResolutionError) as exc_info:
        update_config_secret_reference(
            {
                "bot_token_1password": {
                    "item": "Telegram",
                    "field": "TOKEN",
                },
            },
            "bot_token",
            "new-secret-value",
            helper_path=str(helper),
        )

    message = str(exc_info.value)
    assert "helper exit 9" in message
    assert "new-secret-value" not in message
    assert "sensitive helper output" not in message


def test_reference_update_is_noop_for_legacy_literal():
    assert (
        update_config_secret_reference(
            {"bot_token": "legacy"},
            "bot_token",
            "replacement",
        )
        is False
    )


def test_timeout_kills_helper_descendants(tmp_path, monkeypatch):
    helper = tmp_path / "1password.sh"
    child_pid_file = tmp_path / "child.pid"
    helper.write_text(
        'op_secret() { sleep 5 & printf "%s\\n" "$!" > "$2"; wait; }\n',
    )
    monkeypatch.setattr("enso.secret_refs.ONEPASSWORD_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(SecretResolutionError, match="Timed out resolving"):
        resolve_config_secret(
            {
                "bot_token_1password": {
                    "item": "Transport",
                    "field": str(child_pid_file),
                },
            },
            "bot_token",
            helper_path=str(helper),
        )

    child_pid = int(child_pid_file.read_text())
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        os.kill(child_pid, 9)
        pytest.fail("timed-out helper child remained alive")
