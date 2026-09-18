"""The workspace audit: every check, what ``--fix`` may and may not do, the JSON shape."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from conftest import write_config, write_job, write_project
from test_skills import write_skill

from enso import audit, layout, workspaces
from enso.config import Config, Paths, parse_config

USER_DIRS: list[Path] = []  # no user-level skills unless a test says so
# What the scaffold leaves at each root's top level, with the category the audit reports.
SEEDED_HOME_LAYOUT = {
    ".agents": "required",
    ".bundles.json": "managed",
    ".claude": "required",
    ".git": "required",
    ".migrations.json": "managed",
    "AGENTS.md": "required",
    "CLAUDE.md": "required",
    "knowledge": "required",
    "skills": "required",
    "workspaces": "required",
}
CLEAN_WORKSPACE_LAYOUT = {
    name: "required"
    for name in (
        ".agents",
        ".claude",
        "AGENTS.md",
        "CLAUDE.md",
        *layout.WORKSPACE_DIRS,
    )
}


def finish(root: Path) -> None:
    """Make a bare workspace directory pass: the layout, and an AGENTS.md that was edited."""
    workspaces.ensure_layout(root)
    (root / "AGENTS.md").write_text("# a real workspace\n")


def break_workspace(root: Path, enso_home: Paths) -> None:
    """One of everything the audit reports; ``AGENTS.md`` is left missing."""
    for name in ("memory", "jobs", "projects"):
        (root / name).mkdir()
    (root / "CLAUDE.md").write_text("a copy, not a link")
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".agents").mkdir()
    os.symlink("/nowhere", root / ".agents" / "skills")
    (root / "knowledge").write_text("a file where a directory belongs")
    (root / "skills" / "broken").mkdir(parents=True)  # no SKILL.md
    write_skill(root / "skills", "research")
    write_skill(enso_home.skills, "research")  # the managed collision
    (root / ".git").mkdir()
    (root / "stray.txt").write_text("")
    (root / "notes").mkdir()
    (root / ".DS_Store").write_text("")  # OS noise, never reported


@pytest.mark.parametrize("entry", ["memory", "jobs", "projects", "heartbeat", ".agents"])
def test_fix_preserves_linked_workspace_paths_without_writing_through_them(
    enso_home,
    tmp_path,
    entry,
):
    root = enso_home.workspace("default")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.md"
    marker.write_text("User-owned content\n")
    (root / entry).symlink_to(outside, target_is_directory=True)
    settings = root / "WORKSPACE.md"
    settings.write_text("---\n{}\n---\n\nKeep this explanation.\n")
    report = audit.audit_workspace(enso_home, "default", fix=True, user_dirs=[])
    assert not report.ok
    assert (root / entry).is_symlink()
    assert list(outside.iterdir()) == [marker]
    assert marker.read_text() == "User-owned content\n"
    assert settings.read_text().endswith("Keep this explanation.\n")
    assert not any(f.check == "unexpected" for f in report.findings)


def test_fix_never_scaffolds_a_linked_workspace(enso_home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = enso_home.workspace("alias")
    root.symlink_to(outside, target_is_directory=True)
    report = audit.audit_workspace(enso_home, "alias", fix=True, user_dirs=[])
    assert not report.ok and report.fixed == []
    assert list(outside.iterdir()) == []
    with pytest.raises(FileExistsError):
        workspaces.create_workspace(enso_home, "alias")


def test_a_clean_home_and_workspaces_pass(enso_home: Paths, config: Config) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    meteor = workspaces.create_workspace(enso_home, "meteor")
    (meteor / "AGENTS.md").write_text("# meteor\n")
    write_job(enso_home, workspace="meteor")
    (meteor / "uploads" / "ab12").mkdir()
    (meteor / "uploads" / "ab12" / "photo.jpg").write_bytes(b"x" * 1500)

    report = audit.audit(enso_home, config=config, user_dirs=USER_DIRS)

    assert report.ok and report.home.findings == [] and report.home.fixed == []
    default, found = report.workspaces
    assert (default.name, default.bindings, default.jobs) == (
        "default", ["slack:C1", "slack:dm:U1"], [],
    )  # fmt: skip
    assert (found.name, found.bindings, found.jobs, found.uploads_bytes) == (
        "meteor", [], ["meteor:nightly"], 1500,
    )  # fmt: skip
    assert all(w.ok and w.status == "ok" and w.summary == "ok" for w in report.workspaces)
    assert json.loads(json.dumps(report.as_dict())) == {
        "ok": True,
        "attention": False,
        "home": {
            "path": str(enso_home.home),
            "status": "ok",
            "attention": False,
            "layout": SEEDED_HOME_LAYOUT,
            "findings": [],
            "fixed": [],
        },
        "workspaces": [
            {
                "name": "default",
                "path": str(enso_home.workspace("default")),
                "status": "ok",
                "attention": False,
                "bindings": ["slack:C1", "slack:dm:U1"],
                "jobs": [],
                "uploads_bytes": 0,
                "layout": CLEAN_WORKSPACE_LAYOUT,
                "findings": [],
                "fixed": [],
            },
            {
                "name": "meteor",
                "path": str(meteor),
                "status": "ok",
                "attention": False,
                "bindings": [],
                "jobs": ["meteor:nightly"],
                "uploads_bytes": 1500,
                "layout": CLEAN_WORKSPACE_LAYOUT,
                "findings": [],
                "fixed": [],
            },
        ],
    }


def test_every_check_on_a_broken_workspace(
    enso_home: Paths, config: Config, tmp_path: Path
) -> None:
    workspaces.seed_home(enso_home)
    root = enso_home.workspace("default")
    break_workspace(root, enso_home)
    write_skill(root / "skills", "notes")
    user = tmp_path / ".claude" / "skills"
    write_skill(user, "notes")  # the user-level collision

    (found,) = audit.audit(enso_home, config=config, user_dirs=[user]).workspaces

    assert [(f.check, f.severity, f.fixable) for f in found.findings] == [
        ("directory", "error", False),  # knowledge/ is a file
        ("directory", "error", True),  # drafts/
        ("directory", "error", True),  # uploads/
        ("link", "error", False),  # CLAUDE.md is a file
        ("link", "error", False),  # .claude/skills is a directory
        ("link", "error", True),  # .agents/skills points elsewhere
        ("agents-md", "error", False),
        ("git-root", "error", False),
        ("skill", "error", False),  # broken/ has no SKILL.md
        ("skill-collision", "warning", False),  # notes, against the user copy
        ("skill-collision", "error", False),  # research, against the enso copy
        ("unexpected", "warning", False),  # notes/
        ("unexpected", "warning", False),  # stray.txt
    ]
    messages = [f.message for f in found.findings]
    assert messages[0] == "knowledge/ is a file, not a directory"
    assert messages[3].startswith("CLAUDE.md is a real file, not a symlink to AGENTS.md")
    assert messages[4].startswith(".claude/skills is a real directory, not a symlink to ../skills")
    assert messages[5] == ".agents/skills points to /nowhere, not ../skills"
    assert messages[8] == "skills/broken: SKILL.md is missing"
    assert messages[9].startswith("skills/notes also exists in the user scope")
    assert messages[10].startswith("skills/research also exists in the enso scope")
    assert messages[11:] == [
        f"notes/ is not part of the layout; move it out of {root} or remove it",
        f"stray.txt is not part of the layout; move it out of {root} or remove it",
    ]
    # Classified, not only reported: ``.git`` has no place here, whatever check names it.
    assert found.layout[".git"] == "unexpected" and found.layout["skills"] == "required"
    assert not found.ok and found.status == "error" and found.summary == "10 errors, 3 warnings"
    assert found.bindings == ["slack:C1", "slack:dm:U1"]  # bound, so no orphan warning


def test_fix_creates_and_repairs_but_never_deletes(enso_home: Paths, config: Config) -> None:
    workspaces.seed_home(enso_home)
    root = enso_home.workspace("default")
    break_workspace(root, enso_home)

    report = audit.audit(enso_home, fix=True, config=config, user_dirs=USER_DIRS)

    (found,) = report.workspaces
    assert [line.split(" ", 1)[0] for line in found.fixed] == ["created", "created", "repointed"]
    assert (root / "drafts").is_dir() and (root / "uploads").is_dir()
    assert os.readlink(root / ".agents" / "skills") == "../skills"
    remaining = [(f.check, f.fixable) for f in found.findings]
    assert remaining and not any(fixable for _, fixable in remaining)
    assert [check for check, _ in remaining] == [
        "directory", "link", "link", "agents-md", "git-root", "skill", "skill-collision",
        "unexpected", "unexpected",
    ]  # fmt: skip
    # Nothing was removed or rewritten, whatever was in the way.
    assert (root / "CLAUDE.md").read_text() == "a copy, not a link"
    assert (root / ".claude" / "skills").is_dir() and not (root / ".claude" / "skills").is_symlink()
    assert (root / "knowledge").read_text() == "a file where a directory belongs"
    assert (root / ".git").is_dir() and (root / "stray.txt").exists() and (root / "notes").is_dir()
    assert not (root / "AGENTS.md").exists()

    again = audit.audit(enso_home, fix=True, config=config, user_dirs=USER_DIRS)
    assert again.workspaces[0].fixed == [] and again.workspaces[0].findings == found.findings


def test_untouched_template_and_orphan_are_warnings(enso_home: Paths, config: Config) -> None:
    workspaces.seed_home(enso_home)
    workspaces.create_workspace(enso_home, "lonely")

    (lonely,) = audit.audit(enso_home, ["lonely"], config=config, user_dirs=USER_DIRS).workspaces

    assert lonely.ok and lonely.status == "warning" and lonely.summary == "2 warnings"
    assert [(f.check, f.message) for f in lonely.findings] == [
        ("agents-md", "AGENTS.md is still the untouched template; say what the workspace is for"),
        ("orphan", "nothing is bound to this workspace and no job names it"),
    ]
    # Without a readable config nobody knows what is bound, so the orphan check is skipped.
    (unknown,) = audit.audit(enso_home, ["lonely"], user_dirs=USER_DIRS).workspaces
    assert [f.check for f in unknown.findings] == ["agents-md"]
    # A job naming it is enough.
    write_job(enso_home, workspace="lonely")
    (named,) = audit.audit(enso_home, ["lonely"], config=config, user_dirs=USER_DIRS).workspaces
    assert named.jobs == ["lonely:nightly"] and [f.check for f in named.findings] == ["agents-md"]


def test_project_scripts_that_cannot_run_are_warnings(enso_home: Paths, raw_config: dict) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    write_project(
        enso_home,
        "EN",
        {
            "name": "Enso",
            "setup": "./setup.sh",
            "stages": [
                {"name": "build", "command": "./build.sh"},
                {"name": "work", "checks": [{"name": "tests", "command": "./test.sh"}]},
            ],
            "hooks": {"after:done": "./done.sh"},
        },
    )
    directory = enso_home.project("default", "EN")
    (directory / "setup.sh").write_text("#!/bin/sh\n")  # present, not executable
    (directory / "build.sh").mkdir()
    (directory / "test.sh").symlink_to("nowhere")  # dangling
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems

    (found,) = audit.audit(enso_home, ["default"], config=config, user_dirs=USER_DIRS).workspaces

    assert found.ok and found.attention and found.summary == "4 warnings"
    assert [(f.check, f.fixable, f.message) for f in found.findings] == [
        ("script", False, "projects/EN: setup runs ./setup.sh, which is not an executable file"),
        (
            "script",
            False,
            "projects/EN: stage build runs ./build.sh, which is not an executable file",
        ),
        ("script", False, "projects/EN: check tests runs ./test.sh, which does not exist"),
        ("script", False, "projects/EN: hook after:done runs ./done.sh, which does not exist"),
    ]
    # Without a readable config nobody knows the project's commands, so the check is skipped.
    (unknown,) = audit.audit(enso_home, ["default"], user_dirs=USER_DIRS).workspaces
    assert unknown.findings == []
    # --fix never writes a script: a stub could only guess.
    (fixed,) = audit.audit(
        enso_home, ["default"], fix=True, config=config, user_dirs=USER_DIRS
    ).workspaces
    assert fixed.fixed == [] and fixed.findings == found.findings
    assert not (directory / "done.sh").exists()

    shutil.rmtree(directory / "build.sh")
    (directory / "test.sh").unlink()
    for name in ("setup.sh", "build.sh", "test.sh", "done.sh"):
        (directory / name).write_text("#!/bin/sh\n")
        (directory / name).chmod(0o755)
    (healthy,) = audit.audit(enso_home, ["default"], config=config, user_dirs=USER_DIRS).workspaces
    assert healthy.findings == []


@pytest.mark.parametrize(
    "command, inspected",
    [
        ("./test.sh", True),
        ("./scripts/test.sh", True),
        ("./test.sh --verbose", False),
        ("bash ./test.sh", False),
        ("npm test", False),
        ("cd x && ./test.sh", False),
        ("test.sh", False),
    ],
)
def test_only_a_bare_script_reference_is_inspected(
    enso_home: Paths, raw_config: dict, command: str, inspected: bool
) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    write_project(
        enso_home,
        "EN",
        {
            "name": "Enso",
            "stages": [{"name": "work", "checks": [{"name": "t", "command": command}]}],
        },
    )
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems

    (found,) = audit.audit(enso_home, ["default"], config=config, user_dirs=USER_DIRS).workspaces

    assert [f.check for f in found.findings] == (["script"] if inspected else [])


@pytest.mark.parametrize(
    "stage",
    [
        {"name": "run", "command": "true"},
        {"name": "run", "integrate": True},
    ],
    ids=["command", "integrate"],
)
def test_audit_finds_engine_stage_jobs_with_config(
    enso_home: Paths, raw_config: dict, repo: Path, stage: dict
) -> None:
    workspaces.seed_home(enso_home)
    stage_root = workspaces.create_workspace(enso_home, "stage-only")
    workspaces.create_workspace(enso_home, "ordinary")
    write_project(
        enso_home,
        "DEMO",
        {"name": "Demo", "repo": str(repo), "stages": [stage]},
        workspace="stage-only",
    )
    write_job(
        enso_home,
        "run",
        workspace="stage-only",
        omit=["schedule", "provider", "model", "effort"],
        project="DEMO",
        stage="run",
        prompt="",
    )
    write_job(enso_home, workspace="ordinary")
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is not None, problems

    stage_audit, ordinary_audit = audit.audit(
        enso_home, ["stage-only", "ordinary"], config=config, user_dirs=USER_DIRS
    ).workspaces
    assert stage_audit.jobs == ["stage-only:run"]
    assert ordinary_audit.jobs == ["ordinary:nightly"]
    assert [finding.check for finding in stage_audit.findings] == ["agents-md"]
    assert [finding.check for finding in ordinary_audit.findings] == ["agents-md"]

    stage_without_config, ordinary_without_config = audit.audit(
        enso_home, ["stage-only", "ordinary"], user_dirs=USER_DIRS
    ).workspaces
    assert stage_without_config.jobs == []
    assert ordinary_without_config.jobs == ["ordinary:nightly"]
    assert [finding.check for finding in stage_without_config.findings] == ["agents-md"]

    (stage_root / "CLAUDE.md").unlink()
    assert any(
        line.startswith("workspace stage-only fails its audit:") and "CLAUDE.md" in line
        for line in audit.startup_warnings(enso_home, config)
    )


def test_a_relocated_opencode_root_reaches_the_collision_check(
    enso_home: Paths, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``user_dirs`` the audit uses the shared resolver, so OpenCode's roots count."""
    workspaces.seed_home(enso_home)
    root = enso_home.workspace("default")
    finish(root)
    write_skill(root / "skills", "notes")
    scratch = tmp_path / "user"
    scratch.mkdir()
    monkeypatch.setenv("HOME", str(scratch))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode"))
    write_skill(tmp_path / "opencode" / "skills", "notes")  # the user-level collision

    (found,) = audit.audit(enso_home, ["default"], config=config).workspaces

    assert [(f.check, f.severity) for f in found.findings] == [("skill-collision", "warning")]
    assert found.findings[0].message.startswith("skills/notes also exists in the user scope")


