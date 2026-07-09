"""Shared YAML frontmatter parsing and writing for jobs and tasks.

A frontmatter document is a leading ``---`` fenced YAML block followed by a
free-form Markdown body::

    ---
    title: Example
    tags:
      - a
      - b
    ---

    Body text goes here.

The block is optional: text without an opening fence is treated as a bare
body with empty metadata. Writes are atomic (temp file in the same dir,
flush + fsync, then ``os.replace``) so a crash never leaves a half-written
file behind.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile

import yaml

log = logging.getLogger(__name__)


def parse(text: str) -> tuple[dict, str]:
    """Split ``text`` into (metadata, body).

    Expects the form ``---\\n<yaml>\\n---\\n<body>``. Text without a leading
    ``---`` fence (or without a matching closing fence, or whose block isn't a
    YAML mapping) is returned as ``({}, text)``. A missing trailing newline is
    tolerated. The blank separator line ``dumps`` writes between the closing
    fence and the body is stripped from the returned body.
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text

    closing: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            closing = idx
            break
    if closing is None:
        return {}, text

    yaml_text = "\n".join(lines[1:closing])
    try:
        loaded = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError:
        log.warning("Malformed frontmatter YAML; treating as bare body")
        return {}, text
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return {}, text

    body_lines = lines[closing + 1:]
    # Drop the blank separator line(s) dumps writes between fence and body.
    while body_lines and body_lines[0] == "":
        body_lines.pop(0)
    return loaded, "\n".join(body_lines)


def dumps(meta: dict, body: str) -> str:
    """Render metadata + body into a frontmatter document string."""
    front = yaml.safe_dump(meta, sort_keys=False).strip()
    return "---\n" + front + "\n---\n\n" + body.rstrip() + "\n"


def read(path: str) -> tuple[dict, str]:
    """Read and parse a frontmatter document from ``path``."""
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def write(path: str, meta: dict, body: str) -> None:
    """Atomically write a frontmatter document to ``path``.

    Writes to a temp file in the same directory, flushes + fsyncs, then
    ``os.replace``s it onto the target so the write is atomic and durable.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    text = dumps(meta, body)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
