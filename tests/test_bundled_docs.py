"""Contract checks for Enso's fresh-install starter reference docs."""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import pytest

from enso import frontmatter

EXPECTED_DOCS = {
    "enso/content_model.md": (
        "Enso Content Model",
        "Where durable Enso context belongs and which source wins; read before creating, "
        "moving, or duplicating persistent knowledge.",
    ),
    "enso/layout.md": (
        "Enso Layout",
        "The current managed Enso filesystem and local-history boundaries; read when "
        "locating, validating, or repairing installation content.",
    ),
    "operator.md": (
        "Operator",
        "Confirmed operator identity, locale, communication preferences, and standing "
        "personal context; read when a task depends on those facts.",
    ),
}


def _resource_text(rel_path: str) -> str:
    resource = importlib.resources.files("enso").joinpath("starter_docs", *rel_path.split("/"))
    assert resource.is_file()
    return resource.read_text(encoding="utf-8")


@pytest.mark.parametrize(("rel_path", "metadata"), EXPECTED_DOCS.items())
def test_starter_docs_are_packaged_markdown_with_discoverable_frontmatter(
    rel_path: str,
    metadata: tuple[str, str],
):
    text = _resource_text(rel_path)
    fields, body = frontmatter.parse(text)

    assert fields == {"name": metadata[0], "description": metadata[1]}
    assert fields["description"].endswith(".")
    assert "read " in fields["description"].lower()
    assert body.startswith("# ")
    assert body.strip()


def test_content_model_is_the_complete_placement_and_ownership_contract():
    text = _resource_text("enso/content_model.md")
    prose = " ".join(text.split())

    for placement in (
        "always-loaded behavior",
        "`AGENTS.md`",
        "installation and operator facts",
        "setup-specific runbooks",
        "global docs",
        "workspace-only durable material",
        "`knowledge/`",
        "product and project facts",
        "repository docs",
        "human and business knowledge",
        "configured knowledge base",
        "reusable general procedures",
        "skills",
        "schedules",
        "jobs",
        "structured or queryable facts",
        "tables",
        "editable output",
        "`drafts/`",
        "turn-only facts",
        "reply",
    ):
        assert placement in text

    for ownership_rule in (
        "Search before creating",
        "Update an existing authoritative source",
        "Link to authoritative material instead of copying it",
        "Prefer repository docs or the configured knowledge base over an Enso cache",
        "Record where credentials are stored, never credential or secret values",
        "Mark volatile facts for live verification",
        "source, service, and account",
        "one responsibility",
        "what it contains and when to read it",
    ):
        assert ownership_rule in prose


def test_layout_describes_only_the_canonical_generic_tree_and_history_boundary():
    text = _resource_text("enso/layout.md")

    for contract in (
        "~/.enso/",
        ".git/",
        "AGENTS.md",
        "CLAUDE.md -> AGENTS.md",
        "skills/",
        ".agents/skills -> ../skills",
        ".claude/skills -> ../skills",
        "docs/enso/content_model.md",
        "docs/enso/layout.md",
        "docs/operator.md",
        "workspaces/<name>/",
        "lowercase kebab-case",
        "name-derived",
        "knowledge/",
        "drafts/",
        "uploads/",
        "local history, not a backup",
        "Versioned",
        "Runtime-only",
    ):
        assert contract in text

    lowered = text.lower()
    for live_install_fact in (
        "nightly audit",
        "rule id",
        "gavin",
        "configured job",
        "external workspace",
        "workspace path field",
    ):
        assert live_install_fact not in lowered


def test_operator_doc_is_an_editable_template_without_inferred_or_secret_facts():
    text = _resource_text("operator.md")

    for field in (
        "## Confirmed identity",
        "## Locale and timezone",
        "## Communication preferences",
        "## Standing personal context",
    ):
        assert field in text
    assert "Do not infer missing facts" in text
    assert "Never record secrets" in text
    assert "confirmed with the operator" in text
    assert "No confirmed facts yet." in text

    lowered = text.lower()
    for forbidden in ("password:", "token:", "api key:", "secret:"):
        assert forbidden not in lowered


def test_starter_docs_are_declared_as_package_data():
    project = Path(__file__).resolve().parents[1]
    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")

    assert '"starter_docs/**/*.md"' in pyproject
