"""Safety contract for Enso's local Git content-history boundary."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from enso import config
from enso import repository as repository_module
from enso.repository import EnsoRepository, RepositoryError


def _git(
    root: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        timeout=10,
        env=env,
    )


def _init_repo(root: Path, *, branch: str = "main") -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet", f"--initial-branch={branch}")


def _commit_all(root: Path, message: str = "baseline") -> None:
    _git(root, "add", "--all")
    _git(
        root,
        "-c",
        "user.name=Test Author",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        message,
    )


def _tracked(root: Path) -> set[str]:
    output = _git(root, "ls-files", "-z").stdout
    return {os.fsdecode(raw) for raw in output.split(b"\0") if raw}


def test_default_root_is_resolved_from_config_at_instantiation(tmp_path, monkeypatch):
    root = tmp_path / "dynamic-enso"
    monkeypatch.setattr(config, "CONFIG_DIR", str(root))

    repository = EnsoRepository()

    assert repository.root == str(root)


def test_ensure_creates_physical_root_main_repository_and_ignore_first(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    real_run = repository_module.subprocess.run

    def assert_ignore_before_git_init(argv, **kwargs):
        if "init" in argv:
            ignore = root / ".gitignore"
            assert ignore.is_file()
            assert "config.json" in ignore.read_text(encoding="utf-8")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repository_module.subprocess, "run", assert_ignore_before_git_init)

    repository = EnsoRepository(str(root))
    repository.ensure()

    assert root.is_dir()
    assert not root.is_symlink()
    assert _git(root, "rev-parse", "--show-toplevel").stdout.rstrip() == os.fsencode(root)
    assert _git(root, "symbolic-ref", "--short", "HEAD").stdout.rstrip() == b"main"
    assert _git(root, "remote").stdout == b""


@pytest.mark.parametrize(
    "path",
    [
        "config.json",
        "config.json.lock",
        "secrets/transport.env",
        "enso.db",
        "enso.db-wal",
        "state.json",
        "messages.json",
        "messages.json.lock",
        "update.lock",
        "update.json",
        "audits/turn.json",
        "runs/abc.log",
        "cache/slack.json",
        "logs/enso.log",
        "enso.log",
        "docs/customer.db-wal",
        "docs/customer.sqlite-shm",
        "docs/customer.sqlite3-journal",
        "docs/nested/.env",
        "docs/nested/service.env",
        "skills/helper/auth.json",
        "uploads/request.txt",
        "drafts/report.md",
        "policies/client/claude/settings.json",
        "policies/client/.runtime/codex-home/auth.json",
        "jobs/daily/.run.lock",
        "jobs/daily/output/result.json",
        "jobs/daily/tmp/scratch.txt",
        "workspaces/acme/uploads/request.txt",
        "workspaces/acme/drafts/report.md",
    ],
)
def test_managed_ignore_block_covers_protected_content(tmp_path, path):
    root = tmp_path / "enso"
    EnsoRepository(str(root)).ensure()

    result = _git(root, "check-ignore", "--quiet", "--no-index", "--", path, check=False)

    assert result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "skills/docs/SKILL.md",
        "docs/operator.md",
        "docs/enso/layout.md",
        "jobs/daily/JOB.md",
        "jobs/daily/prerun.sh",
        "jobs/daily/prerun.py",
        "workspaces/acme/AGENTS.md",
        "workspaces/acme/CLAUDE.md",
        "workspaces/acme/skills/release/SKILL.md",
        "workspaces/acme/knowledge/decisions.md",
    ],
)
def test_managed_ignore_does_not_cover_versionable_content(tmp_path, path):
    root = tmp_path / "enso"
    EnsoRepository(str(root)).ensure()

    result = _git(root, "check-ignore", "--quiet", "--no-index", "--", path, check=False)

    assert result.returncode == 1


@pytest.mark.parametrize(
    "path",
    [
        "skills/logs/SKILL.md",
        "jobs/logs/JOB.md",
        "workspaces/logs/AGENTS.md",
        "workspaces/logs/skills/uploads/SKILL.md",
    ],
)
def test_managed_ignore_allows_protected_words_in_structural_identifier_slots(tmp_path, path):
    root = tmp_path / "enso"
    EnsoRepository(str(root)).ensure()
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("versionable\n", encoding="utf-8")

    result = _git(root, "check-ignore", "--quiet", "--no-index", "--", path, check=False)

    assert result.returncode == 1


@pytest.mark.parametrize(
    "path",
    [
        "skills/logs/uploads/private.md",
        "jobs/logs/output/result.json",
        "workspaces/logs/knowledge/uploads/private.md",
        "workspaces/logs/skills/uploads/logs/private.md",
    ],
)
def test_structural_identifier_exceptions_do_not_unprotect_nested_runtime_paths(tmp_path, path):
    root = tmp_path / "enso"
    EnsoRepository(str(root)).ensure()

    assert _git(
        root,
        "check-ignore",
        "--quiet",
        "--no-index",
        "--",
        path,
        check=False,
    ).returncode == 0


def test_ensure_repairs_managed_ignore_block_and_preserves_user_rules(tmp_path):
    root = tmp_path / "enso"
    root.mkdir()
    ignore = root / ".gitignore"
    ignore.write_text(
        "custom-cache/\n"
        "# >>> Enso protected paths (managed; do not edit) >>>\n"
        "old-rule\n"
        "# <<< Enso protected paths (managed; do not edit) <<<\n"
        "!config.json\n",
        encoding="utf-8",
    )

    repository = EnsoRepository(str(root))
    repository.ensure()

    content = ignore.read_text(encoding="utf-8")
    assert "custom-cache/" in content
    assert "!config.json" in content
    assert "old-rule" not in content
    assert content.rstrip().endswith("# <<< Enso protected paths (managed; do not edit) <<<")
    assert (
        _git(
            root,
            "check-ignore",
            "--quiet",
            "--no-index",
            "--",
            "config.json",
            check=False,
        ).returncode
        == 0
    )


def test_ensure_appends_managed_ignore_block_to_custom_file(tmp_path):
    root = tmp_path / "enso"
    root.mkdir()
    ignore = root / ".gitignore"
    ignore.write_text("operator-choice/\n", encoding="utf-8")

    EnsoRepository(str(root)).ensure()

    content = ignore.read_text(encoding="utf-8")
    assert content.startswith("operator-choice/\n")
    assert content.count("# >>> Enso protected paths") == 1


def test_managed_ignore_has_no_retired_rules(tmp_path):
    root = tmp_path / "enso"

    EnsoRepository(str(root)).ensure()

    content = (root / ".gitignore").read_text(encoding="utf-8")
    assert ".deleted" not in content
    assert ".snapshot" not in content
    assert ".config.lock" not in content
    assert "update.json.lock" not in content


@pytest.mark.parametrize(
    "content",
    [
        "# >>> Enso protected paths (managed; do not edit) >>>\n",
        "# <<< Enso protected paths (managed; do not edit) <<<\n",
        (
            "# >>> Enso protected paths (managed; do not edit) >>>\n"
            "# >>> Enso protected paths (managed; do not edit) >>>\n"
            "# <<< Enso protected paths (managed; do not edit) <<<\n"
        ),
    ],
)
def test_ensure_rejects_ambiguous_managed_ignore_markers(tmp_path, content):
    root = tmp_path / "enso"
    root.mkdir()
    (root / ".gitignore").write_text(content, encoding="utf-8")

    with pytest.raises(RepositoryError, match="managed block markers"):
        EnsoRepository(str(root)).ensure()

    assert not (root / ".git").exists()


def test_ensure_rejects_gitignore_symlink_without_changing_target(tmp_path):
    root = tmp_path / "enso"
    root.mkdir()
    target = tmp_path / "outside-ignore"
    target.write_text("outside\n", encoding="utf-8")
    (root / ".gitignore").symlink_to(target)

    with pytest.raises(RepositoryError, match=r"\.gitignore.*regular file"):
        EnsoRepository(str(root)).ensure()

    assert target.read_text(encoding="utf-8") == "outside\n"
    assert not (root / ".git").exists()


def test_ensure_is_idempotent_for_a_valid_repository(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    ignore = root / ".gitignore"
    ignore_before = ignore.stat()
    config_before = (root / ".git" / "config").read_bytes()

    repository.ensure()

    ignore_after = ignore.stat()
    assert ignore_after.st_ino == ignore_before.st_ino
    assert ignore_after.st_mtime_ns == ignore_before.st_mtime_ns
    assert (root / ".git" / "config").read_bytes() == config_before


def test_validate_is_read_only_and_requires_managed_ignore_block(tmp_path):
    root = tmp_path / "enso"
    _init_repo(root)
    ignore = root / ".gitignore"
    ignore.write_text("custom/\n", encoding="utf-8")
    before = ignore.read_bytes()

    with pytest.raises(RepositoryError, match=r"protective \.gitignore"):
        EnsoRepository(str(root)).validate()

    assert ignore.read_bytes() == before


def test_validate_accepts_an_exact_repository_after_ensure(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()

    repository.validate()


def test_validate_accepts_gitfile_worktree_with_exact_root(tmp_path):
    source = tmp_path / "source"
    _init_repo(source, branch="source")
    (source / "seed").write_text("seed\n", encoding="utf-8")
    _commit_all(source)
    root = tmp_path / "enso"
    _git(source, "worktree", "add", "--quiet", "-b", "main", str(root))
    assert (root / ".git").is_file()

    repository = EnsoRepository(str(root))
    repository.ensure()
    repository.validate()

    assert _git(root, "rev-parse", "--show-toplevel").stdout.rstrip() == os.fsencode(root)


def test_ensure_rejects_symlinked_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "enso"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(RepositoryError, match="physical directory"):
        EnsoRepository(str(root)).ensure()

    assert not (target / ".gitignore").exists()


def test_ensure_rejects_symlinked_git_entry(tmp_path):
    other = tmp_path / "other"
    _init_repo(other)
    root = tmp_path / "enso"
    root.mkdir()
    (root / ".git").symlink_to(other / ".git", target_is_directory=True)

    with pytest.raises(RepositoryError, match=r"\.git.*directory or regular gitfile"):
        EnsoRepository(str(root)).ensure()


@pytest.mark.parametrize("git_entry", ["directory", "file"])
def test_ensure_rejects_corrupt_repository(tmp_path, git_entry):
    root = tmp_path / "enso"
    root.mkdir()
    git = root / ".git"
    if git_entry == "directory":
        git.mkdir()
    else:
        git.write_text("gitdir: nowhere\n", encoding="utf-8")

    with pytest.raises(RepositoryError, match="valid Git repository"):
        EnsoRepository(str(root)).ensure()


def test_ensure_rejects_outer_repository_instead_of_nesting(tmp_path):
    outer = tmp_path / "outer"
    _init_repo(outer)
    root = outer / "enso"
    root.mkdir()

    with pytest.raises(RepositoryError, match="outer Git repository"):
        EnsoRepository(str(root)).ensure()

    assert not (root / ".git").exists()
    assert not (root / ".gitignore").exists()


def test_ensure_rejects_corrupt_outer_git_entry_instead_of_nesting(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / ".git").write_text("not a gitfile\n", encoding="utf-8")
    root = outer / "enso"
    root.mkdir()

    with pytest.raises(RepositoryError, match="outer Git repository"):
        EnsoRepository(str(root)).ensure()

    assert not (root / ".git").exists()
    assert not (root / ".gitignore").exists()


def test_ensure_rejects_gitfile_whose_worktree_is_not_exact_root(tmp_path):
    outer = tmp_path / "outer"
    _init_repo(outer)
    root = outer / "enso"
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {outer / '.git'}\n", encoding="utf-8")

    with pytest.raises(RepositoryError, match="exact worktree root"):
        EnsoRepository(str(root)).ensure()


def test_existing_remote_is_unchanged_and_never_invoked(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    _init_repo(root)
    _git(root, "config", "user.name", "Existing User")
    _git(root, "config", "user.email", "existing@example.invalid")
    marker = tmp_path / "remote-contacted"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helper = bin_dir / "git-remote-tripwire"
    helper.write_text(
        f"#!/bin/sh\nprintf contacted > {shlex.quote(str(marker))}\nexit 99\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    remote_url = "tripwire::do-not-contact"
    _git(root, "remote", "add", "origin", remote_url)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    assert repository.commit_all("local baseline") is True

    assert _git(root, "remote", "get-url", "origin").stdout.rstrip() == remote_url.encode()
    assert not marker.exists()


def test_has_head_is_false_for_unborn_history_and_true_after_a_commit(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()

    assert repository.has_head() is False

    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    assert repository.commit_all("baseline") is True
    assert repository.has_head() is True


def test_commit_all_excludes_protected_paths_through_managed_ignore(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "operator.md").write_text("operator\n", encoding="utf-8")
    (root / "config.json").write_text('{"token": "secret"}\n', encoding="utf-8")
    (root / "enso.db").write_text("database\n", encoding="utf-8")
    (root / "secrets").mkdir()
    (root / "secrets" / "transport.env").write_text("TOKEN=x\n", encoding="utf-8")
    workspace = root / "workspaces" / "acme"
    (workspace / "knowledge").mkdir(parents=True)
    (workspace / "uploads").mkdir()
    (workspace / "AGENTS.md").write_text("workspace\n", encoding="utf-8")
    (workspace / "knowledge" / "brief.md").write_text("brief\n", encoding="utf-8")
    (workspace / "uploads" / "inbound.txt").write_text("attachment\n", encoding="utf-8")

    assert repository.commit_all("Initialize Enso content") is True

    tracked = _tracked(root)
    assert {
        ".gitignore",
        "AGENTS.md",
        "docs/operator.md",
        "workspaces/acme/AGENTS.md",
        "workspaces/acme/knowledge/brief.md",
    } <= tracked
    assert not {
        "config.json",
        "enso.db",
        "secrets/transport.env",
        "workspaces/acme/uploads/inbound.txt",
    } & tracked
    assert _git(root, "log", "-1", "--format=%s").stdout.rstrip() == b"Initialize Enso content"


def test_commit_all_preserves_symlinks_and_returns_false_when_clean(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (root / "CLAUDE.md").symlink_to("AGENTS.md")

    assert repository.commit_all("baseline") is True
    assert repository.commit_all("no changes") is False

    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"1"
    mode = _git(root, "ls-files", "-s", "--", "CLAUDE.md").stdout.split()[0]
    assert mode == b"120000"


def test_commit_all_requires_a_meaningful_message(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()

    for message in ("", "   ", "nul\0byte"):
        with pytest.raises(RepositoryError, match="non-empty message"):
            repository.commit_all(message)


def test_commit_all_requires_an_existing_exact_repository(tmp_path):
    root = tmp_path / "enso"
    root.mkdir()

    with pytest.raises(RepositoryError, match=r"missing its required \.git entry"):
        EnsoRepository(str(root)).commit_all("baseline")


def test_tracked_protected_paths_is_empty_for_ordinary_history(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    repository.commit_all("baseline")

    assert repository.tracked_protected_paths() == ()


def test_tracked_protected_paths_reports_force_added_protected_files(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (root / "config.json").write_text('{"token": "secret"}\n', encoding="utf-8")
    (root / "enso.db").write_text("database\n", encoding="utf-8")
    repository.commit_all("baseline")
    _git(root, "add", "--force", "--", "config.json", "enso.db")
    _git(
        root,
        "-c",
        "user.name=Test Author",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "accidental credential commit",
    )

    assert repository.tracked_protected_paths() == ("config.json", "enso.db")


def test_ensure_uses_existing_effective_author_without_local_override(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        "[user]\n\tname = Global User\n\temail = global@example.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    EnsoRepository(str(root)).ensure()

    assert _git(root, "config", "--local", "--get", "user.name", check=False).returncode == 1
    assert _git(root, "config", "--local", "--get", "user.email", check=False).returncode == 1


def test_ensure_sets_repo_local_fallback_identity_without_changing_global(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    global_config = tmp_path / "global.gitconfig"
    original = "[user]\n\tuseConfigOnly = true\n"
    global_config.write_text(original, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("EMAIL", raising=False)

    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    assert repository.commit_all("initial") is True

    assert _git(root, "config", "--local", "--get", "user.name").stdout.rstrip() == (
        b"Enso Local History"
    )
    assert _git(root, "config", "--local", "--get", "user.email").stdout.rstrip() == (
        b"enso@localhost"
    )
    assert _git(root, "log", "-1", "--format=%an <%ae>").stdout.rstrip() == (
        b"Enso Local History <enso@localhost>"
    )
    assert global_config.read_text(encoding="utf-8") == original
