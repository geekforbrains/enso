"""The workspace file browser: which paths may be shown, and how a file is presented.

Only ``knowledge/``, ``drafts/``, and ``uploads/`` are browsable, each rooted separately.
Every candidate is resolved and must stay inside both its root and the workspace after
symlinks are followed, which rejects encoded traversal, an escaping descendant symlink,
and an allowed root that is itself a symlink out of the workspace. Nothing here writes.
"""

from __future__ import annotations

import os
import stat as statmod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markupsafe import Markup

from ..config import Paths, require_workspace

ROOTS = ("knowledge", "drafts", "uploads")
MAX_FILE_PREVIEW_BYTES = 2 * 1024 * 1024
MARKDOWN_SUFFIXES = (".md", ".markdown")
_LINK_SCHEMES = ("http", "https", "mailto")


class PathRejectedError(Exception):
    """The request names something outside the allowed roots; shown as a 404."""


@dataclass(frozen=True)
class Entry:
    """One directory entry as listed."""

    name: str
    kind: str  # dir | file | link | other
    relative: str  # root-relative POSIX path, for the link
    size: int | None = None
    modified: str | None = None  # ISO 8601, local time
    target: str | None = None  # a symlink's own target text


@dataclass
class Listing:
    root: str
    relative: str  # "" at the root
    entries: list[Entry] = field(default_factory=list)


@dataclass
class FileView:
    """A file as shown: text, rendered Markdown, or metadata only with a ``note``."""

    root: str
    relative: str
    name: str
    size: int
    modified: str | None
    kind: str  # text | markdown | binary | large | unreadable | other
    text: str | None = None
    html: Markup | None = None
    note: str = ""


@dataclass(frozen=True)
class Resolved:
    workspace: Path
    root: Path
    target: Path


def crumbs(relative: str) -> list[tuple[str, str]]:
    """``(name, root-relative path)`` for each ancestor of ``relative``, itself included."""
    found: list[tuple[str, str]] = []
    parts = [part for part in relative.split("/") if part]
    for index, part in enumerate(parts):
        found.append((part, "/".join(parts[: index + 1])))
    return found


