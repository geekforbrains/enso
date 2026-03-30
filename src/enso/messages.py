"""Message queue for background communication between jobs and conversations."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .config import MESSAGES_FILE


def send(text: str, source: str = "manual") -> None:
    """Append a message to the queue."""
    messages = _load()
    messages.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "source": source,
    })
    _save(messages)


def pending() -> list[dict]:
    """Return all pending messages without consuming them."""
    return _load()


def consume() -> list[dict]:
    """Return all pending messages and clear the queue atomically."""
    messages = _load()
    if messages:
        _save([])
    return messages


def clear() -> None:
    """Clear all pending messages."""
    _save([])


def format_for_injection(messages: list[dict]) -> str:
    """Format messages for prepending to a user's prompt.

    Returns a block of text that gives the AI agent context about what
    happened in background jobs since the last conversation.
    """
    if not messages:
        return ""
    lines = ["[Background messages since your last conversation]", ""]
    for msg in messages:
        ts = msg.get("timestamp", "?")
        source = msg.get("source", "?")
        text = msg.get("text", "")
        lines.append(f"[{ts}] ({source})")
        lines.append(text)
        lines.append("")
    lines.append("[End of background messages]")
    return "\n".join(lines)


def _load() -> list[dict]:
    """Load messages from disk."""
    if not os.path.exists(MESSAGES_FILE):
        return []
    try:
        with open(MESSAGES_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(messages: list[dict]) -> None:
    """Save messages to disk."""
    os.makedirs(os.path.dirname(MESSAGES_FILE), exist_ok=True)
    with open(MESSAGES_FILE, "w") as f:
        json.dump(messages, f, indent=2)
        f.write("\n")
