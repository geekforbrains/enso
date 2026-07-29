"""Tests for the reference docs store."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from enso import frontmatter
from enso.docs import (
    MAX_DOC_DEPTH,
    MAX_DOCS,
    DocPathError,
    create_doc,
    delete_doc,
    group_docs,
    load_doc,
    load_docs,
    parent_titles,
    read_doc,
    resolve_doc_path,
    title_from_path,
    write_doc,
)


def _docs_root(tmp_enso: str) -> Path:
    """Absolute path of the temporary docs tree."""
    return Path(tmp_enso) / "docs"


def _doc_text(name: str = "Sub Stuff", description: str = "What it holds.") -> str:
    """A minimal well-formed doc document."""
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody prose.\n"


def _write(tmp_enso: str, rel_path: str, text: str = "") -> Path:
    """Create a file under the docs root, making parent directories."""
    path = _docs_root(tmp_enso) / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _link_outside(tmp_enso: str, tmp_path: Path, name: str = "linked.md") -> Path:
    """Link ``name`` inside the docs tree at a file outside it."""
    outside = tmp_path / "outside.md"
    outside.write_text(_doc_text(), encoding="utf-8")
    root = _docs_root(tmp_enso)
    root.mkdir(exist_ok=True)
    (root / name).symlink_to(outside)
    return outside


def _link_inside(tmp_enso: str, name: str, target_rel: str) -> Path:
    """Link ``name`` at another doc within the docs tree."""
    target = _write(tmp_enso, target_rel, _doc_text())
    link = _docs_root(tmp_enso) / name
    link.symlink_to(target)
    return link


def _symlinked_dir(tmp_enso: str, tmp_path: Path, name: str = "link") -> Path:
    """Link ``name`` inside the docs tree at a directory holding a doc."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.md").write_text(_doc_text(), encoding="utf-8")
    root = _docs_root(tmp_enso)
    root.mkdir(exist_ok=True)
    (root / name).symlink_to(outside, target_is_directory=True)
    return outside


# Path validation


def test_resolve_doc_path_accepts_a_nested_path(tmp_enso):
    """A valid nested path returns its identity and its location on disk."""
    rel, path = resolve_doc_path("  stuff/deeper/sub_stuff.md  ")

    assert rel == "stuff/deeper/sub_stuff.md"
    assert path == os.path.join(tmp_enso, "docs", "stuff", "deeper", "sub_stuff.md")


@pytest.mark.parametrize(
    "rel_path",
    [
        "",
        "   ",
        "..",
        "../escape.md",
        "stuff/../../escape.md",
        "stuff//sub_stuff.md",
        "/absolute/notes.md",
        "stuff\\sub_stuff.md",
        "notes\x00.md",
        "notes.txt",
        ".md",
        ".hidden.md",
        ".private/notes.md",
        "stuff/",
    ],
)
def test_resolve_doc_path_rejects_unsafe_paths(tmp_enso, rel_path):
    """Malformed, escaping, and non-Markdown paths are refused before any I/O."""
    with pytest.raises(DocPathError):
        resolve_doc_path(rel_path)

    assert not os.path.exists(os.path.join(tmp_enso, "docs"))


def test_doc_path_error_is_a_value_error():
    """Callers can catch a rejected path with the ValueError idiom."""
    assert issubclass(DocPathError, ValueError)


def test_resolve_doc_path_rejects_paths_beyond_the_depth_cap(tmp_enso):
    """A path at the cap resolves; one segment deeper is refused."""
    parents = "/".join(f"level_{index}" for index in range(MAX_DOC_DEPTH - 1))

    rel, _ = resolve_doc_path(f"{parents}/notes.md")
    assert rel == f"{parents}/notes.md"

    with pytest.raises(DocPathError, match="too deep"):
        resolve_doc_path(f"{parents}/extra/notes.md")


def test_resolve_doc_path_rejects_a_symlink_escape(tmp_enso, tmp_path):
    """A link inside the tree pointing outside it fails containment."""
    _symlinked_dir(tmp_enso, tmp_path)

    with pytest.raises(DocPathError, match="escapes"):
        resolve_doc_path("link/secret.md")


