"""Safety contract for Enso's local content history."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
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


def _snapshot_temp_indexes(root: Path) -> tuple[Path, ...]:
    return tuple((root / ".git").glob(".snapshot-index-*"))


def _run_crashing_snapshot(
    root: Path,
    *,
    condition: str,
    paths: tuple[str, ...] = ("AGENTS.md",),
    message: str = "crashed snapshot",
    umask: int = -1,
) -> subprocess.CompletedProcess[bytes]:
    script = "\n".join(
        (
            "import os",
            "import signal",
            "from enso.repository import EnsoRepository",
            f"repository = EnsoRepository({str(root)!r})",
            "real_run_git = repository._run_git",
            "def crash_at_transaction_boundary(args, **kwargs):",
            "    result = real_run_git(args, **kwargs)",
            f"    if {condition}:",
            "        os.kill(os.getpid(), signal.SIGKILL)",
            "    return result",
            "repository._run_git = crash_at_transaction_boundary",
            f"repository.snapshot({list(paths)!r}, {message!r})",
        )
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=10,
        umask=umask,
    )


def _run_crashing_snapshot_after_native_index_install(
    root: Path,
) -> subprocess.CompletedProcess[bytes]:
    script = "\n".join(
        (
            "import os",
            "import signal",
            "from enso.repository import EnsoRepository",
            f"repository = EnsoRepository({str(root)!r})",
            "real_install = repository._install_transaction_index",
            "def install_then_crash(transaction):",
            "    real_install(transaction)",
            "    os.kill(os.getpid(), signal.SIGKILL)",
            "repository._install_transaction_index = install_then_crash",
            "repository.snapshot(['AGENTS.md'], 'crashed snapshot')",
        )
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=10,
    )


def _run_crashing_snapshot_after_native_index_lock(
    root: Path,
) -> subprocess.CompletedProcess[bytes]:
    script = "\n".join(
        (
            "import os",
            "import signal",
            "from enso.repository import EnsoRepository",
            f"repository = EnsoRepository({str(root)!r})",
            "real_acquire = repository._acquire_native_index_lock",
            "def acquire_then_crash(transaction, **kwargs):",
            "    real_acquire(transaction, **kwargs)",
            "    os.kill(os.getpid(), signal.SIGKILL)",
            "repository._acquire_native_index_lock = acquire_then_crash",
            "repository.snapshot(['AGENTS.md'], 'crashed snapshot')",
        )
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    "path",
    [
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "skills/docs/SKILL.md",
        "skills/logs/SKILL.md",
        ".agents/skills",
        ".claude/skills",
        "docs/operator.md",
        "jobs/daily/JOB.md",
        "jobs/daily/prerun.sh",
        "jobs/daily/prerun.py",
        "jobs/logs/JOB.md",
        "workspaces/acme/AGENTS.md",
        "workspaces/acme/CLAUDE.md",
        "workspaces/acme/skills/release/SKILL.md",
        "workspaces/acme/.agents/skills",
        "workspaces/acme/.claude/skills",
        "workspaces/acme/knowledge/decisions.md",
        "workspaces/logs/AGENTS.md",
        "workspaces/logs/skills/uploads/SKILL.md",
    ],
)
def test_versionable_content_matrix(path):
    assert classify_content_path(path) is PathDisposition.VERSIONABLE


@pytest.mark.parametrize(
    "path",
    [
        "config.json",
        ".snapshot.lock",
        ".snapshot.transaction.json",
        ".snapshot-transaction-0123456789abcdef0123456789abcdef.tmp",
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
        ".snapshot.lock",
        ".snapshot.transaction.json",
        ".snapshot-transaction-0123456789abcdef0123456789abcdef.tmp",
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

    assert classify_content_path(path) is PathDisposition.PROTECTED
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


@pytest.mark.parametrize(
    "configuration_scope",
    ["partial-clone-extension", "local-promisor", "worktree-promisor"],
)
def test_snapshot_rejects_effective_partial_clone_and_promisor_configuration(
    tmp_path,
    configuration_scope,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    _git(root, "remote", "add", "origin", "tripwire::must-not-contact")
    if configuration_scope == "partial-clone-extension":
        _git(root, "config", "extensions.partialClone", "origin")
    elif configuration_scope == "local-promisor":
        _git(root, "config", "remote.origin.promisor", "true")
    else:
        _git(root, "config", "extensions.worktreeConfig", "true")
        _git(root, "config", "--worktree", "remote.origin.promisor", "true")
    (root / "AGENTS.md").write_text("local bytes\n", encoding="utf-8")

    with pytest.raises(RepositoryError, match="partial-clone/promisor"):
        repository.snapshot(["AGENTS.md"], "must remain local")

    assert _git(root, "remote", "get-url", "origin").stdout.rstrip() == (
        b"tripwire::must-not-contact"
    )
    assert not (root / ".snapshot.lock").exists()
    assert not (root / ".snapshot.transaction.json").exists()
    assert _snapshot_temp_indexes(root) == ()


def test_snapshot_disables_reference_transaction_hooks(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    hook_marker = tmp_path / "reference-hook-ran"
    hook = root / ".git" / "hooks" / "reference-transaction"
    hook.parent.mkdir(parents=True)
    hook.write_text(
        f"#!/bin/sh\n: > {shlex.quote(str(hook_marker))}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    (root / "AGENTS.md").write_text("local only\n", encoding="utf-8")

    assert repository.snapshot(["AGENTS.md"], "no hooks") is True

    assert not hook_marker.exists()
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"local only\n"


def test_snapshot_disables_worktree_clean_filters(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    filter_marker = tmp_path / "clean-filter-ran"
    clean_filter = tmp_path / "clean-filter.sh"
    clean_filter.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                f": > {shlex.quote(str(filter_marker))}",
                "cat",
                "",
            )
        ),
        encoding="utf-8",
    )
    clean_filter.chmod(0o755)
    _git(root, "config", "filter.tripwire.clean", str(clean_filter))
    docs = root / "docs"
    docs.mkdir()
    (docs / ".gitattributes").write_text("*.md filter=tripwire\n", encoding="utf-8")
    (docs / "safe.md").write_text("must not reach a filter\n", encoding="utf-8")

    assert repository.snapshot(["docs/safe.md"], "filter-free snapshot") is True

    assert not filter_marker.exists()
    assert _git(root, "show", "HEAD:docs/safe.md").stdout == b"must not reach a filter\n"


def test_snapshot_disables_info_attributes_clean_filters(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    filter_marker = tmp_path / "info-clean-filter-ran"
    clean_filter = tmp_path / "info-clean-filter.sh"
    clean_filter.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                f": > {shlex.quote(str(filter_marker))}",
                "cat",
                "",
            )
        ),
        encoding="utf-8",
    )
    clean_filter.chmod(0o755)
    _git(root, "config", "filter.tripwire.clean", str(clean_filter))
    info_attributes = root / ".git" / "info" / "attributes"
    info_attributes.parent.mkdir(exist_ok=True)
    info_attributes.write_text("docs/*.md filter=tripwire\n", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    (docs / "safe.md").write_text("must bypass info attributes\n", encoding="utf-8")

    assert repository.snapshot(["docs/safe.md"], "info-filter-free snapshot") is True

    assert not filter_marker.exists()
    assert _git(root, "show", "HEAD:docs/safe.md").stdout == (
        b"must bypass info attributes\n"
    )


def test_snapshot_disables_configured_fsmonitor_hook(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    fsmonitor_marker = tmp_path / "fsmonitor-ran"
    fsmonitor = tmp_path / "fsmonitor.sh"
    fsmonitor.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                f": > {shlex.quote(str(fsmonitor_marker))}",
                "printf '0\\n'",
                "",
            )
        ),
        encoding="utf-8",
    )
    fsmonitor.chmod(0o755)
    _git(root, "config", "core.fsmonitor", str(fsmonitor))
    (root / "AGENTS.md").write_text("no fsmonitor\n", encoding="utf-8")

    assert repository.snapshot(["AGENTS.md"], "fsmonitor-free snapshot") is True

    assert not fsmonitor_marker.exists()


def test_snapshot_installs_a_complete_index_when_split_index_is_configured(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    _git(root, "config", "core.splitIndex", "true")
    (root / "AGENTS.md").write_text("complete index\n", encoding="utf-8")

    assert repository.snapshot(["AGENTS.md"], "full alternate index") is True

    assert _git(
        root,
        "-c",
        "core.splitIndex=false",
        "rev-parse",
        "--shared-index-path",
    ).stdout.rstrip() == b""
    assert tuple((root / ".git").glob("sharedindex.*")) == ()
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"complete index\n"
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""


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


def test_snapshot_reaudits_full_temporary_index_after_external_head_change(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("safe baseline\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "safe baseline") is True
    safe_head = _git(root, "rev-parse", "HEAD").stdout.rstrip()
    protected = root / "config.json"
    protected.write_text('{"secret":"must not enter snapshot"}\n', encoding="utf-8")
    _git(root, "add", "--force", "--", "config.json")
    _git(
        root,
        "-c",
        "user.name=External Writer",
        "-c",
        "user.email=external@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "external protected commit",
    )
    protected_head = _git(root, "rev-parse", "HEAD").stdout.rstrip()
    _git(root, "reset", "--hard", safe_head.decode("ascii"))
    agents.write_text("requested change\n", encoding="utf-8")
    real_run_git = repository._run_git
    moved_head = False

    def move_head_after_tracked_path_audit(args, **kwargs):
        nonlocal moved_head
        result = real_run_git(args, **kwargs)
        if (
            args
            and args[0] == "ls-files"
            and not moved_head
        ):
            moved_head = True
            _git(root, "reset", "--hard", protected_head.decode("ascii"))
        return result

    monkeypatch.setattr(repository, "_run_git", move_head_after_tracked_path_audit)

    with pytest.raises(RepositoryError, match="protected paths were staged"):
        repository.snapshot(["AGENTS.md"], "must not adopt protected history")

    assert moved_head is True
    assert _git(root, "rev-parse", "HEAD").stdout.rstrip() == protected_head
    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"2"
    assert _git(root, "log", "-1", "--format=%s").stdout.rstrip() == (
        b"external protected commit"
    )


def test_has_head_is_false_for_unborn_history_and_true_after_a_commit(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()

    assert repository.has_head() is False

    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True

    assert repository.has_head() is True


def test_has_head_rejects_a_detached_missing_object(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / ".git" / "HEAD").write_text("0" * 40 + "\n", encoding="ascii")

    with pytest.raises(RepositoryError, match="HEAD"):
        repository.has_head()


def test_has_head_rejects_a_symbolic_branch_ref_with_a_missing_object(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / ".git" / "refs" / "heads" / "main").write_text(
        "0" * 40 + "\n",
        encoding="ascii",
    )

    with pytest.raises(RepositoryError, match="HEAD"):
        repository.has_head()


def test_commit_subject_lookup_is_exact_and_limited_to_head_ancestry(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    marker = "Initialize Enso content"
    agents = root / "AGENTS.md"
    agents.write_text("baseline\n", encoding="utf-8")
    assert repository.snapshot([".gitignore", "AGENTS.md"], f"{marker} copy") is True

    _git(root, "switch", "--quiet", "-c", "side")
    agents.write_text("side\n", encoding="utf-8")
    _commit_all(root, marker)
    _git(root, "switch", "--quiet", "main")
    agents.write_text("main\n", encoding="utf-8")
    _git(root, "add", "--", "AGENTS.md")
    _git(
        root,
        "-c",
        "user.name=Test Author",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "Different subject",
        "-m",
        marker,
    )

    assert repository.commit_subject_paths(marker) is None

    agents.write_text("exact\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], marker) is True
    agents.write_text("descendant\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "later") is True

    assert repository.commit_subject_paths(marker) is not None


def test_commit_subject_paths_reads_the_historical_tree_with_symlinks(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "docs").mkdir()
    (root / "docs" / "first.md").write_text("first\n", encoding="utf-8")
    (root / "skills").mkdir()
    (root / "skills" / "guide.md").write_text("guide\n", encoding="utf-8")
    (root / ".agents").mkdir()
    (root / ".agents" / "skills").symlink_to("../skills", target_is_directory=True)
    marker = "Initialize Enso content"
    assert repository.snapshot([".agents/skills", "docs", "skills"], marker) is True

    (root / ".agents" / "skills").unlink()
    (root / "docs" / "first.md").unlink()
    assert repository.snapshot([".agents/skills", "docs"], "user deletions") is True

    assert repository.commit_subject_paths(marker) == (
        ".agents/skills",
        "docs/first.md",
        "skills/guide.md",
    )


def test_tracked_paths_returns_exact_nul_delimited_index_entries(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "docs").mkdir()
    (root / "docs" / "--literal.md").write_text("literal\n", encoding="utf-8")
    (root / "docs" / "line\nbreak.md").write_text("newline\n", encoding="utf-8")
    (root / "skills").mkdir()
    (root / ".agents").mkdir()
    (root / ".agents" / "skills").symlink_to("../skills", target_is_directory=True)
    _git(root, "add", "--", ".agents/skills", "docs/--literal.md", "docs/line\nbreak.md")

    assert repository.tracked_paths() == (
        ".agents/skills",
        "docs/--literal.md",
        "docs/line\nbreak.md",
    )


def test_ignored_paths_finds_only_exact_missing_allowlisted_paths(tmp_path):
    root = tmp_path / "enso"
    root.mkdir()
    (root / ".gitignore").write_text(
        "/docs/ignored.md\n/docs/--literal.md\n/workspaces/acme/knowledge/private.md\n",
        encoding="utf-8",
    )
    repository = EnsoRepository(str(root))
    repository.ensure()

    assert repository.ignored_paths(
        (
            "docs/ignored.md.bak",
            str(root / "docs" / "ignored.md"),
            "docs/--literal.md",
            "workspaces/acme/knowledge/private.md",
            "workspaces/acme/knowledge/public.md",
        )
    ) == (
        "docs/ignored.md",
        "docs/--literal.md",
        "workspaces/acme/knowledge/private.md",
    )


def test_ignored_paths_rejects_unsafe_input_without_invoking_git(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    real_run_git = repository._run_git
    checked_ignore = False

    def observe_run_git(args, **kwargs):
        nonlocal checked_ignore
        if "check-ignore" in args:
            checked_ignore = True
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", observe_run_git)

    with pytest.raises(RepositoryError, match="non-empty strings"):
        repository.ignored_paths(["docs/safe.md\0docs/other.md"])

    assert checked_ignore is False


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


def test_snapshot_rejects_invalid_paths_before_creating_its_lock(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()

    with pytest.raises(RepositoryError, match="protected"):
        repository.snapshot(["config.json"], "unsafe")

    assert not (root / ".snapshot.lock").exists()


def test_snapshot_resolves_relative_paths_from_the_explicit_caller_cwd(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    workspace = root / "workspaces" / "acme"
    note = workspace / "knowledge" / "with space.md"
    note.parent.mkdir(parents=True)
    note.write_text("decision\n", encoding="utf-8")

    assert repository.snapshot(
        ["knowledge/with space.md"],
        "workspace decision",
        caller_cwd=str(workspace),
    )

    assert repository.tracked_paths() == (
        "workspaces/acme/knowledge/with space.md",
    )


def test_snapshot_rejects_traversal_even_when_it_would_resolve_beneath_root(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")

    with pytest.raises(RepositoryError, match="traversal"):
        repository.snapshot(
            ["../AGENTS.md"],
            "unsafe traversal",
            caller_cwd=str(docs),
        )


def test_snapshot_accepts_absolute_path_with_caller_outside_repository(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    outside = tmp_path / "caller"
    outside.mkdir()
    note = root / "docs" / "absolute.md"
    note.parent.mkdir()
    note.write_text("absolute\n", encoding="utf-8")

    assert repository.snapshot(
        [str(note)],
        "absolute path",
        caller_cwd=str(outside),
    )

    assert repository.tracked_paths() == ("docs/absolute.md",)


def test_snapshot_rejects_symlink_escape(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "docs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RepositoryError, match="symlink escape"):
        repository.snapshot(["docs/secret.md"], "unsafe")


def test_snapshot_rejects_nested_symlink_escape_in_requested_directory(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    (docs / "escape.md").symlink_to(outside)

    with pytest.raises(RepositoryError, match="symlink escape"):
        repository.snapshot(["docs"], "unsafe nested link")


def test_snapshot_reads_nested_files_through_anchored_directory_descriptors(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    (docs / "safe.md").write_text("anchored bytes\n", encoding="utf-8")
    moved_docs = tmp_path / "moved-docs"
    outside_docs = tmp_path / "outside-docs"
    outside_docs.mkdir()
    (outside_docs / "safe.md").write_text("outside bytes\n", encoding="utf-8")
    docs_identity = (docs.stat().st_dev, docs.stat().st_ino)
    real_scandir = repository_module.os.scandir
    swapped = False

    def swap_ancestor_after_enumeration_anchor(path):
        nonlocal swapped
        if isinstance(path, int):
            opened = os.fstat(path)
            if not swapped and (opened.st_dev, opened.st_ino) == docs_identity:
                swapped = True
                docs.rename(moved_docs)
                docs.symlink_to(outside_docs, target_is_directory=True)
        return real_scandir(path)

    monkeypatch.setattr(repository_module.os, "scandir", swap_ancestor_after_enumeration_anchor)
    monkeypatch.setattr(repository, "_path_is_ignored", lambda _path: False)

    assert repository.snapshot(["docs"], "anchored traversal") is True

    assert swapped is True
    assert _git(root, "show", "HEAD:docs/safe.md").stdout == b"anchored bytes\n"
    assert (docs / "safe.md").read_bytes() == b"outside bytes\n"


def test_snapshot_reads_exact_nested_file_through_anchored_parent_descriptors(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    (docs / "safe.md").write_text("anchored exact bytes\n", encoding="utf-8")
    moved_docs = tmp_path / "moved-exact-docs"
    outside_docs = tmp_path / "outside-exact-docs"
    outside_docs.mkdir()
    (outside_docs / "safe.md").write_text("outside exact bytes\n", encoding="utf-8")
    real_open = repository_module.os.open
    swapped = False

    def swap_ancestor_after_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "docs" and dir_fd is not None and not swapped:
            swapped = True
            docs.rename(moved_docs)
            docs.symlink_to(outside_docs, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(repository_module.os, "open", swap_ancestor_after_open)
    monkeypatch.setattr(repository, "_path_is_ignored", lambda _path: False)

    assert repository.snapshot(["docs/safe.md"], "anchored exact traversal") is True

    assert swapped is True
    assert _git(root, "show", "HEAD:docs/safe.md").stdout == b"anchored exact bytes\n"
    assert (docs / "safe.md").read_bytes() == b"outside exact bytes\n"


def test_snapshot_rejects_case_aliases_and_preserves_exact_deletion_semantics(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    note = docs / "Note.md"
    note.write_text("initial case\n", encoding="utf-8")
    assert repository.snapshot(["docs/Note.md"], "initial spelling") is True
    if not (docs / "note.md").exists():
        pytest.skip("case-alias behavior requires a case-insensitive filesystem")
    original_head = _git(root, "rev-parse", "HEAD").stdout
    note.write_text("updated case\n", encoding="utf-8")

    with pytest.raises(RepositoryError, match="exact spelling"):
        repository.snapshot(["docs/note.md"], "wrong spelling")

    assert _git(root, "rev-parse", "HEAD").stdout == original_head
    assert _git(root, "ls-tree", "-r", "--name-only", "HEAD").stdout == b"docs/Note.md\n"
    note.unlink()
    assert repository.snapshot(["docs/Note.md"], "exact deletion") is True
    assert _git(root, "ls-tree", "-r", "--name-only", "HEAD").stdout == b""


def test_snapshot_preserves_distinct_case_paths_on_case_sensitive_filesystems(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    upper = docs / "Note.md"
    lower = docs / "note.md"
    upper.write_text("upper\n", encoding="utf-8")
    lower.write_text("lower\n", encoding="utf-8")
    if os.path.samefile(upper, lower):
        pytest.skip("distinct case paths require a case-sensitive filesystem")

    assert repository.snapshot(["docs"], "distinct case paths") is True

    assert _git(root, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines() == [
        b"docs/Note.md",
        b"docs/note.md",
    ]


def test_snapshot_preserves_executable_and_internal_symlink_modes(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    executable = docs / "run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (docs / "run-link").symlink_to("run.sh")

    assert repository.snapshot(["docs"], "file modes") is True

    assert _git(root, "ls-tree", "HEAD", "docs/run.sh").stdout.startswith(b"100755 blob ")
    assert _git(root, "ls-tree", "HEAD", "docs/run-link").stdout.startswith(b"120000 blob ")
    assert _git(root, "show", "HEAD:docs/run-link").stdout == b"run.sh"


def test_snapshot_rejects_hardlinked_files(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    source = docs / "source.md"
    source.write_text("shared inode\n", encoding="utf-8")
    linked = docs / "linked.md"
    os.link(source, linked)

    with pytest.raises(RepositoryError, match="hard links"):
        repository.snapshot(["docs"], "unsafe hardlink")


def test_snapshot_rejects_nonregular_files(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    os.mkfifo(docs / "pipe")

    with pytest.raises(RepositoryError, match="regular file or symlink"):
        repository.snapshot(["docs"], "unsafe fifo")


def test_snapshot_rejects_nested_git_repositories(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    nested = root / "docs" / "nested"
    _init_repo(nested)
    (nested / "README.md").write_text("nested\n", encoding="utf-8")
    _commit_all(nested, "nested baseline")

    with pytest.raises(RepositoryError, match="nested Git repositories"):
        repository.snapshot(["docs"], "unsafe gitlink")


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


def test_snapshot_rejects_nul_message_before_staging(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    real_run_git = repository._run_git
    hashed = False

    def observe_run_git(args, **kwargs):
        nonlocal hashed
        if args and args[0] == "hash-object":
            hashed = True
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", observe_run_git)

    with pytest.raises(RepositoryError, match="commit message"):
        repository.snapshot(["AGENTS.md"], "invalid\0message")

    assert hashed is False
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""


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


def test_snapshot_baseline_check_does_not_materialize_an_unborn_native_index(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    native_index = root / ".git" / "index"
    assert not native_index.exists()
    (root / "AGENTS.md").write_text("first snapshot\n", encoding="utf-8")
    real_write = repository._write_snapshot_transaction
    checked = False

    def assert_absent_before_first_marker(transaction):
        nonlocal checked
        if not checked:
            checked = True
            assert not native_index.exists()
        real_write(transaction)

    monkeypatch.setattr(
        repository,
        "_write_snapshot_transaction",
        assert_absent_before_first_marker,
    )

    assert repository.snapshot(["AGENTS.md"], "first snapshot") is True
    assert checked is True


def test_snapshot_baseline_check_preserves_existing_native_index_bytes(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    native_index = root / ".git" / "index"
    before = native_index.read_bytes()
    agents.write_text("updated\n", encoding="utf-8")
    real_write = repository._write_snapshot_transaction
    checked = False

    def assert_unchanged_before_first_marker(transaction):
        nonlocal checked
        if not checked:
            checked = True
            assert native_index.read_bytes() == before
        real_write(transaction)

    monkeypatch.setattr(
        repository,
        "_write_snapshot_transaction",
        assert_unchanged_before_first_marker,
    )

    assert repository.snapshot(["AGENTS.md"], "updated") is True
    assert checked is True


def test_snapshot_tracks_double_protected_names_only_in_identifier_slots(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    paths = (
        "skills/logs/SKILL.md",
        "jobs/logs/JOB.md",
        "workspaces/logs/AGENTS.md",
        "workspaces/logs/skills/uploads/SKILL.md",
    )
    for path in paths:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(f"{path}\n", encoding="utf-8")

    assert repository.snapshot(paths, "structural identifiers") is True

    assert repository.tracked_paths() == tuple(sorted(paths))


def test_snapshot_accepts_an_explicit_empty_allowlisted_directory(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    workspace = root / "workspaces" / "default"
    workspace.mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (workspace / "skills").mkdir()

    assert repository.snapshot(
        ["workspaces/default/AGENTS.md", "workspaces/default/skills"],
        "initial workspace",
    )

    assert repository.tracked_paths() == ("workspaces/default/AGENTS.md",)


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


def test_snapshot_requires_an_existing_exact_repository_without_mutating_absent_root(tmp_path):
    root = tmp_path / "enso"
    root.mkdir()
    repository = EnsoRepository(str(root))

    with pytest.raises(RepositoryError, match=r"missing.*\.git"):
        repository.snapshot(["AGENTS.md"], "must not initialize")

    assert not (root / ".git").exists()
    assert not (root / ".gitignore").exists()
    assert not (root / ".snapshot.lock").exists()


def test_snapshot_rejects_corrupt_repository_before_creating_its_lock(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / ".git" / "HEAD").write_text("not-a-valid-ref\n", encoding="ascii")

    with pytest.raises(RepositoryError, match="valid Git repository"):
        repository.snapshot(["AGENTS.md"], "must not mutate")

    assert not (root / ".snapshot.lock").exists()


def test_snapshot_fails_closed_on_an_existing_native_git_index_lock(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    native_lock = root / ".git" / "index.lock"
    native_lock.write_text("stale or active\n", encoding="utf-8")

    with pytest.raises(RepositoryError, match=r"Git index lock.*remove it"):
        repository.snapshot(["AGENTS.md"], "must not guess")

    assert native_lock.read_text(encoding="utf-8") == "stale or active\n"
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""


def test_snapshot_rejects_unsafe_enso_lock_without_staging(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    outside = tmp_path / "outside.lock"
    outside.write_text("do not touch\n", encoding="utf-8")
    (root / ".snapshot.lock").symlink_to(outside)

    with pytest.raises(RepositoryError, match="snapshot lock"):
        repository.snapshot(["AGENTS.md"], "must not follow lock")

    assert outside.read_text(encoding="utf-8") == "do not touch\n"
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""


def test_snapshot_lock_detects_repository_root_replacement(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    moved_root = tmp_path / "moved-enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    real_open = repository_module.os.open
    replaced = False

    def replace_root_before_lock_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if path == ".snapshot.lock" and not replaced:
            replaced = True
            root.rename(moved_root)
            root.mkdir()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(repository_module.os, "open", replace_root_before_lock_open)

    with pytest.raises(RepositoryError, match="physical repository root"):
        repository.snapshot(["AGENTS.md"], "must not stage in replaced root")

    assert replaced is True
    assert _git(moved_root, "diff", "--cached", "--name-only").stdout == b""


def test_snapshot_lock_detects_repository_root_replacement_after_acquisition(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    moved_root = tmp_path / "moved-enso"
    replacement = tmp_path / "replacement-enso"
    repository = EnsoRepository(str(root))
    replacement_repository = EnsoRepository(str(replacement))
    repository.ensure()
    replacement_repository.ensure()
    (root / "AGENTS.md").write_text("original bytes\n", encoding="utf-8")
    (replacement / "AGENTS.md").write_text("replacement bytes\n", encoding="utf-8")
    real_lock = repository._snapshot_lock

    @repository_module.contextmanager
    def replace_root_after_lock_acquisition():
        with real_lock():
            root.rename(moved_root)
            replacement.rename(root)
            yield

    monkeypatch.setattr(repository, "_snapshot_lock", replace_root_after_lock_acquisition)

    with pytest.raises(RepositoryError, match="locked Enso repository root changed"):
        repository.snapshot(["AGENTS.md"], "must remain bound to locked root")

    assert _git(moved_root, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    assert _git(root, "rev-parse", "--verify", "HEAD", check=False).returncode != 0


def test_snapshot_serializes_concurrent_index_and_head_mutations(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    first_repository = EnsoRepository(str(root))
    second_repository = EnsoRepository(str(root))
    first_repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    (docs / "first.md").write_text("first\n", encoding="utf-8")
    (docs / "second.md").write_text("second\n", encoding="utf-8")

    first_at_commit = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()
    results: dict[str, bool] = {}
    errors: list[BaseException] = []
    real_first_run_git = first_repository._run_git

    def pause_first_before_commit(args, **kwargs):
        if "commit-tree" in args:
            first_at_commit.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("timed out waiting to release first snapshot")
        return real_first_run_git(args, **kwargs)

    monkeypatch.setattr(first_repository, "_run_git", pause_first_before_commit)

    def run_first() -> None:
        try:
            results["first"] = first_repository.snapshot(["docs/first.md"], "first")
        except BaseException as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)

    def run_second() -> None:
        second_started.set()
        try:
            results["second"] = second_repository.snapshot(["docs/second.md"], "second")
        except BaseException as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)
        finally:
            second_finished.set()

    first_thread = threading.Thread(target=run_first)
    second_thread = threading.Thread(target=run_second)
    first_thread.start()
    assert first_at_commit.wait(timeout=5)
    second_thread.start()
    assert second_started.wait(timeout=5)
    assert not second_finished.wait(timeout=0.2)
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert results == {"first": True, "second": True}
    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"2"
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""


def test_snapshot_recovers_after_process_is_killed_after_temporary_staging_with_head(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("survives crash\n", encoding="utf-8")
    crashed = _run_crashing_snapshot(
        root,
        condition="'update-index' in args and '--cacheinfo' in args",
    )

    assert crashed.returncode == -signal.SIGKILL
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert (root / ".snapshot.transaction.json").is_file()
    temp_indexes = _snapshot_temp_indexes(root)
    assert len(temp_indexes) == 1
    assert stat.S_IMODE(temp_indexes[0].stat().st_mode) == 0o600

    assert repository.snapshot(["AGENTS.md"], "recovered snapshot") is True

    assert not (root / ".snapshot.transaction.json").exists()
    assert _snapshot_temp_indexes(root) == ()
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"survives crash\n"
    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"2"


def test_snapshot_hardens_indexes_without_no_follow_chmod_support(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("private index\n", encoding="utf-8")

    def unsupported_path_chmod(*args, **kwargs):
        raise NotImplementedError("follow_symlinks=False is unavailable")

    monkeypatch.setattr(repository_module.os, "chmod", unsupported_path_chmod)

    assert repository.snapshot(["AGENTS.md"], "portable hardening") is True

    assert stat.S_IMODE((root / ".git" / "index").stat().st_mode) == 0o600


def test_snapshot_transaction_recovers_after_crash_under_restrictive_umask(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("private crash state\n", encoding="utf-8")

    crashed = _run_crashing_snapshot(
        root,
        condition="'update-ref' in args",
        umask=0o777,
    )

    assert crashed.returncode == -signal.SIGKILL
    marker = root / ".snapshot.transaction.json"
    native_lock = root / ".git" / "index.lock"
    temp_indexes = _snapshot_temp_indexes(root)
    assert stat.S_IMODE((root / ".snapshot.lock").stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(native_lock.stat().st_mode) == 0o600
    assert len(temp_indexes) == 1
    assert stat.S_IMODE(temp_indexes[0].stat().st_mode) == 0o600

    assert repository.snapshot(["AGENTS.md"], "recover private state") is False

    assert not marker.exists()
    assert not native_lock.exists()
    assert _snapshot_temp_indexes(root) == ()
    assert stat.S_IMODE((root / ".git" / "index").stat().st_mode) == 0o600
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""


def test_snapshot_recovers_after_process_is_killed_after_atomic_ref_update(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("committed before crash\n", encoding="utf-8")

    crashed = _run_crashing_snapshot(root, condition="'update-ref' in args")

    assert crashed.returncode == -signal.SIGKILL
    assert _git(root, "diff", "--cached", "--name-only").stdout == b"AGENTS.md\n"
    assert (root / ".snapshot.transaction.json").is_file()

    assert repository.snapshot(["AGENTS.md"], "retry after committed crash") is False

    assert not (root / ".snapshot.transaction.json").exists()
    assert _snapshot_temp_indexes(root) == ()
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"committed before crash\n"
    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"2"


def test_snapshot_recovers_new_head_old_index_when_native_lock_is_absent(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("committed without surviving lock\n", encoding="utf-8")
    crashed = _run_crashing_snapshot(root, condition="'update-ref' in args")
    assert crashed.returncode == -signal.SIGKILL
    native_lock = root / ".git" / "index.lock"
    assert native_lock.is_file()
    native_lock.unlink()

    assert repository.snapshot(["AGENTS.md"], "retry missing native lock") is False

    assert not native_lock.exists()
    assert not (root / ".snapshot.transaction.json").exists()
    assert _snapshot_temp_indexes(root) == ()
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == (
        b"committed without surviving lock\n"
    )
    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"2"


def test_snapshot_recovers_old_head_old_index_with_enso_native_lock(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    original_head = _git(root, "rev-parse", "HEAD").stdout
    agents.write_text("retry exactly once\n", encoding="utf-8")

    crashed = _run_crashing_snapshot_after_native_index_lock(root)

    assert crashed.returncode == -signal.SIGKILL
    assert _git(root, "rev-parse", "HEAD").stdout == original_head
    assert (root / ".git" / "index.lock").is_file()
    assert (root / ".snapshot.transaction.json").is_file()
    assert len(_snapshot_temp_indexes(root)) == 1

    assert repository.snapshot(["AGENTS.md"], "retry exactly once") is True

    assert not (root / ".git" / "index.lock").exists()
    assert not (root / ".snapshot.transaction.json").exists()
    assert _snapshot_temp_indexes(root) == ()
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"retry exactly once\n"
    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"2"


def test_snapshot_preserves_foreign_native_lock_during_transaction_recovery(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    original_head = _git(root, "rev-parse", "HEAD").stdout
    agents.write_text("pending\n", encoding="utf-8")
    crashed = _run_crashing_snapshot_after_native_index_lock(root)
    assert crashed.returncode == -signal.SIGKILL
    native_lock = root / ".git" / "index.lock"
    native_lock.unlink()
    native_lock.write_bytes(b"foreign lock bytes\n")

    with pytest.raises(RepositoryError, match="does not match Enso's durable transaction"):
        repository.snapshot(["AGENTS.md"], "must preserve foreign lock")

    assert native_lock.read_bytes() == b"foreign lock bytes\n"
    assert _git(root, "rev-parse", "HEAD").stdout == original_head
    assert (root / ".snapshot.transaction.json").is_file()
    assert len(_snapshot_temp_indexes(root)) == 1
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""


def test_snapshot_lock_survives_parent_kill_until_update_ref_child_exits(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("orphaned child commit\n", encoding="utf-8")
    real_git = shutil.which("git")
    assert real_git is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    child_started = tmp_path / "update-ref-started"
    release_child = tmp_path / "release-update-ref"
    wrapper = bin_dir / "git"
    wrapper.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                'case " $* " in',
                '  *" update-ref "*)',
                f"    : > {shlex.quote(str(child_started))}",
                f"    while [ ! -e {shlex.quote(str(release_child))} ]; do sleep 0.02; done",
                "    ;;",
                "esac",
                f"exec {shlex.quote(real_git)} \"$@\"",
                "",
            )
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    first_script = (
        "from enso.repository import EnsoRepository; "
        f"EnsoRepository({str(root)!r}).snapshot(['AGENTS.md'], 'orphan-safe')"
    )
    first = subprocess.Popen(
        [sys.executable, "-c", first_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    deadline = time.monotonic() + 10
    while not child_started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_started.exists()
    first.kill()
    first.wait(timeout=5)
    assert first.returncode == -signal.SIGKILL

    acquired = tmp_path / "second-acquired-enso-lock"
    second_script = "\n".join(
        (
            "import contextlib",
            "from pathlib import Path",
            "from enso.repository import EnsoRepository",
            f"repository = EnsoRepository({str(root)!r})",
            "original_lock = repository._snapshot_lock",
            "@contextlib.contextmanager",
            "def observed_lock():",
            "    with original_lock():",
            f"        Path({str(acquired)!r}).touch()",
            "        yield",
            "repository._snapshot_lock = observed_lock",
            "repository.snapshot(['AGENTS.md'], 'retry after orphan')",
        )
    )
    second = subprocess.Popen(
        [sys.executable, "-c", second_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        time.sleep(0.25)
        assert not acquired.exists()
        assert second.poll() is None
    finally:
        release_child.touch()
    second_stdout, second_stderr = second.communicate(timeout=10)

    assert second.returncode == 0, (second_stdout, second_stderr)
    assert acquired.is_file()
    assert not (root / ".snapshot.transaction.json").exists()
    assert _snapshot_temp_indexes(root) == ()
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"orphaned child commit\n"
    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"2"


def test_snapshot_recovers_after_process_is_killed_after_native_index_install(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("committed and aligned\n", encoding="utf-8")

    crashed = _run_crashing_snapshot_after_native_index_install(root)

    assert crashed.returncode == -signal.SIGKILL
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert (root / ".snapshot.transaction.json").is_file()

    assert repository.snapshot(["AGENTS.md"], "retry after aligned crash") is False

    assert not (root / ".snapshot.transaction.json").exists()
    assert _snapshot_temp_indexes(root) == ()
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"committed and aligned\n"
    assert _git(root, "rev-list", "--count", "HEAD").stdout.rstrip() == b"2"


def test_snapshot_rejects_non_owner_only_transaction_marker(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("pending\n", encoding="utf-8")
    crashed = _run_crashing_snapshot(
        root,
        condition="'update-index' in args and '--cacheinfo' in args",
    )
    assert crashed.returncode == -signal.SIGKILL
    marker = root / ".snapshot.transaction.json"
    marker.chmod(0o640)

    with pytest.raises(RepositoryError, match="owner-only regular file"):
        repository.snapshot(["AGENTS.md"], "must preserve unsafe marker")

    assert marker.is_file()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o640
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""


def test_snapshot_rejects_transaction_marker_with_unsafe_temporary_index(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("pending\n", encoding="utf-8")
    crashed = _run_crashing_snapshot(
        root,
        condition="'update-index' in args and '--cacheinfo' in args",
    )
    assert crashed.returncode == -signal.SIGKILL
    outside = tmp_path / "outside-index"
    outside.write_text("must not be touched\n", encoding="utf-8")
    marker = root / ".snapshot.transaction.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["temp_index"] = "../outside-index"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    marker.chmod(0o600)

    with pytest.raises(RepositoryError, match="unsafe temporary index"):
        repository.ensure()

    assert outside.read_text(encoding="utf-8") == "must not be touched\n"
    assert marker.is_file()


def test_ensure_cleans_owner_only_snapshot_marker_temp_residue(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    residue = root / ".snapshot-transaction-0123456789abcdef0123456789abcdef.tmp"
    residue.write_text("partial marker\n", encoding="utf-8")
    residue.chmod(0o600)

    repository.ensure()

    assert not residue.exists()
    assert not (root / ".snapshot.transaction.json").exists()


def test_ensure_preserves_non_owner_only_snapshot_marker_temp_residue(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    residue = root / ".snapshot-transaction-fedcba9876543210fedcba9876543210.tmp"
    residue.write_text("untrusted partial marker\n", encoding="utf-8")
    residue.chmod(0o640)

    with pytest.raises(RepositoryError, match="residue is unsafe"):
        repository.ensure()

    assert residue.read_text(encoding="utf-8") == "untrusted partial marker\n"
    assert stat.S_IMODE(residue.stat().st_mode) == 0o640


@pytest.mark.parametrize("staging_mode", ["unrelated", "intent-to-add"])
def test_snapshot_recovery_preserves_divergent_native_staging(tmp_path, staging_mode):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("pending crash\n", encoding="utf-8")
    crashed = _run_crashing_snapshot(
        root,
        condition="'update-index' in args and '--cacheinfo' in args",
    )
    assert crashed.returncode == -signal.SIGKILL
    unrelated = root / "docs" / "unrelated.md"
    unrelated.parent.mkdir()
    unrelated.write_text("external staging\n", encoding="utf-8")
    if staging_mode == "intent-to-add":
        _git(root, "add", "--intent-to-add", "--", "docs/unrelated.md")
    else:
        _git(root, "add", "--", "docs/unrelated.md")

    with pytest.raises(RepositoryError, match="diverged"):
        repository.snapshot(["AGENTS.md"], "must preserve divergence")

    assert _git(root, "ls-files", "--", "docs/unrelated.md").stdout == b"docs/unrelated.md\n"
    assert (root / ".snapshot.transaction.json").is_file()
    assert len(_snapshot_temp_indexes(root)) == 1


def test_snapshot_recovery_preserves_unexpected_head_divergence(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("pending crash\n", encoding="utf-8")
    crashed = _run_crashing_snapshot(
        root,
        condition="'update-index' in args and '--cacheinfo' in args",
    )
    assert crashed.returncode == -signal.SIGKILL
    external = root / "docs" / "external.md"
    external.parent.mkdir()
    external.write_text("external commit\n", encoding="utf-8")
    _commit_all(root, "external divergence")
    divergent_head = _git(root, "rev-parse", "HEAD").stdout

    with pytest.raises(RepositoryError, match="diverged"):
        repository.snapshot(["AGENTS.md"], "must preserve head")

    assert _git(root, "rev-parse", "HEAD").stdout == divergent_head
    assert (root / ".snapshot.transaction.json").is_file()


def test_snapshot_clears_only_its_owned_index_lock_when_head_cas_loses(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    old_head = _git(root, "rev-parse", "HEAD").stdout.rstrip().decode("ascii")
    tree = _git(root, "rev-parse", "HEAD^{tree}").stdout.rstrip().decode("ascii")
    divergent_head = _git(root, "commit-tree", tree, "-p", old_head).stdout.rstrip()
    before_index = (root / ".git" / "index").read_bytes()
    agents.write_text("pending Enso snapshot\n", encoding="utf-8")
    real_run_git = repository._run_git
    diverged = False

    def advance_head_before_enso_cas(args, **kwargs):
        nonlocal diverged
        if args and args[0] == "update-ref" and not diverged:
            diverged = True
            _git(root, "update-ref", "HEAD", divergent_head.decode("ascii"), old_head)
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", advance_head_before_enso_cas)

    with pytest.raises(RepositoryError, match="advance the Enso snapshot ref"):
        repository.snapshot(["AGENTS.md"], "losing CAS")

    assert diverged is True
    assert _git(root, "rev-parse", "HEAD").stdout.rstrip() == divergent_head
    assert (root / ".git" / "index").read_bytes() == before_index
    assert not (root / ".git" / "index.lock").exists()
    assert (root / ".snapshot.transaction.json").is_file()
    assert len(_snapshot_temp_indexes(root)) == 1


def test_snapshot_commits_frozen_audited_bytes_when_worktree_changes_before_cas(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("audited bytes\n", encoding="utf-8")
    real_run_git = repository._run_git
    changed = False

    def mutate_after_temporary_add(args, **kwargs):
        nonlocal changed
        result = real_run_git(args, **kwargs)
        if args and args[0] == "hash-object" and "--no-filters" in args and not changed:
            changed = True
            agents.write_text("later worktree bytes\n", encoding="utf-8")
        return result

    monkeypatch.setattr(repository, "_run_git", mutate_after_temporary_add)

    assert repository.snapshot(["AGENTS.md"], "frozen audit") is True

    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"audited bytes\n"
    assert agents.read_text(encoding="utf-8") == "later worktree bytes\n"
    assert _git(root, "diff", "--name-only").stdout == b"AGENTS.md\n"


def test_snapshot_native_index_lock_blocks_external_staging_before_atomic_install(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    agents.write_text("snapshotted\n", encoding="utf-8")
    external = root / "docs" / "external.md"
    external.parent.mkdir()
    external.write_text("must remain unstaged\n", encoding="utf-8")
    real_run_git = repository._run_git
    external_add: subprocess.CompletedProcess[bytes] | None = None

    def attempt_external_add_after_final_audit(args, **kwargs):
        nonlocal external_add
        if args and args[0] == "update-ref" and external_add is None:
            external_add = _git(
                root,
                "add",
                "--",
                "docs/external.md",
                check=False,
            )
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", attempt_external_add_after_final_audit)

    assert repository.snapshot(["AGENTS.md"], "atomic native index") is True

    assert external_add is not None
    assert external_add.returncode != 0
    assert b"index.lock" in external_add.stderr
    assert _git(root, "ls-files", "--", "docs/external.md").stdout == b""
    assert _git(root, "status", "--short", "--", "docs/external.md").stdout.startswith(b"??")
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert _git(root, "show", "HEAD:AGENTS.md").stdout == b"snapshotted\n"


def test_ensure_recovers_completed_initial_snapshot_transaction_before_history_reads(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("initial setup\n", encoding="utf-8")
    subject = "Initialize Enso content"

    crashed = _run_crashing_snapshot(
        root,
        condition="'update-ref' in args",
        paths=(".gitignore", "AGENTS.md"),
        message=subject,
    )

    assert crashed.returncode == -signal.SIGKILL
    assert (root / ".snapshot.transaction.json").is_file()

    repository.ensure()

    assert not (root / ".snapshot.transaction.json").exists()
    assert _snapshot_temp_indexes(root) == ()
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert repository.commit_subject_paths(subject) == (".gitignore", "AGENTS.md")


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


def test_snapshot_refuses_and_preserves_preexisting_intent_to_add(tmp_path):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    ita = docs / "intent.md"
    ita.write_text("intent only\n", encoding="utf-8")
    _git(root, "add", "--intent-to-add", "--", "docs/intent.md")
    (root / "AGENTS.md").write_text("must not snapshot\n", encoding="utf-8")
    before_index = (root / ".git" / "index").read_bytes()

    with pytest.raises(RepositoryError, match="staging area is not clean"):
        repository.snapshot(["AGENTS.md"], "must preserve intent-to-add")

    assert (root / ".git" / "index").read_bytes() == before_index
    assert _git(root, "ls-files", "--", "docs/intent.md").stdout == b"docs/intent.md\n"
    assert _git(
        root,
        "diff",
        "--cached",
        "--ita-visible-in-index",
        "--quiet",
        check=False,
    ).returncode == 1


def test_snapshot_preserves_intent_to_add_racing_the_clean_staging_check(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    docs = root / "docs"
    docs.mkdir()
    ita = docs / "intent.md"
    ita.write_text("racing intent\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("must not snapshot\n", encoding="utf-8")
    real_run_git = repository._run_git
    raced = False

    def add_intent_after_clean_diff(args, **kwargs):
        nonlocal raced
        result = real_run_git(args, **kwargs)
        if args and args[0] == "diff" and "--ita-visible-in-index" in args and not raced:
            raced = True
            _git(root, "add", "--intent-to-add", "--", "docs/intent.md")
        return result

    monkeypatch.setattr(repository, "_run_git", add_intent_after_clean_diff)

    with pytest.raises(RepositoryError, match="clean staging baseline"):
        repository.snapshot(["AGENTS.md"], "must preserve racing intent")

    assert raced is True
    assert _git(root, "ls-files", "--", "docs/intent.md").stdout == b"docs/intent.md\n"
    assert _git(
        root,
        "diff",
        "--cached",
        "--ita-visible-in-index",
        "--quiet",
        check=False,
    ).returncode == 1
    assert not (root / ".snapshot.transaction.json").exists()


def test_snapshot_cleans_temporary_index_when_cacheinfo_update_fails(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    (root / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    real_run_git = repository._run_git
    failed_once = False

    def fail_after_first_cacheinfo_update(args, **kwargs):
        nonlocal failed_once
        if "update-index" in args and "--cacheinfo" in args and not failed_once:
            failed_once = True
            real_run_git(args, **kwargs)
            raise RepositoryError("simulated temporary index update failure")
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", fail_after_first_cacheinfo_update)

    with pytest.raises(RepositoryError, match="temporary index update failure"):
        repository.snapshot(["AGENTS.md"], "interrupted")

    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert repository.snapshot(["AGENTS.md"], "retry") is True


def test_snapshot_cleans_temporary_index_when_cacheinfo_update_is_interrupted(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("worktree bytes\n", encoding="utf-8")
    real_run_git = repository._run_git
    interrupted = False

    def interrupt_after_cacheinfo_update(args, **kwargs):
        nonlocal interrupted
        if "update-index" in args and "--cacheinfo" in args and not interrupted:
            interrupted = True
            real_run_git(args, **kwargs)
            raise KeyboardInterrupt
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", interrupt_after_cacheinfo_update)

    with pytest.raises(KeyboardInterrupt):
        repository.snapshot(["AGENTS.md"], "interrupted add")

    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert agents.read_text(encoding="utf-8") == "worktree bytes\n"


def test_snapshot_cleans_temporary_index_when_tree_audit_fails(tmp_path, monkeypatch):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("worktree bytes\n", encoding="utf-8")
    real_run_git = repository._run_git

    def fail_tree_audit(args, **kwargs):
        if args and args[0] == "diff-tree":
            raise RepositoryError("simulated snapshot tree audit failure")
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", fail_tree_audit)

    with pytest.raises(RepositoryError, match="snapshot tree audit failure"):
        repository.snapshot(["AGENTS.md"], "failed diff")

    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert agents.read_text(encoding="utf-8") == "worktree bytes\n"


def test_snapshot_commit_failure_cleans_transaction_without_reverting_worktree_or_head(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    original_head = _git(root, "rev-parse", "HEAD").stdout
    agents.write_text("changed but preserved\n", encoding="utf-8")
    real_run_git = repository._run_git

    def fail_commit(args, **kwargs):
        if "commit-tree" in args:
            raise RepositoryError("simulated commit interruption")
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", fail_commit)

    with pytest.raises(RepositoryError, match="commit interruption"):
        repository.snapshot(["AGENTS.md"], "interrupted")

    assert _git(root, "rev-parse", "HEAD").stdout == original_head
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert agents.read_text(encoding="utf-8") == "changed but preserved\n"


def test_snapshot_commit_interrupt_cleans_transaction_without_reverting_worktree_or_head(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "enso"
    repository = EnsoRepository(str(root))
    repository.ensure()
    agents = root / "AGENTS.md"
    agents.write_text("initial\n", encoding="utf-8")
    assert repository.snapshot(["AGENTS.md"], "initial") is True
    original_head = _git(root, "rev-parse", "HEAD").stdout
    agents.write_text("changed but preserved\n", encoding="utf-8")
    real_run_git = repository._run_git

    def interrupt_commit(args, **kwargs):
        if "commit-tree" in args:
            raise KeyboardInterrupt
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(repository, "_run_git", interrupt_commit)

    with pytest.raises(KeyboardInterrupt):
        repository.snapshot(["AGENTS.md"], "interrupted")

    assert _git(root, "rev-parse", "HEAD").stdout == original_head
    assert _git(root, "diff", "--cached", "--name-only").stdout == b""
    assert agents.read_text(encoding="utf-8") == "changed but preserved\n"


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
