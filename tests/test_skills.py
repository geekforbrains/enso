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


def test_three_way_collision_takes_the_worst_severity(enso_home: Paths, tmp_path: Path) -> None:
    write_skill(enso_home.workspace("default") / "skills", "x")
    write_skill(enso_home.skills, "x")
    user = tmp_path / ".agents" / "skills"
    write_skill(user, "x")

    ws, enso, user_copy = resolve(enso_home, "default", user_dirs=[user])

    assert (ws.collision, ws.collides_with) == ("error", ("enso", "user"))
    assert (enso.collision, enso.collides_with) == ("error", ("workspace", "user"))
    assert (user_copy.collision, user_copy.collides_with) == ("warning", ("workspace", "enso"))


def test_managed_skills_report_skill_md_problems(enso_home: Paths) -> None:
    ws = enso_home.workspace("default") / "skills"
    (ws / "broken").mkdir(parents=True)
    write_skill(ws, "mismatch", front_name="other")
    write_skill(ws, "nodesc", description=None)
    write_skill(ws, "nofront", frontmatter=False)
    write_skill(ws, "noname", front_name="", description="Fine.")
    (ws / ".hidden").mkdir()
    (ws / "stray.md").write_text("not a skill")

    skills = resolve(enso_home, "default", user_dirs=[])

    assert {skill.name: skill.problems for skill in skills} == {
        "broken": ("SKILL.md is missing",),
        "mismatch": ("SKILL.md name is 'other' but the directory is 'mismatch'",),
        "nodesc": ("SKILL.md has no description",),
        "nofront": ("SKILL.md needs a leading --- frontmatter block",),
        "noname": ("SKILL.md has no name",),
    }
    assert not any(skill.ok for skill in skills)
    assert next(skill for skill in skills if skill.name == "noname").description == "Fine."


def test_skill_frontmatter_is_held_to_the_shared_syntax_only(enso_home: Paths) -> None:
    """SKILL.md shares the syntax rules with JOB.md and none of the job schema.

    A skill reads two fields of its own, so unknown fields, timeouts and booleans are no
    business of this format; what it does inherit is that ambiguous frontmatter is refused
    rather than half-read.
    """
    ws = enso_home.workspace("default") / "skills"
    (ws / "extras").mkdir(parents=True)
    (ws / "extras" / "SKILL.md").write_text(
        "---\nname: extras\ndescription: Fine.\nallowed-tools: Read\nenabled: 12\n---\n\n# x\n"
    )
    (ws / "unquoted").mkdir()
    (ws / "unquoted" / "SKILL.md").write_text(
        "---\nname: unquoted\ndescription: Lays it out: like this.\n---\n\n# x\n"
    )
    (ws / "twice").mkdir()
    (ws / "twice" / "SKILL.md").write_text(
        "---\nname: twice\ndescription: One.\ndescription: Two.\n---\n\n# x\n"
    )

    found = {skill.name: skill for skill in resolve(enso_home, "default", user_dirs=[])}

    assert found["extras"].problems == () and found["extras"].description == "Fine."
    assert found["unquoted"].problems == (
        "SKILL.md frontmatter is not valid YAML at line 3, column 25",
    )
    assert found["twice"].problems == (
        "SKILL.md frontmatter answers 'description' twice at line 4, column 1",
    )
    assert not found["unquoted"].description and not found["twice"].description


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


def test_default_user_dirs_come_from_home(enso_home: Paths, user_home: Path) -> None:
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


def test_empty_opencode_overrides_keep_the_default_root(
    enso_home: Paths, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "")
    write_skill(user_home / ".config" / "opencode" / "skills", "opencode-default")

    assert user_skill_dirs()[-2:] == [
        user_home / ".config" / "opencode" / "skill",
        user_home / ".config" / "opencode" / "skills",
    ]
    assert names(resolve(enso_home, "default")) == [("opencode-default", "user")]


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

    assert user_skill_dirs()[-4:] == [
        user_home / ".config" / "opencode" / "skill",
        user_home / ".config" / "opencode" / "skills",
        extra / "skill",
        extra / "skills",
    ]
    assert names(resolve(enso_home, "default")) == [
        ("from-extra", "user"),
        ("from-global", "user"),
    ]


def test_both_opencode_overrides_are_scanned(
    enso_home: Paths, user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "xdg"
    extra = tmp_path / "extra-opencode"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(extra))
    write_skill(config / "opencode" / "skills", "from-xdg")
    write_skill(extra / "skills", "from-extra")
    write_skill(user_home / ".config" / "opencode" / "skills", "left-behind")

    skills = resolve(enso_home, "default")

    assert names(skills) == [("from-extra", "user"), ("from-xdg", "user")]
    assert [skill.path for skill in skills] == [
        extra / "skills" / "from-extra",
        config / "opencode" / "skills" / "from-xdg",
    ]


def test_overlapping_opencode_roots_are_scanned_once(
    enso_home: Paths, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second root that is really the first — here through a symlink — is not scanned twice."""
    write_skill(user_home / ".config" / "opencode" / "skills", "shared")
    link = user_home / "opencode-link"
    link.symlink_to(user_home / ".config" / "opencode")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(link))

    directories = user_skill_dirs()

    assert directories[-2:] == [
        user_home / ".config" / "opencode" / "skill",
        user_home / ".config" / "opencode" / "skills",
    ]
    assert names(resolve(enso_home, "default")) == [("shared", "user")]


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
