"""Typed, transport-neutral structured outbound messages."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

MAX_FALLBACK_TEXT = 4000
MAX_BLOCKS_PER_MESSAGE = 50
MAX_MARKDOWN_TEXT = 12000
MAX_DATA_TABLE_ROWS = 201
MAX_TABLE_ROWS = 100
MAX_TABLE_COLUMNS = 20
MAX_DATA_TABLE_TEXT = 20000
MAX_TABLE_TEXT = 10000
MAX_NATIVE_TABLE_TEXT = 20000
MAX_DATA_TABLE_PAGE_SIZE = 100
MAX_SECTION_FIELDS = 10
MAX_SECTION_FIELD_TEXT = 2000
MAX_DATA_VISUALIZATION_BLOCKS = 2
MAX_VISUALIZATION_TITLE = 50
MAX_CHART_SERIES = 12
MAX_CHART_POINTS = 20
MAX_CHART_LABEL = 20
MAX_AXIS_LABEL = 50
MAX_PIE_SEGMENTS = 12

STRUCTURED_OUTPUT_INSTRUCTIONS = (
    "[Enso structured output capability]\n"
    "When an explicitly structured Slack layout is useful, your entire final "
    "response may use this exact envelope:\n"
    "```enso-message\n"
    '{"version":1,"fallback_text":"Complete plain-text equivalent",'
    '"blocks":[{"type":"markdown","text":"# Rich presentation"}]}\n'
    "```\n"
    "The fence body must be valid JSON with exactly version, fallback_text, "
    "and blocks; version must be the integer 1. fallback_text must be nonblank, "
    f"complete, and at most {MAX_FALLBACK_TEXT:,} characters. blocks must contain "
    f"1 to {MAX_BLOCKS_PER_MESSAGE} items. Supported blocks are:\n"
    "- Markdown: exactly type and text fields with type markdown: "
    '{"type":"markdown","text":"nonblank Markdown"}. '
    f"Markdown text is limited to {MAX_MARKDOWN_TEXT:,} cumulative characters.\n"
    '- Pageable, sortable, filterable data table: {"type":"data_table",'
    '"caption":"nonblank caption","rows":[...]} with optional integer page_size '
    f"from 1 to {MAX_DATA_TABLE_PAGE_SIZE} and optional zero-based integer "
    "row_header_column_index. The first row is a text-only header, followed by "
    f"1 to {MAX_DATA_TABLE_ROWS - 1} data rows.\n"
    '- Simple aligned/wrapped table: {"type":"table","rows":[...]} with optional '
    'column_settings such as [{"align":"left","is_wrapped":true},{}]. align is '
    f"left, center, or right. Use 1 to {MAX_TABLE_ROWS} rows.\n"
    'Every row is an array of equal length. A text cell is exactly {"type":"text",'
    '"text":"nonblank display text"}; a numeric cell is exactly {"type":"number",'
    '"value":42,"text":"42"}. Use 1 to '
    f"{MAX_TABLE_COLUMNS} columns. Each data_table allows {MAX_DATA_TABLE_TEXT:,} "
    f"cell characters; each table allows {MAX_TABLE_TEXT:,} cell characters; all "
    "native table "
    f"blocks combined allow {MAX_NATIVE_TABLE_TEXT:,} cell characters per message.\n"
    '- Compact two-column fields: {"type":"section","fields":['
    '{"type":"markdown","text":"**MRR**\\n$42k"},'
    '{"type":"text","text":"On target"}]}. Use 1 to '
    f"{MAX_SECTION_FIELDS} fields, each at most {MAX_SECTION_FIELD_TEXT:,} characters.\n"
    '- Charts: {"type":"data_visualization","title":"Short title","chart":{...}}. '
    f"Use at most {MAX_DATA_VISUALIZATION_BLOCKS} charts per message and titles up "
    f"to {MAX_VISUALIZATION_TITLE} characters. A pie chart is "
    '{"type":"pie","segments":[{"label":"A","value":1}]}; use 1 to '
    f"{MAX_PIE_SEGMENTS} segments and pie values must be positive. A series chart is "
    '{"type":"line","series":[{"name":"Revenue","data":['
    '{"label":"Jan","value":10}]}],"axis_config":{"categories":["Jan"],'
    '"x_label":"Month","y_label":"USD"}}; x_label and y_label are optional, and '
    "type may be line, bar, or area, with "
    f"1 to {MAX_CHART_SERIES} series and 1 to {MAX_CHART_POINTS} points each. "
    f"Series, category, point, and segment labels allow {MAX_CHART_LABEL} characters; "
    f"optional axis labels allow {MAX_AXIS_LABEL} characters. Every series must cover "
    "each category exactly once; line, bar, and area values may be negative.\n"
    "Add no prose outside the fence, and otherwise respond normally.\n"
)


class _OutboundLimitError(ValueError):
    """Raised when a recognized envelope exceeds a supported delivery limit."""


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """A standard-Markdown presentation block."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("markdown block text must be a non-empty string")
        if len(self.text) > MAX_MARKDOWN_TEXT:
            raise ValueError(
                f"markdown block text exceeds {MAX_MARKDOWN_TEXT} characters"
            )


