"""Configuration management for Enso."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile

from .logging_config import default_logging_config
from .providers import PROVIDER_CLASSES
from .providers.codex import CODEX_MODEL_ALIASES

log = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.enso")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
JOBS_DIR = os.path.join(CONFIG_DIR, "jobs")
MESSAGES_FILE = os.path.join(CONFIG_DIR, "messages.json")
SKILL_TOMBSTONES_DIRNAME = ".deleted"

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

DEFAULT_RUNS = {"keep": 500, "max_age_days": 30}


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


def _providers_need_migration(raw_providers: object) -> bool:
    """True when provider config is missing defaults or carries retired values."""
    if not isinstance(raw_providers, dict):
        return True
    if any(name not in raw_providers for name in DEFAULT_PROVIDERS):
        return True
    for name, pcfg in raw_providers.items():
        if name not in DEFAULT_PROVIDERS:
            return True
        retired = _RETIRED_PROVIDER_KEYS.get(name, frozenset())
        if isinstance(pcfg, dict) and any(k in retired for k in pcfg):
            return True
    return False


def load_config() -> dict:
    """Load config from ~/.enso/config.json, creating defaults if missing."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                raw = json.load(f)
            config = _with_config_defaults(raw)
            needs_migration = isinstance(raw, dict) and (
                "tasks" in raw or _providers_need_migration(raw.get("providers"))
            )
            if needs_migration:
                try:
                    save_config(config)
                except OSError:
                    log.exception("Could not persist config migration; using it in memory")
                else:
                    log.info("Persisted migrated config")
            return config
        except Exception:
            log.exception("Failed to load config.json, using defaults")
    config = _build_default_config()
    save_config(config)
    return config


def save_config(config: dict) -> None:
    """Atomically save config.json with restricted permissions."""
    config = _with_config_defaults(config)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _build_default_config() -> dict:
    """Build default config with empty transport and all providers."""
    return {
        "working_dir": os.path.join(CONFIG_DIR, "workspace"),
        "transport": "",
        "transports": {},
        "logging": default_logging_config(),
        "providers": resolve_providers(),
        "web": dict(DEFAULT_WEB),
        "runs": dict(DEFAULT_RUNS),
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
                    # Make new aliases available to existing installs while
                    # retaining full, older, or custom model IDs.
                    aliases = list(CODEX_MODEL_ALIASES)
                    existing_models = [
                        model for model in existing["models"]
                        if model not in CODEX_MODEL_ALIASES
                    ]
                    provider["models"] = [*aliases, *existing_models]
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

    # Backfill web/runs blocks added in newer versions without overwriting
    # values the user has already set.
    for key, defaults in (("web", DEFAULT_WEB), ("runs", DEFAULT_RUNS)):
        existing = merged.get(key)
        if isinstance(existing, dict):
            migrated = legacy_runs if key == "runs" else {}
            merged[key] = {**defaults, **migrated, **existing}
        elif key not in merged:
            migrated = legacy_runs if key == "runs" else {}
            merged[key] = {**defaults, **migrated}

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
