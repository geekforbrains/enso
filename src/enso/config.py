"""Configuration management for Enso."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile

from .logging_config import default_logging_config
from .providers.codex import CODEX_MODEL_ALIASES

log = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.enso")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
JOBS_DIR = os.path.join(CONFIG_DIR, "jobs")
MESSAGES_FILE = os.path.join(CONFIG_DIR, "messages.json")
SKILL_TOMBSTONES_DIRNAME = ".deleted"

DEFAULT_PROVIDERS = {
    "claude": {
        "path": "claude",
        "runner": "print",
        "job_runner": "print",
        "kage_path": "kage",
        "kage_timeout": 1800,
        "kage_restart": True,
        "models": ["opus", "sonnet", "haiku", "fable"],
    },
    "codex": {"path": "codex", "models": list(CODEX_MODEL_ALIASES)},
    "gemini": {
        "path": "gemini",
        "models": [
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-pro-latest",
        ],
    },
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


def claude_cfg(config: dict) -> dict:
    """Return the ``providers.claude`` config, tolerating malformed shapes."""
    providers = config.get("providers", {})
    return providers.get("claude", {}) if isinstance(providers, dict) else {}


def load_config() -> dict:
    """Load config from ~/.enso/config.json, creating defaults if missing."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                raw = json.load(f)
            config = _with_config_defaults(raw)
            if isinstance(raw, dict) and "tasks" in raw:
                try:
                    save_config(config)
                except OSError:
                    log.exception(
                        "Could not persist tasks config migration; using it in memory"
                    )
                else:
                    log.info("Migrated legacy tasks config to runs")
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

    # Backfill any provider keys added in newer versions (e.g. job_runner)
    # without overwriting values the user has already set.
    providers = merged.get("providers")
    if isinstance(providers, dict):
        backfilled = dict(providers)
        for name, defaults in DEFAULT_PROVIDERS.items():
            existing = backfilled.get(name)
            if isinstance(existing, dict):
                provider = {**defaults, **existing}
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
        merged["providers"] = backfilled

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
    providers = {}
    for name, defaults in DEFAULT_PROVIDERS.items():
        resolved = shutil.which(name)
        providers[name] = {**defaults, "path": resolved or name}
        if name == "claude":
            kage_resolved = shutil.which("kage")
            providers[name]["kage_path"] = kage_resolved or defaults["kage_path"]
    return providers


def detect_providers() -> dict[str, bool]:
    """Check which provider CLIs are available on PATH."""
    return {name: shutil.which(name) is not None for name in DEFAULT_PROVIDERS}
