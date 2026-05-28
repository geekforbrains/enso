"""Configuration management for Enso."""

from __future__ import annotations

import json
import logging
import os
import shutil

from .logging_config import default_logging_config

log = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.enso")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
JOBS_DIR = os.path.join(CONFIG_DIR, "jobs")
MESSAGES_FILE = os.path.join(CONFIG_DIR, "messages.json")

DEFAULT_PROVIDERS = {
    "claude": {
        "path": "claude",
        "runner": "print",
        "kage_path": "kage",
        "kage_timeout": 1800,
        "kage_restart": True,
        "models": ["opus", "sonnet", "haiku"],
    },
    "codex": {"path": "codex", "models": ["gpt-5.4", "gpt-5.3-codex"]},
    "gemini": {
        "path": "gemini",
        "models": [
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-pro-latest",
        ],
    },
}


def load_config() -> dict:
    """Load config from ~/.enso/config.json, creating defaults if missing."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return _with_config_defaults(json.load(f))
        except Exception:
            log.exception("Failed to load config.json, using defaults")
    config = _build_default_config()
    save_config(config)
    return config


def save_config(config: dict) -> None:
    """Save config to ~/.enso/config.json with restricted permissions."""
    config = _with_config_defaults(config)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.chmod(CONFIG_FILE, 0o600)


def _build_default_config() -> dict:
    """Build default config with empty transport and all providers."""
    return {
        "working_dir": os.path.join(CONFIG_DIR, "workspace"),
        "transport": "",
        "transports": {},
        "logging": default_logging_config(),
        "providers": resolve_providers(),
    }


def _with_config_defaults(config: dict) -> dict:
    """Merge non-interactive defaults into an existing config."""
    merged = dict(config)
    logging_defaults = default_logging_config()
    logging_cfg = merged.get("logging")
    if isinstance(logging_cfg, dict):
        merged_logging = {**logging_defaults, **logging_cfg}
        if not isinstance(merged_logging.get("loggers"), dict):
            merged_logging["loggers"] = {}
        merged["logging"] = merged_logging
    else:
        merged["logging"] = logging_defaults
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
