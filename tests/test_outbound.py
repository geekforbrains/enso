"""Tests for explicit structured outbound messages."""

from __future__ import annotations

import json

import pytest

from enso.outbound import (
    MAX_AXIS_LABEL,
    MAX_BLOCKS_PER_MESSAGE,
    MAX_CHART_LABEL,
    MAX_CHART_POINTS,
    MAX_CHART_SERIES,
    MAX_DATA_TABLE_ROWS,
    MAX_DATA_TABLE_TEXT,
    MAX_DATA_VISUALIZATION_BLOCKS,
    MAX_FALLBACK_TEXT,
    MAX_MARKDOWN_TEXT,
    MAX_PIE_SEGMENTS,
    MAX_SECTION_FIELD_TEXT,
    MAX_SECTION_FIELDS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MAX_TABLE_TEXT,
    MAX_VISUALIZATION_TITLE,
    STRUCTURED_OUTPUT_INSTRUCTIONS,
    ChartAxis,
    ChartPoint,
    ChartSegment,
    ChartSeries,
    DataTableBlock,
    DataVisualizationBlock,
    MarkdownBlock,
    OutboundMessage,
    PieChart,
    SectionField,
    SectionFieldsBlock,
    SeriesChart,
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
    assert '"type":"section"' in instructions
    assert '"type":"data_visualization"' in instructions
    assert all(chart_type in instructions for chart_type in ("pie", "bar", "area", "line"))
    assert '"segments":[{"label":"A","value":1}]' in instructions
    assert '"series":[{"name":"Revenue","data":[' in instructions
    assert '"axis_config":{"categories":["Jan"]' in instructions
    assert f"{MAX_SECTION_FIELDS} fields" in instructions
    assert f"{MAX_SECTION_FIELD_TEXT:,} characters" in instructions
    assert f"{MAX_DATA_VISUALIZATION_BLOCKS} charts" in instructions
    assert f"{MAX_VISUALIZATION_TITLE} characters" in instructions
    assert f"{MAX_CHART_SERIES} series" in instructions
    assert f"{MAX_CHART_POINTS} points" in instructions
    assert f"{MAX_CHART_LABEL} characters" in instructions
    assert f"{MAX_AXIS_LABEL} characters" in instructions
    assert f"{MAX_PIE_SEGMENTS} segments" in instructions
    assert "pie values must be positive" in instructions
    assert "line, bar, and area values may be negative" in instructions


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


def _section(**overrides: object) -> dict:
    block = {
        "type": "section",
        "fields": [
            {"type": "markdown", "text": "**MRR**\n$42k"},
            {"type": "text", "text": "On target"},
        ],
    }
    block.update(overrides)
    return block


def _segment(label: object = "Enterprise", value: object = 60) -> dict:
    return {"label": label, "value": value}


def _point(label: object = "Jan", value: object = 10) -> dict:
    return {"label": label, "value": value}


def _series(name: object = "Revenue", data: object | None = None) -> dict:
    return {
        "name": name,
        "data": [_point()] if data is None else data,
    }


def _axis(categories: object | None = None, **overrides: object) -> dict:
    axis = {"categories": ["Jan"] if categories is None else categories}
    axis.update(overrides)
    return axis


def _pie_chart(**overrides: object) -> dict:
    chart = {"type": "pie", "segments": [_segment()]}
    chart.update(overrides)
    return chart


def _series_chart(chart_type: str = "line", **overrides: object) -> dict:
    chart = {
        "type": chart_type,
        "series": [_series()],
        "axis_config": _axis(),
    }
    chart.update(overrides)
    return chart


def _visualization(chart: object | None = None, **overrides: object) -> dict:
    block = {
        "type": "data_visualization",
        "title": "Revenue mix",
        "chart": _pie_chart() if chart is None else chart,
    }
    block.update(overrides)
    return block


def test_parse_outbound_message_accepts_section_fields_and_pie_chart():
    message = parse_outbound_message(
        _envelope(
            _payload(
                fallback_text="MRR is $42k; enterprise contributes 60%.",
                blocks=[
                    _section(),
                    _visualization(
                        _pie_chart(
                            segments=[
                                _segment("Enterprise", 60),
                                _segment("Self-serve", 40.5),
                            ]
                        ),
                    ),
                ],
            )
        )
    )

    assert message == OutboundMessage(
        fallback_text="MRR is $42k; enterprise contributes 60%.",
        blocks=(
            SectionFieldsBlock(
                fields=(
                    SectionField(kind="markdown", text="**MRR**\n$42k"),
                    SectionField(kind="text", text="On target"),
                )
            ),
            DataVisualizationBlock(
                title="Revenue mix",
                chart=PieChart(
                    segments=(
                        ChartSegment(label="Enterprise", value=60),
                        ChartSegment(label="Self-serve", value=40.5),
                    )
                ),
            ),
        ),
    )


@pytest.mark.parametrize("chart_type", ["line", "bar", "area"])
def test_parse_outbound_message_accepts_series_charts(chart_type):
    chart = _series_chart(
        chart_type,
        series=[
            _series(
                "Revenue",
                [_point("Feb", 12.5), _point("Jan", -3)],
            ),
            _series(
                "Target",
                [_point("Jan", 0), _point("Feb", 10)],
            ),
        ],
        axis_config=_axis(
            ["Jan", "Feb"],
            x_label="Month",
            y_label="USD",
        ),
    )

    message = parse_outbound_message(
        _envelope(_payload(blocks=[_visualization(chart, title="Monthly revenue")]))
    )

    assert message == OutboundMessage(
        fallback_text="Accessible summary",
        blocks=(
            DataVisualizationBlock(
                title="Monthly revenue",
                chart=SeriesChart(
                    chart_type=chart_type,
                    series=(
                        ChartSeries(
                            name="Revenue",
                            data=(
                                ChartPoint(label="Feb", value=12.5),
                                ChartPoint(label="Jan", value=-3),
                            ),
                        ),
                        ChartSeries(
                            name="Target",
                            data=(
                                ChartPoint(label="Jan", value=0),
                                ChartPoint(label="Feb", value=10),
                            ),
                        ),
                    ),
                    axis_config=ChartAxis(
                        categories=("Jan", "Feb"),
                        x_label="Month",
                        y_label="USD",
                    ),
                ),
            ),
        ),
    )


def test_parse_outbound_message_accepts_summary_and_chart_boundaries():
    categories = tuple(
        "C" * MAX_CHART_LABEL if index == 0 else f"C{index:02}"
        for index in range(MAX_CHART_POINTS)
    )
    series = [
        _series(
            "N" * MAX_CHART_LABEL if index == 0 else f"Series {index}",
            [_point(label, index) for label in reversed(categories)],
        )
        for index in range(MAX_CHART_SERIES)
    ]
    blocks = [
        _section(
            fields=[
                {"type": "text", "text": "x" * MAX_SECTION_FIELD_TEXT}
                for _ in range(MAX_SECTION_FIELDS)
            ]
        ),
        _visualization(
            _pie_chart(
                segments=[
                    _segment(
                        "S" * MAX_CHART_LABEL if index == 0 else f"Segment {index}",
                        index + 1,
                    )
                    for index in range(MAX_PIE_SEGMENTS)
                ]
            ),
            title="P" * MAX_VISUALIZATION_TITLE,
        ),
        _visualization(
            _series_chart(
                "line",
                series=series,
                axis_config=_axis(
                    list(categories),
                    x_label="X" * MAX_AXIS_LABEL,
                    y_label="Y" * MAX_AXIS_LABEL,
                ),
            ),
        ),
    ]

    message = parse_outbound_message(_envelope(_payload(blocks=blocks)))

    assert message is not None
    assert len(message.blocks) == 3


def test_blank_optional_axis_labels_are_normalized_to_omission():
    message = parse_outbound_message(
        _envelope(
            _payload(
                blocks=[
                    _visualization(
                        _series_chart(axis_config=_axis(x_label="", y_label="   "))
                    )
                ]
            )
        )
    )

    assert message is not None
    chart = message.blocks[0].chart
    assert isinstance(chart, SeriesChart)
    assert chart.axis_config.x_label is None
    assert chart.axis_config.y_label is None


@pytest.mark.parametrize(
    "block",
    [
        {"type": "section", "fields": []},
        {"type": "section", "fields": "not a list"},
        {"type": "section", "fields": [None]},
        {"type": "section", "fields": [{"type": "markdown", "text": ""}]},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": "raw"}]},
        {
            "type": "section",
            "fields": [{"type": "text", "text": "x", "emoji": True}],
        },
        _visualization(title=""),
        _visualization(title=42),
        _visualization(
            _pie_chart(
                segments=[_segment(f"S{i}", i + 1) for i in range(MAX_PIE_SEGMENTS + 1)]
            ),
            title=42,
        ),
        _visualization(chart="not an object"),
        _visualization({"type": "scatter", "series": []}),
        _visualization(_pie_chart(segments=[])),
        _visualization(_pie_chart(segments=[None])),
        _visualization(_pie_chart(segments=[{"label": "A"}])),
        _visualization(_pie_chart(segments=[_segment(label="")])),
        _visualization(_pie_chart(segments=[_segment(value=True)])),
        _visualization(_pie_chart(segments=[_segment(value="1")])),
        _visualization(_pie_chart(segments=[_segment(value=float("inf"))])),
        _visualization(_pie_chart(segments=[_segment(value=0)])),
        _visualization(_pie_chart(segments=[_segment(value=-1)])),
        _visualization(_series_chart(series=[])),
        _visualization(_series_chart(series=[{"name": "Revenue"}])),
        _visualization(_series_chart(series=[_series(name="")])),
        _visualization(_series_chart(series=[_series(data=[])])),
        _visualization(_series_chart(series=[_series(data=[_point(value=True)])])),
        _visualization(_series_chart(series=[_series(data=[_point(value="1")])])),
        _visualization(
            _series_chart(series=[_series(data=[_point(value=float("nan"))])])
        ),
        _visualization(_series_chart(axis_config=_axis([]))),
        _visualization(_series_chart(axis_config=_axis([""]))),
        _visualization(_series_chart(axis_config=_axis(["Jan", "Jan"]))),
        _visualization(
            _series_chart(
                series=[_series("Revenue"), _series("Revenue")],
            )
        ),
        _visualization(
            _series_chart(
                series=[_series(data=[_point("Feb")])],
                axis_config=_axis(["Jan"]),
            )
        ),
        _visualization(
            _series_chart(
                series=[_series(data=[_point("Jan"), _point("Jan")])],
                axis_config=_axis(["Jan", "Feb"]),
            )
        ),
        _visualization(
            _series_chart(axis_config={"categories": ["Jan"], "extra": True})
        ),
    ],
)
def test_parse_outbound_message_rejects_invalid_summary_and_chart_schema(block):
    text = _envelope(_payload(blocks=[block]))

    assert parse_outbound_message(text) is None
    assert parse_outbound_fallback(text) is None


def _over_limit_summary_and_chart_blocks() -> list[dict]:
    categories = [f"C{i:02}" for i in range(MAX_CHART_POINTS + 1)]
    return [
        _section(
            fields=[{"type": "text", "text": "x"}]
            * (MAX_SECTION_FIELDS + 1)
        ),
        _section(
            fields=[
                {"type": "text", "text": "x" * (MAX_SECTION_FIELD_TEXT + 1)}
            ]
        ),
        _visualization(title="T" * (MAX_VISUALIZATION_TITLE + 1)),
        _visualization(
            _pie_chart(
                segments=[_segment(f"S{i}", i + 1) for i in range(MAX_PIE_SEGMENTS + 1)]
            )
        ),
        _visualization(_pie_chart(segments=[_segment("L" * (MAX_CHART_LABEL + 1))])),
        _visualization(
            _series_chart(
                series=[_series(f"S{i}") for i in range(MAX_CHART_SERIES + 1)]
            )
        ),
        _visualization(_series_chart(series=[_series("N" * (MAX_CHART_LABEL + 1))])),
        _visualization(
            _series_chart(
                series=[
                    _series(
                        data=[_point(label, 1) for label in categories],
                    )
                ],
                axis_config=_axis(categories),
            )
        ),
        _visualization(
            _series_chart(
                series=[_series(data=[_point("L" * (MAX_CHART_LABEL + 1))])],
                axis_config=_axis(["L" * (MAX_CHART_LABEL + 1)]),
            )
        ),
        _visualization(
            _series_chart(axis_config=_axis(x_label="X" * (MAX_AXIS_LABEL + 1)))
        ),
        _visualization(
            _series_chart(axis_config=_axis(y_label="Y" * (MAX_AXIS_LABEL + 1)))
        ),
    ]


@pytest.mark.parametrize("block", _over_limit_summary_and_chart_blocks())
def test_over_limit_summary_and_chart_exposes_complete_safe_fallback(block):
    text = _envelope(
        _payload(fallback_text="Compact complete fallback", blocks=[block])
    )

    assert parse_outbound_message(text) is None
    assert parse_outbound_fallback(text) == "Compact complete fallback"


def test_more_than_two_visualizations_exposes_complete_safe_fallback():
    text = _envelope(
        _payload(
            fallback_text="Compact chart summary",
            blocks=[_visualization(), _visualization(), _visualization()],
        )
    )

    assert parse_outbound_message(text) is None
    assert parse_outbound_fallback(text) == "Compact chart summary"


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
