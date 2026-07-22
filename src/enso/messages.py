"""Message queue for background communication between jobs and conversations."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone

from .config import MESSAGES_FILE


def send(
    text: str,
    source: str = "manual",
    conversation_id: str | None = None,
) -> None:
    """Append a global or conversation-scoped message to the queue."""
    messages = _load()
    message = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "source": source,
    }
    if conversation_id is not None:
        message["conversation_id"] = conversation_id
    messages.append(message)
    _save(messages)


def pending() -> list[dict]:
    """Return all pending messages without consuming them."""
    return _load()


def consume(conversation_id: str | None = None) -> list[dict]:
    """Consume global messages plus those scoped to one conversation.

    Omitting ``conversation_id`` retains the original consume-all behavior.
    """
    queued = _load()
    if conversation_id is None:
        consumed, remaining = queued, []
    else:
        consumed, remaining = [], []
        for message in queued:
            target = message.get("conversation_id")
            if target in (None, conversation_id):
                consumed.append(message)
            else:
                remaining.append(message)
    if consumed:
        _save(remaining)
    return consumed


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
    """Atomically save messages to disk."""
    directory = os.path.dirname(MESSAGES_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(messages, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, MESSAGES_FILE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