@dataclass(frozen=True, slots=True)
class SectionField:
    """One compact summary field in standard Markdown or plain text."""

    kind: str
    text: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in {"markdown", "text"}:
            raise ValueError("section field kind must be markdown or text")
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("section field text must be a non-empty string")


@dataclass(frozen=True, slots=True)
class SectionFieldsBlock:
    """Compact summary fields rendered in two columns."""

    fields: tuple[SectionField, ...]

    def __post_init__(self) -> None:
        if type(self.fields) is not tuple or not self.fields:
            raise ValueError("section fields must be a non-empty tuple")
        if not all(isinstance(field, SectionField) for field in self.fields):
            raise ValueError("section contains an invalid field")
        if len(self.fields) > MAX_SECTION_FIELDS:
            raise _OutboundLimitError(
                f"section exceeds {MAX_SECTION_FIELDS} fields"
            )
        if any(len(field.text) > MAX_SECTION_FIELD_TEXT for field in self.fields):
            raise _OutboundLimitError(
                f"section field exceeds {MAX_SECTION_FIELD_TEXT} characters"
            )


def _is_finite_number(value: Any) -> bool:
    return type(value) in (int, float) and (
        type(value) is int or math.isfinite(value)
    )


@dataclass(frozen=True, slots=True)
class ChartSegment:
    """One labeled positive slice in a pie chart."""

    label: str
    value: int | float

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label.strip():
            raise ValueError("chart segment label must be a non-empty string")
        if not _is_finite_number(self.value) or self.value <= 0:
            raise ValueError("chart segment value must be a positive finite number")


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """One labeled numeric point in a series chart."""

    label: str
    value: int | float

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label.strip():
            raise ValueError("chart point label must be a non-empty string")
        if not _is_finite_number(self.value):
            raise ValueError("chart point value must be a finite number")


@dataclass(frozen=True, slots=True)
class ChartSeries:
    """One named series of chart data points."""

    name: str
    data: tuple[ChartPoint, ...]

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("chart series name must be a non-empty string")
        if type(self.data) is not tuple or not self.data:
            raise ValueError("chart series data must be a non-empty tuple")
        if not all(isinstance(point, ChartPoint) for point in self.data):
            raise ValueError("chart series contains an invalid point")


@dataclass(frozen=True, slots=True)
class ChartAxis:
    """Categories and optional axis labels for a series chart."""

    categories: tuple[str, ...]
    x_label: str | None = None
    y_label: str | None = None

    def __post_init__(self) -> None:
        if type(self.categories) is not tuple or not self.categories:
            raise ValueError("chart categories must be a non-empty tuple")
        if not all(
            type(category) is str and category.strip() for category in self.categories
        ):
            raise ValueError("chart categories must be non-empty strings")
        if len(set(self.categories)) != len(self.categories):
            raise ValueError("chart categories must be unique")
        for attribute in ("x_label", "y_label"):
            label = getattr(self, attribute)
            if label is not None and type(label) is not str:
                raise ValueError("chart axis labels must be strings")
            if label is not None and not label.strip():
                object.__setattr__(self, attribute, None)


