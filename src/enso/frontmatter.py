"""Shared YAML frontmatter parsing and writing for jobs.

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
import re
import tempfile

import yaml

log = logging.getLogger(__name__)


def _is_fence(line: str) -> bool:
    """Return whether ``line`` is an unindented frontmatter fence."""
    return line.rstrip("\r\n").rstrip(" \t") == "---"


def _layout(text: str) -> tuple[int, int, int] | None:
    """Return the content, closing-fence, and body offsets for frontmatter.

    This deliberately locates fences without parsing the YAML between them.
    Callers that only need to edit the body can therefore preserve malformed
    or legacy frontmatter exactly instead of accidentally replacing it with an
    empty mapping.
    """
    lines = text.splitlines(keepends=True)
    if not lines or not _is_fence(lines[0]):
        return None

    content_start = len(lines[0])
    offset = content_start
    for index, line in enumerate(lines[1:], start=1):
        line_start = offset
        offset += len(line)
        if not _is_fence(line):
            continue

        body_start = offset
        # Blank lines immediately after the fence are document separators,
        # not part of the prompt returned by ``parse``. Preserve them too.
        for separator in lines[index + 1 :]:
            if separator.strip():
                break
            body_start += len(separator)
        return content_start, line_start, body_start
    return None


def split_raw(text: str) -> tuple[str, str] | None:
    """Return raw frontmatter content and body without parsing YAML.

    Separator lines immediately after the closing fence are excluded from the
    body, matching ``parse``. Indented ``---`` lines remain metadata content,
    so quoted multiline YAML scalars cannot be mistaken for the closing fence.
    """
    layout = _layout(text)
    if layout is None:
        return None
    content_start, closing_start, body_start = layout
    return text[content_start:closing_start], text[body_start:]


def replace_body(text: str, body: str) -> str:
    """Replace only a fenced document's body, preserving its raw prefix.

    The opening fence, frontmatter bytes, closing fence, newline style, and
    existing separator lines are copied verbatim. ``ValueError`` is raised for
    a document without a complete leading frontmatter block.
    """
    layout = _layout(text)
    if layout is None:
        raise ValueError("document has no complete leading frontmatter block")

    body_start = layout[2]
    if body and not body.endswith(("\n", "\r")):
        body += "\n"
    prefix = text[:body_start]
    if body and not prefix.endswith(("\n", "\r")):
        opening = text[: layout[0]]
        if opening.endswith("\r\n"):
            newline = "\r\n"
        elif opening.endswith("\r"):
            newline = "\r"
        else:
            newline = "\n"
        prefix += newline
    return prefix + body


def replace_scalar(text: str, key: str, value: str) -> str:
    """Replace one top-level frontmatter scalar without reserializing YAML.

    Existing whitespace, comments, line endings, and all unrelated fields are
    retained. If the field is absent, it is inserted immediately before the
    closing fence.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
        raise ValueError(f"invalid frontmatter key: {key!r}")
    layout = _layout(text)
    if layout is None:
        raise ValueError("document has no complete leading frontmatter block")

    content_start, closing_start, _ = layout
    header = text[content_start:closing_start]
    pattern = re.compile(
        rf"^({re.escape(key)}[ \t]*:[ \t]*)([^#\r\n]*?)([ \t]*(?:#[^\r\n]*)?)"
        r"(\r\n|\n|\r|$)",
        re.MULTILINE,
    )
    updated, count = pattern.subn(
        lambda match: match.group(1) + value + match.group(3) + match.group(4),
        header,
    )
    if count:
        return text[:content_start] + updated + text[closing_start:]

    opening = text[:content_start]
    if opening.endswith("\r\n"):
        newline = "\r\n"
    elif opening.endswith("\r"):
        newline = "\r"
    else:
        newline = "\n"
    prefix = text[:closing_start]
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    return prefix + f"{key}: {value}{newline}" + text[closing_start:]


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
    if not _is_fence(lines[0]):
        return {}, text

    closing: int | None = None
    for idx in range(1, len(lines)):
        if _is_fence(lines[idx]):
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

    body_lines = lines[closing + 1 :]
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


def _atomic_write(path: str, text: str) -> None:
    """Fsync a temporary UTF-8 file, then atomically replace ``path``."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def write(path: str, meta: dict, body: str) -> None:
    """Atomically write a serialized frontmatter document to ``path``."""
    _atomic_write(path, dumps(meta, body))


def write_body(path: str, body: str) -> None:
    """Atomically replace a document body without reserializing frontmatter."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    _atomic_write(path, replace_body(text, body))


def write_scalar(path: str, key: str, value: str) -> None:
    """Atomically replace a scalar without reserializing other frontmatter."""
    with open(path, encoding="utf-8", newline="") as f:
        text = f.read()
    _atomic_write(path, replace_scalar(text, key, value))
