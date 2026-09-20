"""The ``enso-message`` envelope: markdown, table, and chart blocks for rich Slack replies."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

FENCE = "```enso-message"
_FENCE_RE = re.compile(r"^[ \t]*```enso-message\b", re.MULTILINE)

# Slack's Block Kit limits (docs.slack.dev/reference/block-kit/blocks).
MAX_FALLBACK_TEXT = 4000
MAX_BLOCKS = 50
MAX_MARKDOWN_TEXT = 12_000  # cumulative across markdown blocks
MAX_TABLE_ROWS = 100
MAX_TABLE_COLUMNS = 20
MAX_TABLE_TEXT = 10_000  # cumulative cell characters across table blocks
MAX_CHARTS = 2
MAX_CHART_TITLE = 50  # also axis labels
MAX_CHART_ITEMS = 12  # pie segments, series per chart
MAX_CHART_POINTS = 20
MAX_CHART_LABEL = 20  # categories, segment labels, series names

FAILURE_NOTICE = "I couldn't format that response correctly. Please try again."

CONTRACT = f"""[Slack rich format]
Reply as ordinary Markdown. Refer to a local file by its workspace-relative path in inline code \
(for example, `work/report.md`), never by an absolute path or Markdown link. Only when a native \
table or chart materially helps, make the \
entire reply exactly one fenced block and nothing else:
{FENCE}
{{"version":1,"fallback_text":"Complete plain-text equivalent",\
"blocks":[{{"type":"markdown","text":"Markdown"}}]}}
```
The fence body is JSON with exactly these keys: version (the integer 1), fallback_text \
(non-blank, complete, at most {MAX_FALLBACK_TEXT:,} characters), blocks (1 to {MAX_BLOCKS}). \
Block types:
- {{"type":"markdown","text":"…"}} — at most {MAX_MARKDOWN_TEXT:,} characters across all \
markdown blocks.
- {{"type":"table","rows":[["Name","Count"],["Widgets",42]]}} — rows of equal length, at most \
{MAX_TABLE_ROWS} rows, {MAX_TABLE_COLUMNS} columns, and {MAX_TABLE_TEXT:,} cell characters per \
message; a cell is a non-blank string or a number (Slack shows every cell as text, so \
format numbers yourself, e.g. "1,240" or "$18,600"; a column of numbers is right-aligned \
unless "columns" says otherwise); optional "columns":[{{"align":"right","wrap":true}}] per \
column from the left (align: left, center, right).
- {{"type":"chart","kind":"pie","title":"Share","segments":[{{"label":"A","value":1}}]}} — \
1 to {MAX_CHART_ITEMS} segments with positive values.
- {{"type":"chart","kind":"bar","title":"Revenue","categories":["Jan","Feb"],"series":\
[{{"name":"Sales","data":[10,20]}}],"x_label":"Month","y_label":"USD"}} — kind bar or line; \
1 to {MAX_CHART_POINTS} categories, 1 to {MAX_CHART_ITEMS} series, each with one number per \
category; x_label and y_label are optional.
At most {MAX_CHARTS} charts per message. Titles and axis labels: {MAX_CHART_TITLE} characters; \
categories, segment labels, and series names: {MAX_CHART_LABEL}."""


@dataclass(frozen=True)
class MarkdownBlock:
    text: str


@dataclass(frozen=True)
class Column:
    align: str | None = None  # left | center | right
    wrap: bool | None = None


@dataclass(frozen=True)
class TableBlock:
    rows: tuple[tuple[str | int | float, ...], ...]
    columns: tuple[Column, ...] = ()


@dataclass(frozen=True)
class Segment:
    label: str
    value: int | float


@dataclass(frozen=True)
class Series:
    name: str
    data: tuple[int | float, ...]  # one value per category


@dataclass(frozen=True)
class ChartBlock:
    kind: str  # pie | bar | line
    title: str
    segments: tuple[Segment, ...] = ()  # pie
    categories: tuple[str, ...] = ()  # bar, line
    series: tuple[Series, ...] = ()
    x_label: str | None = None
    y_label: str | None = None


Block = MarkdownBlock | TableBlock | ChartBlock


@dataclass(frozen=True)
class OutboundMessage:
    fallback_text: str
    blocks: tuple[Block, ...]


class EnvelopeError(ValueError):
    """An ``enso-message`` fence that cannot be delivered as written."""

    def __init__(self, reason: str, fallback: str | None = None):
        super().__init__(reason)
        self.reason = reason
        # The envelope's own fallback_text when it was at least a usable string.
        self.fallback = fallback


def repair_prompt(reason: str) -> str:
    return (
        f"Enso could not deliver your previous reply: {reason}. Send it again as one valid "
        f"{FENCE} fence exactly as the Slack rich format instructions describe, or reply in "
        "plain Markdown."
    )


def parse_outbound_message(text: str) -> OutboundMessage | None:
    """Parse a reply that is exactly one ``enso-message`` fence; ``None`` when it has no fence."""
    fences = len(_FENCE_RE.findall(text))
    if not fences:
        return None
    lines = text.strip().splitlines()
    framed = len(lines) >= 3 and lines[0].strip() == FENCE and lines[-1].strip() == "```"
    if fences > 1 or not framed:
        # Still worth one correction, but the fenced JSON may already hold the answer.
        raise EnvelopeError(
            f"the reply must be exactly one {FENCE} fence with no text outside it",
            _fenced_fallback(text),
        )
    payload = _decode("\n".join(lines[1:-1]))
    try:
        return _message(payload)
    except ValueError as exc:
        raise EnvelopeError(str(exc), _usable_fallback(payload)) from None


