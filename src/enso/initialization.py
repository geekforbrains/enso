"""Offline home preparation and validated writes shared by native and hosted onboarding."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from . import __version__, workspaces
from .config import (
    CONFIG_VERSION,
    LEGACY_HOME_MESSAGE,
    ConfigConflictError,
    ConfigError,
    Paths,
    config_fingerprint,
    config_lock,
    parse_config,
    safe_diagnostics,
    write_config_locked,
)
from .providers import PROVIDER_CLASSES

CONTRACT_VERSION = 1
# The sections read only at start, so a change to them needs a restart: by the service, and
# by the viewer, which is its own process.
SERVICE_RESTART_KEYS = ("transports", "logging")
VIEWER_RESTART_KEYS = ("web",)
RESTART_KEYS = SERVICE_RESTART_KEYS + VIEWER_RESTART_KEYS
PROVIDER_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "grok": "Grok",
    "agy": "Antigravity",
    "opencode": "OpenCode",
}


def provider_catalog() -> dict[str, Any]:
    """Bundled choices without network or authentication; dynamic capabilities stay unknown."""
    providers = []
    for name, cls in PROVIDER_CLASSES.items():
        models: list[dict[str, Any]] = []
        for model in cls.models:
            known = name != "opencode"
            efforts = (
                [
                    effort
                    for effort in cls.effort_levels
                    if cls.clamp_effort(effort, model) == effort
                ]
                if known
                else []
            )
            models.append({"id": model, "label": model, "efforts": efforts, "known": known})
        defaults = models[0]["efforts"]
        providers.append(
            {
                "id": name,
                "label": PROVIDER_LABELS[name],
                "installed": shutil.which(name) is not None,
                "default_model": cls.models[0],
                "default_effort": "high"
                if "high" in defaults
                else defaults[-1]
                if defaults
                else None,
                "models": models,
            }
        )
    return {"version": CONTRACT_VERSION, "enso_version": __version__, "providers": providers}


def example_config() -> dict[str, Any]:
    """An editable template: empty credentials deliberately prevent use as an active config."""
    cls = PROVIDER_CLASSES["claude"]
    return {
        "version": CONFIG_VERSION,
        "transports": {"slack": {"bot_token": "", "app_token": "", "notify": ""}},
        "bindings": {},
        "defaults": {"provider": "claude", "model": cls.models[0], "effort": "high"},
        "projects": {"EX": {"name": "Example", "workspace": "default", "stages": ["work"]}},
        "providers": {
            "claude": {"path": "claude", "models": cls.models, "args": cls.unattended_args}
        },
        "agent": {"timeout": 3600},
        "logging": {"level": "INFO"},
        "runs": {"keep": 500, "max_age_days": 30},
    }


def _layout_problems(paths: Paths) -> list[str]:
    """Preflight the scaffold before creating anything; existing content is never replaced."""
    if (paths.home / "jobs").exists() or (paths.home / "jobs").is_symlink():
        return [LEGACY_HOME_MESSAGE]
    default = paths.workspace("default")
    directories = {
        paths.home,
        paths.workspaces,
        default,
        paths.heartbeat,
        paths.worktrees,
        paths.cache,
        paths.secrets,
        paths.skills,
        paths.knowledge,
        *(default / name for name in workspaces.WORKSPACE_DIRS),
    }
    files = {
        paths.agents_md,
        paths.config_example,
        default / "AGENTS.md",
        paths.workspace_settings("default"),
        *(paths.home / relative for relative in workspaces.BUNDLED_FILES),
        *(paths.skills / name / "SKILL.md" for name in workspaces.BUNDLED_SKILLS),
    }
    links = [
        (root / relative, target)
        for root in (paths.home, default)
        for relative, target in workspaces.LINKS
    ]
    for item in [*files, *(link for link, _ in links)]:
        directories.update(
            parent
            for parent in item.parents
            if parent == paths.home or paths.home in parent.parents
        )
    problems = []
    for directory in sorted(directories):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            problems.append(f"{directory}: expected a directory; existing path was preserved")
    for file in sorted(files):
        if file.is_symlink() or (file.exists() and not file.is_file()):
            problems.append(f"{file}: expected a file; existing path was preserved")
    for link, target in links:
        conflict = os.readlink(link) != target if link.is_symlink() else link.exists()
        if conflict:
            problems.append(f"{link}: expected a link to {target}; existing path was preserved")
    return problems


def initialize_home(paths: Paths) -> dict[str, Any]:
    """Prepare a resumable home without credentials, active config, database, or service."""
    changes: list[str] = []
    result = {
        "version": CONTRACT_VERSION,
        "ok": False,
        "home": str(paths.home),
        "changes": changes,
        "problems": [],
    }
    try:
        if problems := _layout_problems(paths):
            result["problems"] = problems
            return result
        with config_lock(paths):
            for directory in (
                paths.workspaces,
                paths.heartbeat,
                paths.worktrees,
                paths.cache,
                paths.secrets,
                paths.workspace("default"),
            ):
                if not directory.exists():
                    directory.mkdir(parents=True)
                    changes.append(f"created {directory}")
            default = paths.workspace("default")
            if workspaces.write_missing(
                default / "AGENTS.md", workspaces.workspace_template("default")
            ):
                changes.append(f"wrote {default / 'AGENTS.md'}")
            changes.extend(workspaces.ensure_layout(default))
            changes.extend(workspaces.seed_home(paths))
            if workspaces.write_missing(
                paths.config_example, json.dumps(example_config(), indent=2) + "\n"
            ):
                changes.append(f"wrote {paths.config_example}")
        result["ok"] = True
    except ConfigConflictError as exc:
        result["problems"] = exc.problems
    except OSError, subprocess.SubprocessError:
        result["problems"] = [
            "could not finish preparing the home; check path permissions and rerun init"
        ]
    return result


def check_config_report(paths: Paths) -> dict[str, Any]:
    """Validate one byte snapshot so its fingerprint always describes the reported document."""
    result: dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "ok": False,
        "config_hash": None,
        "problems": [],
        "warnings": [],
    }
    try:
        content = paths.config.read_bytes()
        result["config_hash"] = hashlib.sha256(content).hexdigest()
        raw = json.loads(content.decode("utf-8"))
        config, problems, warnings = parse_config(raw, paths)
        result.update(
            ok=config is not None,
            problems=safe_diagnostics(raw, problems),
            warnings=safe_diagnostics(raw, warnings),
        )
    except FileNotFoundError:
        result.update(
            config_hash="missing",
            problems=["config.json is missing; run setup or init and config apply"],
        )
    except OSError, UnicodeError, ValueError, RecursionError:
        result["problems"] = ["config.json could not be read as valid UTF-8 JSON"]
    return result


def _blocking_activity(paths: Paths) -> str | None:
    """Why the document must not change now: a live pairing attempt.

    A running ``enso serve`` is not a reason: it reads the file again for each turn and
    tick. Nor is an update: the CLI's admission gate and shared home access already keep
    a write out of an update's snapshot, and a command admitted before a drain must be
    allowed to finish. Pairing is judged by connection_setup: the native wizard's control
    lock, or a hosted worker still holding the receive lock it was started with; the
    service's own hold on that lock is not one. A state that cannot be read counts as busy.
    """
    from .connection_setup import PairingError, pairing_active

    try:
        pairing = pairing_active(paths)
    except PairingError, ValueError, OSError:
        return "connection state could not be read; cancel or finish the pairing attempt first"
    if pairing:
        return "a pairing attempt is active; finish or cancel it before applying config"
    return None


def _previous_document(paths: Paths) -> dict[str, Any] | None:
    """The document being replaced: None when there is none, empty when it is unreadable."""
    try:
        content = paths.config.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        document = json.loads(content.decode("utf-8"))
    except ValueError, RecursionError:
        return {}
    return document if isinstance(document, dict) else {}


@dataclass(frozen=True)
class ConfigEdit:
    """One dotted-path change: ``set`` stores ``value`` at ``path``; ``unset`` removes the key."""

    op: Literal["set", "unset"]
    path: str
    value: object = None


def _report() -> dict[str, Any]:
    """The documented result, plus ``_restart_sections``: the RESTART_KEYS that changed, so
    the CLI can say which process to restart. It is not part of the JSON; the CLI strips it.
    """
    return {
        "version": CONTRACT_VERSION,
        "ok": False,
        "applied": False,
        "config_hash": None,
        "jobs_complete": False,
        "restart_required": False,
        "changes": [],
        "problems": [],
        "warnings": [],
        "_restart_sections": (),
    }


def _validated_write(
    paths: Paths,
    build: Callable[[dict[str, Any] | None], object],
    *,
    expected_hash: str | None,
) -> dict[str, Any]:
    """The one path that replaces config.json: lock, gates, validation, atomic write, jobs.

    ``build`` receives the document being replaced (None when there is none, empty when it
    is unreadable) and returns the candidate document, or raises ``ConfigError`` with the
    problems that stop it from producing one.
    """
    result = _report()
    try:
        with config_lock(paths):
            if (reason := _blocking_activity(paths)) is not None:
                raise ConfigConflictError([reason])
            result["config_hash"] = config_fingerprint(paths)
            if expected_hash is not None and expected_hash != result["config_hash"]:
                raise ConfigConflictError(
                    ["configuration changed; reload it before applying changes"]
                )
            if paths.config.is_symlink():
                result["problems"] = ["config.json is a symbolic link; existing path was preserved"]
                return result
            previous = _previous_document(paths)
            raw = build(previous)
            config, problems, warnings = parse_config(raw, paths)
            result["problems"] = safe_diagnostics(raw, problems)
            result["warnings"] = safe_diagnostics(raw, warnings)
            if config is None:
                return result
            write_config_locked(paths, config.raw)
            result["applied"] = True
            result["config_hash"] = config_fingerprint(paths)
            if previous is not None:
                result["_restart_sections"] = tuple(
                    key
                    for key in RESTART_KEYS
                    if (previous.get(key) or {}) != (config.raw.get(key) or {})
                )
            result["restart_required"] = bool(result["_restart_sections"])
            try:
                result["changes"] = [
                    change
                    for workspace, settings in config.workspaces.items()
                    for change in workspaces.seed_jobs(
                        paths, config.defaults, workspace=workspace, memory_agent=settings.agent
                    )
                ]
            except OSError:
                result["problems"] = [
                    "configuration was saved, but bundled jobs could not be installed; "
                    "rerun apply to finish"
                ]
                return result
            result["jobs_complete"] = True
            result["ok"] = True
    except ConfigError as exc:
        result["problems"] = exc.problems
    except OSError:
        result["problems"] = ["could not apply configuration; check home permissions and retry"]
    return result


def apply_config(paths: Paths, raw: object, *, expected_hash: str | None = None) -> dict[str, Any]:
    """Validate then replace one full document, reporting retryable job-seeding failures."""
    return _validated_write(paths, lambda _previous: raw, expected_hash=expected_hash)


def _apply_edits(document: dict[str, Any], edits: Sequence[ConfigEdit]) -> list[str]:
    """Apply every edit to ``document`` in place; the problems name paths, never values."""
    problems = []
    for edit in edits:
        segments = edit.path.split(".")
        if not edit.path or "" in segments:
            problems.append(f"{edit.path!r} is not a dotted key path such as defaults.model")
            continue
        node: Any = document
        for depth, segment in enumerate(segments[:-1]):
            if segment not in node and edit.op == "set":
                node[segment] = {}
            node = node.get(segment)
            if not isinstance(node, dict):
                where = ".".join(segments[: depth + 1])
                problems.append(
                    f"{where} is not an object, so {edit.path} cannot be set"
                    if edit.op == "set"
                    else f"{edit.path} is not set"
                )
                break
        else:
            if edit.op == "set":
                node[segments[-1]] = edit.value
            elif segments[-1] in node:
                del node[segments[-1]]
            else:
                problems.append(f"{edit.path} is not set")
    return problems


def patch_config(
    paths: Paths, edits: Sequence[ConfigEdit], *, expected_hash: str | None = None
) -> dict[str, Any]:
    """Change keys of the current document and validate the result exactly as apply does.

    A missing or unreadable document is reported rather than created; apply is the repair
    path. Problems with the edits themselves are reported together, before validation.
    """

    def build(previous: dict[str, Any] | None) -> object:
        if previous is None:
            raise ConfigError(["config.json is missing; run setup or init and config apply"])
        if not previous:
            raise ConfigError(
                [
                    "config.json is empty or could not be read as a JSON object; repair it "
                    "with config apply"
                ]
            )
        document = copy.deepcopy(previous)
        if problems := _apply_edits(document, edits):
            raise ConfigError(problems)
        return document

    return _validated_write(paths, build, expected_hash=expected_hash)
