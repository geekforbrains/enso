"""The editable style checker and its installed support files stay independent of core rules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from enso import workspaces

SKILL = Path(workspaces.__file__).parent / "bundled/skills/enso-knowledge"


def lint(*paths):
    return subprocess.run(
        [sys.executable, str(SKILL / "scripts/lint.py"), *(str(path) for path in paths)],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_style_reports_precise_lines_without_rewriting_content(tmp_path):
    note = tmp_path / "Readable title.md"
    original = b"Intro \r\n## Detail\r\nContent\r\n\r\n\r\nLast"
    note.write_bytes(original)

    result = lint(note)

    assert result.returncode == 1 and result.stderr == ""
    assert f"{note}:1: line-endings:" in result.stdout
    assert f"{note}:1: trailing-whitespace:" in result.stdout
    assert f"{note}:2: heading-spacing: add a blank line before heading" in result.stdout
    assert f"{note}:2: heading-spacing: add a blank line after heading" in result.stdout
    assert f"{note}:5: blank-lines:" in result.stdout
    assert f"{note}:6: final-newline:" in result.stdout
    assert note.read_bytes() == original


def test_style_preserves_frontmatter_code_and_intentional_markdown_breaks(tmp_path):
    note = tmp_path / "Note.md"
    note.write_text(
        "---\nsummary: |\n  old imported metadata  \n\n\n---\n"
        "Opening paragraph without an H1.  \nA Markdown line break.\n\n"
        "````python\n# comment\nvalue = 'spaces' \n\n\n```\n## still code\n````\n\n"
        "~~~text\n## same for tilde fences\n\n\ncontent  \n~~~\n\n"
        "    # indented code\n\n\n    value = 'spaces' \n\n"
        "## Notes\n\nA very long paragraph " + "word " * 100 + "ends here.\n"
    )

    result = lint(note)

    assert result.returncode == 0, result.stdout
    assert "Checked 1 Markdown files: 0 style findings, 0 input errors." in result.stdout


def test_style_walks_nested_notes_once_and_skips_hidden_files_and_links(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    note = root / "One.md"
    note.write_text("A normal note.\n")
    nested = root / "Folder"
    nested.mkdir()
    (nested / "Two.MD").write_text("Another normal note.\n")
    hidden = root / ".obsidian"
    hidden.mkdir()
    (hidden / "Ignored.md").write_text("bad \n\n\n")
    (root / ".Hidden.md").write_text("bad \n\n\n")
    (root / "loop").symlink_to(root, target_is_directory=True)
    outside = tmp_path / "Outside.md"
    outside.write_text("bad \n\n\n")
    (root / "Ignored.md").symlink_to(outside)

    result = lint(root, note)

    assert result.returncode == 0, result.stdout
    assert "Checked 2 Markdown files" in result.stdout


def test_style_reports_bad_inputs_and_keeps_checking_readable_notes(tmp_path):
    (tmp_path / "Invalid.md").write_bytes(b"\xff")
    (tmp_path / "Readable.md").write_text("A note.\n")
    result = lint(tmp_path)
    assert result.returncode == 2 and result.stderr == ""
    assert "Invalid.md:1: input:" in result.stdout
    assert "Checked 1 Markdown files" in result.stdout
    result = lint(tmp_path / "Missing.md")
    assert result.returncode == 2 and "expected an existing file or directory" in result.stdout