def test_a_missing_or_misnamed_workspace_is_one_finding(enso_home: Paths) -> None:
    (gone,) = audit.audit(enso_home, ["gone"], user_dirs=USER_DIRS).workspaces
    assert [(f.check, f.severity) for f in gone.findings] == [("directory", "error")]
    (bad,) = audit.audit(enso_home, ["Bad Name"], user_dirs=USER_DIRS).workspaces
    assert [(f.check, f.severity) for f in bad.findings] == [("unexpected", "error")]


def test_home_checks_and_fix(enso_home: Paths) -> None:
    home = enso_home.home  # the fixture makes only workspaces/default
    (enso_home.workspaces / "Bad Name").mkdir()
    (enso_home.workspaces / "stray.txt").write_text("")
    (enso_home.workspaces / ".DS_Store").write_text("")

    before = audit.audit_home(enso_home, user_dirs=USER_DIRS)
    assert [(f.check, f.severity, f.fixable) for f in before.findings] == [
        ("git-root", "error", True),
        ("directory", "error", True),
        ("directory", "error", True),
        ("link", "error", True),
        ("link", "error", True),
        ("link", "error", True),
        ("agents-md", "error", False),
        ("unexpected", "warning", False),
        ("unexpected", "warning", False),
    ]
    assert before.findings[-2].message.startswith("workspaces/Bad Name/ is not a workspace name")
    assert before.findings[-1].message == "workspaces/stray.txt is a file, not a workspace"

    after = audit.audit_home(enso_home, fix=True, user_dirs=USER_DIRS)
    assert [line.split(" ", 1)[0] for line in after.fixed] == [
        "created", "created", "linked", "linked", "linked", "ran",
    ]  # fmt: skip
    assert (home / ".git").is_dir() and os.readlink(home / ".claude" / "skills") == "../skills"
    assert [f.check for f in after.findings] == ["agents-md", "unexpected", "unexpected"]
    assert not after.ok  # AGENTS.md is content, and --fix never writes content

    enso_home.agents_md.write_text("# Enso\n")
    write_skill(enso_home.skills, "jobs", front_name="job")  # an enso-scope problem
    final = audit.audit_home(enso_home, fix=True, user_dirs=USER_DIRS)
    assert final.fixed == [] and [f.check for f in final.findings] == [
        "skill", "unexpected", "unexpected",
    ]  # fmt: skip
    assert (
        final.findings[0].message
        == "skills/jobs: SKILL.md name is 'job' but the directory is 'jobs'"
    )


