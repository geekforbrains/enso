"""Shared filesystem helpers used across the package."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile


def atomic_write_text(
    path: str,
    text: str,
    *,
    mode: int | None = None,
    newline: str | None = None,
) -> None:
    """Fsync a temporary UTF-8 file, then atomically replace ``path``.

    ``mode`` applies restrictive permissions (e.g. ``0o600``) before the
    replace so the final file never exists with looser bits. ``newline``
    is passed through to ``open`` for callers that must preserve exact
    line endings.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def regular_file_sha256(path: str) -> str | None:
    """Hash a regular, non-symlink file, or return ``None`` safely."""
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()
