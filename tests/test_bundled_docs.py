"""Contract checks for Enso's fresh-install starter reference docs."""

from __future__ import annotations

import importlib.resources
import shutil
import subprocess
import zipfile
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
        "global reference docs",
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
        assert placement in prose

    assert "a global reference doc instead" in prose
    assert "a global doc" not in prose

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
        "global reference docs",
        "workspace knowledge",
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


def test_layout_routes_content_history_through_safe_scoped_snapshots():
    text = _resource_text("enso/layout.md")
    prose = " ".join(text.split())

    for contract in (
        'enso snapshot create --message "<summary>" -- <changed-path>',
        "one coherent change",
        "explicit paths",
        "clean staging area",
        "successful no-op",
        "protected alternate index",
        "complete owner-only `0600` index",
        "resolved Git directory",
        "owner-only transaction marker",
        ".snapshot.transaction.json",
        ".snapshot-transaction-<32-lowercase-hex>.tmp",
        ".snapshot-index-<32-lowercase-hex>",
        "filter-free descriptor reads",
        "`git hash-object -w --no-filters --stdin`",
        "`git update-index --add --cacheinfo`",
        "worktree attributes and clean filters",
        "new-index SHA",
        "atomically hard-links",
        "native `index.lock`",
        "rechecks the old native-index checksum",
        "atomically compare-and-swaps `HEAD`",
        "atomically replaces the native index",
        "fsyncs the Git directory",
        "exact Enso-created lock inode and checksum",
        "unrelated native lock is preserved",
        "without changing worktree files",
        "old/old",
        "new/old",
        "new/new",
        "divergence fails closed",
        "Never use raw broad Git staging",
        "restore, reset, or delete",
    ):
        assert contract in prose


def test_layout_routes_workspace_lifecycle_through_safe_commands():
    text = _resource_text("enso/layout.md")
    prose = " ".join(text.split())

    for contract in (
        "enso workspace list",
        "enso workspace show <name>",
        "enso workspace create <name> --policy <policy>",
        "--concurrency <n>",
        "defaults to `1`",
        "existing policy",
        "enso workspace repair <name>",
        "five versionable seed entries",
        "does not recreate",
        "service restart",
    ):
        assert contract in prose
    assert "workspace create --path" not in prose


def test_layout_links_the_manual_upgrade_procedure() -> None:
    text = _resource_text("enso/layout.md")

    assert (
        "https://github.com/geekforbrains/enso/blob/main/docs/migrations/"
        "v1.3-managed-workspaces.md"
    ) in text


def test_layout_documents_explicit_policy_lifecycle_and_user_ownership():
    text = _resource_text("enso/layout.md")
    prose = " ".join(text.split())

    for contract in (
        "enso policy list",
        "enso policy show <name>",
        "enso policy create <name> --unrestricted",
        "enso policy create <name> --policy-dir <path>",
        "--provider <provider>",
        "--default-provider <provider>",
        "--chat-command <command>",
        "--all-chat-commands",
        "--env-passthrough <name>",
        "only automatic policy creation",
        "full authority",
        "existing, complete provider-native policy directory",
        "user-owned",
        "enso config check",
        "service restart",
    ):
        assert contract in prose

    assert "policy directory defaults" not in prose
    assert "enso policy check" not in prose
    assert "enso policy repair" not in prose


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


def test_fresh_content_resources_are_declared_as_package_data():
    project = Path(__file__).resolve().parents[1]
    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")

    assert '"prompts/*.md"' in pyproject
    assert '"skills/**/*"' in pyproject
    assert '"starter_docs/**/*.md"' in pyproject


def test_built_wheel_contains_every_bundled_content_resource(tmp_path: Path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to verify the built wheel")

    project = Path(__file__).resolve().parents[1]
    build_source = tmp_path / "source"
    shutil.copytree(
        project / "src",
        build_source / "src",
        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", "*.pyc"),
    )
    shutil.copy2(project / "pyproject.toml", build_source / "pyproject.toml")
    shutil.copy2(project / "README.md", build_source / "README.md")
    subprocess.run(
        [uv, "build", "--wheel", "--quiet", "--out-dir", str(tmp_path)],
        cwd=build_source,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    resource_roots = ("prompts", "skills", "starter_docs")
    source_resources = {
        path.relative_to(project / "src").as_posix()
        for root in resource_roots
        for path in (project / "src" / "enso" / root).rglob("*")
        if path.is_file()
    }
    source_resources.add("enso/slack_manifest.yaml")

    with zipfile.ZipFile(wheel) as archive:
        wheel_resources = {
            name
            for name in archive.namelist()
            if name == "enso/slack_manifest.yaml"
            or any(name.startswith(f"enso/{root}/") for root in resource_roots)
        }

    assert wheel_resources == source_resources
