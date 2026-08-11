"""Tests for explicit structured outbound messages."""

from __future__ import annotations

import json

import pytest

from enso.outbound import (
    MAX_BLOCKS_PER_MESSAGE,
    MAX_FALLBACK_TEXT,
    MAX_MARKDOWN_TEXT,
    STRUCTURED_OUTPUT_INSTRUCTIONS,
    MarkdownBlock,
    OutboundMessage,
    parse_outbound_message,
)
from enso.transports import TransportContext


def _envelope(payload: dict) -> str:
    return f"```enso-message\n{json.dumps(payload)}\n```"


def _payload(**overrides: object) -> dict:
    payload = {
        "version": 1,
        "fallback_text": "Accessible summary",
        "blocks": [{"type": "markdown", "text": "# Rich summary"}],
    }
    payload.update(overrides)
    return payload


def test_parse_outbound_message_accepts_versioned_whole_response_envelope():
    message = parse_outbound_message("\n  " + _envelope(_payload()) + "  \n")

    assert message == OutboundMessage(
        fallback_text="Accessible summary",
        blocks=(MarkdownBlock(text="# Rich summary"),),
    )


def test_agent_instructions_match_the_strict_envelope_contract():
    instructions = STRUCTURED_OUTPUT_INSTRUCTIONS

    assert "valid JSON with exactly version, fallback_text, and blocks" in instructions
    assert "version must be the integer 1" in instructions
    assert f"at most {MAX_FALLBACK_TEXT:,} characters" in instructions
    assert f"1 to {MAX_BLOCKS_PER_MESSAGE} items" in instructions
    assert "exactly type and text fields with type markdown" in instructions
    assert f"{MAX_MARKDOWN_TEXT:,} cumulative characters" in instructions


@pytest.mark.parametrize(
    "text",
    [
        "ordinary reply",
        "| Name | Score |\n| --- | --- |\n| Ada | 10 |",
        '{"version":1,"fallback_text":"text","blocks":[]}',
        "before\n" + _envelope(_payload()),
        _envelope(_payload()) + "\nafter",
        "```enso-message\n{not json}\n```",
        '```enso-message\n{"version":1,"version":1,"fallback_text":"x",'
        '"blocks":[{"type":"markdown","text":"x"}]}\n```',
    ],
)
def test_parse_outbound_message_ignores_non_envelopes_and_malformed_envelopes(text):
    assert parse_outbound_message(text) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "fallback_text": "text", "blocks": [], "extra": True},
        _payload(version=True),
        _payload(version=2),
        _payload(version="1"),
        _payload(fallback_text=""),
        _payload(fallback_text="   "),
        _payload(fallback_text="x" * 4001),
        _payload(blocks=[]),
        _payload(blocks={"type": "markdown", "text": "x"}),
        _payload(blocks=[{"type": "section", "text": "raw Slack block"}]),
        _payload(blocks=[{"type": "markdown", "text": ""}]),
        _payload(blocks=[{"type": "markdown", "text": "x", "block_id": "raw"}]),
        _payload(blocks=[{"type": "markdown", "text": "x" * 12001}]),
        _payload(
            blocks=[
                {"type": "markdown", "text": "x" * 6001},
                {"type": "markdown", "text": "y" * 6000},
            ]
        ),
        _payload(blocks=[{"type": "markdown", "text": "x"}] * 51),
    ],
)
def test_parse_outbound_message_rejects_invalid_schema(payload):
    assert parse_outbound_message(_envelope(payload)) is None


class _FallbackContext(TransportContext):
    def __init__(self):
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)

    async def reply_status(self, text: str):
        return None

    async def edit_status(self, handle, text: str) -> None:
        return None

    async def delete_status(self, handle) -> None:
        return None


@pytest.mark.asyncio
async def test_transport_context_structured_reply_defaults_to_fallback_text():
    ctx = _FallbackContext()
    message = OutboundMessage(
        fallback_text="Readable everywhere",
        blocks=(MarkdownBlock(text="# Slack-only presentation"),),
    )

    await ctx.reply_message(message)

    assert ctx.replies == ["Readable everywhere"]
    assert ctx.get_output_instructions() == ""