def test_a_file_where_a_dot_directory_belongs_is_reported_not_fixed(enso_home: Paths) -> None:
    root = enso_home.workspace("default")
    finish(root)
    shutil.rmtree(root / ".agents")
    (root / ".agents").write_text("in the way")

    (found,) = audit.audit(enso_home, ["default"], fix=True, user_dirs=USER_DIRS).workspaces

    assert found.fixed == []
    assert [(f.check, f.message, f.fixable) for f in found.findings] == [
        ("link", ".agents is a file, not a directory; move it aside", False)
    ]
    assert (root / ".agents").read_text() == "in the way"


@pytest.mark.parametrize("scope", ["home", "workspace"])
@pytest.mark.parametrize("kind", ["file", "symlink", "dangling-symlink"])
def test_knowledge_roots_preserve_conflicts_and_never_follow_links(
    enso_home: Paths, tmp_path: Path, scope: str, kind: str
) -> None:
    workspaces.seed_home(enso_home)
    root = enso_home.workspace("default")
    finish(root)
    knowledge = enso_home.knowledge if scope == "home" else root / "knowledge"
    knowledge.rmdir()
    outside = tmp_path / "outside"
    if kind == "file":
        knowledge.write_text("keep this file\n")
    else:
        if kind == "symlink":
            outside.mkdir()
            (outside / "Keep.md").write_text("keep this note\n")
        knowledge.symlink_to(outside, target_is_directory=True)

    report = audit.audit(enso_home, ["default"], fix=True, user_dirs=USER_DIRS)

    findings = report.home.findings if scope == "home" else report.workspaces[0].findings
    assert len(findings) == 1
    assert findings[0].check == "directory" and not findings[0].fixable
    if kind == "file":
        assert knowledge.read_text() == "keep this file\n"
    else:
        assert knowledge.is_symlink() and knowledge.readlink() == outside
        if kind == "symlink":
            assert (outside / "Keep.md").read_text() == "keep this note\n"
        else:
            assert not outside.exists()


