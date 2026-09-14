"""``enso task``: the surface, ``--json`` shapes, exit codes, stdin messages, the run guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import commit_file, git, write_config
from typer.testing import CliRunner

from enso import db, tasks, worktrees
from enso.cli import app
from enso.cli.common import INPUT_LIMIT
from enso.config import Config, Paths, load_config

runner = CliRunner()


def run(*args: str, input: str | None = None) -> tuple[int, str, str]:
    result = runner.invoke(app, ["task", *args], input=input)
    return result.exit_code, result.stdout, result.stderr


def add(title: str = "Fix fences", *extra: str) -> dict:
    code, out, err = run("add", title, "--project", "en", "--json", *extra)
    assert code == 0, err
    return json.loads(out)


def test_add_show_and_list(enso_home: Paths, project_config: Config, tmp_path: Path) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nDo the thing.\n")
    task = add("Fix fences", "--body-file", str(spec), "--priority", "2")
    assert {k: task[k] for k in ("ref", "stage", "title", "body", "priority", "claim_run_id")} == {
        "ref": "EN-001",
        "stage": "triage",
        "title": "Fix fences",
        "body": "# Spec\n\nDo the thing.",
        "priority": 2,
        "claim_run_id": None,
    }
    code, out, _ = run(
        "add",
        "From stdin",
        "--project",
        "EN",
        "--body-file",
        "-",
        "--backlog",
        "--from",
        "en-1",
        input="piped body\n",
    )
    assert code == 0 and out == "created EN-002: backlog — From stdin\n"
    assert tasks.get(enso_home, "EN-002").body == "piped body"
    code, _, err = run("add", "x", "--project", "EN", "--body", "a", "--body-file", str(spec))
    assert code == 1 and err == "error: give --body or --body-file, not both\n"
    code, out, _ = run("add", "x", "--project", "ZZ", "--json")
    assert code == 1 and json.loads(out) == {"ok": False, "error": "project ZZ is not configured"}

    code, out, _ = run("list")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["REF", "STAGE", "PRIORITY", "CLAIM", "IN", "STAGE", "TITLE"]
    assert lines[1].startswith("EN-001  triage   2         -      ") and lines[1].endswith(
        "Fix fences"
    )
    assert lines[2].startswith("EN-002  backlog  0")
    code, out, _ = run("list", "--stage", "backlog", "--json")
    assert code == 0 and [t["ref"] for t in json.loads(out)] == ["EN-002"]
    code, out, _ = run("list", "--ready", "--idle-for", "1d")
    assert code == 0 and out == "no tasks match\n"
    code, _, err = run("list", "--idle-for", "soon")
    assert code == 1 and "'soon' is not a duration such as 30m, 2h, or 1d" in err

    code, out, _ = run("show", "en-1")
    assert code == 0
    assert out.splitlines()[:4] == [
        "EN-001: Fix fences",
        "project: EN (Enso; stages triage, todo, review)",
        "stage: triage",
        "priority: 2",
    ]
    assert "  advance to todo: available" in out
    assert "  return: not available: triage is the first stage" in out
    assert "\n# Spec\n\nDo the thing.\n" in out
    assert out.splitlines()[-1].endswith(" created by user:") or " created" in out.splitlines()[-1]
    code, out, _ = run("show", "EN-1", "--json")
    packet = json.loads(out)
    assert packet["ref"] == "EN-001" and packet["moves"][0]["id"] == "advance"
    assert [e["kind"] for e in packet["events"]] == ["created"] and packet["events_total"] == 1
    code, out, _ = run("show", "EN-9", "--json")
    assert code == 1 and json.loads(out) == {"ok": False, "error": "no task EN-009"}


def test_moves_from_the_terminal(enso_home: Paths, project_config: Config) -> None:
    add()
    code, _, err = run("advance", "EN-1")
    assert code == 2 and "--message" in err  # typer: the flag is required
    code, out, _ = run(
        "advance", "EN-1", "--message", "-", "--ref", "commit:abc", input="scoped it\n"
    )
    assert code == 0 and out == "advance EN-001: todo — Fix fences\n"
    assert [(r.kind, r.value) for r in tasks.refs(enso_home, "EN-001")] == [("commit", "abc")]
    code, out, _ = run("advance", "EN-1", "--message", "m", "--ref", "nocolon", "--json")
    assert code == 1 and json.loads(out) == {
        "ok": False,
        "error": "--ref takes KIND:VALUE, got 'nocolon'",
    }
    code, out, _ = run("return", "EN-1", "--message", "redo", "--json")
    assert code == 0 and json.loads(out)["stage"] == "triage"
    code, _, err = run("return", "EN-1", "--message", "again")
    assert code == 1 and err == "error: cannot return EN-001: triage is the first stage\n"
    add("dependency")
    code, out, _ = run("block", "EN-1", "--message", "needs EN-002", "--after", "en-2")
    assert code == 0 and tasks.get(enso_home, "EN-001").after_ref == "EN-002"
    code, out, _ = run("resume", "EN-1", "--to", "review", "--json")
    assert code == 0 and json.loads(out)["stage"] == "review"
    code, out, _ = run("drop", "EN-2", "--message", "-", input="not needed")
    assert code == 0 and out == "drop EN-002: cancelled — dependency\n"
    code, _, err = run("drop", "EN-2", "--message", "again")
    assert code == 1 and err == "error: cannot drop EN-002: EN-002 is cancelled\n"


def test_inside_a_run_the_environment_is_the_actor(
    enso_home: Paths, project_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    add()
    claimed = tasks.take(enso_home, project_config, "EN", "triage", run_id="r1", actor="job:t")
    assert claimed is not None
    monkeypatch.setenv("ENSO_JOB", "dev-triage")
    monkeypatch.setenv("ENSO_RUN_ID", "r1")
    code, _, err = run("drop", "EN-1", "--message", "give up")
    assert (
        code == 1
        and err == "error: only a person can drop a task; block it with your reasoning instead\n"
    )
    code, _, err = run("advance", "EN-1", "--message", "m", "--force")
    assert code == 1 and "a run cannot force" in err
    code, out, _ = run("note", "EN-1", "-", "--attention", input="odd\n")
    assert code == 0 and out == "noted EN-001\n"
    code, out, _ = run("ref", "en-1", "path", "src/x.py", "--json")
    assert code == 0 and json.loads(out) == {
        "kind": "path",
        "value": "src/x.py",
        "actor": "job:dev-triage",
        "run_id": "r1",
        "created_at": json.loads(out)["created_at"],
    }
    code, out, _ = run("show", "EN-1", "--json")
    assert [m["id"] for m in json.loads(out)["moves"]] == ["advance", "return", "block", "resume"]
    code, out, _ = run("advance", "EN-1", "--message", "handoff", "--json")
    assert code == 0
    moved = json.loads(out)
    assert (moved["stage"], moved["claim_run_id"]) == ("todo", None)
    event = tasks.events(enso_home, "EN-001")[0]
    assert (event.actor, event.run_id, event.message) == ("job:dev-triage", "r1", "handoff")

    monkeypatch.setenv("ENSO_RUN_ID", "r2")  # another run must respect a claim it does not hold
    tasks.take(enso_home, project_config, "EN", "todo", run_id="r1", actor="job:t")
    code, _, err = run("advance", "EN-1", "--message", "m")
    assert code == 1 and err == "error: cannot advance EN-001: EN-001 is claimed by run r1\n"
    code, _, err = run("release", "EN-1", "--message", "m")
    assert code == 1 and "claimed by run r1" in err
    monkeypatch.setenv("ENSO_RUN_ID", "r1")
    code, out, _ = run("release", "EN-1", "--message", "stopping early", "--json")
    assert code == 0 and json.loads(out)["claim_run_id"] is None
    # A deliberate release is never the runner's run_ended, so it is not reported as recovery.
    assert tasks.events(enso_home, "EN-001")[0].payload == {
        "reason": "manual",
        "released_run_id": "r1",
    }
    assert tasks.context(enso_home, project_config, "EN-001", env={})["recovery"] is None


def test_a_person_forces_and_edits(
    enso_home: Paths, project_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "gavin")
    add()
    tasks.take(enso_home, project_config, "EN", "triage", run_id="r1", actor="job:t")
    code, _, err = run("edit", "EN-1", "--title", "New")
    assert code == 1 and err == "error: EN-001 is claimed by run r1; wait for it, or use --force\n"
    code, out, _ = run(
        "edit",
        "EN-1",
        "--title",
        "New",
        "--body-file",
        "-",
        "--priority",
        "3",
        "--force",
        "--json",
        input="new body",
    )
    assert code == 0
    edited = json.loads(out)
    assert (edited["title"], edited["body"], edited["priority"]) == ("New", "new body", 3)
    assert tasks.events(enso_home, "EN-001")[0].actor == "user:gavin"
    code, _, err = run("edit", "EN-1")
    assert code == 1 and "nothing to edit" in err
    code, _, err = run("edit", "EN-1", "--body-file", str(tmp_path / "missing.md"))
    assert code == 1 and err.startswith("error: could not read ")
    code, out, _ = run("release", "EN-1", "--message", "taking it back", "--force")
    assert code == 0 and out == "released EN-001: triage — New\n"
    assert tasks.events(enso_home, "EN-001")[0].payload["reason"] == "manual"
    code, out, _ = run("ref", "EN-1", "url", "https://example.test/x")
    assert code == 0 and out == "EN-001: url https://example.test/x\n"
    code, out, _ = run("show", "EN-1")
    assert "refs:\n  url https://example.test/x\n" in out
    assert "  drop to cancelled: available" in out


@pytest.fixture
def repo_config(enso_home: Paths, raw_config_projects: dict, repo: Path) -> Config:
    """The two projects with ``EN`` bound to a real repository, written and migrated."""
    raw_config_projects["projects"]["EN"]["repo"] = str(repo)
    write_config(enso_home, raw_config_projects)
    config = load_config(enso_home)
    db.migrate(enso_home)
    return config


def test_advance_refuses_a_dirty_worktree_then_land_and_sweep(
    enso_home: Paths, repo_config: Config, repo: Path
) -> None:
    add()
    project = repo_config.projects["EN"]
    info = worktrees.prepare(enso_home, project, "EN-001")
    (info.path / "README.md").write_text("half done\n")
    code, out, _ = run("advance", "EN-1", "--message", "m", "--json")
    assert code == 1 and json.loads(out) == {
        "ok": False,
        "error": "cannot advance EN-001: its worktree has uncommitted changes: README.md; "
        "commit or discard them first",
    }
    assert tasks.get(enso_home, "EN-001").stage == "triage"
    code, _, err = run("land", "EN-1")
    assert code == 1 and err == "error: worktree " + str(info.path) + (
        " has uncommitted changes: README.md\n"
    )
    head = commit_file(info.path, "README.md", "done\n", "docs: finish")
    code, out, _ = run("advance", "EN-1", "--message", "committed")
    assert code == 0 and out == "advance EN-001: todo — Fix fences\n"

    code, out, _ = run("land", "en-1", "--json")
    assert code == 0 and json.loads(out) == {
        "ok": True,
        "ref": "EN-001",
        "base": "main",
        "head": head,
    }
    assert git(repo, "rev-parse", "HEAD").strip() == head
    assert [(r.kind, r.value) for r in tasks.refs(enso_home, "EN-001")] == [("commit", head)]
    code, _, err = run("land", "EN-1")
    assert code == 1 and err == "error: enso/EN-001 has nothing main lacks; it is already landed\n"
    code, _, err = run("land", "MKT-1")
    assert code == 1 and err == "error: no task MKT-001\n"
    tasks.create(enso_home, repo_config, "MKT", "Launch", actor="user:gavin")
    code, _, err = run("land", "MKT-1")
    assert code == 1 and err == "error: project MKT has no repository; nothing to land\n"

    code, out, _ = run("sweep")
    assert code == 0 and out == "nothing to sweep\n"  # EN-001 is not finished yet
    for _ in range(2):
        assert run("advance", "EN-1", "--message", "m")[0] == 0
    code, out, _ = run("sweep", "--project", "en", "--json")
    assert code == 0 and json.loads(out) == {"ok": True, "removed": {"EN": ["EN-001"]}}
    assert not info.path.exists()
    code, out, _ = run("sweep")
    assert code == 0 and out == "nothing to sweep\n"
    code, _, err = run("sweep", "--project", "ZZ")
    assert code == 1 and err == "error: project ZZ is not configured\n"


def test_land_inside_a_run_is_for_the_holding_run_in_the_last_agent_stage(
    enso_home: Paths, repo_config: Config, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add()
    info = worktrees.prepare(enso_home, repo_config.projects["EN"], "EN-001")
    head = commit_file(info.path, "feature.py", "x\n", "feat: one")
    monkeypatch.setenv("ENSO_JOB", "dev-todo")
    monkeypatch.setenv("ENSO_RUN_ID", "r1")
    code, _, err = run("land", "EN-1")  # a run lands only the task it holds
    assert code == 1 and err == (
        "error: EN-001 is not held by this run; a run moves only the task it claimed\n"
    )
    assert tasks.take(enso_home, repo_config, "EN", "triage", run_id="r1", actor="job:t")
    code, _, err = run("land", "EN-1")  # triage: not the landing stage
    assert code == 1 and err == (
        "error: EN-001 is in triage; only the review stage lands a branch, advance it there first\n"
    )
    assert git(repo, "rev-parse", "HEAD").strip() != head
    assert run("advance", "EN-1", "--message", "m")[0] == 0
    code, _, err = run("advance", "EN-1", "--message", "again")  # the handoff ended its standing
    assert code == 1 and err == (
        "error: cannot advance EN-001: EN-001 is not held by this run; "
        "a run moves only the task it claimed\n"
    )
    assert tasks.take(enso_home, repo_config, "EN", "todo", run_id="r1", actor="job:t")
    assert run("advance", "EN-1", "--message", "m")[0] == 0
    assert tasks.take(enso_home, repo_config, "EN", "review", run_id="r1", actor="job:t")
    code, out, _ = run("land", "EN-1")
    assert code == 0 and out == f"landed EN-001: main is now at {head}\n"


def test_stdin_input_is_bounded(enso_home: Paths, project_config: Config) -> None:
    """``--body-file -`` and ``-`` messages stop at INPUT_LIMIT bytes in both output forms."""
    big = "x" * (INPUT_LIMIT + 1)
    code, out, err = run("add", "Big", "--project", "en", "--body-file", "-", "--json", input=big)
    assert code == 1 and err == ""
    assert json.loads(out) == {"ok": False, "error": f"input exceeds {INPUT_LIMIT} bytes"}
    add()
    code, out, err = run("note", "EN-1", "-", input=big)
    assert code == 1 and out == "" and err == f"error: input exceeds {INPUT_LIMIT} bytes\n"
    code, out, _ = run("note", "EN-1", "-", input="x" * INPUT_LIMIT)
    assert code == 0 and out == "noted EN-001\n"
