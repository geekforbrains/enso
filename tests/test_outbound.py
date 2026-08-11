"""Tests for explicit structured outbound messages."""

from __future__ import annotations

import json

import pytest

from enso.outbound import (
    MAX_BLOCKS_PER_MESSAGE,
    MAX_DATA_TABLE_ROWS,
    MAX_DATA_TABLE_TEXT,
    MAX_FALLBACK_TEXT,
    MAX_MARKDOWN_TEXT,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MAX_TABLE_TEXT,
    STRUCTURED_OUTPUT_INSTRUCTIONS,
    DataTableBlock,
    MarkdownBlock,
    OutboundMessage,
    TableBlock,
    TableColumnSetting,
    TableNumberCell,
    TableTextCell,
    parse_outbound_fallback,
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
    assert '"type":"data_table"' in instructions
    assert '"type":"table"' in instructions
    assert '"type":"text"' in instructions
    assert '"type":"number"' in instructions
    assert "left, center, or right" in instructions
    assert f"{MAX_DATA_TABLE_ROWS - 1} data rows" in instructions
    assert f"{MAX_TABLE_ROWS} rows" in instructions
    assert f"{MAX_TABLE_COLUMNS} columns" in instructions
    assert "nonblank display text" in instructions
    assert f"1 to {MAX_TABLE_ROWS} rows" in instructions
    assert f"{MAX_DATA_TABLE_TEXT:,} cell characters" in instructions
    assert f"{MAX_TABLE_TEXT:,} cell characters" in instructions
    assert "all native table blocks combined" in instructions


def test_parse_outbound_message_accepts_native_table_blocks():
    payload = _payload(
        fallback_text="Ada scored 42; Grace scored 38.",
        blocks=[
            {"type": "markdown", "text": "# Scores"},
            {
                "type": "data_table",
                "caption": "Team scores",
                "page_size": 25,
                "row_header_column_index": 0,
                "rows": [
                    [
                        {"type": "text", "text": "Name"},
                        {"type": "text", "text": "Score"},
                    ],
                    [
                        {"type": "text", "text": "Ada"},
                        {"type": "number", "value": 42, "text": "42"},
                    ],
                    [
                        {"type": "text", "text": "Grace"},
                        {"type": "number", "value": 38.5, "text": "38.5"},
                    ],
                ],
            },
            {
                "type": "table",
                "rows": [
                    [
                        {"type": "text", "text": "Owner"},
                        {"type": "text", "text": "Notes"},
                        {"type": "text", "text": "Count"},
                    ],
                    [
                        {"type": "text", "text": "Ada"},
                        {"type": "text", "text": "Longer detail"},
                        {"type": "number", "value": 3, "text": "3"},
                    ],
                ],
                "column_settings": [
                    {},
                    {"is_wrapped": True},
                    {"align": "right", "is_wrapped": False},
                ],
            },
        ],
    )

    message = parse_outbound_message(_envelope(payload))

    assert message == OutboundMessage(
        fallback_text="Ada scored 42; Grace scored 38.",
        blocks=(
            MarkdownBlock(text="# Scores"),
            DataTableBlock(
                caption="Team scores",
                rows=(
                    (TableTextCell(text="Name"), TableTextCell(text="Score")),
                    (TableTextCell(text="Ada"), TableNumberCell(value=42, text="42")),
                    (
                        TableTextCell(text="Grace"),
                        TableNumberCell(value=38.5, text="38.5"),
                    ),
                ),
                page_size=25,
                row_header_column_index=0,
            ),
            TableBlock(
                rows=(
                    (
                        TableTextCell(text="Owner"),
                        TableTextCell(text="Notes"),
                        TableTextCell(text="Count"),
                    ),
                    (
                        TableTextCell(text="Ada"),
                        TableTextCell(text="Longer detail"),
                        TableNumberCell(value=3, text="3"),
                    ),
                ),
                column_settings=(
                    TableColumnSetting(),
                    TableColumnSetting(is_wrapped=True),
                    TableColumnSetting(align="right", is_wrapped=False),
                ),
            ),
        ),
    )


def _text_cell(text: str = "x") -> dict:
    return {"type": "text", "text": text}


def _number_cell(value: object = 1, text: object = "1") -> dict:
    return {"type": "number", "value": value, "text": text}


def _data_table(**overrides: object) -> dict:
    block = {
        "type": "data_table",
        "caption": "Results",
        "rows": [[_text_cell("Name")], [_text_cell("Ada")]],
    }
    block.update(overrides)
    return block


def _table(**overrides: object) -> dict:
    block = {"type": "table", "rows": [[_text_cell("Name")]]}
    block.update(overrides)
    return block


@pytest.mark.parametrize(
    "block",
    [
        _data_table(caption=""),
        _data_table(rows=[]),
        _data_table(rows=[[_text_cell("Header")]]),
        _data_table(rows=[[_text_cell("A")], [_text_cell("B"), _text_cell("C")]]),
        _data_table(rows=[[_number_cell()], [_number_cell()]]),
        _data_table(rows=[[_text_cell()] for _ in range(MAX_DATA_TABLE_ROWS + 1)]),
        _data_table(rows=[[_text_cell() for _ in range(MAX_TABLE_COLUMNS + 1)]] * 2),
        _data_table(page_size=0),
        _data_table(page_size=101),
        _data_table(page_size=True),
        _data_table(row_header_column_index=-1),
        _data_table(row_header_column_index=1),
        _data_table(row_header_column_index=True),
        _table(rows=[]),
        _table(rows=[[_text_cell()]] * (MAX_TABLE_ROWS + 1)),
        _table(rows=[[_text_cell() for _ in range(MAX_TABLE_COLUMNS + 1)]]),
        _table(rows=[[_text_cell("A")], [_text_cell("B"), _text_cell("C")]]),
        _table(column_settings=[{}] * (MAX_TABLE_COLUMNS + 1)),
        _table(column_settings=[{"align": "decimal"}]),
        _table(column_settings=[{"is_wrapped": "yes"}]),
        _table(column_settings=[{"align": "right", "extra": True}]),
        _table(column_settings=[None]),
        _table(column_settings=[42]),
        _table(rows=[[{"type": "rich_text", "elements": []}]]),
        _table(rows=[[{"type": "text", "text": "x", "extra": True}]]),
        _table(rows=[[_number_cell(value=True)]]),
        _table(rows=[[_number_cell(value=float("inf"))]]),
        _table(rows=[[_number_cell(value="1")]]),
        _table(rows=[[_number_cell(text="")]]),
    ],
)
def test_parse_outbound_message_rejects_invalid_native_table_schema(block):
    assert parse_outbound_message(_envelope(_payload(blocks=[block]))) is None


@pytest.mark.parametrize(
    "blocks",
    [
        [
            _data_table(
                rows=[
                    [_text_cell("H")],
                    [_text_cell("x" * (MAX_DATA_TABLE_TEXT - 1))],
                ]
            )
        ],
        [_table(rows=[[_text_cell("x" * MAX_TABLE_TEXT)]])],
        [
            _data_table(rows=[[_text_cell("H")], [_text_cell("x" * 10_000)]]),
            _data_table(rows=[[_text_cell("H")], [_text_cell("y" * 9_998)]]),
        ],
        [
            _table(rows=[[_text_cell("x" * MAX_TABLE_TEXT)]]),
            _table(rows=[[_text_cell("y" * MAX_TABLE_TEXT)]]),
        ],
        [
            _data_table(
                rows=[[_text_cell("H")], [_text_cell("x" * 14_999)]]
            ),
            _table(rows=[[_text_cell("y" * 5_000)]]),
        ],
    ],
)
def test_parse_outbound_message_accepts_native_table_character_boundaries(blocks):
    assert parse_outbound_message(_envelope(_payload(blocks=blocks))) is not None


@pytest.mark.parametrize(
    "block",
    [
        _data_table(rows=[[_text_cell()]] * MAX_DATA_TABLE_ROWS),
        _data_table(rows=[[_text_cell() for _ in range(MAX_TABLE_COLUMNS)]] * 2),
        _table(rows=[[_text_cell()]] * MAX_TABLE_ROWS),
        _table(rows=[[_text_cell() for _ in range(MAX_TABLE_COLUMNS)]]),
        _data_table(page_size=100),
    ],
)
def test_parse_outbound_message_accepts_native_table_shape_boundaries(block):
    assert parse_outbound_message(_envelope(_payload(blocks=[block]))) is not None


@pytest.mark.parametrize(
    "blocks",
    [
        [_data_table(rows=[[_text_cell("H")], [_text_cell("x" * MAX_DATA_TABLE_TEXT)]])],
        [_table(rows=[[_text_cell("x" * (MAX_TABLE_TEXT + 1))]])],
        [
            _data_table(rows=[[_text_cell("H")], [_text_cell("x" * 10_000)]]),
            _data_table(rows=[[_text_cell("H")], [_text_cell("y" * 10_000)]]),
        ],
        [
            _table(rows=[[_text_cell("x" * 7_000)]]),
            _table(rows=[[_text_cell("y" * 7_000)]]),
            _table(rows=[[_text_cell("z" * 7_000)]]),
        ],
        [
            _data_table(
                rows=[[_text_cell("H")], [_text_cell("x" * 14_999)]]
            ),
            _table(rows=[[_text_cell("y" * 5_001)]]),
        ],
    ],
)
def test_parse_outbound_message_rejects_native_table_character_overflow(blocks):
    assert parse_outbound_message(_envelope(_payload(blocks=blocks))) is None


def test_over_limit_native_table_exposes_its_complete_safe_fallback():
    text = _envelope(
        _payload(
            fallback_text="Compact complete fallback",
            blocks=[
                _data_table(
                    rows=[
                        [_text_cell("Header")],
                        [_text_cell("x" * MAX_DATA_TABLE_TEXT)],
                    ]
                )
            ],
        )
    )

    assert parse_outbound_message(text) is None
    assert parse_outbound_fallback(text) == "Compact complete fallback"
    assert parse_outbound_fallback(_envelope(_payload())) is None
    assert parse_outbound_fallback("```enso-message\n{not json}\n```") is None
    assert (
        parse_outbound_fallback(
            _envelope(
                _payload(blocks=[{"type": "section", "text": "unknown block"}])
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "block",
    [
        _data_table(rows=[[_text_cell("Header")]]),
        _data_table(page_size=True),
        _data_table(row_header_column_index=-1),
        _table(column_settings=[None]),
        _data_table(
            rows=[[_number_cell()]]
            + [[_text_cell()]] * MAX_DATA_TABLE_ROWS
        ),
    ],
)
def test_invalid_native_table_schema_does_not_expose_a_safe_fallback(block):
    text = _envelope(_payload(blocks=[block]))

    assert parse_outbound_message(text) is None
    assert parse_outbound_fallback(text) is None


@pytest.mark.parametrize(
    "blocks",
    [
        [{"type": "markdown", "text": "x" * (MAX_MARKDOWN_TEXT + 1)}],
        [{"type": "markdown", "text": "x"}] * (MAX_BLOCKS_PER_MESSAGE + 1),
    ],
)
def test_existing_structured_markdown_limits_keep_the_literal_response_path(blocks):
    text = _envelope(_payload(blocks=blocks))

    assert parse_outbound_message(text) is None
    assert parse_outbound_fallback(text) is None


@pytest.mark.parametrize(
    "other_blocks",
    [
        [{"type": "markdown", "text": "x"}] * MAX_BLOCKS_PER_MESSAGE,
        [
            {"type": "markdown", "text": "x" * 6_001},
            {"type": "markdown", "text": "y" * 6_000},
        ],
    ],
)
def test_native_limit_does_not_hide_existing_message_schema_errors(other_blocks):
    over_limit_table = _data_table(
        rows=[
            [_text_cell("Header")],
            [_text_cell("x" * MAX_DATA_TABLE_TEXT)],
        ]
    )
    text = _envelope(_payload(blocks=[over_limit_table, *other_blocks]))

    assert parse_outbound_message(text) is None
    assert parse_outbound_fallback(text) is None


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