@pytest.mark.parametrize("scope, name", [("home", "skills"), ("workspace", "drafts")])
def test_dangling_links_where_directories_belong_are_reported_not_fixed(
    enso_home: Paths, tmp_path: Path, scope: str, name: str
) -> None:
    workspaces.seed_home(enso_home)
    root = enso_home.workspace("default")
    finish(root)
    directory = (enso_home.home if scope == "home" else root) / name
    shutil.rmtree(directory)
    directory.symlink_to(tmp_path / "gone", target_is_directory=True)

    for _ in range(2):  # the second fixing run must find the same non-repairable state
        report = audit.audit(enso_home, ["default"], fix=True, user_dirs=USER_DIRS)
        assert report.home.fixed == [] and report.workspaces[0].fixed == []
        findings = report.home.findings if scope == "home" else report.workspaces[0].findings
        assert [(f.message, f.fixable) for f in findings if f.check == "directory"] == [
            (f"{name}/ is a dangling symbolic link; move it aside", False)
        ]
    assert directory.is_symlink() and not directory.exists()


def test_missing_shared_knowledge_is_created_without_changing_existing_notes(enso_home: Paths):
    workspaces.seed_home(enso_home)
    enso_home.knowledge.rmdir()
    assert audit.audit_home(enso_home, user_dirs=USER_DIRS).findings[0].fixable
    result = audit.audit_home(enso_home, fix=True, user_dirs=USER_DIRS)
    assert result.ok and result.fixed == [f"created {enso_home.knowledge}"]
    (enso_home.knowledge / "Keep.md").write_text("untouched content\n")
    assert audit.audit_home(enso_home, fix=True, user_dirs=USER_DIRS).fixed == []
    assert (enso_home.knowledge / "Keep.md").read_text() == "untouched content\n"


