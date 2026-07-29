"""Reference docs — operator-authored Markdown under ~/.enso/docs/."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import dataclass, field

from . import frontmatter
from .config import DOCS_DIR
from .fsutil import atomic_write_text, is_within

log = logging.getLogger(__name__)

MAX_DOCS = 500          # total docs a single listing will return
MAX_DOC_DEPTH = 8       # max path segments, filename included ("a/b.md" == 2)

_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")

_DEFAULT_DESCRIPTION = "One line covering what this doc holds and when it is worth reading."
_DEFAULT_BODY = "Reference notes go here."


@dataclass
class Doc:
    """A reference doc parsed from a Markdown file under ~/.enso/docs/."""

    rel_path: str
    name: str
    description: str
    path: str
    has_frontmatter: bool = True


@dataclass
class DocListing:
    """A bounded listing plus whether the entry cap cut it short."""

    docs: list[Doc] = field(default_factory=list)
    truncated: bool = False


@dataclass
class DocGroup:
    """Docs sharing a parent directory, with a derived heading."""

    rel_dir: str
    heading: str
    docs: list[Doc]


class DocPathError(ValueError):
    """Raised when a doc path is unsafe, malformed, or escapes the docs root."""


def title_from_path(path: str) -> str:
    """Derive a display title from the last segment of ``path``."""
    segments = [seg for seg in path.split("/") if seg]
    last = segments[-1] if segments else ""
    stem = last[:-3] if last.lower().endswith(".md") else last
    words = stem.replace("_", " ").replace("-", " ").split()
    # Only the first character is touched, so "AWS_notes.md" stays "AWS Notes".
    return " ".join(word[:1].upper() + word[1:] for word in words) or last


def parent_titles(rel_path: str) -> list[str]:
    """Titles for each directory segment above a doc (breadcrumb order)."""
    return [title_from_path(seg) for seg in rel_path.split("/")[:-1] if seg]


def resolve_doc_path(rel_path: str) -> tuple[str, str]:
    """Validate a doc path, returning (normalized relative path, absolute path)."""
    candidate = rel_path.strip() if isinstance(rel_path, str) else ""
    if candidate.endswith("/"):
        candidate = candidate[:-1]
    if not candidate or "\0" in candidate or "\\" in candidate or os.path.isabs(candidate):
        raise DocPathError("Doc path must be a non-empty relative path")

    segments = candidate.split("/")
    for seg in segments:
        if not _is_safe_segment(seg):
            raise DocPathError(f"Doc path segment is not allowed: {seg!r}")
    if len(segments) > MAX_DOC_DEPTH:
        raise DocPathError(f"Doc path is too deep (max {MAX_DOC_DEPTH} segments)")
    if not segments[-1].endswith(".md") or segments[-1] == ".md":
        raise DocPathError("Doc path must end in .md")

    path = os.path.join(DOCS_DIR, *segments)
    if not is_within(DOCS_DIR, path):
        raise DocPathError("Doc path escapes the docs directory")
    return "/".join(segments), path


def parse_doc(rel_path: str, path: str) -> Doc:
    """Parse one doc file into a Doc, never failing on bad frontmatter."""
    try:
        meta, _ = frontmatter.read(path)
    except (OSError, UnicodeError, ValueError):
        log.warning("Could not read doc %s", path)
        return Doc(rel_path, title_from_path(rel_path), "", path, has_frontmatter=False)

    name = meta.get("name")
    description = meta.get("description")
    has_name = isinstance(name, str) and bool(name.strip())
    has_description = isinstance(description, str) and bool(description.strip())
    return Doc(
        rel_path=rel_path,
        name=name if has_name else title_from_path(rel_path),
        description=description if has_description else "",
        path=path,
        has_frontmatter=has_name and has_description,
    )


def load_docs() -> DocListing:
    """Recursively list docs under ~/.enso/docs/, bounded and symlink-safe."""
    listing = DocListing()
    if not os.path.isdir(DOCS_DIR):
        return listing

    capped = False
    for dirpath, dirnames, filenames in os.walk(DOCS_DIR, followlinks=False):
        depth = _walk_depth(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if _is_safe_segment(name) and not os.path.islink(os.path.join(dirpath, name))
        )
        # A child of this directory could only hold files past the depth cap,
        # which resolve_doc_path rejects: prune rather than walk them.
        if depth + 2 > MAX_DOC_DEPTH:
            dirnames.clear()

        for name in sorted(filenames):
            if not _is_safe_segment(name) or not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            if len(listing.docs) >= MAX_DOCS:
                listing.truncated = capped = True
                break
            rel = os.path.relpath(full, DOCS_DIR).replace(os.sep, "/")
            listing.docs.append(parse_doc(rel, full))
        if capped:
            dirnames.clear()
            break

    # os.walk order is not path-sorted: the cap applies in traversal order,
    # while the returned listing is sorted by relative path.
    listing.docs.sort(key=lambda d: d.rel_path)
    return listing


def load_doc(rel_path: str) -> Doc:
    """Load a single doc by relative path."""
    rel, path = resolve_doc_path(rel_path)
    _require_doc_file(rel, path)
    return parse_doc(rel, path)


def group_docs(docs: list[Doc]) -> list[DocGroup]:
    """Group docs by parent directory for display."""
    grouped: dict[str, list[Doc]] = {}
    for doc in docs:
        rel_dir = doc.rel_path.rsplit("/", 1)[0] if "/" in doc.rel_path else ""
        grouped.setdefault(rel_dir, []).append(doc)
    return [
        DocGroup(
            rel_dir=rel_dir,
            heading=(
                " / ".join(title_from_path(seg) for seg in rel_dir.split("/"))
                if rel_dir
                else "Top level"
            ),
            docs=members,
        )
        for rel_dir, members in sorted(grouped.items())
    ]


def read_doc(rel_path: str) -> str:
    """Return a doc's full file contents."""
    rel, path = resolve_doc_path(rel_path)
    _require_doc_file(rel, path)
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_doc(rel_path: str, content: str) -> str:
    """Atomically replace a doc's full contents; returns the normalized rel path."""
    rel, path = resolve_doc_path(rel_path)
    _require_doc_file(rel, path)
    atomic_write_text(path, content.replace("\r\n", "\n"), newline="")
    return rel


