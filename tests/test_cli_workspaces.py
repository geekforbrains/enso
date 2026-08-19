"""Opinionated workspace CLI behavior and failure recovery."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from enso import cli as cli_mod
from enso import repository as repository_mod
from enso.config import save_config
from enso.repository import EnsoRepository, RepositoryError
from enso.scaffolding import ScaffoldService

runner = CliRunner()


def _installed_config(tmp_enso: str) -> dict:
    repository = EnsoRepository()
    repository.ensure()
    scaffold = ScaffoldService()
    scaffold.seed_fresh_global()
    default = scaffold.workspace_path("default")
    if default.exists():
        default.rmdir()
    scaffold.create_workspace("default")
    config = {
        "transport": "",
        "transports": {},
        "providers": {
            "claude": {"path": "claude", "models": ["sonnet"]},
        },
        "workspaces": {
            "default": {"policy": "admin", "concurrency": 1},
        },
        "policies": {
            "admin": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
        },
        "setup": {"completed_at": "2026-01-01T00:00:00+00:00"},
    }
    save_config(config)
    return config


def _flat(result) -> str:
    return " ".join(result.output.split())


def test_workspace_group_exposes_only_the_supported_lifecycle_commands() -> None:
    result = runner.invoke(cli_mod.app, ["workspace", "--help"])

    assert result.exit_code == 0
    for command in ("list", "show", "create", "repair"):
        assert command in result.output
    for command in ("delete", "move", "rename"):
        assert command not in result.output

    create_help = runner.invoke(cli_mod.app, ["workspace", "create", "--help"])
    assert create_help.exit_code == 0
    assert "--policy" in create_help.output
    assert "--concurrency" in create_help.output
    assert "--path" not in create_help.output


def test_workspace_list_and_show_are_strict_read_only_inspections(tmp_enso: str) -> None:
    _installed_config(tmp_enso)
    config_file = Path(tmp_enso, "config.json")
    original = config_file.read_bytes()
    lock = Path(f"{config_file}.lock")

    listing = runner.invoke(cli_mod.app, ["workspace", "list"])
    detail = runner.invoke(cli_mod.app, ["workspace", "show", "default"])

    assert listing.exit_code == 0, listing.output
    assert "default" in listing.output
    assert "admin" in listing.output
    assert "workspaces/default" in "".join(listing.output.split())
    assert detail.exit_code == 0, detail.output
    flattened = _flat(detail)
    assert "Policy: admin" in flattened
    assert "Concurrency: 1" in flattened
    assert f"Path:{Path(tmp_enso, 'workspaces', 'default')}" in "".join(
        detail.output.split()
    )
    assert "Validation: valid" in flattened
    assert config_file.read_bytes() == original
    assert not lock.exists()


@pytest.mark.parametrize("content", [None, "{broken", "[]"])
def test_workspace_inspection_rejects_missing_or_malformed_config_without_writes(
    tmp_enso: str,
    content: str | None,
) -> None:
    config_file = Path(tmp_enso, "config.json")
    if content is not None:
        config_file.write_text(content, encoding="utf-8")
        original = config_file.read_bytes()

    result = runner.invoke(cli_mod.app, ["workspace", "list"])

    assert result.exit_code == 1
    assert "Configuration error" in result.output
    if content is None:
        assert not config_file.exists()
    else:
        assert config_file.read_bytes() == original
    assert not Path(f"{config_file}.lock").exists()


def test_workspace_show_reports_legacy_path_without_mutating_config(tmp_enso: str) -> None:
    config = _installed_config(tmp_enso)
    config["workspaces"]["default"]["path"] = "/legacy/default"
    save_config(config)
    config_file = Path(tmp_enso, "config.json")
    original = config_file.read_bytes()

    result = runner.invoke(cli_mod.app, ["workspace", "show", "default"])

    assert result.exit_code == 1
    assert "path is no longer supported" in _flat(result)
    assert "v1.3-managed-workspaces.md" in result.output
    assert config_file.read_bytes() == original
    assert not Path(f"{config_file}.lock").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["workspace", "create", "client", "--policy", "admin"],
        ["workspace", "repair", "default"],
    ],
)
def test_workspace_mutations_reject_missing_config_before_creating_a_lock(
    tmp_enso: str,
    args: list[str],
) -> None:
    config_file = Path(tmp_enso, "config.json")

    result = runner.invoke(cli_mod.app, args)

    assert result.exit_code == 1
    assert "config.json is missing" in _flat(result)
    assert not config_file.exists()
    assert not Path(f"{config_file}.lock").exists()


def test_workspace_create_persists_exact_schema_and_snapshots_only_scaffold(
    tmp_enso: str,
) -> None:
    _installed_config(tmp_enso)
    unrelated = Path(tmp_enso, "docs", "unrelated.md")
    unrelated.write_text("leave me untracked\n", encoding="utf-8")

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client-ops", "--policy", "admin"],
    )

    assert result.exit_code == 0, result.output
    flattened = _flat(result)
    workspace = Path(tmp_enso, "workspaces", "client-ops")
    assert "Workspace created: client-ops" in flattened
    assert "Path:" in flattened
    assert "workspaces/client-ops" in flattened.replace(" ", "")
    assert "Policy: admin" in flattened
    assert "Concurrency: 1" in flattened
    assert "Snapshot created" in flattened
    assert "restart" in flattened.lower()
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert persisted["workspaces"]["client-ops"] == {
        "policy": "admin",
        "concurrency": 1,
    }
    assert "path" not in persisted["workspaces"]["client-ops"]
    assert (workspace / "AGENTS.md").is_file()
    assert (workspace / "knowledge" / "README.md").is_file()
    assert os.readlink(workspace / "CLAUDE.md") == "AGENTS.md"
    assert EnsoRepository().commit_subject_paths("Create workspace client-ops") == (
        "workspaces/client-ops/.agents/skills",
        "workspaces/client-ops/.claude/skills",
        "workspaces/client-ops/AGENTS.md",
        "workspaces/client-ops/CLAUDE.md",
        "workspaces/client-ops/knowledge/README.md",
    )
    assert "docs/unrelated.md" not in EnsoRepository().tracked_paths()
    assert "config.json" not in EnsoRepository().tracked_paths()


def test_workspace_create_accepts_explicit_positive_concurrency(tmp_enso: str) -> None:
    _installed_config(tmp_enso)

    result = runner.invoke(
        cli_mod.app,
        [
            "workspace",
            "create",
            "client",
            "--policy",
            "admin",
            "--concurrency",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert persisted["workspaces"]["client"] == {
        "policy": "admin",
        "concurrency": 3,
    }


@pytest.mark.parametrize("name", ["Client", "two words", "two_words", "client--ops"])
def test_workspace_create_rejects_non_kebab_names_before_publication(
    tmp_enso: str,
    name: str,
) -> None:
    _installed_config(tmp_enso)

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", name, "--policy", "admin"],
    )

    assert result.exit_code == 1
    assert "lowercase kebab-case" in _flat(result)
    assert not Path(tmp_enso, "workspaces", name).exists()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ([], "--policy"),
        (["--policy", "missing"], "unknown policy"),
        (["--policy", "admin", "--concurrency", "0"], "positive integer"),
    ],
)
def test_workspace_create_requires_a_valid_explicit_binding(
    tmp_enso: str,
    extra: list[str],
    message: str,
) -> None:
    _installed_config(tmp_enso)

    result = runner.invoke(cli_mod.app, ["workspace", "create", "client", *extra])

    assert result.exit_code != 0
    assert message in _flat(result)
    assert not Path(tmp_enso, "workspaces", "client").exists()


def test_workspace_create_refuses_existing_config_or_destination(tmp_enso: str) -> None:
    config = _installed_config(tmp_enso)
    duplicate = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "default", "--policy", "admin"],
    )
    assert duplicate.exit_code == 1
    assert "already configured" in _flat(duplicate)

    destination = Path(tmp_enso, "workspaces", "orphan")
    destination.mkdir()
    original = Path(tmp_enso, "config.json").read_bytes()
    orphan = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "orphan", "--policy", "admin"],
    )
    assert orphan.exit_code == 1
    assert "destination already exists" in _flat(orphan)
    assert Path(tmp_enso, "config.json").read_bytes() == original
    assert "orphan" not in config["workspaces"]


def test_workspace_create_rejects_a_symlinked_container_without_writing_outside(
    tmp_enso: str,
    tmp_path: Path,
) -> None:
    _installed_config(tmp_enso)
    container = Path(tmp_enso, "workspaces")
    detached = Path(tmp_enso, "detached-workspaces")
    container.rename(detached)
    outside = tmp_path / "outside"
    outside.mkdir()
    container.symlink_to(outside, target_is_directory=True)
    original = Path(tmp_enso, "config.json").read_bytes()

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client", "--policy", "admin"],
    )

    assert result.exit_code == 1
    assert "physical directory" in _flat(result)
    assert list(outside.iterdir()) == []
    assert Path(tmp_enso, "config.json").read_bytes() == original


def test_workspace_create_leaves_published_directory_when_atomic_save_fails(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _installed_config(tmp_enso)
    config_file = Path(tmp_enso, "config.json")
    original = config_file.read_bytes()

    def fail_save(_config: dict) -> None:
        raise OSError("simulated save failure")

    monkeypatch.setattr(cli_mod, "save_config", fail_save)

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client", "--policy", "admin"],
    )

    assert result.exit_code == 1
    workspace = Path(tmp_enso, "workspaces", "client")
    flattened = _flat(result)
    assert "unused workspace directory" in flattened
    assert "workspaces/client" in flattened.replace(" ", "")
    assert "simulated save failure" in flattened
    assert workspace.is_dir()
    assert config_file.read_bytes() == original
    assert EnsoRepository().commit_subject_paths("Create workspace client") is None


def test_workspace_create_leaves_unconfigured_directory_when_scaffold_validation_fails(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _installed_config(tmp_enso)
    config_file = Path(tmp_enso, "config.json")
    original = config_file.read_bytes()
    real_create = ScaffoldService.create_workspace

    def create_invalid_scaffold(self, name):
        report = real_create(self, name)
        assert report.workspace is not None
        Path(report.workspace, "AGENTS.md").unlink()
        return report

    monkeypatch.setattr(ScaffoldService, "create_workspace", create_invalid_scaffold)

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client", "--policy", "admin"],
    )

    assert result.exit_code == 1
    assert "published workspace did not validate" in _flat(result)
    assert "unused workspace directory" in _flat(result)
    assert Path(tmp_enso, "workspaces", "client").is_dir()
    assert config_file.read_bytes() == original


def test_workspace_create_preserves_config_and_content_when_post_check_fails(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _installed_config(tmp_enso)

    def fail_check() -> None:
        raise typer.Exit(1)

    monkeypatch.setattr(cli_mod, "config_check", fail_check)

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client", "--policy", "admin"],
    )

    assert result.exit_code == 1
    assert Path(tmp_enso, "workspaces", "client").is_dir()
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert persisted["workspaces"]["client"]["policy"] == "admin"
    flattened = _flat(result)
    assert "configuration and workspace were preserved" in flattened
    assert "enso config check" in flattened
    assert EnsoRepository().commit_subject_paths("Create workspace client") is None


def test_workspace_create_preserves_config_and_content_when_snapshot_fails(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _installed_config(tmp_enso)

    def fail_snapshot(self, paths, message, *, caller_cwd=None):
        raise RepositoryError("simulated snapshot failure")

    monkeypatch.setattr(repository_mod.EnsoRepository, "snapshot", fail_snapshot)

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client", "--policy", "admin"],
    )

    assert result.exit_code == 1
    assert Path(tmp_enso, "workspaces", "client").is_dir()
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert persisted["workspaces"]["client"]["policy"] == "admin"
    flattened = _flat(result)
    assert "simulated snapshot failure" in flattened
    assert "enso snapshot create" in flattened
    assert "configuration and workspace were preserved" in flattened


def test_workspace_create_reports_an_incomplete_snapshot_if_seed_content_disappears(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _installed_config(tmp_enso)
    workspace = Path(tmp_enso, "workspaces", "client")

    def remove_seed_after_validation() -> None:
        (workspace / "knowledge" / "README.md").unlink()

    monkeypatch.setattr(cli_mod, "config_check", remove_seed_after_validation)

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client", "--policy", "admin"],
    )

    assert result.exit_code == 1
    flattened = _flat(result)
    assert "missing required scaffold entries" in flattened
    assert "workspaces/client/knowledge/README.md" in flattened
    assert "configuration and workspace were preserved" in flattened
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert persisted["workspaces"]["client"]["policy"] == "admin"
    assert EnsoRepository().commit_subject_paths("Create workspace client") == (
        "workspaces/client/.agents/skills",
        "workspaces/client/.claude/skills",
        "workspaces/client/AGENTS.md",
        "workspaces/client/CLAUDE.md",
    )


def test_workspace_repair_restores_structure_but_not_missing_seeded_content(
    tmp_enso: str,
) -> None:
    _installed_config(tmp_enso)
    workspace = Path(tmp_enso, "workspaces", "default")
    (workspace / ".agents" / "skills").unlink()
    (workspace / ".claude" / "skills").unlink()
    (workspace / "knowledge" / "README.md").unlink()

    result = runner.invoke(cli_mod.app, ["workspace", "repair", "default"])

    assert result.exit_code == 0, result.output
    assert os.readlink(workspace / ".agents" / "skills") == "../skills"
    assert os.readlink(workspace / ".claude" / "skills") == "../skills"
    assert not (workspace / "knowledge" / "README.md").exists()
    flattened = _flat(result)
    assert "Created" in flattened
    assert "preserving it without recreation" in flattened


def test_workspace_repair_reports_missing_agents_as_launch_blocking(tmp_enso: str) -> None:
    _installed_config(tmp_enso)
    agents = Path(tmp_enso, "workspaces", "default", "AGENTS.md")
    agents.unlink()

    result = runner.invoke(cli_mod.app, ["workspace", "repair", "default"])

    assert result.exit_code == 1
    assert not agents.exists()
    assert "AGENTS.md" in _flat(result)
    assert "not recreated" in _flat(result)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_workspace_repair_rejects_every_direct_git_entry(
    tmp_enso: str,
    tmp_path: Path,
    kind: str,
) -> None:
    _installed_config(tmp_enso)
    git_entry = Path(tmp_enso, "workspaces", "default", ".git")
    if kind == "directory":
        git_entry.mkdir()
    elif kind == "file":
        git_entry.write_text("gitdir: elsewhere\n", encoding="utf-8")
    else:
        outside = tmp_path / "outside-git"
        outside.mkdir()
        git_entry.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(cli_mod.app, ["workspace", "repair", "default"])

    assert result.exit_code == 1
    assert ".git entry" in _flat(result)


def test_workspace_repair_allows_a_deeper_repository(tmp_enso: str) -> None:
    _installed_config(tmp_enso)
    nested_git = Path(tmp_enso, "workspaces", "default", "drafts", "project", ".git")
    nested_git.mkdir(parents=True)

    result = runner.invoke(cli_mod.app, ["workspace", "repair", "default"])

    assert result.exit_code == 0, result.output
    assert nested_git.is_dir()


def test_workspace_repair_requires_a_configured_workspace(tmp_enso: str) -> None:
    _installed_config(tmp_enso)

    result = runner.invoke(cli_mod.app, ["workspace", "repair", "missing"])

    assert result.exit_code == 1
    assert "not configured" in _flat(result)
    assert not Path(tmp_enso, "workspaces", "missing").exists()


def test_workspace_create_uses_the_strict_transaction_order_and_exact_snapshot_paths(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _installed_config(tmp_enso)
    from enso import scaffolding as scaffolding_mod
    from enso import teams as teams_mod

    events: list[object] = []
    real_load = cli_mod._load_config_or_exit
    real_lock = cli_mod._config_lock_or_exit
    real_catalog = teams_mod.load_catalog
    real_create = scaffolding_mod.ScaffoldService.create_workspace
    real_save = cli_mod.save_config

    def recording_load(*args, **kwargs):
        events.append("strict-read")
        return real_load(*args, **kwargs)

    @contextmanager
    def recording_lock():
        events.append("lock")
        with real_lock():
            yield
        events.append("unlock")

    def recording_catalog(config):
        if "client" in config.get("workspaces", {}):
            events.append("candidate-catalog")
        return real_catalog(config)

    def recording_create(self, name):
        events.append("scaffold")
        return real_create(self, name)

    def recording_save(config):
        events.append("save")
        return real_save(config)

    def recording_check():
        events.append("config-check")

    def recording_snapshot(self, paths, message, *, caller_cwd=None):
        events.append(("snapshot", tuple(paths), message, caller_cwd))
        return True

    def recording_commit_subject_paths(self, subject):
        events.append("verify-snapshot")
        assert subject == "Create workspace client"
        return cli_mod._workspace_snapshot_paths("client")

    monkeypatch.setattr(cli_mod, "_load_config_or_exit", recording_load)
    monkeypatch.setattr(cli_mod, "_config_lock_or_exit", recording_lock)
    monkeypatch.setattr(teams_mod, "load_catalog", recording_catalog)
    monkeypatch.setattr(scaffolding_mod.ScaffoldService, "create_workspace", recording_create)
    monkeypatch.setattr(cli_mod, "save_config", recording_save)
    monkeypatch.setattr(cli_mod, "config_check", recording_check)
    monkeypatch.setattr(repository_mod.EnsoRepository, "snapshot", recording_snapshot)
    monkeypatch.setattr(
        repository_mod.EnsoRepository,
        "commit_subject_paths",
        recording_commit_subject_paths,
    )

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client", "--policy", "admin"],
    )

    assert result.exit_code == 0, result.output
    assert events[0] == "strict-read"
    assert events.index("lock") < events.index("strict-read", 1)
    assert events.index("strict-read", 1) < events.index("candidate-catalog")
    assert events.index("candidate-catalog") < events.index("scaffold")
    assert events.index("scaffold") < events.index("save")
    assert events.index("save") < events.index("config-check")
    snapshot = next(item for item in events if isinstance(item, tuple))
    assert (
        events.index("config-check")
        < events.index(snapshot)
        < events.index("verify-snapshot")
        < events.index("unlock")
    )
    assert snapshot == (
        "snapshot",
        (
            "workspaces/client/AGENTS.md",
            "workspaces/client/CLAUDE.md",
            "workspaces/client/.agents/skills",
            "workspaces/client/.claude/skills",
            "workspaces/client/knowledge/README.md",
        ),
        "Create workspace client",
        None,
    )


@pytest.mark.parametrize("problem", ["legacy", "other-catalog", "malformed"])
def test_workspace_create_rejects_existing_config_errors_before_lock_or_scaffold(
    tmp_enso: str,
    monkeypatch,
    problem: str,
) -> None:
    config_file = Path(tmp_enso, "config.json")
    if problem == "malformed":
        config_file.write_text("{broken", encoding="utf-8")
    else:
        config = _installed_config(tmp_enso)
        if problem == "legacy":
            config["workspaces"]["default"]["path"] = "/legacy/default"
        else:
            config["workspaces"]["default"]["unexpected"] = True
        save_config(config)
    published = False

    def unexpected_create(self, name):
        nonlocal published
        published = True
        pytest.fail("invalid current config must not publish a workspace")

    monkeypatch.setattr(ScaffoldService, "create_workspace", unexpected_create)

    result = runner.invoke(
        cli_mod.app,
        ["workspace", "create", "client", "--policy", "admin"],
    )

    assert result.exit_code == 1
    assert not Path(f"{config_file}.lock").exists()
    assert published is False
    assert not Path(tmp_enso, "workspaces", "client").exists()


def test_workspace_repair_rejects_a_symlinked_workspace_root_without_outside_writes(
    tmp_enso: str,
    tmp_path: Path,
) -> None:
    _installed_config(tmp_enso)
    workspace = Path(tmp_enso, "workspaces", "default")
    detached = Path(tmp_enso, "workspaces", "detached-default")
    workspace.rename(detached)
    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    workspace.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(cli_mod.app, ["workspace", "repair", "default"])

    assert result.exit_code == 1
    assert "physical directory" in _flat(result)
    assert list(outside.iterdir()) == []


def test_concurrent_workspace_creates_serialize_for_same_and_different_names(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _installed_config(tmp_enso)
    real_load = cli_mod._load_config_or_exit

    monkeypatch.setattr(cli_mod, "config_check", lambda: None)

    def run_pair(names: tuple[str, str]) -> list[int]:
        barrier = threading.Barrier(2)
        local = threading.local()

        def synchronized_load(*args, **kwargs):
            config = real_load(*args, **kwargs)
            if not getattr(local, "passed_preflight", False):
                local.passed_preflight = True
                barrier.wait(timeout=5)
            return config

        monkeypatch.setattr(cli_mod, "_load_config_or_exit", synchronized_load)

        def create(name: str) -> int:
            try:
                cli_mod.workspace_create(name, "admin", 1)
            except typer.Exit as exc:
                return exc.exit_code
            return 0

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(create, names))

    assert sorted(run_pair(("same", "same"))) == [0, 1]
    assert sorted(run_pair(("alpha", "beta"))) == [0, 0]

    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert {"same", "alpha", "beta"} <= set(persisted["workspaces"])
    assert Path(tmp_enso, "workspaces", "same").is_dir()
    assert Path(tmp_enso, "workspaces", "alpha").is_dir()
    assert Path(tmp_enso, "workspaces", "beta").is_dir()