def test_startup_warnings_cover_the_home_and_in_use_workspaces(
    enso_home: Paths, config: Config
) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    workspaces.create_workspace(enso_home, "lonely")  # only warnings, and not in use
    assert audit.startup_warnings(enso_home, config) == []

    shutil.rmtree(enso_home.workspace("default") / "drafts")
    (enso_home.workspace("lonely") / "CLAUDE.md").unlink()  # an error, but nobody uses it
    shutil.rmtree(enso_home.home / ".git")
    lines = audit.startup_warnings(enso_home, config)

    assert len(lines) == 2 and all(line.endswith("; see `enso workspace audit`") for line in lines)
    assert lines[0].startswith("the home fails its audit: the home is not a Git root")
    assert lines[1].startswith("workspace default fails its audit: drafts/ is missing")


def test_tree_size_counts_files_without_following_links(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "one").write_bytes(b"12345")
    (tmp_path / "two").write_bytes(b"1")
    os.symlink(tmp_path / "a", tmp_path / "loop")
    assert audit.tree_size(tmp_path) == 6 and audit.tree_size(tmp_path / "missing") == 0


RESERVED = "{} uses the reserved enso- prefix but Enso did not install it; rename it"


def test_reserved_names_warn_unless_enso_installed_them(enso_home: Paths) -> None:
    workspaces.seed_home(enso_home)  # the bundled enso and enso-* skills: no findings
    root = enso_home.workspace("default")
    finish(root)
    write_skill(enso_home.skills, "enso-extra")  # enso scope, but not bundled
    write_skill(root / "skills", "enso-local")  # Enso never installs workspace skills
    write_skill(root / "skills", "research")  # an ordinary name is fine anywhere
    write_job(enso_home, "enso-audit")  # a bundled name: Enso's, whoever wrote this copy
    write_job(enso_home, "enso-extra")  # reserved, and not bundled
    write_job(enso_home, "nightly")

    report = audit.audit(enso_home, ["default"], user_dirs=USER_DIRS)

    assert [(f.check, f.severity, f.message) for f in report.home.findings] == [
        ("reserved", "warning", RESERVED.format("skills/enso-extra")),
        ("reserved", "warning", RESERVED.format("workspaces/default/jobs/enso-extra")),
    ]
    (found,) = report.workspaces
    assert [(f.check, f.message) for f in found.findings] == [
        ("reserved", RESERVED.format("skills/enso-local"))
    ]
    assert report.ok  # warnings only


