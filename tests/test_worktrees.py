"""Worktrees against real temporary repositories: prepare, land, sweep, and every refusal."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import commit_file, git

from enso import db, tasks, worktrees
from enso.config import Config, Paths, ProjectConfig
from enso.worktrees import WorktreeError

USER = "user:gavin"


def project_for(config: Config, repo: Path, **fields: object) -> ProjectConfig:
    return replace(config.projects["EN"], repo=repo, **fields)  # type: ignore[arg-type]


def worktree_list(repo: Path) -> list[str]:
    return [
        line[len("worktree ") :]
        for line in git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def branches(repo: Path) -> list[str]:
    return git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def test_prepare_creates_once_then_reuses(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    project = project_for(
        project_config,
        repo,
        setup='echo ran >> "$ENSO_TASK_DIR/setup.txt"',
        copy=(".env", "missing.txt"),
    )
    info = worktrees.prepare(enso_home, project, "EN-001")
    assert info == worktrees.WorktreeInfo(
        repo / ".worktrees" / "EN-001", "enso/EN-001", "main", True, ()
    )
    assert (info.path / "AGENTS.md").is_file() and (info.path / ".env").read_text() == "SECRET=1\n"
    assert (info.path / "setup.txt").read_text() == "ran\n"
    assert str(info.path.resolve()) in worktree_list(repo)
    assert git(info.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "enso/EN-001"

    (info.path / "README.md").write_text("changed\n")
    again = worktrees.prepare(enso_home, project, "EN-001")
    assert again.created is False and again.dirty == ("README.md",)
    assert (info.path / "setup.txt").read_text() == "ran\n"  # setup ran only on creation
    assert worktrees.dirty_files(enso_home, project, "EN-001") == ("README.md",)
    assert worktrees.dirty_files(enso_home, project, "EN-002") == ()  # no worktree yet
    # Main checkout untouched: still on main, still clean, nothing copied back.
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""


def test_prepare_attaches_an_existing_branch_and_refuses_bad_input(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    project = project_for(project_config, repo)
    git(repo, "branch", "enso/EN-001", "main")
    info = worktrees.prepare(enso_home, project, "EN-001")
    assert info.created is True and info.branch == "enso/EN-001"
    with pytest.raises(WorktreeError, match="not a task reference"):
        worktrees.prepare(enso_home, project, "../etc")
    with pytest.raises(WorktreeError, match="has no repository"):
        worktrees.prepare(enso_home, project_config.projects["EN"], "EN-003")
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(WorktreeError, match="is detached"):
        worktrees.prepare(enso_home, project, "EN-002")


def test_failing_setup_preserves_work_and_can_resume(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    project = project_for(project_config, repo, setup="echo nope >&2; exit 3")
    with pytest.raises(WorktreeError, match=r"setup failed \(exit 3\).*nope"):
        worktrees.prepare(enso_home, project, "EN-001")
    path = repo / ".worktrees" / "EN-001"
    assert path.exists()
    assert "enso/EN-001" in branches(repo)
    assert worktrees.lookup(enso_home, "EN-001")["status"] == "setup_failed"
    (path / ".env").write_text("retained\n")
    info = worktrees.prepare(
        enso_home, replace(project, setup='echo recovered > "$ENSO_TASK_DIR/setup.txt"'), "EN-001"
    )
    assert not info.created
    assert (path / ".env").read_text() == "retained\n"
    assert (path / "setup.txt").read_text() == "recovered\n"
    assert worktrees.lookup(enso_home, "EN-001")["status"] == "ready"
    # A stale directory nobody registered is not silently adopted.
    stale = repo / ".worktrees" / "EN-002"
    stale.mkdir(parents=True)
    (stale / "leftover").write_text("x")
    with pytest.raises(WorktreeError, match="not a registered worktree"):
        worktrees.prepare(enso_home, replace(project, setup=None), "EN-002")


def test_land_rebases_and_fast_forwards(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    project = project_for(project_config, repo)
    info = worktrees.prepare(enso_home, project, "EN-001")
    with pytest.raises(WorktreeError, match="nothing main lacks; it was never committed to"):
        worktrees.land(enso_home, project, "EN-001")
    commit_file(info.path, "feature.py", "print(1)\n", "feat: one")
    (info.path / "README.md").write_text("dirty\n")
    with pytest.raises(WorktreeError, match=r"uncommitted changes: README\.md"):
        worktrees.land(enso_home, project, "EN-001")
    git(info.path, "checkout", "-q", "--", "README.md")
    upstream = commit_file(repo, "other.py", "print(2)\n", "feat: elsewhere")  # main moved on

    head = worktrees.land(enso_home, project, "EN-001")
    assert git(repo, "rev-parse", "HEAD").strip() == head != upstream
    assert (repo / "feature.py").exists() and (repo / "other.py").exists()
    assert git(repo, "log", "--format=%s", "-3").split("\n")[:3] == [
        "feat: one",
        "feat: elsewhere",
        "init",
    ]  # rebased on top, then fast-forwarded: no merge commit
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == "main"
    with pytest.raises(WorktreeError, match="nothing main lacks; it is already landed"):
        worktrees.land(enso_home, project, "EN-001")
    with pytest.raises(WorktreeError, match="EN-002 has no worktree"):
        worktrees.land(enso_home, project, "EN-002")


def test_land_aborts_on_conflict_and_refuses_a_dirty_main_checkout(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    project = project_for(project_config, repo)
    info = worktrees.prepare(enso_home, project, "EN-001")
    mine = commit_file(info.path, "README.md", "mine\n", "docs: mine")
    commit_file(repo, "README.md", "theirs\n", "docs: theirs")
    with pytest.raises(WorktreeError, match=r"conflicts: README\.md"):
        worktrees.land(enso_home, project, "EN-001")
    # The rebase was aborted: the branch is where it was, and no rebase is in progress.
    assert git(info.path, "rev-parse", "HEAD").strip() == mine
    assert list((repo / ".git" / "worktrees").glob("*/rebase-merge")) == []
    assert git(info.path, "status", "--porcelain", "--untracked-files=no") == ""

    git(info.path, "reset", "-q", "--hard", "main")
    commit_file(info.path, "new.py", "x\n", "feat: new")
    (repo / "README.md").write_text("editing in main\n")
    with pytest.raises(WorktreeError, match="main checkout is dirty; block the task"):
        worktrees.land(enso_home, project, "EN-001")
    assert (repo / "README.md").read_text() == "editing in main\n"  # untouched
    assert not (repo / "new.py").exists()


def test_sweep_removes_finished_clean_worktrees_only(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    project = project_for(project_config, repo)
    for title in ("landed", "dirty", "open", "dropped with an untracked file", "dropped clean"):
        tasks.create(enso_home, project_config, "EN", title, actor=USER)
    for ref in ("EN-001", "EN-002", "EN-003", "EN-004", "EN-005"):
        worktrees.prepare(enso_home, project, ref)
    commit_file(repo / ".worktrees" / "EN-001", "a.py", "1\n", "feat: a")
    worktrees.land(enso_home, project, "EN-001")
    commit_file(repo / ".worktrees" / "EN-002", "b.py", "2\n", "feat: b")
    (repo / ".worktrees" / "EN-002" / "README.md").write_text("unfinished\n")
    (repo / ".worktrees" / "EN-004" / "untracked.txt").write_text("scratch\n")
    (repo / ".worktrees" / "EN-005" / ".env").write_text("ignored\n")
    for ref in ("EN-001", "EN-002"):
        for _ in range(3):
            tasks.move(
                enso_home, project_config, ref, "advance", actor=USER, run_id=None, message="m"
            )
    for ref in ("EN-004", "EN-005"):
        tasks.move(enso_home, project_config, ref, "drop", actor=USER, run_id=None, message="no")
    (repo / ".worktrees" / "stray").mkdir()  # not a task reference: never touched

    # EN-004 is finished but holds a file no branch ever had: the sweep must not destroy it.
    # EN-005 holds only an ignored file (a copied .env), which is not work and does not count.
    assert worktrees.sweep(enso_home, project) == ["EN-001", "EN-005"]
    remaining = sorted(p.name for p in (repo / ".worktrees").iterdir())
    assert remaining == ["EN-002", "EN-003", "EN-004", "stray"]
    assert (repo / ".worktrees" / "EN-004" / "untracked.txt").read_text() == "scratch\n"
    assert branches(repo) == ["enso/EN-002", "enso/EN-003", "enso/EN-004", "main"]
    assert all(Path(p).name != "EN-001" for p in worktree_list(repo))
    assert worktrees.sweep(enso_home, project) == []
    assert worktrees.sweep(enso_home, replace(project, key="ZZ")) == []  # no such directory
    # A removal Git refuses (here: the tree went unclean between the check and the remove) is
    # a skip, not a failure that stops the pass.
    original = worktrees._unclean
    monkey = pytest.MonkeyPatch()
    monkey.setattr(worktrees, "_unclean", lambda cwd: ())
    try:
        assert worktrees.sweep(enso_home, project) == []
    finally:
        monkey.undo()
    assert (repo / ".worktrees" / "EN-004" / "untracked.txt").read_text() == "scratch\n"
    assert worktrees._unclean is original


def test_land_aborts_a_rebase_that_times_out(
    enso_home: Paths, project_config: Config, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = project_for(project_config, repo)
    info = worktrees.prepare(enso_home, project, "EN-001")
    mine = commit_file(info.path, "README.md", "mine\n", "docs: mine")
    commit_file(repo, "README.md", "theirs\n", "docs: theirs")
    original = worktrees._run

    def slow_rebase(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        if cmd[:2] == ["git", "rebase"] and "--abort" not in cmd:
            original(cmd, **kwargs)  # type: ignore[arg-type]  # Git stops mid-rebase on the conflict
            raise WorktreeError("git timed out after 120s")
        return original(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees, "_run", slow_rebase)
    with pytest.raises(WorktreeError, match="git timed out after 120s; rebase aborted"):
        worktrees.land(enso_home, project, "EN-001")
    assert git(info.path, "rev-parse", "HEAD").strip() == mine
    assert list((repo / ".git" / "worktrees").glob("*/rebase-merge")) == []


@pytest.mark.parametrize("location", [".tasks", "../sibling-tasks", "~/tasks", "absolute"])
def test_configured_roots_are_ignored_and_identity_survives_config_changes(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    location: str,
    tmp_path: Path,
) -> None:
    selected = str(tmp_path / "absolute-tasks") if location == "absolute" else location
    project = project_for(project_config, repo, worktree_root=selected, base="main")
    info = worktrees.prepare(enso_home, project, "EN-001")
    expected = Path(selected).expanduser()
    if not expected.is_absolute():
        expected = repo / expected
    assert info.path == expected.resolve() / "EN-001"
    assert git(repo, "status", "--porcelain") == ""
    record = worktrees.lookup(enso_home, "EN-001")
    assert record is not None
    assert record["start_revision"] == git(repo, "rev-parse", "main").strip()
    # A removed registration can attach the same retained branch, at its original path.
    commit_file(info.path, "kept.py", "value = 1\n", "feat: keep me")
    git(repo, "worktree", "remove", str(info.path))
    git(repo, "checkout", "-q", "-b", "release")
    changed = replace(project, worktree_root="elsewhere", base="release", repo=tmp_path / "missing")
    recovered = worktrees.prepare(enso_home, changed, "EN-001")
    assert recovered.path == info.path and recovered.base == "main"
    assert (recovered.path / "kept.py").read_text() == "value = 1\n"
    assert worktrees.lookup(enso_home, "EN-001")["repo"] == str(repo)


def test_configured_registration_is_adopted_in_place_and_branch_mismatches_refused(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    project = project_for(project_config, repo, worktree_root="task-worktrees", base="main")
    configured = repo / "task-worktrees" / "EN-001"
    git(repo, "worktree", "add", "-b", "enso/EN-001", str(configured), "main")
    commit_file(configured, "retained.py", "x = 2\n", "feat: retained")
    info = worktrees.prepare(enso_home, project, "EN-001")
    assert info.path == configured and not info.created
    assert worktrees.lookup(enso_home, "EN-001")["path"] == str(configured)
    git(configured, "checkout", "-q", "-b", "unrelated")
    with pytest.raises(WorktreeError, match="not the registered"):
        worktrees.prepare(enso_home, project, "EN-001")
    assert (configured / "retained.py").exists()
    second = repo / "task-worktrees" / "EN-002"
    git(repo, "worktree", "add", "-b", "other-work", str(second), "main")
    with pytest.raises(WorktreeError, match="different branch"):
        worktrees.prepare(enso_home, project, "EN-002")
    assert worktrees.lookup(enso_home, "EN-002") is None


def test_legacy_worktree_is_not_selected_adopted_or_removed(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    project = project_for(project_config, repo, base="main")
    tasks.create(enso_home, project_config, "EN", "old task", actor=USER)
    tasks.move(
        enso_home, project_config, "EN-001", "drop", actor=USER, run_id=None, message="cancelled"
    )
    legacy = enso_home.home / "worktrees" / "EN" / "EN-001"
    git(repo, "worktree", "add", "-b", "enso/EN-001", str(legacy), "main")
    before = (legacy / "README.md").read_bytes()

    assert worktrees.worktree_path(enso_home, project, "EN-001") == repo / ".worktrees" / "EN-001"
    assert worktrees.sweep(enso_home, project) == []
    with pytest.raises(WorktreeError, match="already checked out elsewhere"):
        worktrees.prepare(enso_home, project, "EN-001")
    assert worktrees.lookup(enso_home, "EN-001") is None
    assert (legacy / "README.md").read_bytes() == before
    assert str(legacy) in worktree_list(repo)


def test_worktree_readers_refuse_an_obsolete_database_without_writes(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    old = Paths(enso_home.home / "old")
    old.home.mkdir()
    with sqlite3.connect(old.db) as con:
        con.execute("CREATE TABLE preserved (value TEXT)")
        con.execute("INSERT INTO preserved VALUES ('keep')")
    before = old.db.read_bytes()
    with pytest.raises(db.UnreadableDatabaseError, match=r"predates 0\.2\.0"):
        worktrees.lookup(old, "EN-001")
    with pytest.raises(db.UnreadableDatabaseError, match=r"predates 0\.2\.0"):
        worktrees.sweep(old, project_for(project_config, repo))
    assert old.db.read_bytes() == before


def test_setup_context_and_ignored_copy_exclusions_with_fallback(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tasks.create(enso_home, project_config, "EN", "prepare", actor=USER)
    (repo / ".gitignore").write_text(".env\nsetup.txt\ncache/\nlinked\n")
    (repo / ".worktreeinclude").write_text(
        ".env\ncache/\n!.env.private\n!cache/secret*\nlinked/*\n.worktrees/\nREADME.md\n"
    )
    git(repo, "add", ".gitignore", ".worktreeinclude")
    git(repo, "commit", "-qm", "chore: local inputs")
    (repo / "cache").mkdir()
    (repo / "cache" / "keep").write_text("independent\n")
    (repo / "cache" / "secret-key").write_text("excluded\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("do not follow\n")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    (repo / "cache" / "link").symlink_to(outside, target_is_directory=True)

    def no_clone(source: Path, target: Path) -> None:
        raise OSError("filesystem does not support clones")

    monkeypatch.setattr(worktrees, "_clone_file", no_clone)
    project = project_for(
        project_config,
        repo,
        setup=(
            'printf "%s|%s|%s|%s" "$ENSO_TASK" "$ENSO_PROJECT" '
            '"$ENSO_BASE" "$ENSO_TASK_DIR" > "$ENSO_TASK_DIR/setup.txt"'
        ),
    )
    info = worktrees.prepare(enso_home, project, "EN-001")
    assert (info.path / "setup.txt").read_text() == f"EN-001|EN|main|{info.path}"
    assert (info.path / "cache" / "keep").read_text() == "independent\n"
    assert not (info.path / "cache" / "secret-key").exists()
    assert not (info.path / "cache" / "link").exists()
    assert not (info.path / "linked").exists()
    assert not (info.path / ".worktrees").exists()
    (info.path / "cache" / "keep").write_text("changed\n")
    assert (repo / "cache" / "keep").read_text() == "independent\n"
    event = next(
        event for event in tasks.events(enso_home, "EN-001") if event.kind == "worktree_setup"
    )
    assert event.payload["copy"]["copied"] == 2
    assert event.payload["copy"]["cloned"] == 0


def test_copy_never_writes_through_a_tracked_symlink(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    tmp_path: Path,
) -> None:
    # Source main has a real local folder, while the task's tracked version is a symlink.
    destination = tmp_path / "destination"
    destination.mkdir()
    (repo / "local").symlink_to(destination, target_is_directory=True)
    git(repo, "add", "local")
    git(repo, "commit", "-qm", "chore: link")
    (repo / "local").unlink()
    (repo / "local").mkdir()
    (repo / "local" / "file").write_text("private\n")
    with (repo / ".git" / "info" / "exclude").open("a") as file:
        file.write("\nlocal/\n")
    info = worktrees.prepare(
        enso_home, project_for(project_config, repo, copy=("local",)), "EN-001"
    )
    assert (info.path / "local").is_symlink()
    assert not (destination / "file").exists()


def test_landing_rechecks_candidate_and_target_and_preserves_switched_checkout(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    project = project_for(project_config, repo, base="main")
    info = worktrees.prepare(enso_home, project, "EN-001")
    commit_file(info.path, "feature.py", "feature = 1\n", "feat: candidate")
    with worktrees.landing_context(enso_home, project, "EN-001"):
        candidate, target = worktrees.prepare_land(enso_home, project, "EN-001")
        commit_file(repo, "later.py", "later = 1\n", "feat: target moved")
        with pytest.raises(WorktreeError, match="target changed"):
            worktrees.finish_land(enso_home, project, "EN-001", candidate, target)
        candidate, target = worktrees.prepare_land(enso_home, project, "EN-001")
        commit_file(info.path, "feature.py", "feature = 2\n", "fix: revised candidate")
        with pytest.raises(WorktreeError, match="candidate changed"):
            worktrees.finish_land(enso_home, project, "EN-001", candidate, target)
        candidate, target = worktrees.prepare_land(enso_home, project, "EN-001")
        git(repo, "checkout", "-q", "-b", "release", target)
        assert worktrees.finish_land(enso_home, project, "EN-001", candidate, target) == candidate
    assert git(repo, "branch", "--show-current").strip() == "release"
    assert git(repo, "rev-parse", "HEAD").strip() == target
    assert git(repo, "rev-parse", "main").strip() == candidate
    assert not (repo / "feature.py").exists()


def test_execution_and_landing_locks_exclude_competing_owners(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    tmp_path: Path,
) -> None:
    project = project_for(project_config, repo)
    with (
        worktrees.execution_context(enso_home, "EN-001"),
        pytest.raises(WorktreeError, match="in use"),
        worktrees.execution_context(enso_home, "EN-001"),
    ):
        pass
    # Even a separate Enso home contends on the repository's landing lock.
    with (
        worktrees.landing_context(enso_home, project, "EN-001"),
        pytest.raises(WorktreeError, match="another task is integrating"),
        worktrees.landing_context(Paths(tmp_path / "another-home"), project, "EN-002"),
    ):
        pass


def test_cleanup_waits_for_claims_events_and_execution_and_preserves_unmerged_work(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    project = project_for(project_config, repo)
    for title in ("retained", "clean"):
        tasks.create(enso_home, project_config, "EN", title, actor=USER)
    first = worktrees.prepare(enso_home, project, "EN-001")
    second = worktrees.prepare(enso_home, project, "EN-002")
    commit_file(first.path, "unmerged.py", "preserve = True\n", "feat: unfinished")
    for ref in ("EN-001", "EN-002"):
        tasks.move(
            enso_home, project_config, ref, "drop", actor=USER, run_id=None, message="cancel"
        )
    with db.transaction(enso_home) as con:
        con.execute(
            "INSERT INTO _enso_workflow_events(id,task_ref,status,data) "
            "VALUES ('pending','EN-002','pending','{}')"
        )
    assert worktrees.sweep(enso_home, project) == []
    assert first.path.exists() and second.path.exists()
    assert "not integrated" in worktrees.lookup(enso_home, "EN-001")["error"]
    with db.transaction(enso_home) as con:
        con.execute("UPDATE _enso_workflow_events SET status='failed'")
    assert worktrees.sweep(enso_home, project) == []
    with db.transaction(enso_home) as con:
        con.execute("UPDATE _enso_workflow_events SET status='passed'")
        con.execute(
            "UPDATE _enso_tasks SET claim_run_id='owner', claim_actor='owner', claim_at='now' "
            "WHERE ref='EN-002'"
        )
    assert worktrees.sweep(enso_home, project) == []
    with db.transaction(enso_home) as con:
        con.execute(
            "UPDATE _enso_tasks SET claim_run_id=NULL,claim_actor=NULL,claim_at=NULL "
            "WHERE ref='EN-002'"
        )
    with worktrees.execution_context(enso_home, "EN-002"):
        assert worktrees.sweep(enso_home, project) == []
    assert worktrees.sweep(enso_home, project) == ["EN-002"]
    assert first.path.exists() and "enso/EN-001" in branches(repo)


def test_teardown_failure_retains_work_and_success_is_not_repeated(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks.create(enso_home, project_config, "EN", "cleanup", actor=USER)
    project = project_for(project_config, repo, hooks={"teardown": "echo failed; exit 4"})
    info = worktrees.prepare(enso_home, project, "EN-001")
    tasks.move(
        enso_home, project_config, "EN-001", "drop", actor=USER, run_id=None, message="cancel"
    )
    assert worktrees.sweep(enso_home, project) == []
    assert info.path.exists()
    assert "teardown failed" in worktrees.lookup(enso_home, "EN-001")["error"]
    assert tasks.get(enso_home, "EN-001").attention
    project = replace(project, hooks={"teardown": 'echo successful >> "$ENSO_TASK_DIR/setup.txt"'})
    original = worktrees._git

    def temporarily_refuse_remove(args: list[str], **kwargs: object) -> tuple[int, str]:
        if args[:2] == ["worktree", "remove"]:
            raise WorktreeError("temporary removal refusal")
        return original(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees, "_git", temporarily_refuse_remove)
    assert worktrees.sweep(enso_home, project) == []
    assert (info.path / "setup.txt").read_text() == "successful\n"
    assert worktrees.sweep(enso_home, project) == []
    assert (info.path / "setup.txt").read_text() == "successful\n"
    monkeypatch.setattr(worktrees, "_git", original)
    assert worktrees.sweep(enso_home, project) == ["EN-001"]
    events = [
        event for event in tasks.events(enso_home, "EN-001") if event.kind == "worktree_teardown"
    ]
    assert len(events) == 2
    assert events[0].payload["event_id"] == events[1].payload["event_id"]
    assert {event.payload["status"] for event in events} == {"failed", "passed"}
    assert worktrees.lookup(enso_home, "EN-001")["status"] == "removed"


def test_cleanup_recovers_after_directory_removal_before_branch_deletion(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks.create(enso_home, project_config, "EN", "interrupted cleanup", actor=USER)
    project = project_for(project_config, repo)
    info = worktrees.prepare(enso_home, project, "EN-001")
    tasks.move(
        enso_home, project_config, "EN-001", "drop", actor=USER, run_id=None, message="cancel"
    )
    original = worktrees._git

    def fail_delete(args: list[str], **kwargs: object) -> tuple[int, str]:
        if args[:2] == ["update-ref", "-d"]:
            raise WorktreeError("interrupted before branch deletion")
        return original(args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktrees, "_git", fail_delete)
    assert worktrees.sweep(enso_home, project) == []
    assert not info.path.exists() and "enso/EN-001" in branches(repo)
    assert worktrees.lookup(enso_home, "EN-001")["status"] == "removing"
    monkeypatch.setattr(worktrees, "_git", original)
    assert worktrees.sweep(enso_home, project) == ["EN-001"]
    assert "enso/EN-001" not in branches(repo)


def test_teardown_retries_are_bounded_and_operator_reset_allows_recovery(
    enso_home: Paths,
    project_config: Config,
    repo: Path,
) -> None:
    from enso import workflows

    tasks.create(enso_home, project_config, "EN", "bounded teardown", actor=USER)
    project = project_for(
        project_config,
        repo,
        hooks={"teardown": 'echo attempted >> "$ENSO_TASK_DIR/setup.txt"; exit 1'},
    )
    info = worktrees.prepare(enso_home, project, "EN-001")
    tasks.move(
        enso_home, project_config, "EN-001", "drop", actor=USER, run_id=None, message="cancel"
    )
    for _ in range(5):
        assert worktrees.sweep(enso_home, project) == []
    assert (info.path / "setup.txt").read_text() == "attempted\n" * 3
    assert "retry limit" in worktrees.lookup(enso_home, "EN-001")["error"]
    workflows.reset(enso_home, "EN-001", "Fixed the external cleanup prerequisite")
    project = replace(
        project,
        hooks={
            "teardown": (
                'test "$ENSO_LIFECYCLE" = 1 && test "$ENSO_EVENT" = teardown '
                '&& test "$ENSO_ATTEMPT" = 1'
            )
        },
    )
    assert worktrees.sweep(enso_home, project) == ["EN-001"]
