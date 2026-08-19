"""Portable-format and behavior checks for Enso's bundled Agent Skills."""

from __future__ import annotations

import importlib.resources
import re

from enso import frontmatter

EXPECTED_SKILLS = {"docs", "jobs", "slack", "tables", "workspace"}
ALLOWED_FRONTMATTER = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def test_bundled_skills_follow_agent_skills_specification():
    bundled = importlib.resources.files("enso").joinpath("skills")
    skill_dirs = {entry.name: entry for entry in bundled.iterdir() if entry.is_dir()}

    assert set(skill_dirs) == EXPECTED_SKILLS
    for directory_name, skill_dir in skill_dirs.items():
        skill_file = skill_dir.joinpath("SKILL.md")
        assert skill_file.is_file()
        text = skill_file.read_text(encoding="utf-8")
        metadata, body = frontmatter.parse(text)

        assert set(metadata) <= ALLOWED_FRONTMATTER
        assert metadata["name"] == directory_name
        assert 1 <= len(metadata["name"]) <= 64
        assert SKILL_NAME.fullmatch(metadata["name"])
        assert isinstance(metadata["description"], str)
        assert 1 <= len(metadata["description"].strip()) <= 1024
        assert body.strip()
        assert len(text.splitlines()) < 500

        compatibility = metadata.get("compatibility")
        if compatibility is not None:
            assert isinstance(compatibility, str)
            assert 1 <= len(compatibility.strip()) <= 500
        extra = metadata.get("metadata")
        if extra is not None:
            assert isinstance(extra, dict)
            assert all(isinstance(key, str) for key in extra)
            assert all(isinstance(value, str) for value in extra.values())
        allowed_tools = metadata.get("allowed-tools")
        if allowed_tools is not None:
            assert isinstance(allowed_tools, str)


def test_slack_skill_discovers_rich_output_and_guards_text_only_paths():
    skill = (
        importlib.resources.files("enso")
        .joinpath("skills", "slack", "SKILL.md")
        .read_text(encoding="utf-8")
    )
    metadata, _ = frontmatter.parse(skill)
    description = metadata["description"].lower()

    for trigger in ("rich replies", "tables", "charts", "canvas", "app home"):
        assert trigger in description
    for contract in ("enso-message", "enso-surface", "fallback_text"):
        assert contract in skill
    assert "current interactive turn advertises that capability" in skill
    assert "Never send an `enso-message` or `enso-surface` envelope through" in skill
    assert "`enso message send`" in skill
    assert 'enso message send "text" --to D0123456789' in skill


def test_workspace_skill_documents_only_the_canonical_managed_layout():
    skill = (
        importlib.resources.files("enso")
        .joinpath("skills", "workspace", "SKILL.md")
        .read_text(encoding="utf-8")
    )

    for contract in (
        "lowercase kebab-case",
        "~/.enso/workspaces/<name>",
        "~/.enso/skills/",
        "~/.enso/.agents/skills -> ../skills",
        "~/.enso/.claude/skills -> ../skills",
        "<workspace>/skills/",
        "<workspace>/.agents/skills -> ../skills",
        "<workspace>/.claude/skills -> ../skills",
        "CLAUDE.md -> AGENTS.md",
        "knowledge/README.md",
    ):
        assert contract in skill

    assert "starts empty" in skill
    assert "user-owned" in skill
    assert "never overwrites" in skill
    assert "unknown paths" in skill
    assert "migration guide" in skill

    # Workspace paths are name-derived; the removed configurable-path shape and
    # hand-built provider copies must not return as a compatibility mechanism.
    assert '"path": "~/.enso/workspaces/project-name"' not in skill
    assert "absolute path or a path beginning" not in skill
    assert "<workspace>/.agents/skills/<skill-name>" not in skill
    assert "enso workspace create" not in skill
    assert "enso workspace repair" not in skill


def test_docs_skill_is_the_complete_content_placement_contract():
    skill = (
        importlib.resources.files("enso")
        .joinpath("skills", "docs", "SKILL.md")
        .read_text(encoding="utf-8")
    )

    for contract in (
        "Search before creating",
        "update the existing authoritative source",
        "link to it instead of copying",
        "always-loaded behavior",
        "setup-specific runbooks",
        "workspace-only durable material",
        "product and project facts",
        "configured knowledge base",
        "reusable general procedures",
        "structured, queryable facts",
        "editable output",
        "current turn",
        "credential locations",
        "never secret values",
        "live verification",
    ):
        assert contract in skill

    assert "Enso ships no docs" not in skill


def test_workspace_skill_prefers_a_small_prompt_and_knowledge_index():
    skill = (
        importlib.resources.files("enso")
        .joinpath("skills", "workspace", "SKILL.md")
        .read_text(encoding="utf-8")
    )

    for contract in (
        "small routing prompt",
        "meanings of ambiguous terms",
        "critical approvals",
        "authoritative source",
        "when to read",
        "knowledge/README.md",
    ):
        assert contract in skill

    assert "self-contained domain manual" not in skill


def test_content_mutation_skills_require_scoped_enso_snapshots():
    bundled = importlib.resources.files("enso").joinpath("skills")

    for name in ("docs", "jobs", "workspace"):
        skill = bundled.joinpath(name, "SKILL.md").read_text(encoding="utf-8")
        for contract in (
            "after each coherent change to versionable Enso content",
            'enso snapshot create --message "<summary>" -- <changed-path>',
            "explicit paths",
            "snapshot locks and transaction state",
            ".snapshot.transaction.json",
            ".snapshot-transaction-*.tmp",
            ".snapshot-index-*",
            "resolved Git directory",
            "Never remove a native Git index lock",
            "Never use raw broad Git staging",
            "If the active policy denies",
            "report that boundary",
            "restore, reset, or delete",
        ):
            assert contract in skill, f"{name} skill is missing {contract!r}"


def test_tables_skill_keeps_runtime_database_out_of_content_snapshots():
    skill = (
        importlib.resources.files("enso")
        .joinpath("skills", "tables", "SKILL.md")
        .read_text(encoding="utf-8")
    )

    for contract in (
        "protected runtime state",
        "not versionable Enso content",
        "never pass it to `enso snapshot create`",
    ):
        assert contract in skill
