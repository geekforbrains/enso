"""The home scaffold (prompt, skills, jobs, Git root) and workspaces under ``$ENSO_HOME``."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from importlib import resources
from pathlib import Path

from .config import Agent, Paths, require_workspace, valid_workspace_name

# ``src/enso/bundled/`` mirrors the home: ``AGENTS.md``, ``skills/<name>/SKILL.md``, and
# ``jobs/<name>/`` (a ``JOB.md`` with its scripts) are copied to the same path under
# ``$ENSO_HOME`` when missing. ``bundled/workspace/AGENTS.md`` is the per-workspace template,
# stamped by ``create_workspace``; a job is stamped with the agent chosen at setup.
# Managed updates refresh only bundle files matching their recorded baseline. What
# Enso installs says so in its name: ``enso`` and
# ``enso-*`` are reserved for it, so a skill or job of that name Enso did not install is an
# audit warning, and a refresh rewrites only these and never the operator's own.
BUNDLED_SKILLS = (
    "enso",
    "enso-browser",
    "enso-heartbeat",
    "enso-jobs",
    "enso-knowledge",
    "enso-security",
    "enso-skills",
    "enso-slack",
    "enso-tables",
    "enso-tasks",
    "enso-update",
    "enso-workflow",
    "enso-workspace",
)
# Explicitly list support files too: a local __pycache__ must never become a bundle.
BUNDLED_SKILL_SUPPORT = {
    "enso-browser": ("scripts/browser.py", "references/setup.md"),
    "enso-knowledge": ("references/formatting.md", "scripts/lint.py"),
}
BUNDLED_JOBS: tuple[str, ...] = ("enso-audit", "enso-update")
BUNDLED_FILES = ("slack/manifest.json",)
RESERVED_PREFIX = "enso-"
# The documented workspace layout (docs/workspaces.md § Layout): the directories, and the
# links the home carries too. Link targets are relative to the link's own directory. The
# provider CLIs find the skill links by walking up from the workspace to the Git root.
WORKSPACE_DIRS = ("skills", "knowledge", "memory", "jobs", "projects", "drafts", "uploads")
LINKS = (
    ("CLAUDE.md", "AGENTS.md"),
    (".claude/skills", "../skills"),
    (".agents/skills", "../skills"),
)


def reserved(name: str) -> bool:
    """Whether ``name`` is in the namespace Enso keeps for what it installs."""
    return name == "enso" or name.startswith(RESERVED_PREFIX)


def _bundled(relative: str) -> str:
    """The text of ``src/enso/bundled/<relative>``."""
    return resources.files("enso").joinpath("bundled", relative).read_text("utf-8")


def bundled_skill_files() -> tuple[str, ...]:
    """Home-relative paths of the explicitly shipped skill instructions and helpers."""
    return tuple(
        f"skills/{name}/{filename}"
        for name in BUNDLED_SKILLS
        for filename in ("SKILL.md", *BUNDLED_SKILL_SUPPORT.get(name, ()))
    )


def _stamp(text: str, values: Mapping[str, str]) -> str:
    """Fill the ``{{key}}`` placeholders named in ``values``; any other is left as written.

    Only the named keys are touched, so a job prompt keeps its ``{{prerun_output}}`` for
    the runner to fill at run time.
    """
    return re.sub(r"\{\{([a-z_]+)\}\}", lambda match: values.get(match[1], match[0]), text)


def write_missing(target: Path, text: str) -> bool:
    """Publish a complete seed file atomically, preserving any existing path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent) as temporary:
        temporary.write(text.encode("utf-8"))
        temporary.flush()
        os.fsync(temporary.fileno())
        try:
            os.link(temporary.name, target)
        except FileExistsError:
            return False
    return True


def _seed(
    paths: Paths,
    relative: str,
    done: list[str],
    *,
    refresh: bool = False,
    stamp: Mapping[str, str] | None = None,
) -> None:
    """Copy one bundled file to the same path under the home when it is missing.

    With ``refresh`` a differing copy is rewritten and the old one kept beside it as
    ``<name>.bak``. Otherwise an existing file is the operator's and is never touched.
    ``stamp`` fills the file's ``{{key}}`` placeholders first.
    """
    target = paths.home / relative
    if any(
        part.is_symlink() for part in (target, *target.parents) if part.is_relative_to(paths.home)
    ):
        return  # Seeding new helpers must not follow an operator's directory link.
    text = _bundled(relative)
    if stamp:
        text = _stamp(text, stamp)
    if target.exists() and (not refresh or target.read_text("utf-8") == text):
        return
    if target.exists():
        backup = target.with_name(target.name + ".bak")
        shutil.copy2(target, backup)
        done.append(f"kept the old {target} as {backup.name}")
    if not refresh:
        if not write_missing(target, text):
            return
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    done.append(f"wrote {target}")
    _record_bundle(paths, relative, text)


def _record_bundle(paths: Paths, relative: str, text: str) -> None:
    from .maintenance import read_json, write_json

    state = read_json(paths.home / ".bundles.json")
    files = state.setdefault("files", {})
    files[relative] = hashlib.sha256(text.encode()).hexdigest()
    write_json(paths.home / ".bundles.json", state)