def test_bundled_memory_job_in_nondefault_workspace_has_no_reserved_warning(
    enso_home: Paths, config: Config
) -> None:
    workspaces.seed_home(enso_home)
    workspaces.create_workspace(enso_home, "team")
    workspaces.seed_jobs(enso_home, config.defaults, workspace="default")
    workspaces.seed_jobs(enso_home, config.defaults, workspace="team")

    report = audit.audit(enso_home, config=config, user_dirs=USER_DIRS)

    assert not [finding for finding in report.home.findings if finding.check == "reserved"]
    assert not [
        finding
        for workspace in report.workspaces
        for finding in workspace.findings
        if finding.check == "reserved"
    ]


def test_official_receipts_recognized_only_at_home_and_bad_receipts_warn(enso_home: Paths):
    from enso.skill_catalog import RECEIPT, SOURCE

    workspaces.seed_home(enso_home)
    root = enso_home.workspace("default")
    finish(root)
    name = "enso-example"
    for parent in (enso_home.skills, root / "skills"):
        write_skill(parent, name)
        (parent / name / RECEIPT).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": SOURCE,
                    "commit": "a" * 40,
                    "name": name,
                    "files": {"SKILL.md": "b" * 64},
                }
            )
        )
    # Provenance is not a promise the current bytes remain vetted after local edits.
    report = audit.audit(enso_home, ["default"], user_dirs=USER_DIRS)
    assert not [f for f in report.home.findings if f.check == "reserved"]
    assert [f for f in report.workspaces[0].findings if f.check == "reserved"]
    (enso_home.skills / name / RECEIPT).write_text('{"source":"someone/else"}')
    report = audit.audit(enso_home, ["default"], user_dirs=USER_DIRS)
    assert [f for f in report.home.findings if f.check == "reserved"]


def test_audit_preserves_optional_provider_files_without_checking_permissions(enso_home, config):
    workspaces.seed_home(enso_home)
    workspace = enso_home.workspace("default")
    finish(workspace)
    (found,) = audit.audit(enso_home, config=config, user_dirs=USER_DIRS).workspaces
    assert found.findings == []

    files = [
        workspace / path
        for path in (
            ".claude/settings.json",
            ".codex/config.toml",
            ".grok/config.toml",
            "opencode.json",
        )
    ]
    for file in files:
        file.parent.mkdir(exist_ok=True)
        file.write_text("user-authored provider settings")
    (found,) = audit.audit(enso_home, config=config, user_dirs=USER_DIRS, fix=True).workspaces
    assert found.findings == []
    assert all(file.read_text() == "user-authored provider settings" for file in files)


def test_the_layout_table_covers_everything_the_scaffold_writes(enso_home: Paths) -> None:
    """The guard against drift: a new shipped path must be declared, not discovered later."""
    declared = (*layout.HOME, *layout.WORKSPACE)
    assert {entry.category for entry in declared} <= set(layout.CATEGORIES) - {
        layout.UNEXPECTED
    }  # a table entry is owned by someone; "unexpected" is only ever a scan result
    named = {entry.name for entry in layout.HOME}
    assert {relative.split("/")[0] for relative in workspaces.BUNDLED_FILES} <= named
    assert {relative.split("/")[0] for relative in workspaces.bundled_skill_files()} <= named
    assert {link.split("/")[0] for link, _ in layout.LINKS} <= named

    workspaces.seed_home(enso_home)
    assert not (enso_home.home / "slack").exists()
    workspaces.ensure_layout(enso_home.workspace("default"))
    report = audit.audit(enso_home, user_dirs=USER_DIRS)
    assert layout.UNEXPECTED not in report.home.layout.values()
    assert layout.UNEXPECTED not in report.workspaces[0].layout.values()
    legacy = enso_home.home / "slack" / "manifest.json"
    legacy.parent.mkdir()
    legacy.write_text("customized legacy manifest")
    assert audit.audit(enso_home, user_dirs=USER_DIRS).home.layout["slack"] == layout.USER
    assert {entry.name for entry in layout.WORKSPACE} >= {
        *layout.WORKSPACE_DIRS,
        *(link.split("/")[0] for link, _ in layout.LINKS),
    }


