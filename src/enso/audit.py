"""The installation audit: the documented layout, the skill wiring, and what ``--fix`` repairs.

Every row of the check table in ``docs/workspaces.md`` § Auditing, for the home and for
each workspace. The layout itself is :mod:`enso.layout`'s, so setup, the scaffold, and
this audit agree on what belongs where and who owns it. A finding carries a stable check
id, a severity, a message, whether ``--fix`` repairs it, and whether it is worth reporting
to the operator on its own. ``--fix`` only creates and repairs (directories, links, the
home's Git root, and the permissions of the roots Enso keeps private); it never deletes,
never edits ``AGENTS.md``, and never touches the contents of a knowledge root, ``work/``,
``drafts/``, or ``uploads/``. ``enso doctor`` and the viewer share the report.

Only a root's own top-level entries are classified; ``shared/`` is checked the same way as
the home. Nothing recurses into a core-managed root such as ``runtime/`` or ``cache/``, so
private operating state is never mistaken for the operator's clutter.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import layout, skills, workspaces
from .config import (
    Config,
    Paths,
    ProjectConfig,
    require_default_workspace,
    require_workspace,
    valid_workspace_name,
)
from .jobs import load_jobs
from .skills import ERROR, WARNING

# Check ids: one per row of the check table, stable for --json consumers.
DIRECTORY = "directory"  # a required directory is missing or an operating root is a file
LINK = "link"  # a documented link is wrong, or a top-level link is irregular
GIT_ROOT = "git-root"  # the home is not a Git root, or a workspace is one
AGENTS_MD = "agents-md"  # missing, unreadable, or still the untouched template
SKILL = "skill"  # a managed skill directory's SKILL.md is missing or wrong
SKILL_COLLISION = "skill-collision"  # a name shared with another scope
ORPHAN = "orphan"  # nothing is bound to the workspace and no job names it
UNEXPECTED = "unexpected"  # an entry the layout has no place for
RESERVED = "reserved"  # an enso-* skill or job that Enso did not install
PERMISSIONS = "permissions"  # a root Enso keeps private is readable by other users
STALE = "stale"  # a generated file whose owner is gone
SCRIPT = "script"  # a project command names a ./script that is missing or not executable


@dataclass(frozen=True)
class Finding:
    """One thing wrong with a root, from one check."""

    check: str
    severity: str  # error | warning
    message: str
    fixable: bool = False  # --fix creates or repairs it
    # Worth telling the operator even at warning severity, because it is a portable fact
    # about the installation rather than a matter of taste. Errors need no such mark: they
    # already fail the audit. See ``Report.attention``.
    attention: bool = False

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "fixable": self.fixable,
            "attention": self.attention,
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
    def attention(self) -> bool:
        """Anything here is worth reporting: an error, or a marked warning."""
        return bool(self.errors) or any(finding.attention for finding in self.warnings)

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
    # Every top-level entry that is actually there, by layout category. A consumer reads
    # ownership from here instead of guessing from a name.
    layout: dict[str, str] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "status": self.status,
            "attention": self.attention,
            "layout": dict(self.layout),
            "findings": [finding.as_dict() for finding in self.findings],
            "fixed": list(self.fixed),
        }


@dataclass(kw_only=True)
class WorkspaceAudit(_Root):
    """One workspace against the documented layout, plus what uses it."""

    name: str
    path: Path
    bindings: list[str] = field(default_factory=list)  # binding keys pointing here
    jobs: list[str] = field(default_factory=list)  # qualified references of jobs owned by it
    uploads_bytes: int = 0
    layout: dict[str, str] = field(default_factory=dict)  # present entries, by category
    findings: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "status": self.status,
            "attention": self.attention,
            "bindings": list(self.bindings),
            "jobs": list(self.jobs),
            "uploads_bytes": self.uploads_bytes,
            "layout": dict(self.layout),
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

    @property
    def attention(self) -> bool:
        """Whether anything here is worth reporting to the operator unprompted.

        Wider than ``not ok``: a tidy-but-healthy installation is ``ok`` while still
        having, say, an unexpected entry or a world-readable ``config.json`` to mention.
        """
        return self.home.attention or any(space.attention for space in self.workspaces)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "attention": self.attention,
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

    ``config`` supplies bindings and project definitions. Without it the orphan and script
    checks are skipped, since a workspace may be bound in a config that cannot be read. Jobs
    are read either way, though some stage jobs cannot be parsed without their definitions.
    ``user_dirs`` overrides where user-level skills are looked for (tests).
    """
    jobs, _ = load_jobs(paths, config)
    named: dict[str, list[str]] = {}
    for job in jobs:
        named.setdefault(job.workspace, []).append(job.ref)
    if names is None:
        names = workspaces.list_workspaces(paths)
    found = []
    for name in names:
        bound = None
        projects: list[ProjectConfig] = []
        if config is not None:
            bound = sorted(key for key, target in config.bindings.items() if target == name)
            projects = [p for p in config.projects.values() if p.workspace == name]
        result = audit_workspace(
            paths,
            name,
            fix=fix,
            bound=bound,
            jobs=named.get(name, []),
            projects=projects,
            user_dirs=user_dirs,
        )
        found.append(result)
    return Report(audit_home(paths, fix=fix, user_dirs=user_dirs), found)


