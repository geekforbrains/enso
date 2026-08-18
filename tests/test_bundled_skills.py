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