def workspace_template(name: str) -> str:
    """The workspace ``AGENTS.md`` template stamped with ``name``: what a new workspace gets."""
    return _stamp(_bundled("workspace/AGENTS.md"), {"workspace_name": name})


def seed_home(paths: Paths, *, refresh_skills: bool = False) -> list[str]:
    """Seed AGENTS.md, the bundled skills, the links, and a Git root; says what changed.

    AGENTS.md is the operator's once it exists. Skills are written when missing, and
    rewritten with ``refresh_skills`` (a differing copy is kept as ``SKILL.md.bak``). Jobs
    are ``seed_jobs``'s: they need the agent chosen at setup, and no refresh reaches them.
    """
    done: list[str] = []
    paths.home.mkdir(parents=True, exist_ok=True)
    _seed(paths, "AGENTS.md", done)
    for relative in BUNDLED_FILES:
        _seed(paths, relative, done)
    for relative in bundled_skill_files():
        _seed(paths, relative, done, refresh=refresh_skills)
    done.extend(ensure_home(paths.home))
    return done


def seed_jobs(paths: Paths, agent: Agent) -> list[str]:
    """Install the bundled jobs that are not there yet, stamped with ``agent``; says what changed.

    A job is written once: an existing ``jobs/<name>/`` is the operator's whatever it holds
    (an edited ``JOB.md`` stays, a deleted script is not put back), and no refresh flag
    reaches jobs. ``provider``, ``model``, and ``effort`` come from the default agent chosen
    at setup, so the job runs with what the operator picked rather than a guess of ours.
    """
    done: list[str] = []
    if paths.jobs.is_symlink():
        raise OSError("jobs directory must not be a symbolic link")
    # The job template wraps these fields in YAML double quotes. JSON escaping preserves
    # arbitrary configured model ids as one scalar rather than injecting frontmatter.
    stamps = {key: json.dumps(value)[1:-1] for key, value in asdict(agent).items()}
    for name in BUNDLED_JOBS:
        if (paths.jobs / name).exists():
            continue
        paths.jobs.mkdir(parents=True, exist_ok=True)
        bundled = resources.files("enso").joinpath("bundled", "jobs", name)
        # Publish the whole job at once: an interrupted install cannot leave an owned-looking
        # partial directory which future setup correctly refuses to repair over user edits.
        with tempfile.TemporaryDirectory(dir=paths.jobs, prefix=".seed-") as staging:
            staged = Path(staging) / name
            staged.mkdir()
            names = []
            for entry in sorted(bundled.iterdir(), key=lambda entry: entry.name):
                if entry.is_file():
                    write_missing(staged / entry.name, _stamp(entry.read_text("utf-8"), stamps))
                    names.append(entry.name)
            try:
                os.rename(staged, paths.jobs / name)
            except FileExistsError:
                continue
            done.extend(f"wrote {paths.jobs / name / filename}" for filename in names)
            for filename in names:
                _record_bundle(
                    paths,
                    f"jobs/{name}/{filename}",
                    (paths.jobs / name / filename).read_text("utf-8"),
                )
    return done


def _new_bundle_file(relative: str, previous: dict[str, str], existing: set[str]) -> bool:
    """Distinguish a new helper from a deleted file or an untracked historical bundle."""
    if relative in previous:
        return False
    if not relative.startswith(("skills/", "jobs/")):
        return True
    bundle = "/".join(relative.split("/")[:2])
    tracked = any(item.startswith(bundle + "/") for item in previous)
    return tracked == (bundle in existing)


def reconcile_bundles(paths: Paths, agent: Agent) -> list[str]:
    """Refresh proven untouched files; retain edits and remembered deletions.

    Historical files without a receipt are user-owned. New bundle names are
    installed when absent, but removing a previously tracked bundle is a choice
    that survives later releases. Existing jobs keep their own agent triple.
    """
    import yaml

    from .maintenance import read_json, write_bytes, write_json

    state = read_json(paths.home / ".bundles.json")
    previous = state.get("files", {})
    known = dict(previous)
    contents = {"AGENTS.md": _bundled("AGENTS.md")}
    contents.update({name: _bundled(name) for name in BUNDLED_FILES})
    contents.update({relative: _bundled(relative) for relative in bundled_skill_files()})
    for name in BUNDLED_JOBS:
        values = asdict(agent)
        job = paths.jobs / name / "JOB.md"
        if job.is_file() and not job.is_symlink():
            try:
                header = yaml.safe_load(job.read_text("utf-8").split("---", 2)[1])
                values = {key: header[key] for key in values}
                if not all(isinstance(value, str) for value in values.values()):
                    continue
            except ValueError, IndexError, KeyError, TypeError, yaml.YAMLError:
                continue
        stamps = {key: json.dumps(value)[1:-1] for key, value in values.items()}
        for entry in resources.files("enso").joinpath("bundled", "jobs", name).iterdir():
            if entry.is_file():
                contents[f"jobs/{name}/{entry.name}"] = _stamp(entry.read_text("utf-8"), stamps)
    changed: list[str] = []
    preexisting_bundles = {
        "/".join(relative.split("/")[:2])
        for relative in contents
        if relative.startswith(("skills/", "jobs/"))
        and (paths.home / "/".join(relative.split("/")[:2])).exists()
    }
    for relative, text in contents.items():
        target = paths.home / relative
        digest = hashlib.sha256(text.encode()).hexdigest()
        # Never follow a user link while reconciling shipped content.
        if any(
            part.is_symlink() for part in (target, *target.parents) if part != paths.home.parent
        ):
            continue
        if target.exists():
            if not target.is_file() or relative not in previous:
                continue
            if hashlib.sha256(target.read_bytes()).hexdigest() != previous[relative]:
                continue
        elif not _new_bundle_file(relative, previous, preexisting_bundles):
            continue
        if target.is_file() and target.read_bytes() == text.encode():
            known[relative] = digest
            continue
        write_bytes(target, text.encode())
        known[relative] = digest
        changed.append(relative)
    write_json(paths.home / ".bundles.json", {"files": known})
    if not paths.knowledge.exists() and not paths.knowledge.is_symlink():
        paths.knowledge.mkdir()
        changed.append("knowledge/")
    return changed