def split_relative(relative: str) -> list[str]:
    """Path segments of a root-relative path; raises ``PathRejectedError`` on any escape attempt."""
    if relative.startswith("/") or "\\" in relative or "\0" in relative:
        raise PathRejectedError(relative)
    parts = [part for part in relative.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise PathRejectedError(relative)
    return parts


def resolve(paths: Paths, workspace: str, root: str, relative: str) -> Resolved:
    """The real path for ``relative`` under ``root``, proven to stay inside the workspace.

    Raises ``PathRejectedError`` for anything outside, and ``FileNotFoundError`` when the
    workspace or the root does not exist. The target itself may be missing.
    """
    if root not in ROOTS:
        raise PathRejectedError(f"{workspace}/{root}")
    parts = split_relative(relative)
    try:
        workspace_dir = require_workspace(paths, workspace).resolve(strict=True)
    except ValueError as exc:
        raise PathRejectedError(str(exc)) from exc
    root_dir = (workspace_dir / root).resolve(strict=True)
    if not root_dir.is_relative_to(workspace_dir):
        raise PathRejectedError(f"{root}/ leaves the workspace")
    target = root_dir.joinpath(*parts).resolve(strict=False)
    if not (target.is_relative_to(root_dir) and target.is_relative_to(workspace_dir)):
        raise PathRejectedError(relative)
    return Resolved(workspace_dir, root_dir, target)


def listing(resolved: Resolved, root: str, relative: str) -> Listing:
    """Every entry of a directory, dotfiles included, directories first."""
    found = Listing(root, "/".join(split_relative(relative)))
    try:
        entries = list(os.scandir(resolved.target))
    except OSError as exc:
        raise PathRejectedError(str(exc)) from exc
    rows: list[Entry] = []
    for entry in entries:
        rows.append(_entry(entry, found.relative))
    order = {"dir": 0, "link": 1, "file": 2, "other": 3}
    found.entries = sorted(rows, key=lambda row: (order[row.kind], row.name))
    return found


def _entry(entry: os.DirEntry, parent: str) -> Entry:
    relative = f"{parent}/{entry.name}" if parent else entry.name
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return Entry(entry.name, "other", relative)
    modified = datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(timespec="seconds")
    if statmod.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(entry.path)
        except OSError:
            target = None
        return Entry(entry.name, "link", relative, modified=modified, target=target)
    if statmod.S_ISDIR(info.st_mode):
        return Entry(entry.name, "dir", relative, modified=modified)
    if statmod.S_ISREG(info.st_mode):
        return Entry(entry.name, "file", relative, size=info.st_size, modified=modified)
    return Entry(entry.name, "other", relative, modified=modified)


def view(resolved: Resolved, root: str, relative: str, *, raw: bool = False) -> FileView:
    """A file's text, rendered Markdown, or metadata when it is binary, large, or unreadable."""
    target = resolved.target
    name = PurePosixPath("/".join(split_relative(relative))).name or target.name
    info = target.stat()
    modified = datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(timespec="seconds")
    shown = FileView(
        root, "/".join(split_relative(relative)), name, info.st_size, modified, "other"
    )
    if not statmod.S_ISREG(info.st_mode):
        shown.note = "Not a regular file"
        return shown
    if info.st_size > MAX_FILE_PREVIEW_BYTES:
        shown.kind = "large"
        shown.note = f"Larger than the {MAX_FILE_PREVIEW_BYTES // (1024 * 1024)} MiB preview limit"
        return shown
    try:
        with open(target, "rb") as handle:
            data = handle.read(MAX_FILE_PREVIEW_BYTES + 1)
    except OSError as exc:
        shown.kind = "unreadable"
        shown.note = f"Could not read the file: {exc.strerror or exc}"
        return shown
    if len(data) > MAX_FILE_PREVIEW_BYTES:
        shown.kind = "large"
        shown.note = f"Larger than the {MAX_FILE_PREVIEW_BYTES // (1024 * 1024)} MiB preview limit"
        return shown
    if b"\0" in data:
        shown.kind = "binary"
        shown.note = "Binary content is not shown"
        return shown
    shown.text = data.decode("utf-8", errors="replace")
    if not raw and target.suffix.lower() in MARKDOWN_SUFFIXES:
        shown.kind = "markdown"
        shown.html = render_markdown(shown.text)
    else:
        shown.kind = "text"
    return shown


def tree_summary(directory: Path) -> tuple[int, int]:
    """``(entries, bytes)`` of a root: how much is there before opening it."""
    count = total = 0
    for current, _dirs, names in os.walk(directory):
        count += len(names)
        for file_name in names:
            try:
                total += os.lstat(os.path.join(current, file_name)).st_size
            except OSError:
                continue
    return count, total


# -- Markdown -------------------------------------------------------------------


def _safe_link(url: str) -> bool:
    """Relative links and http/https/mailto only; nothing that runs or fetches."""
    scheme = urlsplit(url.strip()).scheme.lower()
    return scheme == "" or scheme in _LINK_SCHEMES


def _renderer(*, breaks: bool) -> MarkdownIt:
    md = MarkdownIt(
        "commonmark", {"html": False, "linkify": False, "typographer": False, "breaks": breaks}
    )
    md.enable(["table", "strikethrough"])
    md.disable("image")  # an image is a remote load the viewer must never trigger
    md.validateLink = _safe_link  # type: ignore[method-assign]
    return md


_markdown = _renderer(breaks=False)
# Provider output is a transcript, not a document: its single newlines separate log lines,
# command echoes, and stderr, so they have to survive as line breaks. Authored Markdown
# (a task spec, a job prompt) reflows the way CommonMark says, hence the second renderer.
_transcript = _renderer(breaks=True)
# A run of ``-`` or ``=`` under a line is a separator the CLI printed, not a heading
# underline. Left enabled, every provider banner and every grep hit that happens to be
# followed by dashes becomes the largest text on the page; disabled, a ``---`` line reads
# as the horizontal rule it was meant to be, and real ``#`` headings still work.
_transcript.disable("lheading")

# Output is kept to the final 1 MiB, and rendering that much Markdown costs tens of
# milliseconds for ordinary output but approaches a second for a pathological shape such as
# a megabyte of list items. Above this the page keeps the preformatted block, which every
# real run in a home this size stays well under.
RENDER_LIMIT = 256 * 1024


def render_markdown(text: str) -> Markup:
    """Markdown as HTML with raw HTML escaped, links filtered, and images left as text.

    This and ``render_output`` are the only HTML the viewer marks safe: markdown-it-py's
    own output, with ``html`` off so anything that looks like a tag in the source is escaped.
    """
    return Markup(_markdown.render(text))


def render_output(text: str) -> Markup | None:
    """Provider output as HTML, keeping its line breaks, or ``None`` when it is too large.

    ``None`` tells the page to fall back to the preformatted block rather than spend the
    request rendering a transcript nobody reads to the end.
    """
    if len(text) > RENDER_LIMIT:
        return None
    return Markup(_transcript.render(text))
