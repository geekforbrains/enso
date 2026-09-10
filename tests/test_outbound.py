"""``enso-message`` parsing: what is delivered natively and what the correction prompt says."""

from __future__ import annotations

import json

import pytest

from enso.outbound import (
    CONTRACT,
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


def test_contract_uses_workspace_relative_inline_paths_for_local_files() -> None:
    assert "workspace-relative path in inline code" in CONTRACT
    assert "never by an absolute path or Markdown link" in CONTRACT


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Here you go:\n" + envelope([MARKDOWN]), "no text outside it"),
        ("```enso-message\n{oops\n```", "not valid JSON"),
        (envelope([MARKDOWN], extra=1), "the envelope has unknown key extra"),
        (envelope([MARKDOWN], version=2), "version must be 1"),
        (envelope([MARKDOWN], fallback_text=" "), "fallback_text must be a non-blank string"),
        (envelope([]), "blocks must be a non-empty array"),
        (envelope([{"type": "section"}]), "block 1 type must be markdown, table, or chart"),
        (envelope([{"type": "markdown", "text": "x" * 12_001}]), "markdown text exceeds 12,000"),
        (
            envelope([{"type": "table", "rows": [["a", "b"], ["c"]]}]),
            "row 2 has 1 cells, expected 2",
        ),
        (envelope([{"type": "table", "rows": [["a", True]]}]), "row 1 cell 2 must be a non-blank"),
        (envelope([{"type": "table", "rows": [["a"]], "columns": [{}, {}]}]), "at most one entry"),
        (envelope([{**PIE, "segments": [{"label": "A", "value": 0}]}]), "segment 1 value must be"),
        (
            envelope([{**BAR, "series": [{"name": "S", "data": [1]}]}]),
            "one number per category (2)",
        ),
        (envelope([{**BAR, "kind": "area"}]), "chart kind must be pie, bar, or line"),
        (envelope([{**PIE, "title": "x" * 51}]), "block 1 (chart): chart title exceeds 50"),
        (envelope([PIE, PIE, PIE]), "at most 2 charts per message"),
        (envelope([MARKDOWN]) + "\n" + envelope([MARKDOWN]), "no text outside it"),
        (envelope([{"type": [], "text": "x"}]), "block 1 type must be markdown, table, or chart"),
        (envelope([TABLE]).replace("42", str(10**400)), "row 2 cell 2 must be a non-blank"),
        (envelope([{**PIE, "segments": [{"label": "A", "value": 10**400}]}]), "value must be a"),
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
        ("```enso-message json\n" + GOOD.split("\n", 1)[1], "Plain answer"),
        (GOOD + "\n" + envelope([MARKDOWN]), "Plain answer"),
        ("Note:\n" + envelope([], fallback_text=" "), None),
        ("Note:\n" + GOOD.rsplit("\n", 1)[0], None),
        ("Note:\n```enso-message\n{\n```", None),
        ("Note:\n" + GOOD.replace('"# Hi"', "1" * 5000), None),
        ("```enso-message\n{\n```", None),
    ],
)
def test_invalid_envelope_keeps_a_usable_fallback(text: str, fallback: str | None) -> None:
    with pytest.raises(EnvelopeError) as info:
        parse_outbound_message(text)
    assert info.value.fallback == fallback