@dataclass(frozen=True, slots=True)
class PieChart:
    """A pie chart composed of positive segments."""

    segments: tuple[ChartSegment, ...]

    def __post_init__(self) -> None:
        if type(self.segments) is not tuple or not self.segments:
            raise ValueError("pie chart segments must be a non-empty tuple")
        if not all(isinstance(segment, ChartSegment) for segment in self.segments):
            raise ValueError("pie chart contains an invalid segment")
        if len(self.segments) > MAX_PIE_SEGMENTS:
            raise _OutboundLimitError(
                f"pie chart exceeds {MAX_PIE_SEGMENTS} segments"
            )
        if any(len(segment.label) > MAX_CHART_LABEL for segment in self.segments):
            raise _OutboundLimitError(
                f"pie chart label exceeds {MAX_CHART_LABEL} characters"
            )


@dataclass(frozen=True, slots=True)
class SeriesChart:
    """A line, bar, or area chart with shared category axes."""

    chart_type: str
    series: tuple[ChartSeries, ...]
    axis_config: ChartAxis

    def __post_init__(self) -> None:
        if type(self.chart_type) is not str or self.chart_type not in {
            "line",
            "bar",
            "area",
        }:
            raise ValueError("series chart type must be line, bar, or area")
        if type(self.series) is not tuple or not self.series:
            raise ValueError("chart series must be a non-empty tuple")
        if not all(isinstance(series, ChartSeries) for series in self.series):
            raise ValueError("chart contains an invalid series")
        if not isinstance(self.axis_config, ChartAxis):
            raise ValueError("chart contains an invalid axis_config")

        names = tuple(series.name for series in self.series)
        if len(set(names)) != len(names):
            raise ValueError("chart series names must be unique")
        categories = set(self.axis_config.categories)
        for series in self.series:
            labels = tuple(point.label for point in series.data)
            if len(set(labels)) != len(labels) or set(labels) != categories:
                raise ValueError(
                    "each chart series must cover every category exactly once"
                )

        if len(self.series) > MAX_CHART_SERIES:
            raise _OutboundLimitError(
                f"chart exceeds {MAX_CHART_SERIES} series"
            )
        if any(len(name) > MAX_CHART_LABEL for name in names):
            raise _OutboundLimitError(
                f"chart series name exceeds {MAX_CHART_LABEL} characters"
            )
        if len(self.axis_config.categories) > MAX_CHART_POINTS:
            raise _OutboundLimitError(
                f"chart exceeds {MAX_CHART_POINTS} categories"
            )
        if any(
            len(category) > MAX_CHART_LABEL
            for category in self.axis_config.categories
        ):
            raise _OutboundLimitError(
                f"chart category exceeds {MAX_CHART_LABEL} characters"
            )
        for series in self.series:
            if len(series.data) > MAX_CHART_POINTS:
                raise _OutboundLimitError(
                    f"chart series exceeds {MAX_CHART_POINTS} points"
                )
            if any(len(point.label) > MAX_CHART_LABEL for point in series.data):
                raise _OutboundLimitError(
                    f"chart point label exceeds {MAX_CHART_LABEL} characters"
                )
        for label in (self.axis_config.x_label, self.axis_config.y_label):
            if label is not None and len(label) > MAX_AXIS_LABEL:
                raise _OutboundLimitError(
                    f"chart axis label exceeds {MAX_AXIS_LABEL} characters"
                )


@dataclass(frozen=True, slots=True)
class DataVisualizationBlock:
    """A native pie, line, bar, or area chart."""

    title: str
    chart: PieChart | SeriesChart

    def __post_init__(self) -> None:
        if type(self.title) is not str or not self.title.strip():
            raise ValueError("visualization title must be a non-empty string")
        if not isinstance(self.chart, (PieChart, SeriesChart)):
            raise ValueError("visualization contains an invalid chart")
        if len(self.title) > MAX_VISUALIZATION_TITLE:
            raise _OutboundLimitError(
                f"visualization title exceeds {MAX_VISUALIZATION_TITLE} characters"
            )


