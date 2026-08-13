"""Message queue for background communication between jobs and conversations."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from datetime import datetime, timezone

from .config import MESSAGES_FILE
from .fsutil import atomic_write_text


@contextlib.contextmanager
def _queue_lock():
    """Serialize load-modify-save cycles across processes.

    Writers run concurrently in separate processes (the serve process,
    ``enso message send`` invoked by agents and hooks), so the whole
    read-modify-write sequence must be exclusive or appends get lost.
    """
    os.makedirs(os.path.dirname(MESSAGES_FILE), exist_ok=True)
    with open(MESSAGES_FILE + ".lock", "a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def send(
    text: str,
    source: str = "manual",
    conversation_id: str | None = None,
) -> None:
    """Append a global or conversation-scoped message to the queue."""
    message = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "source": source,
    }
    if conversation_id is not None:
        message["conversation_id"] = conversation_id
    with _queue_lock():
        messages = _load()
        messages.append(message)
        _save(messages)


def pending() -> list[dict]:
    """Return all pending messages without consuming them."""
    return _load()


def consume(
    conversation_id: str | None = None, *, include_global: bool = True
) -> list[dict]:
    """Consume messages scoped to one conversation, plus global ones by default.

    Omitting ``conversation_id`` retains the original consume-all behavior.
    Teams-mode execution passes ``include_global=False`` so one route can
    never consume content that wasn't explicitly addressed to it.
    """
    with _queue_lock():
        queued = _load()
        consumed: list[dict]
        remaining: list[dict]
        if conversation_id is None:
            consumed, remaining = queued, []
        else:
            accepted = (conversation_id,) if not include_global else (None, conversation_id)
            consumed, remaining = [], []
            for message in queued:
                if message.get("conversation_id") in accepted:
                    consumed.append(message)
                else:
                    remaining.append(message)
        if consumed:
            _save(remaining)
    return consumed


def clear() -> None:
    """Clear all pending messages."""
    with _queue_lock():
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
    atomic_write_text(
        MESSAGES_FILE, json.dumps(messages, indent=2) + "\n", mode=0o600,
    )
