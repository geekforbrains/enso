"""Tests for logging configuration."""

from __future__ import annotations

import logging

from enso.logging_config import configure_logging, logging_flags, parse_log_level


def test_parse_log_level_accepts_names_and_numbers():
    assert parse_log_level("debug") == logging.DEBUG
    assert parse_log_level("WARN") == logging.WARNING
    assert parse_log_level("40") == logging.ERROR
    assert parse_log_level("invalid", logging.CRITICAL) == logging.CRITICAL


def test_logging_flags_default_false():
    assert logging_flags({}) == {"debug_prompts": False, "debug_events": False}


def test_logging_flags_read_config_values():
    config = {"logging": {"debug_prompts": True, "debug_events": True}}
    assert logging_flags(config) == {"debug_prompts": True, "debug_events": True}


def test_configure_logging_sets_package_and_dependency_levels():
    names = ["", "enso", "httpx", "custom.test"]
    previous = {name: logging.getLogger(name).level for name in names}
    try:
        state = configure_logging(
            {
                "logging": {
                    "level": "WARNING",
                    "enso_level": "DEBUG",
                    "noisy_level": "ERROR",
                    "loggers": {"custom.test": "CRITICAL", "httpx": "INFO"},
                }
            },
            force=False,
        )

        assert state == {
            "level": "WARNING",
            "enso_level": "DEBUG",
            "noisy_level": "ERROR",
        }
        assert logging.getLogger().level == logging.WARNING
        assert logging.getLogger("enso").level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.INFO
        assert logging.getLogger("httpcore").level == logging.ERROR
        assert logging.getLogger("custom.test").level == logging.CRITICAL
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)
