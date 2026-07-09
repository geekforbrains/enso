"""Tests for enso.frontmatter."""

from __future__ import annotations

import os

from enso import frontmatter


def test_dumps_parse_round_trip():
    meta = {"title": "Hello", "status": "todo", "notify": True}
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
    parsed_meta, _ = frontmatter.parse(frontmatter.dumps(meta, "task with tags"))
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
    path = str(tmp_path / "nested" / "TASK.md")
    meta = {"title": "Persisted", "tags": ["x", "y"]}
    body = "Body text.\n\nWith a blank line."
    frontmatter.write(path, meta, body)
    assert os.path.exists(path)
    read_meta, read_body = frontmatter.read(path)
    assert read_meta == meta
    assert read_body == body + "\n"


def test_write_leaves_no_temp_files(tmp_path):
    path = str(tmp_path / "TASK.md")
    frontmatter.write(path, {"a": 1}, "hi")
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers == []


def test_write_overwrites_existing(tmp_path):
    path = str(tmp_path / "TASK.md")
    frontmatter.write(path, {"v": 1}, "first")
    frontmatter.write(path, {"v": 2}, "second")
    meta, body = frontmatter.read(path)
    assert meta == {"v": 2}
    assert body == "second\n"
