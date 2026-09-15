"""Vault imports preserve originals and publish only a complete, isolated copy."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from enso import frontmatter


@pytest.fixture
def importer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "import-knowledge.py"
    spec = importlib.util.spec_from_file_location("knowledge_import_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_import_preserves_sources_assets_and_legacy_metadata(tmp_path, importer):
    source = tmp_path / "source"
    note = source / "Campaigns" / "North coast" / "Locations.md"
    note.parent.mkdir(parents=True)
    original = (
        b"---\ntags: [places]\nsource: https://example.com\n---\n\n## Places\n\n[[Overview]]\n"
    )
    note.write_bytes(original)
    asset = note.with_suffix(".png")
    asset.write_bytes(b"\x89PNG\x00example")
    legacy = source / "Legacy.markdown"  # not a note to Enso: copied byte for byte
    legacy.write_bytes(b"---\ntags: [old]\n---\nlegacy\n")
    (source / "Empty folder").mkdir()
    (source / ".obsidian").mkdir()
    (source / ".obsidian" / "workspace.json").write_text("{}")
    (source / "escape.md").symlink_to(tmp_path / "unrelated.md")
    destination = tmp_path / "home" / "knowledge"
    receipt = tmp_path / "import.json"
    report = importer.import_vault(source, destination, receipt)

    assert note.read_bytes() == original
    imported = destination / note.relative_to(source)
    parsed, problem = frontmatter.parse(imported.read_text())
    assert problem is None
    assert parsed.fields.keys() == {"schema", "id"}
    assert "tags: [places]" in parsed.body and "https://example.com" in parsed.body
    assert "[[Overview]]" in parsed.body
    assert imported.with_suffix(".png").read_bytes() == asset.read_bytes()
    assert (destination / "Legacy.markdown").read_bytes() == legacy.read_bytes()
    assert (destination / "Empty folder").is_dir()
    assert not (destination / ".obsidian").exists()
    assert not (destination / "escape.md").exists()
    assert json.loads(receipt.read_text())["state"] == "complete"
    assert len(report["files"]) == 3
    (entry,) = [item for item in report["files"] if item["markdown"]]
    assert entry["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert entry["imported_sha256"] == hashlib.sha256(imported.read_bytes()).hexdigest()


def test_import_refuses_existing_content_and_destination_links(tmp_path, importer):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("## Note\n")
    destination = tmp_path / "knowledge"
    destination.mkdir()
    existing = destination / "existing.md"
    existing.write_text("preserve me")
    with pytest.raises(ValueError, match="empty"):
        importer.import_vault(source, destination, tmp_path / "receipt.json")
    assert existing.read_text() == "preserve me"
    alias = tmp_path / "alias"
    alias.symlink_to(destination)
    with pytest.raises(ValueError, match="symbolic"):
        importer.import_vault(source, alias, tmp_path / "receipt.json")


def test_failed_import_does_not_publish_partial_vault(tmp_path, importer):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("## Valid\n")
    (source / "z.md").write_bytes(b"\xffinvalid UTF-8")
    destination = tmp_path / "knowledge"
    destination.mkdir()
    with pytest.raises(UnicodeError):
        importer.import_vault(source, destination, tmp_path / "receipt.json")
    assert not list(destination.iterdir())
    assert not list(tmp_path.glob(".knowledge-import-*/"))
    assert (source / "z.md").read_bytes() == b"\xffinvalid UTF-8"


def test_import_rejects_a_destination_inside_the_source(tmp_path, importer):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="outside"):
        importer.import_vault(source, source / "knowledge", tmp_path / "receipt.json")