# Titles


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("sub_stuff.md", "Sub Stuff"),
        ("stuff/sub_stuff.md", "Sub Stuff"),
        ("stuff", "Stuff"),
        ("some_thing", "Some Thing"),
        ("deploy-runbook.md", "Deploy Runbook"),
        ("AWS_notes.md", "AWS Notes"),
        ("NOTES.MD", "NOTES"),
    ],
)
def test_title_from_path(path, expected):
    """Titles come from the last segment, keeping existing capitalization."""
    assert title_from_path(path) == expected


def test_parent_titles_are_breadcrumb_ordered():
    """Directory segments above a doc are titled in breadcrumb order."""
    assert parent_titles("stuff/deeper/notes.md") == ["Stuff", "Deeper"]
    assert parent_titles("homelab.md") == []


# Listing


def test_load_docs_without_a_docs_directory(tmp_enso):
    """An absent docs root lists nothing and reports no truncation."""
    listing = load_docs()

    assert listing.docs == []
    assert listing.truncated is False


def test_load_docs_lists_the_nested_tree_sorted_by_path(tmp_enso):
    """Docs at any depth are listed, ordered by relative path."""
    _write(tmp_enso, "homelab.md", _doc_text())
    _write(tmp_enso, "stuff/sub_stuff.md", _doc_text())
    _write(tmp_enso, "stuff/deeper/notes.md", _doc_text())

    listing = load_docs()

    assert [d.rel_path for d in listing.docs] == [
        "homelab.md",
        "stuff/deeper/notes.md",
        "stuff/sub_stuff.md",
    ]
    assert listing.truncated is False


def test_load_docs_skips_non_markdown_and_dot_entries(tmp_enso):
    """Assets, dotfiles, and dot-directories are left out of the listing."""
    _write(tmp_enso, "homelab.md", _doc_text())
    _write(tmp_enso, "diagram.png", "not markdown")
    _write(tmp_enso, ".hidden.md", _doc_text())
    _write(tmp_enso, ".private/secret.md", _doc_text())

    assert [d.rel_path for d in load_docs().docs] == ["homelab.md"]


def test_load_docs_skips_symlinked_files(tmp_enso, tmp_path):
    """A symlinked .md file is never listed, so listing matches validation."""
    _write(tmp_enso, "homelab.md", _doc_text())
    _link_outside(tmp_enso, tmp_path)

    assert [d.rel_path for d in load_docs().docs] == ["homelab.md"]


def test_load_docs_does_not_follow_symlinked_directories(tmp_enso, tmp_path):
    """The walk never descends through a linked directory."""
    _write(tmp_enso, "homelab.md", _doc_text())
    _symlinked_dir(tmp_enso, tmp_path)

    assert [d.rel_path for d in load_docs().docs] == ["homelab.md"]


def test_load_docs_reports_truncation_at_the_entry_cap(tmp_enso):
    """Past the entry cap the listing stops and says it is incomplete."""
    for index in range(MAX_DOCS + 1):
        _write(tmp_enso, f"note_{index:04d}.md", _doc_text())

    listing = load_docs()

    assert len(listing.docs) == MAX_DOCS
    assert listing.truncated is True


def test_load_docs_skips_files_past_the_depth_cap(tmp_enso):
    """Files too deep to be addressable are dropped without claiming truncation."""
    parents = "/".join(f"level_{index}" for index in range(MAX_DOC_DEPTH - 1))
    _write(tmp_enso, f"{parents}/notes.md", _doc_text())
    _write(tmp_enso, f"{parents}/extra/too_deep.md", _doc_text())

    listing = load_docs()

    assert [d.rel_path for d in listing.docs] == [f"{parents}/notes.md"]
    # The entry cap is the only truncation the listing reports: anything past
    # the depth cap fails resolve_doc_path, so it is not a doc that was hidden.
    assert listing.truncated is False


def test_load_docs_does_not_report_truncation_for_a_deep_assets_directory(tmp_enso):
    """A pruned subtree holding no addressable doc leaves the listing complete."""
    _write(tmp_enso, "homelab.md", _doc_text())
    deep = os.path.join(*(f"level_{index}" for index in range(MAX_DOC_DEPTH)), "images")
    os.makedirs(os.path.join(tmp_enso, "docs", deep), exist_ok=True)

    listing = load_docs()

    assert [d.rel_path for d in listing.docs] == ["homelab.md"]
    assert listing.truncated is False