def test_the_home_repository_is_the_operators_to_use(enso_home: Paths, config: Config) -> None:
    """Enso creates the Git root and never commits; what the operator keeps there is theirs."""
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    (enso_home.home / ".gitignore").write_text("/secrets/\n/enso.db*\n")

    report = audit.audit(enso_home, config=config, user_dirs=USER_DIRS)

    assert report.home.findings == [] and report.home.layout[".gitignore"] == "user"


def test_managed_roots_are_classified_not_searched(enso_home: Paths, config: Config) -> None:
    """Private operating state stays private: no recursion, no findings about what is in it."""
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    for name in ("runtime", "cache"):
        root = enso_home.home / name
        root.mkdir(mode=layout.PRIVATE_DIR if name == "runtime" else 0o777)
        (root / "anything-at-all.tmp").write_text("managed state")
        (root / "nested").mkdir()
        (root / "nested" / "deeper.json").write_text("{}")

    report = audit.audit(enso_home, config=config, user_dirs=USER_DIRS)

    assert report.home.findings == [] and report.ok
    assert [report.home.layout[name] for name in ("runtime", "cache")] == ["managed", "managed"]
    # The operator's own roots are equally off limits.
    (enso_home.knowledge / "notes.md").write_text("# a note\n")
    assert audit.audit(enso_home, config=config, user_dirs=USER_DIRS).home.findings == []


def test_an_unexpected_home_entry_is_reported_and_left_alone(
    enso_home: Paths, config: Config
) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    stray = enso_home.home / "notes-to-self.md"
    stray.write_text("mine")
    (enso_home.home / "old-backup").mkdir()

    report = audit.audit(enso_home, fix=True, config=config, user_dirs=USER_DIRS)

    found = [f for f in report.home.findings if f.check == "unexpected"]
    assert [(f.severity, f.fixable, f.attention) for f in found] == [
        ("warning", False, True),
        ("warning", False, True),
    ]
    assert found[0].message == (
        f"notes-to-self.md is not part of the layout; move it out of {enso_home.home} or remove it"
    )
    assert found[1].message.startswith("old-backup/ is not part of the layout")
    assert report.home.layout["notes-to-self.md"] == "unexpected"
    # An unexpected entry is a warning, so the home still passes; --fix never removes it.
    assert report.ok and report.home.attention
    assert stray.read_text() == "mine" and (enso_home.home / "old-backup").is_dir()


def test_a_dangling_top_level_link_is_reported_once(enso_home: Paths, config: Config) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    (enso_home.home / "cache").symlink_to("/nowhere-at-all")

    report = audit.audit(enso_home, fix=True, config=config, user_dirs=USER_DIRS)

    (found,) = [f for f in report.home.findings if f.check == "link"]
    assert (found.severity, found.fixable, found.attention) == ("warning", False, True)
    assert found.message.startswith("cache/ is a dangling symbolic link")
    assert report.home.layout["cache"] == "managed"
    assert (enso_home.home / "cache").is_symlink()  # --fix neither repoints nor removes it
    # A required entry keeps its own, stronger report instead of a second one.
    (enso_home.knowledge).rmdir()
    (enso_home.knowledge).symlink_to("/nowhere-at-all")
    report = audit.audit(enso_home, config=config, user_dirs=USER_DIRS)
    assert [f.message for f in report.home.findings if "knowledge" in f.message] == [
        "knowledge/ is a dangling symbolic link; move it aside"
    ]


@pytest.mark.parametrize("name", ["runtime", "cache", "secrets"])
def test_operating_roots_must_be_real_directories(
    enso_home: Paths, config: Config, tmp_path: Path, name: str
) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    outside = tmp_path / "outside"
    outside.mkdir()
    root = enso_home.home / name
    root.symlink_to(outside, target_is_directory=True)

    report = audit.audit(enso_home, fix=True, config=config, user_dirs=USER_DIRS)

    found = [f for f in report.home.findings if f.check == "link" and f.message.startswith(name)]
    assert len(found) == 1
    assert (found[0].severity, found[0].fixable, found[0].attention) == (
        "warning", False, True,
    )  # fmt: skip
    assert "not a real directory" in found[0].message
    assert report.ok and report.attention
    assert root.is_symlink() and list(outside.iterdir()) == []


def test_operating_root_file_is_reported_without_replacing_it(
    enso_home: Paths, config: Config
) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    root = enso_home.home / "runtime"
    root.write_text("preserve me")

    report = audit.audit(enso_home, fix=True, config=config, user_dirs=USER_DIRS)

    found = [f for f in report.home.findings if f.check == "directory"]
    assert len(found) == 1 and found[0].message.startswith("runtime/ is a file")
    assert found[0].attention and not found[0].fixable
    assert root.read_text() == "preserve me"


def test_a_missing_workspaces_root_is_reported_and_fixable(enso_home: Paths) -> None:
    workspaces.seed_home(enso_home)
    shutil.rmtree(enso_home.workspaces)

    report = audit.audit(enso_home, user_dirs=USER_DIRS)

    found = [f for f in report.home.findings if f.check == "directory"]
    assert [(f.message, f.fixable) for f in found] == [("workspaces/ is missing", True)]
    assert not report.ok and report.attention

    fixed = audit.audit(enso_home, fix=True, user_dirs=USER_DIRS)
    assert fixed.home.findings == []
    assert fixed.home.fixed == [f"created {enso_home.workspaces}"]
    assert enso_home.workspaces.is_dir()