def ensure_home(home: Path) -> list[str]:
    """Create missing ``skills/``, ``knowledge/``, the links, and a Git root.

    Never writes content: ``AGENTS.md`` and the skills are ``seed_home``'s. The CLIs find
    them by walking up from a workspace to the Git root, and Enso never commits there.
    """
    done: list[str] = []
    for directory in (Paths(home).skills, Paths(home).knowledge):
        if not directory.exists() and not directory.is_symlink():
            directory.mkdir(parents=True)
            done.append(f"created {directory}")
    done.extend(ensure_links(home))
    if not (home / ".git").exists() and shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=home, check=True, capture_output=True, timeout=15)
        done.append(f"ran git init in {home}")
    return done


def ensure_links(root: Path) -> list[str]:
    """Create or repoint the ``CLAUDE.md`` and skill links under ``root``; says what changed.

    A link is repointed only when it is a symlink to the wrong place. A real file or
    directory sitting where a link belongs (or where its parent belongs) is left alone for
    the audit to report.
    """
    done: list[str] = []
    if root.is_symlink():
        return done
    for relative, target in LINKS:
        link = root / relative
        if link.parent != root and link.parent.is_symlink():
            continue
        if link.is_symlink():
            if os.readlink(link) == target:
                continue
            link.unlink()
            os.symlink(target, link)
            done.append(f"repointed {link} -> {target}")
        elif link.exists():
            continue
        else:
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(target, link)
            except OSError:
                continue  # a file where .claude/ or .agents/ belongs, say
            done.append(f"linked {link} -> {target}")
    return done


def ensure_layout(root: Path) -> list[str]:
    """Create what the documented layout is missing under ``root``; says what changed.

    Directories and links only: ``AGENTS.md`` is the operator's file and is never written
    here, and nothing that exists is removed or rewritten. Safe to run on a live workspace.
    """
    done: list[str] = []
    if any(path.is_symlink() for path in (root, root.parent, root.parent.parent)):
        return done
    for name in WORKSPACE_DIRS:
        directory = root / name
        if not directory.exists() and not directory.is_symlink():
            directory.mkdir(parents=True)
            done.append(f"created {directory}")
    done.extend(ensure_links(root))
    return done


def list_workspaces(paths: Paths) -> list[str]:
    if paths.home.is_symlink() or paths.workspaces.is_symlink() or not paths.workspaces.is_dir():
        return []
    return sorted(
        entry.name
        for entry in paths.workspaces.iterdir()
        if not entry.is_symlink() and entry.is_dir() and valid_workspace_name(entry.name)
    )


def create_workspace(paths: Paths, name: str) -> Path:
    """Create ``<name>/`` in the documented layout, with ``AGENTS.md`` from the template."""
    if not valid_workspace_name(name):
        raise ValueError("workspace names are lowercase kebab-case (letters, digits, hyphens)")
    root = paths.workspace(name)
    if paths.home.is_symlink() or paths.workspaces.is_symlink():
        raise ValueError("home and workspaces must be real directories, not symbolic links")
    if root.exists() or root.is_symlink():
        raise FileExistsError(f"workspace {name} already exists at {root}")
    root.mkdir(parents=True)
    (root / "AGENTS.md").write_text(workspace_template(name), "utf-8")
    ensure_layout(root)
    return root


def new_uploads_dir(paths: Paths, workspace: str) -> Path:
    """A fresh ``<workspace>/uploads/<8 hex>/`` for one turn's attachments."""
    require_workspace(paths, workspace)
    uploads = paths.workspace_uploads(workspace)
    if uploads.is_symlink():
        raise ValueError(f"{uploads}: uploads must be a real directory")
    directory = uploads / uuid.uuid4().hex[:8]
    directory.mkdir(parents=True)
    return directory
