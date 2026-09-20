"""``enso-message`` parsing: what is delivered natively and what a broken envelope falls back to."""

from __future__ import annotations

import json

import pytest

from enso.outbound import (
    ChartBlock,
    Column,
    EnvelopeError,
    MarkdownBlock,
    Segment,
    Series,
    TableBlock,
    parse_outbound_message,
)

MARKDOWN = {"type": "markdown", "text": "# Hi"}
TABLE = {
    "type": "table",
    "rows": [["Name", "Count"], ["Widgets", 42]],
    "columns": [{}, {"align": "right", "wrap": False}],
}
PIE = {"type": "chart", "kind": "pie", "title": "Share", "segments": [{"label": "A", "value": 1}]}
BAR = {
    "type": "chart",
    "kind": "bar",
    "title": "Revenue",
    "categories": ["Jan", "Feb"],
    "series": [{"name": "Sales", "data": [10, 20.5]}],
    "y_label": "USD",
}


def envelope(blocks: list, **overrides: object) -> str:
    payload = {"version": 1, "fallback_text": "plain", "blocks": blocks, **overrides}
    return f"```enso-message\n{json.dumps(payload)}\n```"


def test_parses_every_block_type() -> None:
    message = parse_outbound_message("\n " + envelope([MARKDOWN, TABLE, PIE, BAR]) + "\n")
    assert message is not None and message.fallback_text == "plain"
    assert message.blocks == (
        MarkdownBlock("# Hi"),
        TableBlock((("Name", "Count"), ("Widgets", 42)), (Column(), Column("right", False))),
        ChartBlock("pie", "Share", segments=(Segment("A", 1),)),
        ChartBlock(
            "bar",
            "Revenue",
            categories=("Jan", "Feb"),
            series=(Series("Sales", (10, 20.5)),),
            y_label="USD",
        ),
    )


def test_plain_text_is_not_an_envelope() -> None:
    assert parse_outbound_message("Just **markdown**, no fence.") is None


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Here you go:\n" + envelope([MARKDOWN]), "no text outside it"),
        ("```enso-message\n{oops\n```", "not valid JSON"),
        (envelope([TABLE]).replace("42", str(10**400)), "row 2 cell 2 must be a non-blank"),
        (envelope([TABLE]).replace("42", "1" * 5000), "not valid JSON (a value is too large)"),
        ("```enso-message\n" + "[" * 100_000 + "\n```", "not valid JSON (a value is too large)"),
    ],
)
def test_rejects_with_a_reason(text: str, reason: str) -> None:
    with pytest.raises(EnvelopeError) as info:
        parse_outbound_message(text)
    assert reason in info.value.reason


GOOD = envelope([MARKDOWN], fallback_text="Plain answer")


@pytest.mark.parametrize(
    ("text", "fallback"),
    [
        (envelope([], fallback_text="Plain answer"), "Plain answer"),
        ("Here you go:\n" + GOOD, "Plain answer"),
        (GOOD + "\nAnything else?", "Plain answer"),
        ("Note:\n" + envelope([], fallback_text=" "), None),
        ("```enso-message\n{\n```", None),
    ],
)
def test_invalid_envelope_keeps_a_usable_fallback(text: str, fallback: str | None) -> None:
    with pytest.raises(EnvelopeError) as info:
        parse_outbound_message(text)
    assert info.value.fallback == fallback
