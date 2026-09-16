"""Skill resolution: what a workspace's agent has, from which scope, and any collisions.

Three scopes, in the order they are listed: ``workspace`` (``<workspace>/skills/``),
``enso`` (``~/.enso/skills/``), and ``user`` (the provider CLIs' own directories). The
first two are Enso's and are held to the layout rules; the third is read for the full
picture and never written to. See ``docs/workspaces.md`` § Skills.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import frontmatter
from .config import Paths, valid_workspace_name

SCOPES = ("workspace", "enso", "user")
MANAGED_SCOPES = ("workspace", "enso")
# Where each provider CLI reads its own user-level skills, relative to the user's home.
# OpenCode's own directories are not here: they follow its configuration roots instead.
USER_SKILL_DIRS = (
    ".claude/skills",
    ".agents/skills",
    ".codex/skills",
    ".grok/skills",
    ".gemini/config/skills",
)
# The two names OpenCode reads below each of its configuration roots.
OPENCODE_SKILL_DIRS = ("skill", "skills")
ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Skill:
    """One skill directory as the agent in a workspace would see it."""

    name: str  # the directory name, which is the identity every CLI uses
    scope: str  # workspace | enso | user
    path: Path  # the skill directory
    description: str = ""
    problems: tuple[str, ...] = ()  # SKILL.md missing, unparsable, or misnamed
    collision: str | None = None  # error | warning: the worst collision this entry is in
    collides_with: tuple[str, ...] = ()  # scopes holding another skill of this name

    @property
    def ok(self) -> bool:
        """Loadable and unambiguous: no problems and no error-level collision."""
        return not self.problems and self.collision != ERROR

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "scope": self.scope,
            "path": str(self.path),
            "description": self.description,
            "problems": list(self.problems),
            "collision": self.collision,
            "collides_with": list(self.collides_with),
        }


def user_skill_dirs(home: Path | None = None) -> list[Path]:
    """The user-level skill directories under ``home`` (default: the real home).

    Most CLIs fix theirs under the home. OpenCode's follow its configuration instead, so
    they are appended for each root in ``_opencode_config_roots``. A directory two roots
    both resolve to is listed once, at its first position.
    """
    base = home or Path.home()
    directories = [base / relative for relative in USER_SKILL_DIRS]
    for root in _opencode_config_roots(base):
        directories.extend(root / name for name in OPENCODE_SKILL_DIRS)
    seen: set[Path] = set()
    unique: list[Path] = []
    for directory in directories:
        key = _resolved(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def _opencode_config_roots(base: Path) -> list[Path]:
    """OpenCode's configuration roots: its global one, then any ``OPENCODE_CONFIG_DIR``.

    The global root is ``$XDG_CONFIG_HOME/opencode`` when that variable is non-empty and
    ``<home>/.config/opencode`` otherwise; it is also where OpenCode reads its global
    ``AGENTS.md``. ``OPENCODE_CONFIG_DIR`` names an additional root, not a replacement.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME")
    global_root = Path(config_home).expanduser() if config_home else base / ".config"
    roots = [global_root / "opencode"]
    extra = os.environ.get("OPENCODE_CONFIG_DIR")
    if extra:
        roots.append(Path(extra).expanduser())
    return roots


def _resolved(directory: Path) -> Path:
    """``directory`` with symlinks and relative parts collapsed, for identity only."""
    try:
        return directory.resolve()
    except OSError:  # an unreadable or cyclic path is still a distinct entry
        return directory


def resolve(
    paths: Paths, workspace: str | None = None, *, user_dirs: Sequence[Path] | None = None
) -> list[Skill]:
    """Every skill the agent in ``workspace`` can reach, with scope, problems, and collisions.

    Workspace and enso skills are Enso's: every directory is returned, with its problems
    when its ``SKILL.md`` is missing, unparsable, or misnamed. User skills belong to the
    CLIs: a directory counts when it has a ``SKILL.md``, is otherwise ignored, and one entry
    stands for a name found in several user directories. Ordered workspace, enso, user,
    then by name. Without ``workspace`` only the last two scopes are read.
    """
    if workspace is not None and not valid_workspace_name(workspace):
        raise ValueError("workspace names are lowercase kebab-case (letters, digits, hyphens)")
    found: list[Skill] = []
    if workspace is not None:
        found.extend(_scan(paths.workspace_skills(workspace), "workspace"))
    found.extend(_scan(paths.skills, "enso"))
    found.extend(_user_skills(user_skill_dirs() if user_dirs is None else user_dirs))
    return _mark_collisions(found)


# -- Reading ------------------------------------------------------------------


def _scan(directory: Path, scope: str) -> list[Skill]:
    """Every visible subdirectory (symlinks followed) as a skill, by name."""
    if not directory.is_dir():
        return []
    return [
        _read(entry, scope)
        for entry in sorted(directory.iterdir())
        if entry.is_dir() and not entry.name.startswith(".")
    ]


def _read(directory: Path, scope: str) -> Skill:
    skill_md = directory / "SKILL.md"
    problems: list[str] = []
    description = ""
    if not skill_md.is_file():
        problems.append("SKILL.md is missing")
        return Skill(directory.name, scope, directory, problems=tuple(problems))
    try:
        text = skill_md.read_text("utf-8")
    except (OSError, UnicodeError) as exc:
        problems.append(f"could not read SKILL.md: {exc}")
        return Skill(directory.name, scope, directory, problems=tuple(problems))
    document, problem = frontmatter.parse(text)
    if document is None:
        problems.append(f"SKILL.md {problem}")
    else:
        description = _text(document.fields.get("description")) or ""
        name = _text(document.fields.get("name"))
        if name is None:
            problems.append("SKILL.md has no name")
        elif name != directory.name:
            problems.append(f"SKILL.md name is {name!r} but the directory is {directory.name!r}")
        if not description:
            problems.append("SKILL.md has no description")
    return Skill(directory.name, scope, directory, description, tuple(problems))


def _text(value: object) -> str | None:
    """One frontmatter value as the text a SKILL.md field holds, else nothing.

    A skill declares two strings and nothing else, so a value YAML read as some other
    type is simply not the field it was meant to be; job rules do not apply here.
    """
    return value if isinstance(value, str) and value else None


def _user_skills(directories: Iterable[Path]) -> list[Skill]:
    """One entry per name across the user directories, first location wins, no problems."""
    seen: dict[str, Skill] = {}
    for directory in directories:
        for skill in _scan(directory, "user"):
            if skill.name in seen or not (skill.path / "SKILL.md").is_file():
                continue
            seen[skill.name] = replace(skill, problems=())
    return sorted(seen.values(), key=lambda skill: skill.name)


# -- Collisions ---------------------------------------------------------------


def _mark_collisions(skills: list[Skill]) -> list[Skill]:
    """Annotate every entry whose name appears in another scope.

    Two managed copies (workspace and enso) are an error, because the CLIs disagree about
    which would win. A managed copy beside a user-level one is a warning, on both sides.
    """
    by_name: dict[str, list[Skill]] = {}
    for skill in skills:
        by_name.setdefault(skill.name, []).append(skill)
    marked: list[Skill] = []
    for skill in skills:
        others = [other for other in by_name[skill.name] if other is not skill]
        if not others:
            marked.append(skill)
            continue
        scopes = {other.scope for other in others}
        managed = skill.scope in MANAGED_SCOPES and any(s in MANAGED_SCOPES for s in scopes)
        marked.append(
            replace(
                skill,
                collision=ERROR if managed else WARNING,
                collides_with=tuple(scope for scope in SCOPES if scope in scopes),
            )
        )
    return marked