def _decode(body: str) -> Any:
    """``json.loads`` with every decoder failure reported as an ``EnvelopeError``."""
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(
            f"the fence body is not valid JSON ({exc.msg} at line {exc.lineno})"
        ) from None
    except ValueError, RecursionError:
        # An integer past the interpreter's digit limit, or nesting past its stack.
        raise EnvelopeError("the fence body is not valid JSON (a value is too large)") from None


def _fenced_fallback(text: str) -> str | None:
    """The fallback_text inside the first complete fence of a badly framed reply."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if _FENCE_RE.match(line)), len(lines))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "```"), None)
    if end is None:
        return None
    try:
        return _usable_fallback(_decode("\n".join(lines[start + 1 : end])))
    except EnvelopeError:
        return None


def _usable_fallback(payload: Any) -> str | None:
    """The envelope's own fallback_text when it is at least a non-blank string."""
    fallback = payload.get("fallback_text") if isinstance(payload, dict) else None
    return fallback if isinstance(fallback, str) and fallback.strip() else None


# -- Validation helpers: each raises ValueError with the reason the model is told --


def _keys(value: Any, what: str, required: set[str], optional: Iterable[str] = ()) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be an object")
    if missing := required - value.keys():
        raise ValueError(f"{what} is missing {', '.join(sorted(missing))}")
    if extra := value.keys() - required - set(optional):
        raise ValueError(f"{what} has unknown key {', '.join(sorted(extra))}")
    return value


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-blank string")
    if len(value) > limit:
        raise ValueError(f"{what} exceeds {limit:,} characters")
    return value


