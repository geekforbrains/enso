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

from . import frontmatter, layout
from .config import Agent, Paths, require_workspace, valid_workspace_name
from .layout import LINKS, WORKSPACE_DIRS

# Home-copied files in ``src/enso/bundled/`` keep their relative paths under ``$ENSO_HOME``.
# The Slack manifest stays packaged for ``enso slack manifest`` but is not home-copied.
# ``jobs/<name>/`` seeds maintenance jobs in the default workspace.
# ``bundled/workspace/AGENTS.md`` is the per-workspace template,
# stamped by ``create_workspace``; a job is stamped with the agent chosen at setup.
# Managed updates refresh only bundle files matching their recorded baseline.
# Bundled job and skill names use the reserved ``enso`` and ``enso-*`` namespace.
BUNDLED_SKILLS = (
    "enso",
    "enso-browser",
    "enso-heartbeat",
    "enso-jobs",
    "enso-knowledge",
    "enso-projects",
    "enso-security",
    "enso-skills",
    "enso-slack",
    "enso-tables",
    "enso-update",
    "enso-workspace",
)
# Explicitly list support files too: a local __pycache__ must never become a bundle.
BUNDLED_SKILL_SUPPORT = {
    "enso-browser": ("references/setup.md",),
    "enso-knowledge": ("references/formatting.md", "scripts/lint.py"),
    "enso-projects": (
        "references/projects.md",
        "references/tasks.md",
        "references/workflows.md",
    ),
}
BUNDLED_JOBS: tuple[str, ...] = ("enso-audit", "enso-update")
BUNDLED_FILES: tuple[str, ...] = ()
RESERVED_PREFIX = "enso-"


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


def _seed(paths: Paths, relative: str, done: list[str]) -> None:
    """Copy a missing bundled file; upgrades use receipt-based reconciliation."""
    target = paths.home / relative
    if any(
        part.is_symlink() for part in (target, *target.parents) if part.is_relative_to(paths.home)
    ):
        return  # Seeding new helpers must not follow an operator's directory link.
    if target.exists():
        return
    text = _bundled(relative)
    if not write_missing(target, text):
        return
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


def seed_home(paths: Paths) -> list[str]:
    """Seed AGENTS.md, the bundled skills, the links, and a Git root; says what changed.

    Existing content is preserved. Jobs are ``seed_jobs``'s: they need the agent chosen
    at setup. Managed updates reconcile bundles separately using recorded baselines.
    """
    done: list[str] = []
    paths.home.mkdir(parents=True, exist_ok=True)
    _seed(paths, "AGENTS.md", done)
    for relative in BUNDLED_FILES:
        _seed(paths, relative, done)
    for relative in bundled_skill_files():
        _seed(paths, relative, done)
    done.extend(ensure_home(paths.home))
    return done


def seed_jobs(paths: Paths, agent: Agent) -> list[str]:
    """Install the bundled jobs that are not there yet, stamped with ``agent``; says what changed.

    A job is written once: an existing ``jobs/<name>/`` is the operator's whatever it holds
    (an edited ``JOB.md`` stays, a deleted script is not put back), and no refresh flag
    reaches jobs. ``provider``, ``model``, and ``effort`` come from the default agent chosen
    at setup. Maintenance jobs belong to the default workspace.
    """
    done: list[str] = []
    require_workspace(paths, "default")
    jobs_root = paths.workspace_jobs("default")
    if jobs_root.is_symlink():
        raise OSError("jobs directory must not be a symbolic link")
    for name in BUNDLED_JOBS:
        # Templates quote these fields; JSON escaping keeps configured IDs in one scalar.
        stamps = {key: json.dumps(value)[1:-1] for key, value in asdict(agent).items()}
        relative = f"workspaces/default/jobs/{name}"
        target = jobs_root / name
        if target.exists() or target.is_symlink():
            continue
        jobs_root.mkdir(parents=True, exist_ok=True)
        bundled = resources.files("enso").joinpath("bundled", "jobs", name)
        # Publish the whole job at once: an interrupted install cannot leave an owned-looking
        # partial directory which future setup correctly refuses to repair over user edits.
        with tempfile.TemporaryDirectory(dir=jobs_root, prefix=".seed-") as staging:
            staged = Path(staging) / name
            staged.mkdir()
            names = []
            for entry in sorted(bundled.iterdir(), key=lambda entry: entry.name):
                if entry.is_file():
                    write_missing(staged / entry.name, _stamp(entry.read_text("utf-8"), stamps))
                    names.append(entry.name)
            try:
                os.rename(staged, jobs_root / name)
            except FileExistsError:
                continue
            done.extend(f"wrote {jobs_root / name / filename}" for filename in names)
            for filename in names:
                _record_bundle(
                    paths,
                    f"{relative}/{filename}",
                    (jobs_root / name / filename).read_text("utf-8"),
                )
    return done


def _bundle_root(relative: str) -> str | None:
    if relative.startswith("skills/"):
        return "/".join(relative.split("/")[:2])
    parts = relative.split("/")
    if len(parts) >= 4 and parts[0] == "workspaces" and parts[2] == "jobs":
        return "/".join(relative.split("/")[:4])
    return None


