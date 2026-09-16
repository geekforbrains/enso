"""The workspace audit: the documented layout, the skill wiring, and what ``--fix`` repairs.

Every row of the check table in ``docs/workspaces.md`` § Auditing, for the home and for
each workspace. A finding carries a stable check id, a severity, a message, and whether
``--fix`` repairs it. ``--fix`` only creates and repairs (directories, links, the home's
Git root); it never deletes, never edits ``AGENTS.md``, and never touches contents under
``knowledge/``, ``drafts/``, or ``uploads/``. ``enso doctor`` and the viewer share the report.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import skills, workspaces
from .config import Config, Paths, valid_workspace_name
from .jobs import load_jobs
from .skills import ERROR, WARNING

# Check ids: one per row of the check table, stable for --json consumers.
DIRECTORY = "directory"  # a required directory is missing or is a file
LINK = "link"  # CLAUDE.md or a skill link is missing, wrong, or a real file
GIT_ROOT = "git-root"  # the home is not a Git root, or a workspace is one
AGENTS_MD = "agents-md"  # missing, unreadable, or still the untouched template
SKILL = "skill"  # a managed skill directory's SKILL.md is missing or wrong
SKILL_COLLISION = "skill-collision"  # a name shared with another scope
ORPHAN = "orphan"  # nothing is bound to the workspace and no job names it
UNEXPECTED = "unexpected"  # an entry the layout has no place for
RESERVED = "reserved"  # an enso-* skill or job that Enso did not install
# The layout includes optional provider configuration; its contents belong to the provider.
EXPECTED_ENTRIES = frozenset(
    {
        "AGENTS.md",
        "worktrees",
        *workspaces.WORKSPACE_DIRS,
        *(link.split("/")[0] for link, _ in workspaces.LINKS),
        ".codex",
        ".grok",
        "opencode.json",
    }
)
IGNORED_ENTRIES = frozenset({".DS_Store"})


@dataclass(frozen=True)
class Finding:
    """One thing wrong with a root, from one check."""

    check: str
    severity: str  # error | warning
    message: str
    fixable: bool = False  # --fix creates or repairs it

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "fixable": self.fixable,
        }


class _Root:
    """What the home and a workspace audit share: findings, fixes, and the verdict."""

    findings: list[Finding]
    fixed: list[str]

    @property
    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == WARNING]

    @property
    def ok(self) -> bool:
        """No errors; warnings do not fail an audit."""
        return not self.errors

    @property
    def status(self) -> str:
        """``error``, ``warning``, or ``ok``: the worst severity present."""
        return ERROR if self.errors else WARNING if self.warnings else "ok"

    @property
    def summary(self) -> str:
        """``ok``, or counts like ``2 errors, 1 warning``."""
        return count_summary(len(self.errors), len(self.warnings))


@dataclass(kw_only=True)
class HomeAudit(_Root):
    """The home: a Git root with instructions, skills, shared knowledge, and the links."""

    path: Path
    findings: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "status": self.status,
            "findings": [finding.as_dict() for finding in self.findings],
            "fixed": list(self.fixed),
        }


@dataclass(kw_only=True)
class WorkspaceAudit(_Root):
    """One workspace against the documented layout, plus what uses it."""

    name: str
    path: Path
    bindings: list[str] = field(default_factory=list)  # binding keys pointing here
    jobs: list[str] = field(default_factory=list)  # job directory names naming it
    uploads_bytes: int = 0
    findings: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "status": self.status,
            "bindings": list(self.bindings),
            "jobs": list(self.jobs),
            "uploads_bytes": self.uploads_bytes,
            "findings": [finding.as_dict() for finding in self.findings],
            "fixed": list(self.fixed),
        }


@dataclass
class Report:
    """The home and every audited workspace; the ``--json`` shape is ``as_dict``."""

    home: HomeAudit
    workspaces: list[WorkspaceAudit]

    @property
    def ok(self) -> bool:
        return self.home.ok and all(workspace.ok for workspace in self.workspaces)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "home": self.home.as_dict(),
            "workspaces": [workspace.as_dict() for workspace in self.workspaces],
        }


def audit(
    paths: Paths,
    names: Sequence[str] | None = None,
    *,
    fix: bool = False,
    config: Config | None = None,
    user_dirs: Sequence[Path] | None = None,
) -> Report:
    """Audit the home and ``names`` (default: every workspace), repairing first with ``fix``.

    ``config`` supplies the bindings; without it the orphan check is skipped, since a
    workspace may be bound in a config that cannot be read. Jobs are read either way.
    ``user_dirs`` overrides where user-level skills are looked for (tests).
    """
    jobs, _ = load_jobs(paths)
    named: dict[str, list[str]] = {}
    for job in jobs:
        named.setdefault(job.workspace, []).append(job.dir_name)
    if names is None:
        names = workspaces.list_workspaces(paths)
    found = []
    for name in names:
        bound = None
        if config is not None:
            bound = sorted(key for key, target in config.bindings.items() if target == name)
        result = audit_workspace(
            paths, name, fix=fix, bound=bound, jobs=named.get(name, []), user_dirs=user_dirs
        )
        found.append(result)
    return Report(audit_home(paths, fix=fix, user_dirs=user_dirs), found)


def audit_home(
    paths: Paths, *, fix: bool = False, user_dirs: Sequence[Path] | None = None
) -> HomeAudit:
    """The Git root, links, instructions, skills, shared knowledge, jobs, and workspaces."""
    home = paths.home
    result = HomeAudit(path=home)
    if fix:
        result.fixed.extend(workspaces.ensure_home(home))
    findings = result.findings
    if not (home / ".git").exists():
        git = shutil.which("git") is not None
        findings.append(
            Finding(
                GIT_ROOT,
                ERROR,
                "the home is not a Git root, so the CLIs walk past it and never find its "
                "AGENTS.md or skills" + ("" if git else " (git is not on PATH)"),
                fixable=git,
            )
        )
    findings.extend(_check_dir(paths.skills, "skills", allow_link=True))
    findings.extend(_check_dir(paths.knowledge, "knowledge", allow_link=False))
    findings.extend(_check_links(home))
    if not paths.agents_md.is_file():
        findings.append(Finding(AGENTS_MD, ERROR, "AGENTS.md is missing"))
    findings.extend(_check_skills(skills.resolve(paths, user_dirs=user_dirs), "enso"))
    findings.extend(_check_jobs(paths))
    findings.extend(_check_workspace_entries(paths))
    return result


def audit_workspace(
    paths: Paths,
    name: str,
    *,
    fix: bool = False,
    bound: Sequence[str] | None = None,
    jobs: Sequence[str] = (),
    user_dirs: Sequence[Path] | None = None,
) -> WorkspaceAudit:
    """One workspace. ``bound`` is its binding keys, or None to skip the orphan check."""
    root = paths.workspace(name)
    result = WorkspaceAudit(name=name, path=root, bindings=list(bound or ()), jobs=list(jobs))
    if not valid_workspace_name(name):
        result.findings.append(Finding(UNEXPECTED, ERROR, "names are lowercase kebab-case"))
        return result
    if not root.is_dir():
        result.findings.append(Finding(DIRECTORY, ERROR, f"{root} does not exist"))
        return result
    if fix:
        result.fixed.extend(workspaces.ensure_layout(root))
    findings = result.findings
    findings.extend(_check_dirs(root))
    findings.extend(_check_links(root))
    findings.extend(_check_agents_md(root, name))
    if (root / ".git").exists():
        findings.append(
            Finding(
                GIT_ROOT,
                ERROR,
                ".git makes the workspace its own Git root, so the CLIs stop there and never "
                "see the home's AGENTS.md or skills; keep the repository elsewhere",
            )
        )
    findings.extend(_check_skills(skills.resolve(paths, name, user_dirs=user_dirs), "workspace"))
    findings.extend(_check_entries(root))
    if bound is not None and not bound and not jobs:
        findings.append(
            Finding(ORPHAN, WARNING, "nothing is bound to this workspace and no job names it")
        )
    result.uploads_bytes = tree_size(root / "uploads")
    return result


def startup_warnings(paths: Paths, config: Config) -> list[str]:
    """One line per in-use workspace (bound, or named by a job) that fails its audit.

    For ``enso serve``: a malformed workspace is logged and turns still run. The home is
    included, since every workspace depends on it. Warnings alone say nothing.
    """
    report = audit(paths, config=config)
    lines: list[str] = []
    if not report.home.ok:
        lines.append(f"the home fails its audit: {_join(report.home.errors)}")
    for workspace in report.workspaces:
        if not workspace.ok and (workspace.bindings or workspace.jobs):
            lines.append(f"workspace {workspace.name} fails its audit: {_join(workspace.errors)}")
    return [f"{line}; see `enso workspace audit`" for line in lines]


def tree_size(path: Path) -> int:
    """Bytes of every regular file under ``path``, symlinks not followed; 0 when absent."""
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


# -- Checks -------------------------------------------------------------------


def _check_dirs(root: Path) -> Iterator[Finding]:
    for name in workspaces.WORKSPACE_DIRS:
        yield from _check_dir(root / name, name, allow_link=name != "knowledge")


def _check_dir(path: Path, name: str, *, allow_link: bool) -> Iterator[Finding]:
    """One required directory. A link is only ever reported: ``--fix`` neither creates nor
    removes one, so a dangling link must not be reported as a repairable absence."""
    if path.is_symlink() and not (allow_link and path.is_dir()):
        kind = "a symbolic link" if path.exists() else "a dangling symbolic link"
        yield Finding(DIRECTORY, ERROR, f"{name}/ is {kind}; move it aside")
    elif not path.exists():
        yield Finding(DIRECTORY, ERROR, f"{name}/ is missing", fixable=True)
    elif not path.is_dir():
        yield Finding(DIRECTORY, ERROR, f"{name}/ is a file, not a directory")


def _check_links(root: Path) -> Iterator[Finding]:
    """Each documented link is a symlink with exactly the documented target."""
    for relative, target in workspaces.LINKS:
        link = root / relative
        parent = link.parent
        if parent != root and (parent.is_symlink() or parent.exists()) and not parent.is_dir():
            where = parent.relative_to(root)
            yield Finding(LINK, ERROR, f"{where} is a file, not a directory; move it aside")
        elif link.is_symlink():
            actual = os.readlink(link)
            if actual != target:
                yield Finding(
                    LINK, ERROR, f"{relative} points to {actual}, not {target}", fixable=True
                )
        elif link.exists():
            kind = "directory" if link.is_dir() else "file"
            dest = "skills/" if target.endswith("skills") else "AGENTS.md"
            yield Finding(
                LINK,
                ERROR,
                f"{relative} is a real {kind}, not a symlink to {target}; "
                f"move its contents into {dest} and remove it",
            )
        else:
            yield Finding(
                LINK, ERROR, f"{relative} is missing (a symlink to {target})", fixable=True
            )


def _check_agents_md(root: Path, name: str) -> Iterator[Finding]:
    path = root / "AGENTS.md"
    if not path.is_file():
        yield Finding(AGENTS_MD, ERROR, "AGENTS.md is missing")
        return
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeError) as exc:
        yield Finding(AGENTS_MD, ERROR, f"could not read AGENTS.md: {exc}")
        return
    if text.strip() == workspaces.workspace_template(name).strip():
        yield Finding(
            AGENTS_MD,
            WARNING,
            "AGENTS.md is still the untouched template; say what the workspace is for",
        )


def _check_skills(found: list[skills.Skill], scope: str) -> Iterator[Finding]:
    """Problems, collisions, and reserved names for the managed ``scope`` entries.

    Enso installs its skills into the enso scope only, so a reserved name anywhere else,
    or one in the enso scope without a bundle name or official receipt, is unrecognized.
    """
    from .skill_catalog import official_installation

    for skill in found:
        if skill.scope != scope:
            continue
        for problem in skill.problems:
            yield Finding(SKILL, ERROR, f"skills/{skill.name}: {problem}")
        if workspaces.reserved(skill.name) and not (
            scope == "enso"
            and (skill.name in workspaces.BUNDLED_SKILLS or official_installation(skill.path))
        ):
            yield Finding(RESERVED, WARNING, _reserved_message("skills", skill.name))
        if skill.collision is not None:
            others = " and ".join(skill.collides_with)
            yield Finding(
                SKILL_COLLISION,
                skill.collision,
                f"skills/{skill.name} also exists in the {others} scope; "
                "the CLIs disagree about which copy wins",
            )


def _reserved_message(kind: str, name: str) -> str:
    return f"{kind}/{name} uses the reserved enso- prefix but Enso did not install it; rename it"


def _check_jobs(paths: Paths) -> Iterator[Finding]:
    """Job directories named as if Enso had installed them when it did not.

    Exempt by name, as for skills: a bundled name is Enso's whoever wrote the copy, since
    nothing records who did.
    """
    if not paths.jobs.is_dir():
        return
    for entry in sorted(paths.jobs.iterdir()):
        installed = entry.name in workspaces.BUNDLED_JOBS
        if entry.is_dir() and workspaces.reserved(entry.name) and not installed:
            yield Finding(RESERVED, WARNING, _reserved_message("jobs", entry.name))


def _check_entries(root: Path) -> Iterator[Finding]:
    """Top-level entries the layout has no place for. ``.git`` has its own check."""
    for entry in sorted(root.iterdir()):
        if entry.name in EXPECTED_ENTRIES or entry.name in IGNORED_ENTRIES or entry.name == ".git":
            continue
        shown = f"{entry.name}/" if entry.is_dir() else entry.name
        yield Finding(UNEXPECTED, WARNING, f"{shown} is not part of the layout")


def _check_workspace_entries(paths: Paths) -> Iterator[Finding]:
    """Entries under ``workspaces/`` that are not workspaces, which Enso silently skips."""
    if not paths.workspaces.is_dir():
        return
    for entry in sorted(paths.workspaces.iterdir()):
        if entry.name in IGNORED_ENTRIES:
            continue
        if not entry.is_dir():
            yield Finding(
                UNEXPECTED, WARNING, f"workspaces/{entry.name} is a file, not a workspace"
            )
        elif not valid_workspace_name(entry.name):
            yield Finding(
                UNEXPECTED,
                WARNING,
                f"workspaces/{entry.name}/ is not a workspace name (lowercase kebab-case)",
            )


def count_summary(errors: int, warnings: int) -> str:
    """``ok``, or counts like ``2 errors, 1 warning``; shared with ``enso doctor``."""
    parts = [
        f"{count} {noun}{'s' if count != 1 else ''}"
        for count, noun in ((errors, "error"), (warnings, "warning"))
        if count
    ]
    return ", ".join(parts) or "ok"


def _join(findings: list[Finding]) -> str:
    return "; ".join(finding.message for finding in findings)