def _is_number(value: Any) -> bool:
    """A finite JSON number: not a bool, not inf/nan, not an integer too large for a float."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _number(value: Any, what: str) -> int | float:
    if not _is_number(value):
        raise ValueError(f"{what} must be a number")
    return value


def _items(value: Any, what: str, limit: int) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{what} must be a non-empty array")
    if len(value) > limit:
        raise ValueError(f"{what} exceeds {limit} items")
    return value


def _markdown(value: dict) -> MarkdownBlock:
    _keys(value, "markdown block", {"type", "text"})
    return MarkdownBlock(_text(value["text"], "markdown text", MAX_MARKDOWN_TEXT))


def _cell(value: Any, what: str) -> str | int | float:
    if (isinstance(value, str) and value.strip()) or _is_number(value):
        return value
    raise ValueError(f"{what} must be a non-blank string or a number")


def _column(value: Any, index: int) -> Column:
    _keys(value, f"column {index}", set(), ("align", "wrap"))
    align, wrap = value.get("align"), value.get("wrap")
    if align is not None and align not in ("left", "center", "right"):
        raise ValueError(f"column {index} align must be left, center, or right")
    if wrap is not None and not isinstance(wrap, bool):
        raise ValueError(f"column {index} wrap must be true or false")
    return Column(align, wrap)


def _table(value: dict) -> TableBlock:
    _keys(value, "table block", {"type", "rows"}, {"columns"})
    raw_rows = _items(value["rows"], "rows", MAX_TABLE_ROWS)
    width = len(_items(raw_rows[0], "row 1", MAX_TABLE_COLUMNS))
    rows = []
    for r, raw in enumerate(raw_rows, 1):
        cells = _items(raw, f"row {r}", MAX_TABLE_COLUMNS)
        if len(cells) != width:
            raise ValueError(f"row {r} has {len(cells)} cells, expected {width}")
        rows.append(tuple(_cell(cell, f"row {r} cell {c}") for c, cell in enumerate(cells, 1)))
    raw_columns = value.get("columns", [])
    if not isinstance(raw_columns, list) or len(raw_columns) > width:
        raise ValueError("columns must be an array with at most one entry per table column")
    columns = tuple(_column(column, i) for i, column in enumerate(raw_columns, 1))
    return TableBlock(tuple(rows), columns)


def _segment(value: Any, index: int) -> Segment:
    _keys(value, f"segment {index}", {"label", "value"})
    number = _number(value["value"], f"segment {index} value")
    if number <= 0:
        raise ValueError(f"segment {index} value must be positive")
    return Segment(_text(value["label"], f"segment {index} label", MAX_CHART_LABEL), number)


def _series(value: Any, index: int, points: int) -> Series:
    _keys(value, f"series {index}", {"name", "data"})
    data = value["data"]
    if not isinstance(data, list) or len(data) != points:
        raise ValueError(f"series {index} data must have one number per category ({points})")
    name = _text(value["name"], f"series {index} name", MAX_CHART_LABEL)
    return Series(name, tuple(_number(v, f"series {index} data") for v in data))


def _chart(value: dict) -> ChartBlock:
    kind = value.get("kind")
    if kind == "pie":
        _keys(value, "pie chart", {"type", "kind", "title", "segments"})
        raw = _items(value["segments"], "segments", MAX_CHART_ITEMS)
        title = _text(value["title"], "chart title", MAX_CHART_TITLE)
        segments = tuple(_segment(s, i) for i, s in enumerate(raw, 1))
        return ChartBlock("pie", title, segments=segments)
    if kind not in ("bar", "line"):
        raise ValueError("chart kind must be pie, bar, or line")
    required = {"type", "kind", "title", "categories", "series"}
    _keys(value, f"{kind} chart", required, {"x_label", "y_label"})
    title = _text(value["title"], "chart title", MAX_CHART_TITLE)
    raw = _items(value["categories"], "categories", MAX_CHART_POINTS)
    categories = tuple(_text(c, f"category {i}", MAX_CHART_LABEL) for i, c in enumerate(raw, 1))
    if len(set(categories)) != len(categories):
        raise ValueError("categories must be unique")
    raw = _items(value["series"], "series", MAX_CHART_ITEMS)
    series = tuple(_series(s, i, len(categories)) for i, s in enumerate(raw, 1))
    if len({s.name for s in series}) != len(series):
        raise ValueError("series names must be unique")
    x_label, y_label = (
        _text(value[key], key, MAX_CHART_TITLE) if value.get(key) is not None else None
        for key in ("x_label", "y_label")
    )
    return ChartBlock(
        kind, title, categories=categories, series=series, x_label=x_label, y_label=y_label
    )


_BLOCK_PARSERS: dict[str, Callable[[dict], Block]] = {
    "markdown": _markdown,
    "table": _table,
    "chart": _chart,
}


def _block(value: Any, index: int) -> Block:
    kind = value.get("type") if isinstance(value, dict) else None
    if not isinstance(kind, str) or kind not in _BLOCK_PARSERS:
        raise ValueError(f"block {index} type must be markdown, table, or chart")
    try:
        return _BLOCK_PARSERS[kind](value)
    except ValueError as exc:
        raise ValueError(f"block {index} ({kind}): {exc}") from None


def _message(payload: Any) -> OutboundMessage:
    _keys(payload, "the envelope", {"version", "fallback_text", "blocks"})
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("version must be 1")
    fallback = _text(payload["fallback_text"], "fallback_text", MAX_FALLBACK_TEXT)
    raw = _items(payload["blocks"], "blocks", MAX_BLOCKS)
    blocks = tuple(_block(b, i) for i, b in enumerate(raw, 1))
    markdown = sum(len(b.text) for b in blocks if isinstance(b, MarkdownBlock))
    if markdown > MAX_MARKDOWN_TEXT:
        raise ValueError(f"markdown blocks exceed {MAX_MARKDOWN_TEXT:,} characters in total")
    tables = [b for b in blocks if isinstance(b, TableBlock)]
    cells = sum(len(str(cell)) for b in tables for row in b.rows for cell in row)
    if cells > MAX_TABLE_TEXT:
        raise ValueError(f"table cells exceed {MAX_TABLE_TEXT:,} characters in total")
    if sum(isinstance(b, ChartBlock) for b in blocks) > MAX_CHARTS:
        raise ValueError(f"at most {MAX_CHARTS} charts per message")
    return OutboundMessage(fallback, blocks)