# Frontmatter


def test_load_doc_reads_frontmatter_fields(tmp_enso):
    """Name and description come from frontmatter when both are present."""
    _write(tmp_enso, "stuff/sub_stuff.md", _doc_text("Sub Stuff", "Where the sub stuff lives."))

    doc = load_doc("stuff/sub_stuff.md")

    assert doc.rel_path == "stuff/sub_stuff.md"
    assert doc.name == "Sub Stuff"
    assert doc.description == "Where the sub stuff lives."
    assert doc.has_frontmatter is True


def test_doc_without_frontmatter_is_listed_with_a_derived_title(tmp_enso):
    """A bare Markdown file is still listed, titled from its filename."""
    _write(tmp_enso, "stuff/sub_stuff.md", "Just prose, no header.\n")

    (doc,) = load_docs().docs

    assert doc.rel_path == "stuff/sub_stuff.md"
    assert doc.name == "Sub Stuff"
    assert doc.description == ""
    assert doc.has_frontmatter is False


def test_doc_with_malformed_yaml_is_listed_and_flagged(tmp_enso):
    """Unparseable frontmatter never hides a doc."""
    _write(tmp_enso, "broken_notes.md", "---\nname: [unclosed\n---\n\nBody.\n")

    doc = load_doc("broken_notes.md")

    assert doc.name == "Broken Notes"
    assert doc.has_frontmatter is False


def test_doc_missing_description_keeps_its_name_but_is_flagged(tmp_enso):
    """Fields fall back independently; a partial header still needs fixing."""
    _write(tmp_enso, "homelab.md", "---\nname: The Homelab\n---\n\nBody.\n")

    doc = load_doc("homelab.md")

    assert doc.name == "The Homelab"
    assert doc.description == ""
    assert doc.has_frontmatter is False


def test_load_doc_rejects_a_symlinked_file(tmp_enso):
    """A symlink is never openable as a doc, even pointing within the tree."""
    _link_inside(tmp_enso, "linked.md", "homelab.md")

    with pytest.raises(FileNotFoundError):
        load_doc("linked.md")


def test_load_doc_missing_file(tmp_enso):
    """A valid path with nothing behind it is a missing doc."""
    with pytest.raises(FileNotFoundError, match=r"stuff/sub_stuff\.md"):
        load_doc("stuff/sub_stuff.md")


def test_group_docs_puts_top_level_first_with_derived_headings(tmp_enso):
    """Groups are ordered by directory, headed by title-cased segments."""
    _write(tmp_enso, "homelab.md", _doc_text())
    _write(tmp_enso, "stuff/sub_stuff.md", _doc_text())
    _write(tmp_enso, "stuff/deeper/notes.md", _doc_text())

    groups = group_docs(load_docs().docs)

    assert [(g.rel_dir, g.heading) for g in groups] == [
        ("", "Top level"),
        ("stuff", "Stuff"),
        ("stuff/deeper", "Stuff / Deeper"),
    ]
    assert [d.rel_path for d in groups[0].docs] == ["homelab.md"]


# Whole-file read and write


def test_read_doc_returns_the_whole_file(tmp_enso):
    """Reading a doc yields its frontmatter and body verbatim."""
    text = _doc_text()
    _write(tmp_enso, "stuff/sub_stuff.md", text)

    assert read_doc("stuff/sub_stuff.md") == text


def test_read_doc_refuses_a_symlink_escaping_the_root(tmp_enso, tmp_path):
    """A link inside the tree cannot read a file outside it."""
    _link_outside(tmp_enso, tmp_path)

    with pytest.raises(DocPathError):
        read_doc("linked.md")


def test_write_doc_replaces_contents_and_normalizes_newlines(tmp_enso):
    """A whole-file save lands verbatim with CRLF collapsed to LF."""
    path = _write(tmp_enso, "stuff/sub_stuff.md", _doc_text())

    rel = write_doc("stuff/sub_stuff.md", "---\r\nname: New\r\n---\r\n\r\nRewritten.\r\n")

    assert rel == "stuff/sub_stuff.md"
    assert path.read_bytes() == b"---\nname: New\n---\n\nRewritten.\n"