def audit_home(
    paths: Paths, *, fix: bool = False, user_dirs: Sequence[Path] | None = None
) -> HomeAudit:
    """The Git root, links, instructions, skills, shared knowledge, jobs, and workspaces."""
    home = paths.home
    result = HomeAudit(path=home)
    if home.is_symlink():
        result.findings.append(Finding(DIRECTORY, ERROR, "home must not be a symbolic link"))
        return result
    if fix:
        result.fixed.extend(workspaces.ensure_home(home))
        result.fixed.extend(_repair_permissions(paths))
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
    findings.extend(_check_shared(paths))
    findings.extend(_check_links(home))
    if not paths.agents_md.is_file():
        findings.append(Finding(AGENTS_MD, ERROR, "AGENTS.md is missing"))
    findings.extend(_check_skills(skills.resolve(paths, user_dirs=user_dirs), "enso"))
    findings.extend(_check_jobs(paths))
    findings.extend(_check_workspace_entries(paths))
    try:
        require_default_workspace(paths)
    except ValueError as exc:
        findings.append(Finding(DIRECTORY, ERROR, str(exc)))
    findings.extend(_check_permissions(paths))
    findings.extend(_check_stale(paths))
    result.layout = _scan_entries(home, layout.HOME, findings)
    return result


def audit_workspace(
    paths: Paths,
    name: str,
    *,
    fix: bool = False,
    bound: Sequence[str] | None = None,
    jobs: Sequence[str] = (),
    projects: Sequence[ProjectConfig] = (),
    user_dirs: Sequence[Path] | None = None,
) -> WorkspaceAudit:
    """One workspace. ``bound`` is its binding keys, or None to skip the orphan check.

    ``projects`` are the workspace's own definitions; their script references are checked.
    """
    root = paths.workspaces / name
    result = WorkspaceAudit(name=name, path=root, bindings=list(bound or ()), jobs=list(jobs))
    if not valid_workspace_name(name):
        result.findings.append(Finding(UNEXPECTED, ERROR, "names are lowercase kebab-case"))
        return result
    try:
        require_workspace(paths, name)
    except ValueError as exc:
        result.findings.append(Finding(DIRECTORY, ERROR, str(exc)))
        return result
    if fix:
        result.fixed.extend(workspaces.ensure_layout(root, include_optional=False))
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
    findings.extend(_check_scripts(paths, projects))
    result.layout = _scan_entries(root, layout.WORKSPACE, findings)
    if name != "default" and bound is not None and not bound and not jobs:
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
    for name in (*layout.WORKSPACE_DIRS, "knowledge", "drafts", "heartbeat"):
        entry = layout.classify(layout.WORKSPACE, name)
        if (
            entry is not None
            and not entry.required
            and not (root / name).exists()
            and not (root / name).is_symlink()
        ):
            continue
        yield from _check_dir(root / name, name, allow_link=name in {"skills", "drafts"})


