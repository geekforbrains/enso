"""Worktrees against real temporary repositories: prepare, land, sweep, and every refusal."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import commit_file, git

from enso import tasks, worktrees
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
        project_config, repo, setup="echo ran >> setup.txt", copy=(".env", "missing.txt")
    )
    info = worktrees.prepare(enso_home, project, "EN-001")
    assert info == worktrees.WorktreeInfo(
        enso_home.worktrees / "EN" / "EN-001", "enso/EN-001", "main", True, ()
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
        worktrees.prepare(enso_home, project_config.projects["EN"], "EN-001")
    git(repo, "checkout", "-q", "--detach")
    with pytest.raises(WorktreeError, match="is detached"):
        worktrees.prepare(enso_home, project, "EN-002")


def test_failing_setup_is_an_error_and_leaves_nothing_behind(
    enso_home: Paths, project_config: Config, repo: Path
) -> None:
    project = project_for(project_config, repo, setup="echo nope >&2; exit 3")
    with pytest.raises(WorktreeError, match=r"setup failed \(exit 3\).*nope"):
        worktrees.prepare(enso_home, project, "EN-001")
    assert not (enso_home.worktrees / "EN" / "EN-001").exists()
    assert "enso/EN-001" not in branches(repo)
    # A stale directory nobody registered is not silently adopted.
    stale = enso_home.worktrees / "EN" / "EN-002"
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
    commit_file(enso_home.worktrees / "EN" / "EN-001", "a.py", "1\n", "feat: a")
    worktrees.land(enso_home, project, "EN-001")
    commit_file(enso_home.worktrees / "EN" / "EN-002", "b.py", "2\n", "feat: b")
    (enso_home.worktrees / "EN" / "EN-002" / "README.md").write_text("unfinished\n")
    (enso_home.worktrees / "EN" / "EN-004" / "untracked.txt").write_text("scratch\n")
    (enso_home.worktrees / "EN" / "EN-005" / ".env").write_text("ignored\n")
    for ref in ("EN-001", "EN-002"):
        for _ in range(3):
            tasks.move(
                enso_home, project_config, ref, "advance", actor=USER, run_id=None, message="m"
            )
    for ref in ("EN-004", "EN-005"):
        tasks.move(enso_home, project_config, ref, "drop", actor=USER, run_id=None, message="no")
    (enso_home.worktrees / "EN" / "stray").mkdir()  # not a task reference: never touched

    # EN-004 is finished but holds a file no branch ever had: the sweep must not destroy it.
    # EN-005 holds only an ignored file (a copied .env), which is not work and does not count.
    assert worktrees.sweep(enso_home, project) == ["EN-001", "EN-005"]
    remaining = sorted(p.name for p in (enso_home.worktrees / "EN").iterdir())
    assert remaining == ["EN-002", "EN-003", "EN-004", "stray"]
    assert (enso_home.worktrees / "EN" / "EN-004" / "untracked.txt").read_text() == "scratch\n"
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
    assert (enso_home.worktrees / "EN" / "EN-004" / "untracked.txt").read_text() == "scratch\n"
    assert worktrees._unclean is original


def test_sweep_asks_git_nothing_while_the_project_has_no_worktrees(
    enso_home: Paths, project_config: Config, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = project_for(project_config, repo)
    calls: list[list[str]] = []
    monkeypatch.setattr(worktrees, "_run", lambda cmd, **kwargs: calls.append(cmd) or (0, ""))
    assert worktrees.sweep(enso_home, project) == []  # no directory at all
    (enso_home.worktrees / "EN").mkdir(parents=True)
    assert worktrees.sweep(enso_home, project) == []  # an empty one
    assert calls == []
    (enso_home.worktrees / "EN" / "stray").mkdir()
    with pytest.raises(WorktreeError):  # the fake answers nothing, so the base check refuses
        worktrees.sweep(enso_home, project)
    assert calls and calls[0][:2] == ["git", "worktree"]  # something to look at: Git is asked


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