def test_write_doc_refuses_a_missing_doc(tmp_enso):
    """Saving never creates a doc that does not exist yet."""
    with pytest.raises(FileNotFoundError):
        write_doc("stuff/sub_stuff.md", "Body.\n")

    assert not (_docs_root(tmp_enso) / "stuff").exists()


# Create


def test_create_doc_scaffolds_frontmatter_that_parses_back(tmp_enso):
    """The scaffold is a valid doc titled from its filename."""
    doc = create_doc("stuff/sub_stuff.md")

    meta, body = frontmatter.parse(Path(doc.path).read_text(encoding="utf-8"))
    assert meta["name"] == "Sub Stuff"
    assert meta["description"]
    assert body.strip()
    assert doc.rel_path == "stuff/sub_stuff.md"
    assert doc.name == "Sub Stuff"
    assert doc.has_frontmatter is True


def test_create_doc_appends_the_md_suffix_and_creates_parents(tmp_enso):
    """Missing directories and a missing suffix are filled in."""
    doc = create_doc("stuff/deeper/notes")

    assert doc.rel_path == "stuff/deeper/notes.md"
    assert os.path.isfile(doc.path)
    assert [d.rel_path for d in load_docs().docs] == ["stuff/deeper/notes.md"]


def test_create_doc_uses_an_explicit_name(tmp_enso):
    """A given name wins over the filename-derived title."""
    doc = create_doc("stuff/sub_stuff.md", "Deploy Runbook")

    meta, _ = frontmatter.read(doc.path)
    assert doc.name == "Deploy Runbook"
    assert meta["name"] == "Deploy Runbook"


def test_create_doc_refuses_an_existing_doc(tmp_enso):
    """An existing file is never clobbered."""
    path = _write(tmp_enso, "stuff/sub_stuff.md", _doc_text())
    original = path.read_bytes()

    with pytest.raises(FileExistsError, match=r"stuff/sub_stuff\.md"):
        create_doc("stuff/sub_stuff.md")

    assert path.read_bytes() == original


def test_create_doc_cleans_up_directories_it_created_on_failure(tmp_enso, monkeypatch):
    """A failed write leaves behind no partial file and no new directories."""

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(frontmatter, "write", boom)

    with pytest.raises(OSError, match="disk full"):
        create_doc("stuff/deeper/notes.md")

    assert not (_docs_root(tmp_enso) / "stuff").exists()


# Delete


def test_delete_doc_removes_the_file_and_prunes_empty_parents(tmp_enso):
    """Emptied directories go with the doc, but never the docs root."""
    path = _write(tmp_enso, "stuff/deeper/notes.md", _doc_text())

    delete_doc("stuff/deeper/notes.md")

    assert not path.exists()
    assert not (_docs_root(tmp_enso) / "stuff").exists()
    assert _docs_root(tmp_enso).is_dir()


def test_delete_doc_keeps_directories_that_still_hold_docs(tmp_enso):
    """Pruning stops at the first directory with anything left in it."""
    _write(tmp_enso, "stuff/sub_stuff.md", _doc_text())
    _write(tmp_enso, "stuff/deeper/notes.md", _doc_text())

    delete_doc("stuff/deeper/notes.md")

    assert not (_docs_root(tmp_enso) / "stuff" / "deeper").exists()
    assert (_docs_root(tmp_enso) / "stuff" / "sub_stuff.md").is_file()


def test_delete_doc_refuses_a_path_outside_the_docs_root(tmp_enso, tmp_path):
    """A linked directory cannot be used to delete someone else's file."""
    outside = _symlinked_dir(tmp_enso, tmp_path)

    with pytest.raises(DocPathError):
        delete_doc("link/secret.md")

    assert (outside / "secret.md").is_file()


def test_delete_doc_refuses_a_symlinked_file(tmp_enso):
    """A symlink is not a doc, so this surface will not remove it."""
    link = _link_inside(tmp_enso, "linked.md", "homelab.md")

    with pytest.raises(FileNotFoundError):
        delete_doc("linked.md")

    assert link.is_symlink()
    assert (_docs_root(tmp_enso) / "homelab.md").is_file()
