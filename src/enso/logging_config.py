"""Logging configuration helpers for Enso."""

from __future__ import annotations

import copy
import logging
from typing import Any

DEFAULT_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s - %(message)s"

DEFAULT_LOGGING: dict[str, Any] = {
    "level": "INFO",
    "enso_level": "INFO",
    "noisy_level": "WARNING",
    "debug_prompts": False,
    "debug_events": False,
    "loggers": {},
}

NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "telegram",
    "telegram.ext",
    "telegram.ext._application",
    "telegram.ext._updater",
    "telegram.ext._base_update_handler",
    "telegram._bot",
    "hpack",
    "urllib3",
    "h11",
    "h2",
)

_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
    "NOTSET": logging.NOTSET,
}


def default_logging_config() -> dict[str, Any]:
    """Return a mutable copy of the default logging config."""
    return copy.deepcopy(DEFAULT_LOGGING)


def parse_log_level(value: Any, default: int = logging.INFO) -> int:
    """Parse a logging level name or integer, falling back to ``default``."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip().upper()
        if cleaned in _LEVELS:
            return _LEVELS[cleaned]
        try:
            return int(cleaned)
        except ValueError:
            return default
    return default


def logging_flags(config: dict | None) -> dict[str, bool]:
    """Extract runtime debug flags from the logging config."""
    logging_cfg = config.get("logging", {}) if isinstance(config, dict) else {}
    if not isinstance(logging_cfg, dict):
        logging_cfg = {}
    return {
        "debug_prompts": bool(logging_cfg.get("debug_prompts", False)),
        "debug_events": bool(logging_cfg.get("debug_events", False)),
    }


def configure_logging(
    config: dict | None = None,
    *,
    force: bool = False,
) -> dict[str, str]:
    """Configure Python logging from Enso config.

    Supported ``config["logging"]`` keys:
    - ``level``: root logger level.
    - ``enso_level``: package logger level for ``enso`` and children.
    - ``noisy_level``: default level for chat/http dependency loggers.
    - ``loggers``: explicit per-logger level overrides.
    - ``format``: optional logging format string.
    """
    logging_cfg = config.get("logging", {}) if isinstance(config, dict) else {}
    if not isinstance(logging_cfg, dict):
        logging_cfg = {}

    root_level = parse_log_level(logging_cfg.get("level"), logging.INFO)
    fmt = logging_cfg.get("format") or DEFAULT_LOG_FORMAT

    root = logging.getLogger()
    if force or not root.handlers:
        logging.basicConfig(format=fmt, level=root_level, force=force)
    else:
        root.setLevel(root_level)

    enso_level = parse_log_level(logging_cfg.get("enso_level"), root_level)
    noisy_level = parse_log_level(logging_cfg.get("noisy_level"), logging.WARNING)
    loggers = logging_cfg.get("loggers", {})
    if not isinstance(loggers, dict):
        loggers = {}

    logging.getLogger("enso").setLevel(enso_level)

    for name in NOISY_LOGGERS:
        if name not in loggers:
            logging.getLogger(name).setLevel(noisy_level)

    for name, level in loggers.items():
        logging.getLogger(str(name)).setLevel(parse_log_level(level, root_level))

    return {
        "level": logging.getLevelName(root_level),
        "enso_level": logging.getLevelName(enso_level),
        "noisy_level": logging.getLevelName(noisy_level),
    }