@dataclass(frozen=True, slots=True)
class TableTextCell:
    """A plain-text table cell."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("table cell text must be a non-empty string")


@dataclass(frozen=True, slots=True)
class TableNumberCell:
    """A sortable number plus its human-readable display text."""

    value: int | float
    text: str

    def __post_init__(self) -> None:
        if type(self.value) not in (int, float):
            raise ValueError("table number value must be a JSON number")
        if type(self.value) is float and not math.isfinite(self.value):
            raise ValueError("table number value must be finite")
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("table number text must be a non-empty string")


TableCell = TableTextCell | TableNumberCell


def _inspect_rows(
    rows: tuple[tuple[TableCell, ...], ...],
    *,
    text_header: bool = False,
) -> tuple[int, int, int]:
    if type(rows) is not tuple or not rows:
        raise ValueError("table rows must be a non-empty tuple")
    if type(rows[0]) is not tuple or not rows[0]:
        raise ValueError("table rows must contain at least one column")

    column_count = len(rows[0])
    for row in rows:
        if type(row) is not tuple or len(row) != column_count:
            raise ValueError("table rows must have equal column counts")
        if not all(isinstance(cell, (TableTextCell, TableNumberCell)) for cell in row):
            raise ValueError("table contains an unsupported cell type")
    if text_header and not all(isinstance(cell, TableTextCell) for cell in rows[0]):
        raise ValueError("data table header cells must be text")
    return len(rows), column_count, _table_text_length(rows)


def _table_text_length(rows: tuple[tuple[TableCell, ...], ...]) -> int:
    return sum(len(cell.text) for row in rows for cell in row)


@dataclass(frozen=True, slots=True)
class DataTableBlock:
    """A pageable, sortable, and filterable dataset."""

    caption: str
    rows: tuple[tuple[TableCell, ...], ...]
    page_size: int | None = None
    row_header_column_index: int | None = None

    def __post_init__(self) -> None:
        if type(self.caption) is not str or not self.caption.strip():
            raise ValueError("data table caption must be a non-empty string")
        row_count, column_count, text_length = _inspect_rows(
            self.rows, text_header=True
        )
        if row_count < 2:
            raise ValueError("data table requires a header and at least one data row")
        if self.page_size is not None and type(self.page_size) is not int:
            raise ValueError("data table page_size must be an integer")
        if self.page_size is not None and self.page_size < 1:
            raise ValueError("data table page_size must be positive")
        if self.row_header_column_index is not None and (
            type(self.row_header_column_index) is not int
            or not 0 <= self.row_header_column_index < column_count
        ):
            raise ValueError("data table row_header_column_index is out of range")

        if row_count > MAX_DATA_TABLE_ROWS:
            raise _OutboundLimitError(
                f"data table exceeds {MAX_DATA_TABLE_ROWS} rows"
            )
        if column_count > MAX_TABLE_COLUMNS:
            raise _OutboundLimitError(
                f"data table exceeds {MAX_TABLE_COLUMNS} columns"
            )
        if text_length > MAX_DATA_TABLE_TEXT:
            raise _OutboundLimitError(
                f"data table cells exceed {MAX_DATA_TABLE_TEXT} characters"
            )
        if (
            self.page_size is not None
            and self.page_size > MAX_DATA_TABLE_PAGE_SIZE
        ):
            raise _OutboundLimitError(
                f"data table page_size must be 1 to {MAX_DATA_TABLE_PAGE_SIZE}"
            )


@dataclass(frozen=True, slots=True)
class TableColumnSetting:
    """Alignment and wrapping for one simple-table column."""

    align: str | None = None
    is_wrapped: bool | None = None

    def __post_init__(self) -> None:
        if self.align is not None and (
            type(self.align) is not str
            or self.align not in {"left", "center", "right"}
        ):
            raise ValueError("table column align must be left, center, or right")
        if self.is_wrapped is not None and type(self.is_wrapped) is not bool:
            raise ValueError("table column is_wrapped must be a boolean")


@dataclass(frozen=True, slots=True)
class TableBlock:
    """A simple table with optional alignment and wrapping."""

    rows: tuple[tuple[TableCell, ...], ...]
    column_settings: tuple[TableColumnSetting, ...] = ()

    def __post_init__(self) -> None:
        row_count, column_count, text_length = _inspect_rows(self.rows)
        if type(self.column_settings) is not tuple:
            raise ValueError("table column_settings must be a tuple")
        if not all(
            isinstance(setting, TableColumnSetting) for setting in self.column_settings
        ):
            raise ValueError("table contains an invalid column setting")
        if len(self.column_settings) > column_count:
            raise ValueError("table column_settings exceeds the table width")

        if row_count > MAX_TABLE_ROWS:
            raise _OutboundLimitError(f"table exceeds {MAX_TABLE_ROWS} rows")
        if column_count > MAX_TABLE_COLUMNS:
            raise _OutboundLimitError(f"table exceeds {MAX_TABLE_COLUMNS} columns")
        if text_length > MAX_TABLE_TEXT:
            raise _OutboundLimitError(
                f"table cells exceed {MAX_TABLE_TEXT} characters"
            )


OutboundBlock = (
    MarkdownBlock
    | SectionFieldsBlock
    | DataVisualizationBlock
    | DataTableBlock
    | TableBlock
)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A rich presentation plus its complete human-readable fallback."""

    fallback_text: str
    blocks: tuple[OutboundBlock, ...]

    def __post_init__(self) -> None:
        _validate_fallback_text(self.fallback_text)
        if type(self.blocks) is not tuple or not self.blocks:
            raise ValueError("blocks must be a non-empty tuple")
        if len(self.blocks) > MAX_BLOCKS_PER_MESSAGE:
            raise ValueError(f"blocks exceeds {MAX_BLOCKS_PER_MESSAGE} items")
        if not all(
            isinstance(
                block,
                (
                    MarkdownBlock,
                    SectionFieldsBlock,
                    DataVisualizationBlock,
                    DataTableBlock,
                    TableBlock,
                ),
            )
            for block in self.blocks
        ):
            raise ValueError("message contains an unsupported block type")

        markdown_text = sum(
            len(block.text) for block in self.blocks if isinstance(block, MarkdownBlock)
        )
        if markdown_text > MAX_MARKDOWN_TEXT:
            raise ValueError(
                f"markdown blocks exceed {MAX_MARKDOWN_TEXT} cumulative characters"
            )

        native_table_text = sum(
            _table_text_length(block.rows)
            for block in self.blocks
            if isinstance(block, (DataTableBlock, TableBlock))
        )
        if native_table_text > MAX_NATIVE_TABLE_TEXT:
            raise _OutboundLimitError(
                "native table blocks exceed "
                f"{MAX_NATIVE_TABLE_TEXT} cumulative cell characters"
            )
        visualization_count = sum(
            isinstance(block, DataVisualizationBlock) for block in self.blocks
        )
        if visualization_count > MAX_DATA_VISUALIZATION_BLOCKS:
            raise _OutboundLimitError(
                "message exceeds "
                f"{MAX_DATA_VISUALIZATION_BLOCKS} data visualization blocks"
            )


