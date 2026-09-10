"""Markdown frontmatter syntax: the leading ``---`` block, read as exactly one YAML mapping.

Syntax only. Which fields a document may carry, and what they mean, belongs to whichever
module owns that format — ``jobs`` for ``JOB.md``, ``skills`` for ``SKILL.md`` — so neither
schema can leak into the other.

Nothing here guesses. A block that is not valid YAML, is not a mapping, or answers a key
twice has no single meaning, so it is refused rather than partly recovered. Problems name a
line and column, and the key they are about, but never repeat what a document says: its
frontmatter values and its body are both untrusted text a user or an agent wrote.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any

import yaml

# The first frontmatter line is the file's second: the first is the opening ``---`` fence.
_FIRST_FIELD_LINE = 2
# A key is a short label, but the bound holds whatever a crafted document pushes into one.
_KEY_LIMIT = 120


@dataclass(frozen=True)
class Document:
    """One Markdown document split into its frontmatter mapping and its body."""

    fields: dict[str, Any]
    body: str


class _BadKeyError(Exception):
    """A mapping key the shared rules refuse; ``str`` is the finished problem text."""


class _Loader(yaml.SafeLoader):
    """The safe loader, with mapping keys held to those rules.

    ``SafeLoader`` builds only plain data, never arbitrary Python objects. Its own
    ``construct_mapping`` keeps the last of a repeated key and accepts a key of any type;
    silently choosing one of two answers is exactly what a strict format must not do, so
    both are refused here. Skipping ``flatten_mapping`` also leaves a ``<<`` merge key with
    a tag nothing constructs, so no document can pull fields in from an anchor and read
    differently than it looks.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        mapping: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise _BadKeyError(f"frontmatter keys must be text{_at(key_node.start_mark)}")
            if key in mapping:
                raise _BadKeyError(
                    f"frontmatter answers {key_text(key)} twice{_at(key_node.start_mark)}"
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def parse(text: str) -> tuple[Document | None, str | None]:
    """One document's frontmatter mapping and body, or why its syntax is unusable.

    The problem reads as a predicate about the file, so a caller prefixes the name it
    already knows: ``f"JOB.md {problem}"``.
    """
    split = _split(text)
    if split is None:
        return None, "needs a leading --- frontmatter block"
    raw, body = split
    try:
        loaded = yaml.load(raw, Loader=_Loader)
    except _BadKeyError as exc:
        return None, str(exc)
    except yaml.YAMLError as exc:
        return None, _yaml_problem(exc)
    if not isinstance(loaded, dict):
        return None, "frontmatter must be a block of key: value fields"
    return Document(loaded, body), None


def key_text(key: str) -> str:
    """One key, safe to print inside a problem.

    ``repr`` escapes every character Python calls unprintable, so a newline or a
    bidirectional control character in a key cannot forge a line of terminal output.
    """
    return _bounded(repr(key))


def _split(text: str) -> tuple[str, str] | None:
    """(raw frontmatter, body) for a document fenced by unindented ``---`` lines."""
    lines = text.splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :]).strip()
    return None


def _yaml_problem(exc: yaml.YAMLError) -> str:
    """A YAML failure as a location, and nothing else.

    None of PyYAML's own text is safe to repeat. ``str(exc)`` quotes the offending source
    line outright, and even its short ``problem`` interpolates what it just read: an
    undefined alias or an unknown tag comes back carrying the document's own characters.
    Either would put frontmatter into a CLI message, a log, and the web viewer, so the
    line and column say where to look without saying what is written there.
    """
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    return f"frontmatter is not valid YAML{_at(mark)}"


def _at(mark: yaml.Mark | None) -> str:
    """Where a mark points, in the whole file's coordinates, one-based, or nothing."""
    if mark is None:
        return ""
    return f" at line {mark.line + _FIRST_FIELD_LINE}, column {mark.column + 1}"


def _bounded(text: str) -> str:
    """One line of diagnostic text: printable, single-line, and length-bounded."""
    cleaned = "".join(char if char.isprintable() else " " for char in text).strip()
    return f"{cleaned[:_KEY_LIMIT].rstrip()}…" if len(cleaned) > _KEY_LIMIT else cleaned