def create_doc(rel_path: str, name: str = "") -> Doc:
    """Create a doc with scaffolded frontmatter, creating parent dirs."""
    rel_path = rel_path.strip()
    if not rel_path.endswith(".md"):
        rel_path += ".md"
    rel, path = resolve_doc_path(rel_path)
    title = name.strip() or title_from_path(rel)

    created = _missing_parents(path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Exclusive creation, not a lexists check: atomic_write_text ends in
        # os.replace and would silently clobber a racing writer.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise FileExistsError(f"Doc '{rel}' already exists") from None
        try:
            os.close(fd)
            frontmatter.write(
                path,
                {"name": title, "description": _DEFAULT_DESCRIPTION},
                _DEFAULT_BODY,
            )
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(path)
            raise
    except BaseException:
        for directory in reversed(created):
            with contextlib.suppress(OSError):
                os.rmdir(directory)
        raise

    return Doc(rel, title, _DEFAULT_DESCRIPTION, path, has_frontmatter=True)


def delete_doc(rel_path: str) -> None:
    """Remove a doc, pruning parent directories it leaves empty."""
    rel, path = resolve_doc_path(rel_path)
    _require_doc_file(rel, path)
    os.remove(path)
    _prune_empty_parents(os.path.dirname(path))


def _is_safe_segment(name: str) -> bool:
    """True for a portable path segment that is neither a dotfile nor a dot-dir."""
    return bool(_SEGMENT_RE.fullmatch(name)) and not name.startswith(".")


def _require_doc_file(rel: str, path: str) -> None:
    """Raise unless ``path`` is a regular, non-symlink doc file."""
    if os.path.islink(path) or not os.path.isfile(path):
        raise FileNotFoundError(f"Doc '{rel}' not found")


def _walk_depth(dirpath: str) -> int:
    """Segment depth of a walked directory, with the docs root at zero."""
    rel = os.path.relpath(dirpath, DOCS_DIR)
    return 0 if rel == os.curdir else rel.count(os.sep) + 1


def _missing_parents(path: str) -> list[str]:
    """Directories between the docs root and ``path`` that do not exist yet."""
    missing: list[str] = []
    directory = os.path.dirname(path)
    root = os.path.realpath(DOCS_DIR)
    while os.path.realpath(directory) != root and not os.path.isdir(directory):
        missing.append(directory)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    missing.reverse()
    return missing


def _prune_empty_parents(directory: str) -> None:
    """Remove now-empty directories up to, but never including, the docs root."""
    root = os.path.realpath(DOCS_DIR)
    while os.path.realpath(directory) != root and is_within(DOCS_DIR, directory):
        try:
            os.rmdir(directory)
        except OSError:
            break
        directory = os.path.dirname(directory)
