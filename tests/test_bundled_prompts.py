"""Contracts for the small, always-loaded bundled instruction prompts."""

from __future__ import annotations

import importlib.resources


def _prompt(name: str) -> str:
    return importlib.resources.files("enso").joinpath("prompts", name).read_text(encoding="utf-8")


def test_bundled_prompt_inventory_is_complete_and_bounded() -> None:
    prompts = importlib.resources.files("enso").joinpath("prompts")
    names = {entry.name for entry in prompts.iterdir() if entry.is_file()}

    assert names == {
        "AGENTS.md",
        "WORKSPACE_AGENTS.md",
        "WORKSPACE_KNOWLEDGE_README.md",
    }


def test_root_prompt_keeps_only_always_needed_authority_and_delivery_rules():
    prompt = _prompt("AGENTS.md")

    for required in (
        "configured policy",
        "direct request",
        "untrusted data",
        "Confirm before",
        "final response",
        "originating conversation",
        "enso message send",
        "text-only",
        "enso doc list",
        "when present",
        "enso/content_model.md",
        "enso/layout.md",
        "operator.md",
        "workspace's local `AGENTS.md`",
    ):
        assert required in prompt


def test_root_prompt_does_not_embed_dynamic_inventories_or_operating_manuals():
    prompt = _prompt("AGENTS.md")

    for excluded in (
        "## Enso CLI and bundled skills",
        "## Workspace conventions",
        "## Origin metadata",
        "## Scheduled jobs",
        "enso job list",
        "enso table list",
        "enso config check",
        "ENSO_ORIGIN_",
        "~/.enso/skills/",
        "knowledge/",
        "drafts/",
        "uploads/",
    ):
        assert excluded not in prompt


def test_root_prompt_requires_safe_scoped_content_snapshots():
    prompt = _prompt("AGENTS.md")

    for contract in (
        "after each coherent change to versionable Enso content",
        "one scoped local snapshot",
        'enso snapshot create --message "<summary>" -- <changed-path>',
        "explicit paths",
        "root and workspace instructions",
        "canonical skills",
        "global reference docs",
        "workspace knowledge",
        "durable job definitions",
        "protected runtime state",
        "credentials",
        "databases",
        "uploads",
        "drafts",
        "native policy homes",
        "snapshot locks and transaction state",
        ".snapshot.transaction.json",
        ".snapshot-transaction-*.tmp",
        ".snapshot-index-*",
        "resolved Git directory",
        "Never remove a native Git index lock",
        "Never use raw broad Git staging",
        "If the active policy denies",
        "report that boundary",
        "restore, reset, or delete history operations",
    ):
        assert contract in prompt

    for unavailable in (
        "enso snapshot restore",
        "enso snapshot reset",
        "enso snapshot delete",
    ):
        assert unavailable not in prompt


def test_workspace_prompt_is_a_minimal_scope_and_source_router():
    prompt = _prompt("WORKSPACE_AGENTS.md")

    for required in (
        "{{workspace_name}}",
        "purpose",
        "scope",
        "ambiguous terms",
        "configured policy",
        "Confirm before",
        "knowledge/README.md",
        "when to read",
        "authoritative source",
        "ask the user before relying on assumptions",
    ):
        assert required in prompt

    for excluded in (
        "drafts/",
        "uploads/",
        ".agents/",
        ".claude/",
        "skill definitions",
        "enso job",
    ):
        assert excluded not in prompt


def test_workspace_knowledge_index_is_a_minimal_source_router() -> None:
    prompt = _prompt("WORKSPACE_KNOWLEDGE_README.md")

    for contract in (
        "# Workspace knowledge index",
        "durable context",
        "only to this workspace",
        "authoritative sources",
        'path-and-"when to read" entry',
        "added, moved, or removed",
    ):
        assert contract in prompt

    assert "references/" not in prompt
