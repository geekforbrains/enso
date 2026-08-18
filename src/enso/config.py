"""Configuration management for Enso."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import stat
from collections.abc import Iterator
from datetime import datetime
from enum import Enum

from .fsutil import atomic_write_text
from .logging_config import default_logging_config
from .providers import PROVIDER_CLASSES
from .providers.codex import CODEX_MODEL_ALIASES

CONFIG_DIR = os.path.expanduser("~/.enso")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
DOCS_DIR = os.path.join(CONFIG_DIR, "docs")
JOBS_DIR = os.path.join(CONFIG_DIR, "jobs")
MESSAGES_FILE = os.path.join(CONFIG_DIR, "messages.json")

# Derived from the provider registry so supported names, CLI paths, and
# default model lists have one source of truth.
DEFAULT_PROVIDERS = {
    name: {"path": name, "models": list(cls.default_models)}
    for name, cls in PROVIDER_CLASSES.items()
}

# Provider options removed in past releases. Only these are stripped from
# existing configs (and the removal persisted); unknown keys pass through so
# a rollback from a newer version never destroys its settings.
_RETIRED_PROVIDER_KEYS: dict[str, frozenset[str]] = {
    "claude": frozenset({"runner", "job_runner", "kage_path", "kage_timeout", "kage_restart"}),
}

DEFAULT_WEB = {
    "enabled": True,
    "host": "127.0.0.1",
    "port": 1337,
    "token": "",
    "allowed_hosts": [],
    "external_skill_roots": ["~/.claude/skills"],
}

DEFAULT_AGENT = {"timeout": 30 * 60}
DEFAULT_RUNS = {"keep": 500, "max_age_days": 30}
DEFAULT_WORKSPACE_NAME = "default"
DEFAULT_POLICY_NAME = "admin"
WORKSPACE_NAME_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
WORKSPACE_NAME_MAX_LENGTH = 64
_WORKSPACE_NAME_RE = re.compile(WORKSPACE_NAME_PATTERN)


class ConfigError(ValueError):
    """Configuration cannot be read or safely updated."""


class SetupState(str, Enum):
    """Meaning of the optional setup completion marker."""

    PRE_FEATURE = "pre-feature"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"


def setup_state(config: dict) -> SetupState:
    """Classify fresh, interrupted, complete, and pre-feature configurations."""
    if "setup" not in config:
        return SetupState.PRE_FEATURE
    setup = config.get("setup")
    if not isinstance(setup, dict) or "completed_at" not in setup:
        raise ConfigError("setup.completed_at must be null or an ISO 8601 timestamp")
    completed_at = setup["completed_at"]
    if completed_at is None:
        return SetupState.INCOMPLETE
    if not isinstance(completed_at, str):
        raise ConfigError("setup.completed_at must be null or an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(
            "setup.completed_at must be null or an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ConfigError("setup.completed_at must include a timezone")
    return SetupState.COMPLETE


def validate_workspace_name(name: str) -> str:
    """Return a portable workspace name or reject it before path derivation."""
    if (
        not isinstance(name, str)
        or len(name) > WORKSPACE_NAME_MAX_LENGTH
        or not _WORKSPACE_NAME_RE.fullmatch(name)
    ):
        raise ConfigError(
            "workspace names must be lowercase kebab-case using letters, numbers, "
            f"and single hyphens ({WORKSPACE_NAME_PATTERN}), with at most "
            f"{WORKSPACE_NAME_MAX_LENGTH} characters"
        )
    return name


def managed_workspace_path(name: str = DEFAULT_WORKSPACE_NAME) -> str:
    """Return the name-derived managed location for a validated workspace."""
    return os.path.join(CONFIG_DIR, "workspaces", validate_workspace_name(name))


def unrestricted_policy_config(provider_names: list[str]) -> dict:
    """Build the standard unrestricted policy used by fresh installations."""
    providers = [name for name in PROVIDER_CLASSES if name in provider_names]
    if not providers:
        providers = list(PROVIDER_CLASSES)
    default_provider = "claude" if "claude" in providers else providers[0]
    return {
        "unrestricted": True,
        "providers": providers,
        "default_provider": default_provider,
        "chat_commands": "*",
    }


def provider_models(config: dict) -> dict[str, list[str]]:
    """Configured models per supported provider, normalized to string lists.

    Malformed shapes (non-dict provider entries, non-list ``models``, or
    non-string members) collapse to an empty/filtered list so validation and
    selection never substring-match against a string or raise on bad types.
    """
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        return {}
    result: dict[str, list[str]] = {}
    for name, pcfg in providers.items():
        if name not in DEFAULT_PROVIDERS or not isinstance(pcfg, dict):
            continue
        models = pcfg.get("models")
        result[name] = (
            [m for m in models if isinstance(m, str)] if isinstance(models, list) else []
        )
    return result


def load_config(*, allow_missing: bool = False) -> dict:
    """Read config strictly without creating files or persisting migrations.

    Existing bytes are authoritative: malformed JSON and non-object roots fail
    instead of being replaced with defaults. Only setup may opt into an
    in-memory fresh-install candidate with ``allow_missing=True``.
    """
    if not os.path.lexists(CONFIG_FILE):
        if allow_missing:
            return _build_default_config()
        raise ConfigError(f"{CONFIG_FILE} is missing; run `enso setup` first")
    try:
        path_stat = os.lstat(CONFIG_FILE)
        if not stat.S_ISREG(path_stat.st_mode):
            raise ConfigError(f"{CONFIG_FILE} must be a regular file, not a symlink")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(CONFIG_FILE, flags)
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or not _same_file(path_stat, opened_stat):
            os.close(descriptor)
            raise ConfigError(f"{CONFIG_FILE} changed while it was opened")
        with os.fdopen(descriptor, encoding="utf-8") as file:
            raw = json.load(file)
        if not _same_file(opened_stat, os.lstat(CONFIG_FILE)):
            raise ConfigError(f"{CONFIG_FILE} changed while it was read")
    except ConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read {CONFIG_FILE}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_FILE} must contain a JSON object")
    config = _with_config_defaults(raw)
    setup_state(config)
    return config


def save_config(config: dict) -> None:
    """Atomically save config.json with restricted permissions."""
    if not isinstance(config, dict):
        raise ConfigError("config must be a JSON object")
    setup_state(config)
    config = _with_config_defaults(config)
    atomic_write_text(CONFIG_FILE, json.dumps(config, indent=2) + "\n", mode=0o600)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_config_lock(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise ConfigError("config lock must be a regular file")
    if file_stat.st_uid != os.getuid():
        raise ConfigError("config lock must be owned by the current user")
    if file_stat.st_nlink != 1:
        raise ConfigError("config lock must not have additional hard links")


def _ensure_physical_config_dir() -> None:
    """Create or validate the config root without following directory links."""
    root = os.path.abspath(CONFIG_DIR)
    if os.path.dirname(os.path.abspath(CONFIG_FILE)) != root:
        raise ConfigError("config file must live directly in the config directory")

    if os.path.lexists(root):
        try:
            root_stat = os.lstat(root)
        except OSError as exc:
            raise ConfigError(f"Could not inspect config directory safely: {exc}") from exc
        if not stat.S_ISDIR(root_stat.st_mode) or os.path.realpath(root) != root:
            raise ConfigError(f"{root} must be a physical directory, not a symlink")
        return

    parent = os.path.dirname(root)
    try:
        parent_stat = os.lstat(parent)
    except OSError as exc:
        raise ConfigError(
            f"config directory parent {parent} must be a physical directory: {exc}"
        ) from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or os.path.realpath(parent) != parent:
        raise ConfigError(f"config directory parent {parent} must be a physical directory")
    try:
        os.mkdir(root, mode=0o700)
    except FileExistsError:
        # A concurrent setup may have created it after the existence check.
        _ensure_physical_config_dir()
    except OSError as exc:
        raise ConfigError(f"Could not create config directory safely: {exc}") from exc


@contextlib.contextmanager
def config_lock() -> Iterator[None]:
    """Serialize config read-modify-write operations across processes."""
    _ensure_physical_config_dir()
    lock_path = f"{CONFIG_FILE}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ConfigError(f"Could not open config lock safely: {exc}") from exc

    locked = False
    try:
        opened_stat = os.fstat(descriptor)
        _validate_config_lock(opened_stat)
        if not _same_file(opened_stat, os.lstat(lock_path)):
            raise ConfigError("config lock changed while it was opened")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        locked_stat = os.fstat(descriptor)
        _validate_config_lock(locked_stat)
        if not _same_file(locked_stat, os.lstat(lock_path)):
            raise ConfigError("config lock changed while it was acquired")
        os.fchmod(descriptor, 0o600)
        yield
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError(f"Could not secure config lock: {exc}") from exc
    finally:
        if locked:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def config_transaction() -> Iterator[dict]:
    """Yield a locked config and save it atomically after successful mutation."""
    # Validate before the lock path itself is created. Read again under the
    # lock so a concurrent Enso mutation cannot leave this transaction stale.
    load_config()
    with config_lock():
        config = load_config()
        yield config
        save_config(config)


def _build_default_config() -> dict:
    """Build default config with empty transport and all providers."""
    providers = resolve_providers()
    return {
        "transport": "",
        "transports": {},
        "logging": default_logging_config(),
        "providers": providers,
        "workspaces": {
            DEFAULT_WORKSPACE_NAME: {
                "policy": DEFAULT_POLICY_NAME,
                "concurrency": 1,
            },
        },
        "policies": {
            DEFAULT_POLICY_NAME: unrestricted_policy_config(list(providers)),
        },
        "agent": dict(DEFAULT_AGENT),
        "web": dict(DEFAULT_WEB),
        "runs": dict(DEFAULT_RUNS),
        "setup": {"completed_at": None},
    }


def _with_config_defaults(config: dict) -> dict:
    """Merge non-interactive defaults into an existing config."""
    merged = dict(config)

    # Tasks were removed in favor of scheduled jobs. Preserve the two
    # retention settings that used to live in that block, while allowing the
    # replacement ``runs`` block to take precedence during an upgrade.
    legacy_tasks = merged.pop("tasks", None)
    legacy_runs: dict = {}
    if isinstance(legacy_tasks, dict):
        if "runs_keep" in legacy_tasks:
            legacy_runs["keep"] = legacy_tasks["runs_keep"]
        if "runs_max_age_days" in legacy_tasks:
            legacy_runs["max_age_days"] = legacy_tasks["runs_max_age_days"]

    logging_defaults = default_logging_config()
    logging_cfg = merged.get("logging")
    if isinstance(logging_cfg, dict):
        merged_logging = {**logging_defaults, **logging_cfg}
        if not isinstance(merged_logging.get("loggers"), dict):
            merged_logging["loggers"] = {}
        merged["logging"] = merged_logging
    else:
        merged["logging"] = logging_defaults

    # Normalize provider entries: drop retired providers and retired keys,
    # backfill newly added defaults without overwriting user values.
    providers = merged.get("providers")
    if isinstance(providers, dict):
        backfilled = {
            name: value
            for name, value in providers.items()
            if name in DEFAULT_PROVIDERS
        }
        for name, defaults in DEFAULT_PROVIDERS.items():
            existing = backfilled.get(name)
            if isinstance(existing, dict):
                retired = _RETIRED_PROVIDER_KEYS.get(name, frozenset())
                provider = {
                    key: value
                    for key, value in {**defaults, **existing}.items()
                    if key not in retired
                }
                if name == "codex" and isinstance(existing.get("models"), list):
                    # One-time upgrade: configs predating the aliases gain
                    # them up front. Configs that already carry any alias
                    # keep the user's list verbatim, including deliberate
                    # removals and reordering.
                    existing_models = existing["models"]
                    if not any(m in CODEX_MODEL_ALIASES for m in existing_models):
                        provider["models"] = [
                            *CODEX_MODEL_ALIASES, *existing_models,
                        ]
                backfilled[name] = provider
            else:
                backfilled[name] = {
                    "path": defaults["path"],
                    "models": list(defaults["models"]),
                }
        merged["providers"] = backfilled
    else:
        merged["providers"] = {
            name: {"path": defaults["path"], "models": list(defaults["models"])}
            for name, defaults in DEFAULT_PROVIDERS.items()
        }

    agent = merged.get("agent")
    merged_agent = (
        {**DEFAULT_AGENT, **agent}
        if isinstance(agent, dict)
        else dict(DEFAULT_AGENT)
    )
    timeout = merged_agent.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0:
        merged_agent["timeout"] = DEFAULT_AGENT["timeout"]
    merged["agent"] = merged_agent

    # Backfill web/runs blocks added in newer versions without overwriting
    # values the user has already set.
    for key, block_defaults in (("web", DEFAULT_WEB), ("runs", DEFAULT_RUNS)):
        existing = merged.get(key)
        if isinstance(existing, dict):
            migrated = legacy_runs if key == "runs" else {}
            merged[key] = {**block_defaults, **migrated, **existing}
        elif key not in merged:
            migrated = legacy_runs if key == "runs" else {}
            merged[key] = {**block_defaults, **migrated}

    return merged


def resolve_providers() -> dict:
    """Build provider config with absolute paths where available."""
    return {
        name: {**defaults, "path": shutil.which(name) or name}
        for name, defaults in DEFAULT_PROVIDERS.items()
    }


def detect_providers() -> dict[str, bool]:
    """Check which provider CLIs are available on PATH."""
    return {name: shutil.which(name) is not None for name in DEFAULT_PROVIDERS}
