"""Portable note links and heading anchors, without the optional web dependencies."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt("commonmark", {"html": False})


@dataclass(frozen=True)
class Link:
    """One target span in a Markdown body, suitable for lossless link replacement."""

    target: str
    start: int
    end: int
    wiki: bool
    image: bool = False


def _mask_code(text: str) -> str:
    """Keep offsets intact while excluding fenced/indented code and inline backticks."""
    output = text.splitlines(keepends=True)
    for token in _MARKDOWN.parse(text):
        if token.type in {"fence", "code_block"} and token.map:
            for index in range(*token.map):
                line = output[index]
                output[index] = " " * len(line.rstrip("\r\n")) + line[len(line.rstrip("\r\n")) :]
    masked = "".join(output)
    return re.sub(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)", lambda m: " " * len(m[0]), masked)


def extract_links(body: str) -> tuple[Link, ...]:
    """Find wiki, inline Markdown, and reference-definition targets outside code."""
    masked = _mask_code(body)
    links: list[Link] = []
    occupied: list[tuple[int, int]] = []
    for match in re.finditer(r"(?<![\\\[])\[\[([^\]\n]+)\]\]", masked):
        raw, separator, _ = match[1].partition("|")
        # GFM tables escape the alias separator: [[Page\|Label]]. Keep that escape
        # outside the target span so moves leave the table delimiter intact.
        if separator and raw.endswith("\\"):
            raw = raw[:-1]
        target = raw.strip()
        start = match.start(1) + len(raw) - len(raw.lstrip())
        links.append(
            Link(
                target,
                start,
                start + len(target),
                True,
                match.start() > 0 and body[match.start() - 1] == "!",
            )
        )
        occupied.append((match.start(), match.end()))
    for match in re.finditer(r"(?<!\\)\]\(\s*", masked):
        if any(a <= match.start() < b for a, b in occupied):
            continue
        span = _destination(masked, match.end())
        if span:
            start, end = span
            bracket = masked.rfind("[", 0, match.start())
            links.append(
                Link(body[start:end], start, end, False, bracket > 0 and body[bracket - 1] == "!")
            )
    for match in re.finditer(r"^ {0,3}\[[^\]\n]+\]:[ \t]*(?:<([^>\n]+)>|(\S+))", masked, re.M):
        group = 1 if match[1] else 2
        start, end = match.span(group)
        links.append(Link(body[start:end], start, end, False))
    return tuple(sorted(links, key=lambda link: link.start))


def _destination(text: str, start: int) -> tuple[int, int] | None:
    if start >= len(text):
        return None
    if text[start] == "<":
        end = text.find(">", start + 1)
        return (start + 1, end) if end >= 0 else None
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return start, index
            depth -= 1
        elif char.isspace() and depth == 0:
            return (start, index) if index > start else None
        index += 1
    return None


def slug_heading(text: str) -> str:
    """Stable Unicode anchors: lowercase visible text, spaces to hyphens, no punctuation."""
    text = html.unescape(re.sub(r"<[^>]*>", "", text))
    text = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", text).strip().lower()
    return re.sub(
        r"\s",
        "-",
        "".join(
            c for c in text if c in "-_" or not unicodedata.category(c).startswith(("P", "S", "C"))
        ),
    )


@lru_cache(maxsize=8192)
def heading_ids(body: str) -> tuple[str, ...]:
    """Return ATX and setext heading IDs, adding -1, -2 for repeated headings."""
    tokens = _MARKDOWN.parse(body)
    counts: dict[str, int] = {}
    result: list[str] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and index + 1 < len(tokens):
            value = tokens[index + 1].content
            slug = slug_heading(value)
            number = counts.get(slug, 0)
            counts[slug] = number + 1
            result.append(f"{slug}-{number}" if number else slug)
    return tuple(result)
