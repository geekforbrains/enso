"""Skill resolution across workspace, enso, and user scope, with collisions and problems."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from enso import workspaces
from enso.config import Paths
from enso.skills import USER_SKILL_DIRS, Skill, resolve, user_skill_dirs


def write_skill(
    root: Path,
    name: str,
    *,
    front_name: str | None = None,
    description: str | None = "Does a thing. Use when asked to do the thing.",
    frontmatter: bool = True,
) -> Path:
    """``<root>/<name>/SKILL.md`` with a name and description unless told otherwise."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    fields = [f"name: {front_name if front_name is not None else name}"]
    if description is not None:
        fields.append(f"description: {description}")
    front = "---\n" + "\n".join(fields) + "\n---\n\n" if frontmatter else ""
    (directory / "SKILL.md").write_text(f"{front}# {name}\n")
    return directory


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch ``HOME`` so the real user-level skills never leak into a test."""
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in ("XDG_CONFIG_HOME", "OPENCODE_CONFIG_DIR"):
        monkeypatch.delenv(key, raising=False)  # OpenCode's roots start at their defaults
    return home


def names(skills: list[Skill]) -> list[tuple[str, str]]:
    return [(skill.name, skill.scope) for skill in skills]


def test_resolve_lists_scopes_in_order(enso_home: Paths, user_home: Path) -> None:
    ws = enso_home.workspace("default") / "skills"
    write_skill(ws, "notes", description="Keep notes.")
    write_skill(enso_home.skills, "jobs", description="Run jobs.")
    user = user_home / ".claude" / "skills"
    write_skill(user, "commit", description="Commit work.")

    skills = resolve(enso_home, "default", user_dirs=[user])

    assert names(skills) == [("notes", "workspace"), ("jobs", "enso"), ("commit", "user")]
    assert [skill.path for skill in skills] == [
        ws / "notes",
        enso_home.skills / "jobs",
        user / "commit",
    ]
    assert [skill.description for skill in skills] == ["Keep notes.", "Run jobs.", "Commit work."]
    assert all(skill.ok and skill.collision is None and not skill.problems for skill in skills)


def test_symlinked_skill_directory_is_followed(enso_home: Paths, tmp_path: Path) -> None:
    real = write_skill(tmp_path / "elsewhere", "linked")
    ws = enso_home.workspace("default") / "skills"
    ws.mkdir()
    os.symlink(real, ws / "linked")

    (skill,) = resolve(enso_home, "default", user_dirs=[])

    assert (skill.name, skill.scope, skill.path) == ("linked", "workspace", ws / "linked")
    assert skill.ok


def test_workspace_and_enso_collision_is_an_error(enso_home: Paths) -> None:
    write_skill(enso_home.workspace("default") / "skills", "research")
    write_skill(enso_home.skills, "research")

    ws, enso = resolve(enso_home, "default", user_dirs=[])

    assert (ws.collision, ws.collides_with) == ("error", ("enso",))
    assert (enso.collision, enso.collides_with) == ("error", ("workspace",))
    assert not ws.ok and not enso.ok
    assert not ws.problems and not enso.problems  # a collision is not a SKILL.md problem


def test_user_collision_is_a_warning_on_both_sides(enso_home: Paths, tmp_path: Path) -> None:
    write_skill(enso_home.skills, "notes")
    user = tmp_path / ".claude" / "skills"
    write_skill(user, "notes")

    enso, user_copy = resolve(enso_home, "default", user_dirs=[user])

    assert (enso.collision, enso.collides_with) == ("warning", ("user",))
    assert (user_copy.collision, user_copy.collides_with) == ("warning", ("enso",))
    assert enso.ok and user_copy.ok


def test_managed_skills_report_skill_md_problems(enso_home: Paths) -> None:
    ws = enso_home.workspace("default") / "skills"
    (ws / "broken").mkdir(parents=True)
    write_skill(ws, "mismatch", front_name="other")
    write_skill(ws, "nodesc", description=None)
    write_skill(ws, "nofront", frontmatter=False)
    write_skill(ws, "noname", front_name="", description="Fine.")
    (ws / "extras").mkdir()
    (ws / "extras" / "SKILL.md").write_text(
        "---\nname: extras\ndescription: Fine.\nallowed-tools: Read\nenabled: 12\n---\n\n# x\n"
    )
    (ws / ".hidden").mkdir()
    (ws / "stray.md").write_text("not a skill")

    skills = resolve(enso_home, "default", user_dirs=[])

    assert {skill.name: skill.problems for skill in skills} == {
        "broken": ("SKILL.md is missing",),
        "extras": (),  # a skill reads two fields; unknown ones are no business of this format
        "mismatch": ("SKILL.md name is 'other' but the directory is 'mismatch'",),
        "nodesc": ("SKILL.md has no description",),
        "nofront": ("SKILL.md needs a leading --- frontmatter block",),
        "noname": ("SKILL.md has no name",),
    }
    assert [skill.name for skill in skills if skill.ok] == ["extras"]
    assert next(skill for skill in skills if skill.name == "noname").description == "Fine."


def test_bundled_skills_resolve_without_problems(enso_home: Paths) -> None:
    """Every skill Enso ships must satisfy the rules Enso holds everyone else to."""
    bundled = Path(workspaces.__file__).parent / "bundled" / "skills"
    shutil.copytree(bundled, enso_home.skills, dirs_exist_ok=True)

    skills = resolve(enso_home, user_dirs=[])

    assert [skill.name for skill in skills] == sorted(entry.name for entry in bundled.iterdir())
    assert all(skill.ok and skill.description for skill in skills), [
        (skill.name, skill.problems) for skill in skills if not skill.ok
    ]


def test_user_scope_is_lenient_and_deduplicated(enso_home: Paths, tmp_path: Path) -> None:
    claude = tmp_path / ".claude" / "skills"
    agents = tmp_path / ".agents" / "skills"
    (claude / "junk").mkdir(parents=True)  # no SKILL.md: not a skill to any CLI
    write_skill(claude, "odd", front_name="something-else", description=None)
    write_skill(claude, "shared", description="Claude copy.")
    write_skill(agents, "shared", description="Codex copy.")
    write_skill(agents, "only-agents", description="Codex only.")

    skills = resolve(enso_home, "default", user_dirs=[claude, agents])

    assert names(skills) == [("odd", "user"), ("only-agents", "user"), ("shared", "user")]
    assert all(not skill.problems and skill.collision is None for skill in skills)
    shared = next(skill for skill in skills if skill.name == "shared")
    assert (shared.path, shared.description) == (claude / "shared", "Claude copy.")


def test_default_user_dirs_come_from_home(
    enso_home: Paths, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("XDG_CONFIG_HOME", "OPENCODE_CONFIG_DIR"):
        monkeypatch.setenv(key, "")  # an empty override is no override: the default root stands
    write_skill(user_home / ".gemini" / "config" / "skills", "gem")
    write_skill(user_home / ".config" / "opencode" / "skill", "opencode-singular")
    write_skill(user_home / ".config" / "opencode" / "skills", "opencode-plural")
    (user_home / ".codex" / "skills").mkdir(parents=True)  # present but empty

    assert user_skill_dirs() == [user_home / relative for relative in USER_SKILL_DIRS] + [
        user_home / ".config" / "opencode" / "skill",
        user_home / ".config" / "opencode" / "skills",
    ]
    assert names(resolve(enso_home, "default")) == [
        ("gem", "user"),
        ("opencode-plural", "user"),
        ("opencode-singular", "user"),
    ]


def test_xdg_config_home_moves_the_opencode_global_root(
    enso_home: Paths, user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    write_skill(config / "opencode" / "skill", "moved-singular")
    write_skill(config / "opencode" / "skills", "moved-plural")
    write_skill(user_home / ".config" / "opencode" / "skills", "left-behind")
    write_skill(user_home / ".claude" / "skills", "commit")  # an unrelated root still works

    assert user_skill_dirs()[-2:] == [
        config / "opencode" / "skill",
        config / "opencode" / "skills",
    ]
    assert names(resolve(enso_home, "default")) == [
        ("commit", "user"),
        ("moved-plural", "user"),
        ("moved-singular", "user"),
    ]


def test_opencode_config_dir_adds_a_root_beside_the_global_one(
    enso_home: Paths, user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "extra-opencode"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(extra))
    write_skill(user_home / ".config" / "opencode" / "skills", "from-global")
    write_skill(extra / "skill", "from-extra")
    global_dirs = [user_home / ".config" / "opencode" / name for name in ("skill", "skills")]

    assert user_skill_dirs()[-4:] == [*global_dirs, extra / "skill", extra / "skills"]
    assert names(resolve(enso_home, "default")) == [
        ("from-extra", "user"),
        ("from-global", "user"),
    ]

    link = user_home / "opencode-link"  # the same root twice is scanned once
    link.symlink_to(user_home / ".config" / "opencode")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(link))

    assert user_skill_dirs() == [
        *(user_home / relative for relative in USER_SKILL_DIRS),
        *global_dirs,
    ]


def test_resolve_without_a_workspace_or_with_a_missing_one(enso_home: Paths) -> None:
    write_skill(enso_home.skills, "jobs")

    assert names(resolve(enso_home, user_dirs=[])) == [("jobs", "enso")]
    assert names(resolve(enso_home, "absent", user_dirs=[])) == [("jobs", "enso")]
    with pytest.raises(ValueError, match="kebab-case"):
        resolve(enso_home, "../escape", user_dirs=[])


def test_as_dict_is_json_ready(enso_home: Paths) -> None:
    write_skill(enso_home.workspace("default") / "skills", "research", description="Dig.")
    write_skill(enso_home.skills, "research")

    (ws, _) = resolve(enso_home, "default", user_dirs=[])
    data = json.loads(json.dumps(ws.as_dict()))

    assert data == {
        "name": "research",
        "scope": "workspace",
        "path": str(enso_home.workspace("default") / "skills" / "research"),
        "description": "Dig.",
        "problems": [],
        "collision": "error",
        "collides_with": ["enso"],
    }
