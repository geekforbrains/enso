"""Exact private-user authorization for Telegram."""

from __future__ import annotations

from typing import Any


def parse_telegram_allowed_users(config: dict[str, Any]) -> list[str]:
    """Return a complete valid Telegram allowlist, or fail closed."""
    if "allowed_user_ids" in config:
        return []

    raw = config.get("allowed_users")
    if not isinstance(raw, list) or not raw:
        return []
    if any(
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or int(value) <= 0
        for value in raw
    ):
        return []
    if len(raw) != len(set(raw)):
        return []
    return list(raw)


def is_authorized(user_id: str, allowed_users: list[str]) -> bool:
    """Check an exact user ID against the configured allowlist.

    An empty or malformed allowlist allows no one. Wildcards are not
    supported.
    """
    return user_id in allowed_users
