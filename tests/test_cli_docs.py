"""CLI behavior for the reference doc subcommands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from enso import cli as cli_mod
from enso import frontmatter

runner = CliRunner()


def docs_dir(tmp_enso: str) -> Path:
    """Path to the temporary docs root, which starts out absent."""
    return Path(tmp_enso) / "docs"


def write_doc(tmp_enso: str, rel_path: str, text: str) -> Path:
    """Write a doc file under the docs root, creating parent directories."""
    path = docs_dir(tmp_enso) / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def doc_text(name: str, description: str) -> str:
    """Render a well-formed doc with both required frontmatter fields."""
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody.\n"


def test_doc_list_reports_empty_tree(tmp_enso):
    result = runner.invoke(cli_mod.app, ["doc", "list"])

    assert result.exit_code == 0
    assert "No docs found" in result.output
    assert "enso doc create" in result.output


def test_doc_list_shows_path_name_and_description_at_any_depth(tmp_enso):
    write_doc(tmp_enso, "homelab.md", doc_text("Homelab", "Network map."))
    write_doc(tmp_enso, "stuff/sub_stuff.md", doc_text("Sub Stuff", "Side notes."))
    write_doc(tmp_enso, "stuff/deeper/notes.md", doc_text("Notes", "Deep notes."))

    result = runner.invoke(cli_mod.app, ["doc", "list"])

    assert result.exit_code == 0
    for expected in (
        "homelab.md",
        "Homelab",
        "Network map.",
        "stuff/sub_stuff.md",
        "Sub Stuff",
        "Side notes.",
        "stuff/deeper/notes.md",
        "Notes",
        "Deep notes.",
    ):
        assert expected in result.output


@pytest.mark.parametrize(
    ("content", "expected_name"),
    [
        ("Body without any frontmatter.\n", "Sub Stuff"),
        ("---\nname: [unclosed\n---\n\nBody.\n", "Sub Stuff"),
        ("---\nname: Kept Title\n---\n\nBody.\n", "Kept Title"),
    ],
)
def test_doc_list_keeps_docs_with_unusable_frontmatter(tmp_enso, content, expected_name):
    write_doc(tmp_enso, "sub_stuff.md", content)

    result = runner.invoke(cli_mod.app, ["doc", "list"])

    assert result.exit_code == 0
    assert "sub_stuff.md" in result.output
    assert expected_name in result.output
    assert "needs frontmatter" in result.output


def test_doc_list_renders_bracketed_frontmatter_literally(tmp_enso):
    """Square brackets in operator text are content, never Rich markup."""
    write_doc(tmp_enso, "a.md", doc_text("A", "Use [/] to close."))
    write_doc(tmp_enso, "b.md", doc_text("B [beta]", "See [network] doc."))

    result = runner.invoke(cli_mod.app, ["doc", "list"])

    assert result.exit_code == 0
    assert "Use [/] to close." in result.output
    assert "B [beta]" in result.output
    assert "See [network] doc." in result.output


def test_doc_create_reports_an_unusable_parent_path(tmp_enso):
    """A regular file standing where a parent directory belongs fails cleanly."""
    existing = write_doc(tmp_enso, "notes.md", doc_text("Notes", "Already here."))

    result = runner.invoke(cli_mod.app, ["doc", "create", "notes.md/sub/x.md"])

    assert result.exit_code == 1
    assert "Could not create doc" in result.output
    assert existing.is_file()


def test_doc_create_scaffolds_frontmatter_derived_from_filename(tmp_enso):
    result = runner.invoke(cli_mod.app, ["doc", "create", "deploy_runbook.md"])

    assert result.exit_code == 0
    assert "Doc created" in result.output
    path = docs_dir(tmp_enso) / "deploy_runbook.md"
    assert path.is_file()
    meta, body = frontmatter.read(str(path))
    assert meta["name"] == "Deploy Runbook"
    assert meta["description"].strip()
    assert body.strip()


def test_doc_create_name_option_overrides_derived_title(tmp_enso):
    result = runner.invoke(
        cli_mod.app, ["doc", "create", "deploy_runbook.md", "--name", "Deploy Runbook v2"]
    )

    assert result.exit_code == 0
    meta, _ = frontmatter.read(str(docs_dir(tmp_enso) / "deploy_runbook.md"))
    assert meta["name"] == "Deploy Runbook v2"


def test_doc_create_appends_md_suffix_and_creates_parents(tmp_enso):
    result = runner.invoke(cli_mod.app, ["doc", "create", "stuff/deeper/notes"])

    assert result.exit_code == 0
    assert (docs_dir(tmp_enso) / "stuff" / "deeper" / "notes.md").is_file()


def test_doc_create_refuses_existing_doc(tmp_enso):
    args = ["doc", "create", "stuff/sub_stuff.md"]
    assert runner.invoke(cli_mod.app, args).exit_code == 0
    path = docs_dir(tmp_enso) / "stuff" / "sub_stuff.md"
    original = path.read_bytes()

    second = runner.invoke(cli_mod.app, args)

    assert second.exit_code == 1
    assert "already exists" in second.output
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.md",
        "stuff/../../escape.md",
        "/etc/escape.md",
        "stuff/.hidden.md",
        "stuff\\escape.md",
    ],
)
def test_doc_create_rejects_unsafe_path(tmp_enso, bad_path):
    result = runner.invoke(cli_mod.app, ["doc", "create", bad_path])

    assert result.exit_code == 1
    assert "Could not create doc" in result.output
    assert not docs_dir(tmp_enso).exists()