def _new_bundle_file(relative: str, previous: dict[str, str], existing: set[str]) -> bool:
    """Distinguish a new helper from a deleted file or an untracked historical bundle."""
    if relative in previous:
        return False
    bundle = _bundle_root(relative)
    if bundle is None:
        return True
    tracked = any(item.startswith(bundle + "/") for item in previous)
    return tracked == (bundle in existing)


def _installed_agent(job: Path, default: Agent) -> Agent | None:
    if not job.is_file() or job.is_symlink():
        return default
    try:
        fields = frontmatter.read(job).fields
        values = [
            value
            for key in ("provider", "model", "effort")
            if isinstance(value := fields.get(key), str)
        ]
        return Agent(*values) if len(values) == 3 else None
    except OSError, ValueError:
        return None


def _bundle_receipts(paths: Paths) -> dict[str, str]:
    """Validate persisted paths before using old installation receipts to remove files."""
    from .maintenance import UpdateError, read_json

    previous = read_json(paths.home / ".bundles.json").get("files", {})
    if not isinstance(previous, dict) or any(
        not isinstance(relative, str)
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or "\x00" in relative
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for relative, digest in previous.items()
    ):
        raise UpdateError(".bundles.json contains invalid file paths or hashes")
    return previous


def _retire_bundles(paths: Paths, previous: dict[str, str], shipped: set[str]) -> list[str]:
    """Remove unchanged retired files and empty bundle directories within snapshot roots."""
    changed = []
    for relative in sorted(previous.keys() - shipped):
        parts = relative.split("/")
        bundle = _bundle_root(relative)
        if bundle is not None:
            name = bundle.split("/")[-1]
            if (
                relative == bundle
                or not reserved(name)
                or not valid_workspace_name(name)
                or not valid_workspace_name(parts[1])
            ):
                continue
            root = paths.home / bundle
        elif parts[0] == "slack" and len(parts) > 1:
            root = paths.home / "slack"
        else:
            continue  # Other old home locations need an explicit migration and snapshot.
        target = paths.home / relative
        if any(
            part.is_symlink()
            for part in (target, *target.parents)
            if part.is_relative_to(paths.home)
        ):
            continue
        if (
            not target.is_file()
            or hashlib.sha256(target.read_bytes()).hexdigest() != previous[relative]
        ):
            continue
        target.unlink()
        changed.append(relative)
        parent = target.parent
        while parent.is_relative_to(root) and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return changed


def reconcile_bundles(paths: Paths, agent: Agent) -> list[str]:
    """Refresh or retire proven untouched files; retain edits and remembered deletions.

    Historical files without a receipt are user-owned. New bundle names are
    installed when absent, but removing a previously tracked bundle is a choice
    that survives later releases. Existing jobs keep their own agent triple.
    """
    from .maintenance import write_bytes, write_json

    previous = _bundle_receipts(paths)
    known = dict(previous)
    contents = {"AGENTS.md": _bundled("AGENTS.md")}
    contents.update({name: _bundled(name) for name in BUNDLED_FILES})
    contents.update({relative: _bundled(relative) for relative in bundled_skill_files()})
    shipped = set(contents)
    changed: list[str] = []
    for name in BUNDLED_JOBS:
        relative = f"workspaces/default/jobs/{name}"
        entries = tuple(
            entry
            for entry in resources.files("enso").joinpath("bundled", "jobs", name).iterdir()
            if entry.is_file()
        )
        # A malformed customized JOB.md must not make its still-shipped files look retired.
        shipped.update(f"{relative}/{entry.name}" for entry in entries)
        job = paths.home / relative / "JOB.md"
        selected = _installed_agent(job, agent)
        if selected is None:
            continue
        values = asdict(selected)
        stamps = {key: json.dumps(value)[1:-1] for key, value in values.items()}
        for entry in entries:
            contents[f"{relative}/{entry.name}"] = _stamp(entry.read_text("utf-8"), stamps)
    preexisting_bundles = {
        bundle
        for relative in contents
        if (bundle := _bundle_root(relative)) is not None and (paths.home / bundle).exists()
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
    changed.extend(_retire_bundles(paths, previous, shipped))
    write_json(paths.home / ".bundles.json", {"files": known})
    return changed


def ensure_home(home: Path) -> list[str]:
    """Create missing home roots, the skill links, and a Git root.

    Never writes content: ``AGENTS.md`` and the skills are ``seed_home``'s. The CLIs find
    them by walking up from a workspace to the Git root, and Enso never commits there.
    """
    done: list[str] = []
    paths = Paths(home)
    for directory in (paths.skills, paths.knowledge, paths.workspaces):
        parent = directory.parent
        # A link or file where shared/ belongs is the audit's to report, never created through.
        if parent != home and (parent.is_symlink() or (parent.exists() and not parent.is_dir())):
            continue
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


def ensure_layout(root: Path, *, include_optional: bool = True) -> list[str]:
    """Create what the documented layout is missing under ``root``; says what changed.

    Directories and links only: ``AGENTS.md`` is the operator's file and is never written
    here, and nothing that exists is removed or rewritten. Audits exclude optional content
    directories so an operator's consolidation or rename stays in place.
    """
    done: list[str] = []
    if any(path.is_symlink() for path in (root, root.parent, root.parent.parent)):
        return done
    for name in WORKSPACE_DIRS:
        entry = layout.classify(layout.WORKSPACE, name)
        if not include_optional and entry is not None and not entry.required:
            continue
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
