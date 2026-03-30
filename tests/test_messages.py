"""Tests for the message queue system."""

from __future__ import annotations

from enso import messages


def test_send_and_pending(tmp_enso):
    """Messages are queued and retrievable."""
    assert messages.pending() == []
    messages.send("hello", source="test")
    msgs = messages.pending()
    assert len(msgs) == 1
    assert msgs[0]["text"] == "hello"
    assert msgs[0]["source"] == "test"
    assert "timestamp" in msgs[0]


def test_consume_clears(tmp_enso):
    """Consume returns messages and clears the queue."""
    messages.send("one")
    messages.send("two")
    consumed = messages.consume()
    assert len(consumed) == 2
    assert messages.pending() == []


def test_consume_empty(tmp_enso):
    """Consuming an empty queue returns empty list."""
    assert messages.consume() == []


def test_clear(tmp_enso):
    """Clear removes all pending messages."""
    messages.send("will be cleared")
    messages.clear()
    assert messages.pending() == []


def test_format_for_injection_empty():
    """Empty message list returns empty string."""
    assert messages.format_for_injection([]) == ""


def test_format_for_injection():
    """Messages are formatted with timestamps and sources."""
    msgs = [
        {"timestamp": "2026-01-01T00:00:00", "text": "hello world", "source": "test"},
    ]
    result = messages.format_for_injection(msgs)
    assert "Background messages" in result
    assert "hello world" in result
    assert "(test)" in result
    assert result.startswith("[Background messages")
    assert result.endswith("[End of background messages]")


def test_send_default_source(tmp_enso):
    """Default source is 'manual'."""
    messages.send("test")
    assert messages.pending()[0]["source"] == "manual"