def _validate_fallback_text(value: Any) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError("fallback_text must be a non-empty string")
    if len(value) > MAX_FALLBACK_TEXT:
        raise ValueError(f"fallback_text exceeds {MAX_FALLBACK_TEXT} characters")


class _DuplicateKeyError(ValueError):
    """Raised when an envelope object repeats a JSON key."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _parse_table_cell(value: Any) -> TableCell:
    if type(value) is not dict:
        raise ValueError("invalid table cell schema")
    if value.get("type") == "text" and set(value) == {"type", "text"}:
        return TableTextCell(text=value["text"])
    if value.get("type") == "number" and set(value) == {"type", "value", "text"}:
        return TableNumberCell(value=value["value"], text=value["text"])
    raise ValueError("invalid table cell schema")


def _parse_rows(value: Any) -> tuple[tuple[TableCell, ...], ...]:
    if type(value) is not list:
        raise ValueError("invalid table rows")
    rows: list[tuple[TableCell, ...]] = []
    for row in value:
        if type(row) is not list:
            raise ValueError("invalid table row")
        rows.append(tuple(_parse_table_cell(cell) for cell in row))
    return tuple(rows)


def _parse_markdown_block(value: dict[str, Any]) -> MarkdownBlock:
    if set(value) != {"type", "text"}:
        raise ValueError("invalid markdown block schema")
    return MarkdownBlock(text=value["text"])


def _parse_data_table_block(value: dict[str, Any]) -> DataTableBlock:
    required = {"type", "caption", "rows"}
    allowed = required | {"page_size", "row_header_column_index"}
    if not required <= set(value) <= allowed:
        raise ValueError("invalid data table block schema")
    return DataTableBlock(
        caption=value["caption"],
        rows=_parse_rows(value["rows"]),
        page_size=value.get("page_size"),
        row_header_column_index=value.get("row_header_column_index"),
    )


def _parse_column_settings(value: Any) -> tuple[TableColumnSetting, ...]:
    if type(value) is not list:
        raise ValueError("invalid table column_settings")
    settings: list[TableColumnSetting] = []
    for setting in value:
        if type(setting) is not dict or not set(setting) <= {"align", "is_wrapped"}:
            raise ValueError("invalid table column setting")
        settings.append(
            TableColumnSetting(
                align=setting.get("align"),
                is_wrapped=setting.get("is_wrapped"),
            )
        )
    return tuple(settings)


def _parse_table_block(value: dict[str, Any]) -> TableBlock:
    required = {"type", "rows"}
    allowed = required | {"column_settings"}
    if not required <= set(value) <= allowed:
        raise ValueError("invalid table block schema")
    return TableBlock(
        rows=_parse_rows(value["rows"]),
        column_settings=(
            _parse_column_settings(value["column_settings"])
            if "column_settings" in value
            else ()
        ),
    )


def _parse_section_field(value: Any) -> SectionField:
    if type(value) is not dict or set(value) != {"type", "text"}:
        raise ValueError("invalid section field schema")
    return SectionField(kind=value["type"], text=value["text"])


def _parse_section_fields_block(value: dict[str, Any]) -> SectionFieldsBlock:
    if set(value) != {"type", "fields"} or type(value["fields"]) is not list:
        raise ValueError("invalid section fields block schema")
    return SectionFieldsBlock(
        fields=tuple(_parse_section_field(field) for field in value["fields"])
    )


def _parse_chart_segment(value: Any) -> ChartSegment:
    if type(value) is not dict or set(value) != {"label", "value"}:
        raise ValueError("invalid chart segment schema")
    return ChartSegment(label=value["label"], value=value["value"])


def _parse_chart_point(value: Any) -> ChartPoint:
    if type(value) is not dict or set(value) != {"label", "value"}:
        raise ValueError("invalid chart point schema")
    return ChartPoint(label=value["label"], value=value["value"])


def _parse_chart_series(value: Any) -> ChartSeries:
    if (
        type(value) is not dict
        or set(value) != {"name", "data"}
        or type(value["data"]) is not list
    ):
        raise ValueError("invalid chart series schema")
    return ChartSeries(
        name=value["name"],
        data=tuple(_parse_chart_point(point) for point in value["data"]),
    )


def _parse_chart_axis(value: Any) -> ChartAxis:
    required = {"categories"}
    allowed = required | {"x_label", "y_label"}
    if (
        type(value) is not dict
        or not required <= set(value) <= allowed
        or type(value["categories"]) is not list
    ):
        raise ValueError("invalid chart axis schema")
    return ChartAxis(
        categories=tuple(value["categories"]),
        x_label=value.get("x_label"),
        y_label=value.get("y_label"),
    )


def _parse_pie_chart(value: dict[str, Any]) -> PieChart:
    if set(value) != {"type", "segments"} or type(value["segments"]) is not list:
        raise ValueError("invalid pie chart schema")
    return PieChart(
        segments=tuple(_parse_chart_segment(segment) for segment in value["segments"])
    )


def _parse_series_chart(value: dict[str, Any]) -> SeriesChart:
    if (
        set(value) != {"type", "series", "axis_config"}
        or type(value["series"]) is not list
    ):
        raise ValueError("invalid series chart schema")
    return SeriesChart(
        chart_type=value["type"],
        series=tuple(_parse_chart_series(series) for series in value["series"]),
        axis_config=_parse_chart_axis(value["axis_config"]),
    )


def _parse_data_visualization_block(value: dict[str, Any]) -> DataVisualizationBlock:
    if set(value) != {"type", "title", "chart"} or type(value["chart"]) is not dict:
        raise ValueError("invalid data visualization block schema")
    if type(value["title"]) is not str or not value["title"].strip():
        raise ValueError("visualization title must be a non-empty string")
    chart_value = value["chart"]
    chart_type = chart_value.get("type")
    if chart_type == "pie":
        chart: PieChart | SeriesChart = _parse_pie_chart(chart_value)
    elif chart_type in {"line", "bar", "area"}:
        chart = _parse_series_chart(chart_value)
    else:
        raise ValueError("unsupported visualization chart type")
    return DataVisualizationBlock(title=value["title"], chart=chart)


def _parse_block(value: Any) -> OutboundBlock:
    if type(value) is not dict or type(value.get("type")) is not str:
        raise ValueError("invalid block schema")
    if value["type"] == "markdown":
        return _parse_markdown_block(value)
    if value["type"] == "section":
        return _parse_section_fields_block(value)
    if value["type"] == "data_visualization":
        return _parse_data_visualization_block(value)
    if value["type"] == "data_table":
        return _parse_data_table_block(value)
    if value["type"] == "table":
        return _parse_table_block(value)
    raise ValueError("unsupported block type")


def _parse_envelope_payload(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0] != "```enso-message" or lines[-1] != "```":
        return None

    try:
        payload = json.loads("\n".join(lines[1:-1]), object_pairs_hook=_unique_object)
        if type(payload) is not dict:
            return None
        if set(payload) != {"version", "fallback_text", "blocks"}:
            return None
        if type(payload["version"]) is not int or payload["version"] != 1:
            return None
        if type(payload["blocks"]) is not list:
            return None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return payload


def _build_outbound_message(payload: dict[str, Any]) -> OutboundMessage:
    _validate_fallback_text(payload["fallback_text"])
    if len(payload["blocks"]) > MAX_BLOCKS_PER_MESSAGE:
        raise ValueError(f"blocks exceeds {MAX_BLOCKS_PER_MESSAGE} items")
    blocks: list[OutboundBlock] = []
    limit_error: _OutboundLimitError | None = None
    for block in payload["blocks"]:
        try:
            blocks.append(_parse_block(block))
        except _OutboundLimitError as exc:
            limit_error = limit_error or exc
    markdown_text = sum(
        len(block.text) for block in blocks if isinstance(block, MarkdownBlock)
    )
    if markdown_text > MAX_MARKDOWN_TEXT:
        raise ValueError(
            f"markdown blocks exceed {MAX_MARKDOWN_TEXT} cumulative characters"
        )
    if limit_error is not None:
        raise limit_error
    return OutboundMessage(
        fallback_text=payload["fallback_text"],
        blocks=tuple(blocks),
    )


def parse_outbound_message(text: str) -> OutboundMessage | None:
    """Parse an exact versioned ``enso-message`` fence, or return ``None``.

    Invalid or unknown envelopes deliberately remain ordinary response text.
    Recognized native blocks that exceed delivery limits can use
    :func:`parse_outbound_fallback` instead.
    """
    payload = _parse_envelope_payload(text)
    if payload is None:
        return None
    try:
        return _build_outbound_message(payload)
    except (KeyError, TypeError, ValueError):
        return None


def parse_outbound_fallback(text: str) -> str | None:
    """Return fallback text only when a valid known envelope exceeds limits."""
    payload = _parse_envelope_payload(text)
    if payload is None:
        return None
    try:
        _build_outbound_message(payload)
    except _OutboundLimitError:
        return payload["fallback_text"]
    except (KeyError, TypeError, ValueError):
        return None
    return None
