"""Tests for Telegram transport helpers."""

from __future__ import annotations

from types import SimpleNamespace

from enso.transports.telegram import _resolve_file


def _msg(**kwargs):
    fields = {
        "document": None, "photo": None, "audio": None,
        "voice": None, "video": None, "video_note": None,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def test_resolve_file_sanitizes_document_name():
    doc = SimpleNamespace(file_name="../../etc/passwd")
    _obj, name, desc = _resolve_file(_msg(document=doc))
    assert name == "passwd"
    assert desc == "file (passwd)"


def test_resolve_file_dot_only_name_falls_back_to_generated():
    """A name that sanitizes to empty (e.g. '...') must not yield an empty path."""
    doc = SimpleNamespace(file_name="...")
    _obj, name, _desc = _resolve_file(_msg(document=doc))
    assert name.startswith("document_")
    assert len(name) > len("document_")


def test_resolve_file_missing_audio_name_falls_back_to_generated():
    audio = SimpleNamespace(file_name=None)
    _obj, name, _desc = _resolve_file(_msg(audio=audio))
    assert name.startswith("audio_")
    assert name.endswith(".mp3")
