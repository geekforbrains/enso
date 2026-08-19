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
        "does not rewrite `config.json` or synthesize a `setup` marker",
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


def test_rollout_docs_describe_nonfresh_setup_as_structural_only() -> None:
    readme = " ".join(_read("README.md").split())
    changelog = " ".join(_read("CHANGELOG.md").split())
    architecture = " ".join(_read("docs/specs/architecture.md").split())
    data_model = " ".join(_read("docs/specs/data-model.md").split())
    docs_spec = " ".join(_read("docs/specs/docs.md").split())
    product_requirements = " ".join(_read("docs/PRD.md").split())

    for document in (readme, changelog):
        assert "pre-feature or completed installation" in document
        assert "structural-only" in document
        assert (
            "does not reconfigure providers, workspaces, transports, messaging, or the "
            "background service"
        ) in document
        assert "does not rewrite `config.json` or synthesize a `setup` marker" in document

    for specification in (architecture, data_model, docs_spec, product_requirements):
        assert "structural-only" in specification
        assert "does not rewrite `config.json` or synthesize a `setup` marker" in specification


def test_nonfresh_setup_guidance_does_not_promise_slack_manifest_refresh() -> None:
    readme = " ".join(_read("README.md").split())
    slack_output = " ".join(_read("docs/specs/slack-output.md").split())
    data_model = " ".join(_read("docs/specs/data-model.md").split())

    stale_claim = (
        "`enso setup` refreshes `~/.enso/slack-app-manifest.yaml` even when Slack "
        "credentials are left unchanged"
    )
    assert stale_claim not in slack_output
    assert "even when you keep existing credentials" not in readme

    for document in (readme, slack_output):
        assert "fresh or incomplete setup" in document
        assert "does not refresh `~/.enso/slack-app-manifest.yaml`" in document

    assert "When the fresh or incomplete setup wizard reconfigures a transport" in readme
    assert "copied only by fresh or incomplete Slack setup" in data_model


def test_changelog_covers_fresh_content_and_exactly_once_discovery() -> None:
    changelog = " ".join(_read("CHANGELOG.md").split())

    for contract in (
        "`setup.completed_at: null`",
        "three global reference docs",
        "one initial local Git snapshot",
        "user-owned immediately",
        "Claude and Codex now discover both instruction and skill scopes natively",
        "without a duplicate `--append-system-prompt-file` or `developer_instructions` override",
        "Grok receives the freshly validated shared content once through `--rules`",
        "unrestricted Agy receives it once through Enso's prompt envelope",
        "[unified-policy guide](docs/migrations/unified-workspace-policies.md)",
        "[v1.3 workspace guide](docs/migrations/v1.3-managed-workspaces.md)",
    ):
        assert contract in changelog


def test_active_rollout_guidance_contains_no_retired_content_model_claims() -> None:
    active_files = (
        ROOT / "README.md",
        ROOT / "docs" / "PRD.md",
        *(ROOT / "docs" / "specs").glob("*.md"),
        *(ROOT / "docs" / "examples").iterdir(),
        *(ROOT / "src" / "enso" / "prompts").glob("*.md"),
        *(ROOT / "src" / "enso" / "skills").glob("*/SKILL.md"),
        *(ROOT / "src" / "enso" / "starter_docs").glob("**/*.md"),
    )
    forbidden = (
        "enso does not initialize git",
        "enso ships no docs",
        "docs tree starts empty",
        "shared instructions are injected into every provider launch",
        "global docs",
        "global enso docs",
        "workspaces/<name>/references/",
        "~/.enso/workspaces/clients/",
    )

    for path in active_files:
        text = path.read_text(encoding="utf-8").lower()
        for claim in forbidden:
            assert claim not in text, f"{path.relative_to(ROOT)} contains {claim!r}"
