"""The log's command redaction: prompts and tokens never reach ``enso.log``."""

from __future__ import annotations

import pytest

from enso.log import redacted_command

# One of each shape _TOKEN_RE guards: Slack bot, Slack app, and Telegram bot tokens.
TOKENS = ["xoxb-secret", "xapp-secret", "123456:a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6_-"]


def test_redacted_command_hides_attached_and_separated_prompts() -> None:
    attached = ["grok", "--output-format", "plain", "--model", "grok-4.6", "--single=hi there"]
    assert redacted_command(attached) == (
        "grok --output-format plain --model grok-4.6 '--single=<prompt chars=8>'"
    )
    agy = ["agy", "--output-format", "stream-json", "--new-project", "--prompt=hi there"]
    assert redacted_command(agy) == (
        "agy --output-format stream-json --new-project '--prompt=<prompt chars=8>'"
    )
    separated = ["claude", "-p", "--", "hi", "there"]
    assert redacted_command(separated) == "claude -p -- '<prompt chars=7>'"


@pytest.mark.parametrize("token", TOKENS)
def test_redacted_command_hides_every_token_shape(token: str) -> None:
    assert redacted_command(["claude", "-p", token, "--", "hi"]) == (
        "claude -p '<redacted>' -- '<prompt chars=2>'"
    )


def test_redacted_command_keeps_ordinary_arguments() -> None:
    # A short number is not a Telegram token; over-redacting would make the log useless.
    assert redacted_command(["claude", "-p", "12345", "--", "hi"]) == (
        "claude -p 12345 -- '<prompt chars=2>'"
    )
