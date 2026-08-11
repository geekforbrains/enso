"""Typed, transport-neutral structured outbound messages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_FALLBACK_TEXT = 4000
MAX_BLOCKS_PER_MESSAGE = 50
MAX_MARKDOWN_TEXT = 12000

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
    f"1 to {MAX_BLOCKS_PER_MESSAGE} items. The only supported block has exactly "
    "type and text fields with type markdown and nonblank text; Markdown text is "
    f"limited to {MAX_MARKDOWN_TEXT:,} cumulative characters. Add no prose outside "
    "the fence, and otherwise respond normally.\n"
)


@dataclass(frozen=True, slots=True)
class MarkdownBlock:
    """A standard-Markdown presentation block."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text.strip():
            raise ValueError("markdown block text must be a non-empty string")
        if len(self.text) > MAX_MARKDOWN_TEXT:
            raise ValueError(f"markdown block text exceeds {MAX_MARKDOWN_TEXT} characters")


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A rich presentation plus its complete human-readable fallback."""

    fallback_text: str
    blocks: tuple[MarkdownBlock, ...]

    def __post_init__(self) -> None:
        if type(self.fallback_text) is not str or not self.fallback_text.strip():
            raise ValueError("fallback_text must be a non-empty string")
        if len(self.fallback_text) > MAX_FALLBACK_TEXT:
            raise ValueError(f"fallback_text exceeds {MAX_FALLBACK_TEXT} characters")
        if type(self.blocks) is not tuple or not self.blocks:
            raise ValueError("blocks must be a non-empty tuple")
        if len(self.blocks) > MAX_BLOCKS_PER_MESSAGE:
            raise ValueError(f"blocks exceeds {MAX_BLOCKS_PER_MESSAGE} items")
        if not all(isinstance(block, MarkdownBlock) for block in self.blocks):
            raise ValueError("message contains an unsupported block type")
        if sum(len(block.text) for block in self.blocks) > MAX_MARKDOWN_TEXT:
            raise ValueError(f"markdown blocks exceed {MAX_MARKDOWN_TEXT} cumulative characters")


class _DuplicateKeyError(ValueError):
    """Raised when an envelope object repeats a JSON key."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _parse_markdown_block(value: Any) -> MarkdownBlock:
    if type(value) is not dict or set(value) != {"type", "text"}:
        raise ValueError("invalid block schema")
    if value["type"] != "markdown":
        raise ValueError("unsupported block type")
    return MarkdownBlock(text=value["text"])


def parse_outbound_message(text: str) -> OutboundMessage | None:
    """Parse an exact versioned ``enso-message`` fence, or return ``None``.

    Invalid or unknown envelopes deliberately remain ordinary response text;
    callers can then use their existing text path without losing content.
    """
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
        blocks = tuple(_parse_markdown_block(block) for block in payload["blocks"])
        return OutboundMessage(
            fallback_text=payload["fallback_text"],
            blocks=blocks,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