def _check_shared(paths: Paths) -> list[Finding]:
    """``shared/`` like the home: its required knowledge root, then anything unclaimed."""
    findings = list(_check_dir(paths.shared, "shared", allow_link=False))
    if not findings:
        findings.extend(_check_dir(paths.knowledge, "shared/knowledge", allow_link=False))
        _scan_entries(paths.shared, layout.SHARED, findings)
    return findings


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
    for relative, target in layout.LINKS:
        link = root / relative
        parent = link.parent
        if parent != root and (parent.is_symlink() or (parent.exists() and not parent.is_dir())):
            where = parent.relative_to(root)
            kind = "symbolic link" if parent.is_symlink() else "file"
            yield Finding(LINK, ERROR, f"{where} is a {kind}, not a directory; move it aside")
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
    for workspace in workspaces.list_workspaces(paths):
        root = paths.workspace_jobs(workspace)
        if root.is_symlink() or not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            installed = workspace == "default" and entry.name in workspaces.BUNDLED_JOBS
            if entry.is_dir() and workspaces.reserved(entry.name) and not installed:
                yield Finding(
                    RESERVED, WARNING, _reserved_message(f"workspaces/{workspace}/jobs", entry.name)
                )


def _check_scripts(paths: Paths, projects: Sequence[ProjectConfig]) -> Iterator[Finding]:
    """A project command that is one ``./script`` beside ``PROJECT.md`` which cannot run.

    Only that documented shape is inspected: a bare relative path and nothing else. Any
    other command is shell, which Enso does not parse; the runtime reports it at exit
    126/127 when it fails. This only stats a path, so a false match costs one warning.
    """
    for project in projects:
        directory = paths.project(project.workspace, project.key)
        for source, command in _project_commands(project):
            words = command.split()
            if len(words) != 1 or not words[0].startswith("./"):
                continue
            script = directory / words[0]
            if not script.exists():
                problem = "does not exist"
            elif not script.is_file() or not os.access(script, os.X_OK):
                problem = "is not an executable file"
            else:
                continue
            yield Finding(
                SCRIPT,
                WARNING,
                f"projects/{project.key}: {source} runs {words[0]}, which {problem}",
                attention=True,
            )


def _project_commands(project: ProjectConfig) -> Iterator[tuple[str, str]]:
    """Every command string a project can run, with the field that names it."""
    if project.setup:
        yield "setup", project.setup
    for stage in project.stages:
        if stage.command:
            yield f"stage {stage.name}", stage.command
        for check in stage.checks:
            yield f"check {check.name}", check.command
    for event, command in project.hooks.items():
        yield f"hook {event}", command


def _scan_entries(
    root: Path, table: tuple[layout.Entry, ...], findings: list[Finding]
) -> dict[str, str]:
    """Classify the root's own top-level entries; append what the layout cannot place.

    Returns the present names mapped to their category, so a consumer can tell a
    core-managed root from a user one without knowing the table. Nothing is opened and no
    directory is descended into: what lives inside a declared root is its owner's business.

    A required entry has its own check above, which already describes a missing or
    irregular one; this scan covers optional entries and the names the table does not claim.
    """
    present: dict[str, str] = {}
    if root.is_symlink() or not root.is_dir():
        return present
    for path in sorted(root.iterdir()):
        if path.name in layout.IGNORED:
            continue
        entry = layout.classify(table, path.name)
        present[path.name] = entry.category if entry else layout.UNEXPECTED
        if entry is None:
            # A workspace's own ``.git`` is unexpected, but the Git-root check above already
            # reported it, at error severity and saying what it actually breaks.
            if path.name != ".git":
                shown = f"{path.name}/" if path.is_dir() else path.name
                findings.append(
                    Finding(
                        UNEXPECTED,
                        WARNING,
                        f"{shown} is not part of the layout; move it out of {root} or remove it",
                        attention=True,
                    )
                )
        elif entry.real_directory and path.is_symlink():
            kind = "a dangling symbolic link" if not path.exists() else "a symbolic link"
            findings.append(
                Finding(
                    LINK,
                    WARNING,
                    f"{path.name}/ is {kind}, not a real directory; move it aside",
                    attention=True,
                )
            )
        elif entry.real_directory and not path.is_dir():
            findings.append(
                Finding(
                    DIRECTORY,
                    WARNING,
                    f"{path.name}/ is a file, not a directory; move it aside",
                    attention=True,
                )
            )
        elif (
            path.is_symlink()
            and not path.exists()
            and not entry.required
            and not (
                table is layout.WORKSPACE
                and path.name in {*layout.WORKSPACE_DIRS, "knowledge", "drafts", "heartbeat"}
            )
        ):
            findings.append(
                Finding(
                    LINK,
                    WARNING,
                    f"{path.name} is a dangling symbolic link ({entry.what}); "
                    "repoint it or remove it",
                    attention=True,
                )
            )
    return present


