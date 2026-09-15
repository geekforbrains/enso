"""The workspace audit: every check, what ``--fix`` may and may not do, the JSON shape."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from conftest import write_job
from test_skills import write_skill

from enso import audit, workspaces
from enso.config import Config, Paths

USER_DIRS: list[Path] = []  # no user-level skills unless a test says so


def finish(root: Path) -> None:
    """Make a bare workspace directory pass: the layout, and an AGENTS.md that was edited."""
    workspaces.ensure_layout(root)
    (root / "AGENTS.md").write_text("# a real workspace\n")


def break_workspace(root: Path, enso_home: Paths) -> None:
    """One of everything the audit reports; ``AGENTS.md`` is left missing."""
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
        "meteor", [], ["nightly"], 1500,
    )  # fmt: skip
    assert all(w.ok and w.status == "ok" and w.summary == "ok" for w in report.workspaces)
    assert json.loads(json.dumps(report.as_dict())) == {
        "ok": True,
        "home": {"path": str(enso_home.home), "status": "ok", "findings": [], "fixed": []},
        "workspaces": [
            {
                "name": "default",
                "path": str(enso_home.workspace("default")),
                "status": "ok",
                "bindings": ["slack:C1", "slack:dm:U1"],
                "jobs": [],
                "uploads_bytes": 0,
                "findings": [],
                "fixed": [],
            },
            {
                "name": "meteor",
                "path": str(meteor),
                "status": "ok",
                "bindings": [],
                "jobs": ["nightly"],
                "uploads_bytes": 1500,
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
        "notes/ is not part of the layout",
        "stray.txt is not part of the layout",
    ]
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
    assert named.jobs == ["nightly"] and [f.check for f in named.findings] == ["agents-md"]


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

    bundled = workspaces.BUNDLED_SKILLS + workspaces.BUNDLED_JOBS
    assert all(workspaces.reserved(name) for name in bundled)
    assert [(f.check, f.severity, f.message) for f in report.home.findings] == [
        ("reserved", "warning", RESERVED.format("skills/enso-extra")),
        ("reserved", "warning", RESERVED.format("jobs/enso-extra")),
    ]
    (found,) = report.workspaces
    assert [(f.check, f.message) for f in found.findings] == [
        ("reserved", RESERVED.format("skills/enso-local"))
    ]
    assert report.ok  # warnings only


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


def test_a_restricted_workspace_must_be_able_to_launch_its_chat_provider(
    enso_home: Paths, config: Config
) -> None:
    from dataclasses import replace

    from enso.config import WorkspaceConfig

    workspaces.seed_home(enso_home)
    finish(enso_home.workspace("default"))
    config = replace(config, workspaces={"default": WorkspaceConfig(restricted=True)})

    (found,) = audit.audit(enso_home, config=config, user_dirs=USER_DIRS).workspaces
    assert [(f.check, f.severity, f.fixable) for f in found.findings] == [
        ("policy", "error", False)
    ]
    assert found.findings[0].message.startswith(
        "workspace default is restricted and has no claude policy file"
    )

    settings = enso_home.workspace("default") / ".claude" / "settings.json"
    settings.write_text("{}")
    (enso_home.workspace("default") / "opencode.json").write_text("{}")  # expected, not noise
    (found,) = audit.audit(enso_home, config=config, user_dirs=USER_DIRS).workspaces
    assert found.findings == []
