"""Authorization — shared access control for all transports."""

from __future__ import annotations


def is_authorized(user_id: str, allowed_users: list[str]) -> bool:
    """Check if a user is allowed to interact with the bot.

    - ``["*"]`` allows everyone.
    - An empty list allows no one (fail-closed).
    - Otherwise the user's ID must be in the list.
    """
    if "*" in allowed_users:
        return True
    return user_id in allowed_users