def _check_permissions(paths: Paths) -> Iterator[Finding]:
    """Roots Enso keeps private that other users on this machine can read.

    Only where Enso owns the security contract: the credentials it writes and the private
    state it creates. It says nothing about what the operator's own umask does elsewhere.
    """
    for entry in layout.private(layout.HOME):
        path = paths.home / entry.name
        if path.is_symlink() or not path.exists():
            continue
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            continue
        if not mode & layout.SHARED_BITS:
            continue
        wanted = mode & ~layout.SHARED_BITS
        yield Finding(
            PERMISSIONS,
            WARNING,
            f"{path} holds {entry.what} and is readable by other users on this machine "
            f"(mode {mode:04o}); remove group and other access to make it {wanted:04o}",
            fixable=True,
            attention=True,
        )


def _repair_permissions(paths: Paths) -> list[str]:
    """``--fix`` for the check above: tighten, never widen, and never follow a link."""
    done: list[str] = []
    for entry in layout.private(layout.HOME):
        path = paths.home / entry.name
        if path.is_symlink() or not path.exists():
            continue
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if not mode & layout.SHARED_BITS:
                continue
            wanted = mode & ~layout.SHARED_BITS
            path.chmod(wanted)
        except OSError:
            continue  # the check reports it; a repair that cannot run is not a failure
        done.append(f"restricted {path} to {wanted:04o}")
    return done


def _check_stale(paths: Paths) -> Iterator[Finding]:
    """Generated files whose owner is gone, where that is certain rather than a guess.

    SQLite's sidecars belong to ``enso.db``. Without it they are unreadable leftovers, and
    a later database would not adopt them. Nothing deletes them for the operator.
    """
    if paths.db.exists() or paths.db.is_symlink():
        return
    for name in ("enso.db-wal", "enso.db-shm"):
        path = paths.home / name
        if path.exists() or path.is_symlink():
            yield Finding(
                STALE,
                WARNING,
                f"{path} is left over from a removed enso.db and is safe to delete",
                attention=True,
            )


def _check_workspace_entries(paths: Paths) -> Iterator[Finding]:
    """Entries under ``workspaces/`` that are not workspaces, which Enso silently skips."""
    if paths.workspaces.is_symlink():
        yield Finding(DIRECTORY, ERROR, "workspaces/ must be a real directory, not a symbolic link")
        return
    if not paths.workspaces.is_dir():
        if paths.workspaces.exists():
            yield Finding(DIRECTORY, ERROR, "workspaces/ is a file, not a directory")
        else:
            yield Finding(DIRECTORY, ERROR, "workspaces/ is missing", fixable=True)
        return
    for entry in sorted(paths.workspaces.iterdir()):
        if entry.name in layout.IGNORED:
            continue
        if entry.is_symlink():
            yield Finding(DIRECTORY, ERROR, f"workspaces/{entry.name} must not be a symbolic link")
        elif not entry.is_dir():
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
