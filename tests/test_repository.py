"""Safety contract for Enso's local content history."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from enso import config
from enso import repository as repository_module
from enso.repository import (
    EnsoRepository,
    PathDisposition,
    RepositoryError,
    classify_content_path,
    protected_tracked_paths,
)


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


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "skills/docs/SKILL.md",
        ".agents/skills",
        ".claude/skills",
        "docs/operator.md",
        "jobs/daily/JOB.md",
        "jobs/daily/prerun.sh",
        "jobs/daily/prerun.py",
        "workspaces/acme/AGENTS.md",
        "workspaces/acme/CLAUDE.md",
        "workspaces/acme/skills/release/SKILL.md",
        "workspaces/acme/.agents/skills",
        "workspaces/acme/.claude/skills",
        "workspaces/acme/knowledge/decisions.md",
    ],
)
def test_versionable_content_matrix(path):
    assert classify_content_path(path) is PathDisposition.VERSIONABLE


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
        "audits/turn.json",
        "runs/abc.log",
        "cache/slack.json",
        "logs/enso.log",
        "enso.log",
        "docs/customer.db-wal",
        "docs/customer.sqlite-shm",
        "docs/customer.sqlite3-journal",
        "uploads/request.txt",
        "drafts/report.md",
        "policies/client/claude/settings.json",
        "policies/client/.runtime/codex-home/auth.json",
        "jobs/daily/.run.lock",
        "jobs/daily/output/result.json",
        "workspaces/acme/uploads/request.txt",
        "workspaces/acme/drafts/report.md",
        "workspaces/acme/.git/config",
    ],
)
def test_protected_content_matrix(path):
    assert classify_content_path(path) is PathDisposition.PROTECTED


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "jobs/daily/result.csv",
        "jobs/daily/scripts/helper.rb",
        "workspaces/acme/random.txt",
        "../outside",
        "/absolute/path",
        "",
    ],
)
def test_unapproved_content_is_not_versionable(path):
    assert classify_content_path(path) is PathDisposition.UNSUPPORTED


def test_tracked_sensitive_paths_block_automatic_snapshots():
    tracked = [
        "AGENTS.md",
        "config.json",
        "workspaces/acme/knowledge/brief.md",
        "workspaces/acme/uploads/request.txt",
        "enso.db-wal",
    ]

    assert protected_tracked_paths(tracked) == (
        "config.json",
        "enso.db-wal",
        "workspaces/acme/uploads/request.txt",
    )


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
        "audits/turn.json",
        "runs/abc.log",
        "cache/slack.json",
        "logs/enso.log",
        "docs/customer.db-wal",
        "docs/customer.sqlite-shm",
        "docs/customer.sqlite3-journal",
        "uploads/request.txt",
        "drafts/report.md",
        "policies/client/claude/settings.json",
        "policies/client/.runtime/codex-home/auth.json",
        "jobs/daily/.run.lock",
        "jobs/daily/output/result.json",
        "workspaces/acme/uploads/request.txt",
        "workspaces/acme/drafts/report.md",
    ],
)
def test_managed_ignore_block_covers_protected_content(tmp_path, path):
    root = tmp_path / "enso"
    EnsoRepository(str(root)).ensure()

    result = _git(root, "check-ignore", "--quiet", "--no-index", "--", path, check=False)

    assert result.returncode == 0


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


def test_managed_ignore_has_no_retired_skill_tombstone_rule(tmp_path):
    root = tmp_path / "enso"

    EnsoRepository(str(root)).ensure()

    assert ".deleted" not in (root / ".gitignore").read_text(encoding="utf-8")


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
    assert repository.snapshot(["AGENTS.md"], "local snapshot") is True

    assert _git(root, "remote", "get-url", "origin").stdout.rstrip() == remote_url.encode()
    assert not marker.exists()


def test_tracked_protected_paths_reads_all_index_paths_with_nul_delimiters(tmp_path):
    root = tmp_path / "enso"
    _init_repo(root)
    protected = [
        "config.json",
        "enso.db-wal",
        "secrets/line\nbreak.env",
        "workspaces/acme/uploads/request.txt",
    ]
    for path in protected:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("sensitive\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("safe\n", encoding="utf-8")
    _git(root, "add", "--force", "--", *protected, "AGENTS.md")
    _commit_all(root)
    repository = EnsoRepository(str(root))
    repository.ensure()

    assert repository.tracked_protected_paths() == tuple(sorted(protected))

    with pytest.raises(RepositoryError, match="protected paths are already tracked"):
        repository.snapshot(["AGENTS.md"], "must be blocked")


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("config.json", "protected"),
        ("README.md", "allowlisted"),
        ("../outside.md", "beneath"),
        ("/outside.md", "beneath"),
    ],
)
def test_snapshot_rejects_non_versionable_or_outside_paths(tmp_path, path, error):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()

    with pytest.raises(RepositoryError, match=error):
        repository.snapshot([path], "unsafe")


def test_snapshot_rejects_symlink_escape(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "docs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RepositoryError, match="symlink escape"):
        repository.snapshot(["docs/secret.md"], "unsafe")


def test_snapshot_rechecks_staged_paths_when_nested_ignore_negates_protection(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    uploads = root / "docs" / "uploads"
    uploads.mkdir(parents=True)
    (root / "docs" / ".gitignore").write_text("!uploads/\n!uploads/token.env\n", encoding="utf-8")
    (uploads / "token.env").write_text("secret\n", encoding="utf-8")

    with pytest.raises(RepositoryError, match="protected paths were staged") as exc_info:
        repository.snapshot(["docs"], "must recheck staged paths")

    assert "cleanup failed" not in str(exc_info.value)
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert (
        _git(root, "ls-files", "--error-unmatch", "docs/uploads/token.env", check=False).returncode
        != 0
    )


def test_snapshot_of_directory_does_not_track_nested_database_sidecars(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    (docs / "notes.md").write_text("safe\n", encoding="utf-8")
    (docs / "customer.db-wal").write_text("database bytes\n", encoding="utf-8")

    assert repository.snapshot(["docs"], "safe docs") is True

    assert _git(root, "ls-files", "-z").stdout.split(b"\0")[:-1] == [b"docs/notes.md"]


def test_snapshot_requires_explicit_paths_and_message(tmp_path):
    repository = EnsoRepository(str(tmp_path / "enso"))
    repository.ensure()

    with pytest.raises(RepositoryError, match="at least one explicit path"):
        repository.snapshot([], "empty")
    with pytest.raises(RepositoryError, match="commit message"):
        repository.snapshot(["AGENTS.md"], "   ")


def test_snapshot_commits_only_explicit_allowlisted_paths(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    requested = docs / "with space.md"
    unrelated = docs / "unrelated.md"
    requested.write_text("requested\n", encoding="utf-8")
    unrelated.write_text("unrelated\n", encoding="utf-8")

    assert repository.snapshot([str(requested)], "docs: requested only") is True

    assert _git(root, "ls-files", "-z").stdout.split(b"\0")[:-1] == [b"docs/with space.md"]
    assert _git(root, "show", "--format=", "--name-only", "-z", "HEAD").stdout == (
        b"docs/with space.md\0"
    )
    assert _git(root, "status", "--short", "--", "docs/unrelated.md").stdout.startswith(b"??")


def test_snapshot_stages_deletion_for_an_explicit_path(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("first\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "add instructions") is True
    agents.unlink()

    assert repository.snapshot(["AGENTS.md"], "remove instructions") is True

    assert _git(root, "ls-files", "--", "AGENTS.md").stdout == b""


def test_snapshot_clean_request_is_successful_noop(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("first\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "first") is True
    head = _git(root, "rev-parse", "HEAD").stdout

    assert repository.snapshot(["AGENTS.md"], "nothing changed") is False

    assert _git(root, "rev-parse", "HEAD").stdout == head


def test_snapshot_refuses_an_existing_staging_area(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "--", "AGENTS.md")
    (root / "docs").mkdir()
    (root / "docs" / "new.md").write_text("new\n", encoding="utf-8")

    with pytest.raises(RepositoryError, match="staging area is not clean"):
        repository.snapshot(["docs/new.md"], "must not merge staging")

    assert _git(root, "diff", "--cached", "--name-only").stdout == b"AGENTS.md\n"


def test_snapshot_cleans_partial_initial_staging_when_git_add_fails(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    real_run_git = repository._run_git
    failed_once = False

    def fail_after_first_add(args, **kwargs):
        nonlocal failed_once
        if "add" in args and not failed_once:
            failed_once = True
            real_run_git(args, **kwargs)
            raise RepositoryError("simulated partial git add failure")
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", fail_after_first_add)

    with pytest.raises(RepositoryError, match="partial git add failure"):
        repository.snapshot(["AGENTS.md"], "interrupted")

    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert repository.snapshot(["AGENTS.md"], "retry") is True


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
    assert repository.snapshot(["AGENTS.md"], "initial") is True

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