def test_a_file_cannot_replace_the_workspaces_root(enso_home: Paths) -> None:
    workspaces.seed_home(enso_home)
    shutil.rmtree(enso_home.workspaces)
    enso_home.workspaces.write_text("preserve me")

    report = audit.audit(enso_home, fix=True, user_dirs=USER_DIRS)

    assert [f.message for f in report.home.findings if f.check == "directory"] == [
        "workspaces/ is a file, not a directory"
    ]
    assert enso_home.workspaces.read_text() == "preserve me"


def test_private_roots_are_reported_and_tightened_when_other_users_can_read_them(
    enso_home: Paths, raw_config: dict
) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    write_config(enso_home, raw_config)
    enso_home.config.chmod(0o644)
    enso_home.secrets.mkdir()
    enso_home.secrets.chmod(0o755)
    (enso_home.secrets / "slack.env").write_text("TOKEN=xoxb-real\n")

    report = audit.audit(enso_home, user_dirs=USER_DIRS)

    found = [f for f in report.home.findings if f.check == "permissions"]
    assert [(f.severity, f.fixable, f.attention) for f in found] == [
        ("warning", True, True),
        ("warning", True, True),
    ]
    assert str(enso_home.config) in found[0].message and "0644" in found[0].message
    assert "0600" in found[0].message and "readable by other users" in found[0].message
    assert str(enso_home.secrets) in found[1].message and "0700" in found[1].message
    assert report.ok and report.home.attention  # untidy, not unhealthy

    fixed = audit.audit(enso_home, fix=True, user_dirs=USER_DIRS)

    assert [f for f in fixed.home.findings if f.check == "permissions"] == []
    assert [line.split(" ", 1)[0] for line in fixed.home.fixed] == ["restricted", "restricted"]
    assert stat.S_IMODE(enso_home.config.stat().st_mode) == 0o600
    assert stat.S_IMODE(enso_home.secrets.stat().st_mode) == 0o700
    assert (enso_home.secrets / "slack.env").read_text() == "TOKEN=xoxb-real\n"
    # Already-private roots are left exactly as they are, not widened to the wanted mode.
    enso_home.config.chmod(0o400)
    again = audit.audit(enso_home, fix=True, user_dirs=USER_DIRS)
    assert again.home.fixed == [] and stat.S_IMODE(enso_home.config.stat().st_mode) == 0o400


def test_permission_fix_preserves_the_owners_existing_access(enso_home: Paths) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    enso_home.config.write_text("{}")
    enso_home.config.chmod(0o440)

    report = audit.audit(enso_home, user_dirs=USER_DIRS)
    found = [f for f in report.home.findings if f.check == "permissions"]
    assert len(found) == 1 and "make it 0400" in found[0].message

    fixed = audit.audit(enso_home, fix=True, user_dirs=USER_DIRS)
    assert not any(f.check == "permissions" for f in fixed.home.findings)
    assert stat.S_IMODE(enso_home.config.stat().st_mode) == 0o400


def test_sqlite_sidecars_are_stale_only_without_their_database(enso_home: Paths) -> None:
    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    enso_home.db.write_bytes(b"")
    (enso_home.home / "enso.db-wal").write_bytes(b"")
    (enso_home.home / "enso.db-shm").write_bytes(b"")

    report = audit.audit(enso_home, user_dirs=USER_DIRS)
    assert [f for f in report.home.findings if f.check == "stale"] == []

    enso_home.db.unlink()
    report = audit.audit(enso_home, fix=True, user_dirs=USER_DIRS)

    found = [f for f in report.home.findings if f.check == "stale"]
    assert [(f.severity, f.fixable, f.attention) for f in found] == [
        ("warning", False, True),
        ("warning", False, True),
    ]
    assert all("safe to delete" in f.message for f in found)
    # Reported, never removed: deleting a write-ahead log is the operator's call.
    assert (enso_home.home / "enso.db-wal").exists()


def test_warnings_the_operator_may_simply_disagree_with_do_not_ask_for_attention(
    enso_home: Paths,
) -> None:
    """An orphan workspace and an unedited template are matters of taste, not portable facts.

    They stay warnings the audit reports and the nightly job leaves alone.
    """
    root = enso_home.workspace("default")
    workspaces.seed_home(enso_home)
    workspaces.ensure_layout(root)
    (root / "AGENTS.md").write_text(workspaces.workspace_template("default"))

    found = audit.audit_workspace(enso_home, "default", bound=[], user_dirs=USER_DIRS)

    assert [f.check for f in found.warnings] == ["agents-md", "orphan"]
    assert found.ok and not found.attention
