"""Filesystem knowledge: discover, read, validate, and link portable Markdown notes."""

from .catalog import Catalog, KnowledgeError, Note, Resolution, Root, discover_roots, scan
from .links import Link, extract_links, heading_ids, slug_heading
from .storage import read_bytes, safe_path
from .writing import adopt_note, create_note, move_note, normalize_text, update_note

__all__ = [
    "Catalog",
    "KnowledgeError",
    "Link",
    "Note",
    "Resolution",
    "Root",
    "adopt_note",
    "create_note",
    "discover_roots",
    "extract_links",
    "heading_ids",
    "move_note",
    "normalize_text",
    "read_bytes",
    "safe_path",
    "scan",
    "slug_heading",
    "update_note",
]
