"""Contracts for the breaking managed-workspace migration documentation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "migrations" / "v1.3-managed-workspaces.md"


def _read(relative: str) -> str:
    return ROOT.joinpath(relative).read_text(encoding="utf-8")


def test_v13_guide_is_manual_complete_and_ordered() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    flat_text = " ".join(text.split())

    assert (
        "Enso never relocates, copies, or deletes a legacy workspace and never follows "
        "its configured path as a compatibility fallback."
    ) in text
    for contract in (
        "if [ -e ~/.enso-backup-before-v1.3 ] || [ -L ~/.enso-backup-before-v1.3 ]",
        "cp -a ~/.enso ~/.enso-backup-before-v1.3",
        "Refuse any existing backup destination and choose a new path",
        "lowercase letters and numbers",
        "If the destination already contains content, stop",
        "The `/.` form is intentional: it includes dotfiles.",
        "Preserve file ownership and modes",
        "A `.git` directory, file, or symlink directly inside the workspace is invalid",
        "Enso will not follow it as a compatibility shortcut",
        "An existing configuration with no `setup` field is a pre-feature installation",
        "Adopt any of this content into an upgraded installation manually and deliberately",
        "this setup invocation is structural-only",
        "never opens the provider, default-workspace, transport, test-message, or "
        "background-service prompts",
        "leaves the stopped service untouched",
        "Service installation and restart belong only in step 7",
        "Only then remove or archive the old roots",
    ):
        assert contract in flat_text

    ordered_steps = (
        "enso service stop",
        "## 2. Choose valid names and inspect collisions",
        "cp -a /old/workspace/. ~/.enso/workspaces/client-a/",
        "Remove `path` from every workspace entry",
        "enso setup",
        "enso workspace repair client-a",
        "enso config check",
        "enso service restart",
        "Only then remove or archive the old roots",
    )
    position = -1
    for step in ordered_steps:
        position = text.index(step, position + 1)


def test_active_guidance_has_no_legacy_workspace_path_examples() -> None:
    active_files = (
        "README.md",
        "docs/examples/teams-config.jsonc",
        "docs/specs/architecture.md",
        "docs/specs/data-model.md",
        "docs/specs/permissions.md",
        "docs/specs/teams.md",
        "docs/specs/web.md",
        "src/enso/skills/workspace/SKILL.md",
        "src/enso/starter_docs/enso/layout.md",
    )
    legacy_path = re.compile(r'"path"\s*:\s*"~/.enso/workspaces/')
    for relative in active_files:
        text = _read(relative)
        assert legacy_path.search(text) is None, relative
        assert "~/.enso/workspaces/clients/" not in text, relative


def test_packaged_workspace_guidance_links_the_canonical_migration_guide() -> None:
    skill = _read("src/enso/skills/workspace/SKILL.md")

    assert (
        "https://github.com/geekforbrains/enso/blob/main/docs/migrations/"
        "v1.3-managed-workspaces.md"
    ) in skill


def test_older_unified_policy_guide_remains_an_identified_historical_record() -> None:
    historical = _read("docs/migrations/unified-workspace-policies.md")

    assert "Historical record" in historical
    assert "[v1.3 managed-workspace guide](v1.3-managed-workspaces.md)" in historical
    assert '"path": "~/.enso/workspaces/default"' in historical
    assert "are injected into every provider launch" in historical
    assert "seeds missing shared and local templates on setup or service start" in historical
    assert (
        "immutable content-addressed snapshot under `~/.enso/runtime/instructions/`"
        in historical
    )
    assert "Claude receives that snapshot through `--append-system-prompt-file`" in historical
    assert "Codex receives the validated content through `developer_instructions`" in historical
