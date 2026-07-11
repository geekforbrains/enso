"""Tests for enso.frontmatter."""

from __future__ import annotations

import os

import pytest

from enso import frontmatter


def test_dumps_parse_round_trip():
    meta = {"name": "Hello", "enabled": False, "notify": True}
    body = "This is the body.\nSecond line."
    parsed_meta, parsed_body = frontmatter.parse(frontmatter.dumps(meta, body))
    assert parsed_meta == meta
    assert parsed_body == body + "\n"


def test_dumps_preserves_key_order():
    meta = {"z": 1, "a": 2, "m": 3}
    text = frontmatter.dumps(meta, "body")
    # Keys serialize in insertion order (sort_keys=False), not alphabetically.
    assert text.index("z:") < text.index("a:") < text.index("m:")


def test_parse_no_frontmatter():
    text = "just a plain body\nno fence here"
    meta, body = frontmatter.parse(text)
    assert meta == {}
    assert body == text


def test_parse_unterminated_fence_is_bare_body():
    text = "---\ntitle: X\nno closing fence"
    meta, body = frontmatter.parse(text)
    assert meta == {}
    assert body == text


def test_parse_missing_trailing_newline():
    text = "---\ntitle: X\n---\n\nbody without newline"
    meta, body = frontmatter.parse(text)
    assert meta == {"title": "X"}
    assert body == "body without newline"


def test_list_values_round_trip():
    meta = {"tags": ["alpha", "beta", "gamma"], "notify": False}
    parsed_meta, _ = frontmatter.parse(frontmatter.dumps(meta, "document with tags"))
    assert parsed_meta["tags"] == ["alpha", "beta", "gamma"]
    assert parsed_meta["notify"] is False


def test_empty_meta_round_trip():
    meta, body = frontmatter.parse(frontmatter.dumps({}, "body only"))
    assert meta == {}
    assert body == "body only\n"


def test_body_internal_blank_line_preserved():
    body = "First para.\n\nSecond para."
    _, parsed_body = frontmatter.parse(frontmatter.dumps({"a": 1}, body))
    assert parsed_body == body + "\n"


def test_read_write_round_trip(tmp_path):
    path = str(tmp_path / "nested" / "JOB.md")
    meta = {"title": "Persisted", "tags": ["x", "y"]}
    body = "Body text.\n\nWith a blank line."
    frontmatter.write(path, meta, body)
    assert os.path.exists(path)
    read_meta, read_body = frontmatter.read(path)
    assert read_meta == meta
    assert read_body == body + "\n"


def test_write_leaves_no_temp_files(tmp_path):
    path = str(tmp_path / "JOB.md")
    frontmatter.write(path, {"a": 1}, "hi")
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers == []


def test_write_overwrites_existing(tmp_path):
    path = str(tmp_path / "JOB.md")
    frontmatter.write(path, {"v": 1}, "first")
    frontmatter.write(path, {"v": 2}, "second")
    meta, body = frontmatter.read(path)
    assert meta == {"v": 2}
    assert body == "second\n"


def test_replace_body_preserves_raw_legacy_frontmatter():
    prefix = (
        "---\r\n"
        "# Keep this comment and CRLF formatting.\r\n"
        "name: Daily: Review\r\n"
        "notify:\r\n"
        "enabled: false\r\n"
        "---\r\n"
        "\r\n"
    )
    text = prefix + "Old prompt.\r\n"

    assert frontmatter.replace_body(text, "New prompt.") == prefix + "New prompt.\n"


def test_replace_body_adds_line_ending_after_closing_fence_at_eof():
    text = "---\nname: Demo\n---"

    updated = frontmatter.replace_body(text, "New prompt.")

    assert updated == text + "\nNew prompt.\n"
    assert frontmatter.parse(updated) == ({"name": "Demo"}, "New prompt.\n")
    assert frontmatter.replace_body(updated, "Second prompt.").endswith(
        "---\nSecond prompt.\n"
    )


def test_write_body_preserves_frontmatter_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "JOB.md"
    prefix = "---\nname: Daily: Review\noptional:\n---\n\n"
    path.write_text(prefix + "Old body.\n", encoding="utf-8")

    frontmatter.write_body(str(path), "Replacement body.")

    assert path.read_text(encoding="utf-8") == prefix + "Replacement body.\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_replace_scalar_preserves_unrelated_frontmatter():
    text = (
        "---\r\n"
        "name: Daily: Review\r\n"
        "optional:\r\n"
        "enabled : false  # preserve this comment\r\n"
        "---\r\n\r\n"
        "Prompt.\r\n"
    )

    assert frontmatter.replace_scalar(text, "enabled", "true") == text.replace(
        "enabled : false  #", "enabled : true  #"
    )


def test_replace_scalar_inserts_missing_field_before_closing_fence():
    text = "---\nname: Demo\n---\n\nPrompt.\n"

    assert frontmatter.replace_scalar(text, "enabled", "false") == (
        "---\nname: Demo\nenabled: false\n---\n\nPrompt.\n"
    )


@pytest.mark.parametrize("operation", [frontmatter.replace_body, frontmatter.replace_scalar])
def test_lossless_edits_reject_bare_documents(operation):
    with pytest.raises(ValueError, match="no complete leading frontmatter"):
        if operation is frontmatter.replace_scalar:
            operation("Bare document", "enabled", "true")
        else:
            operation("Bare document", "Body")
