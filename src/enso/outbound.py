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
    f"blocks combined allow {MAX_NATIVE_TABLE_TEXT:,} cell characters per message. "
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


OutboundBlock = MarkdownBlock | DataTableBlock | TableBlock


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
            isinstance(block, (MarkdownBlock, DataTableBlock, TableBlock))
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


def _parse_block(value: Any) -> OutboundBlock:
    if type(value) is not dict or type(value.get("type")) is not str:
        raise ValueError("invalid block schema")
    if value["type"] == "markdown":
        return _parse_markdown_block(value)
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
    Recognized native tables that exceed delivery limits can use
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
