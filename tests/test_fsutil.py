"""Tests for shared filesystem helpers."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from enso.fsutil import harden_sqlite_files

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is POSIX-only
    fcntl = None


def test_harden_sqlite_files_makes_database_and_sidecars_private(tmp_path):
    database = tmp_path / "enso.db"
    sqlite_files = [
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
    ]
    for path in sqlite_files:
        path.write_bytes(b"")
        path.chmod(0o644)

    harden_sqlite_files(str(database))

    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in sqlite_files)


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_harden_sqlite_files_rejects_non_regular_paths(tmp_path, kind):
    database = tmp_path / "enso.db"
    if kind == "symlink":
        target = tmp_path / "target.db"
        target.write_bytes(b"")
        target.chmod(0o644)
        database.symlink_to(target)
    else:
        database.mkdir()

    with pytest.raises(OSError, match="not a regular file"):
        harden_sqlite_files(str(database))

    if kind == "symlink":
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.skipif(fcntl is None, reason="POSIX record locks require fcntl")
@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_harden_sqlite_files_preserves_process_record_locks(tmp_path, suffix):
    database = tmp_path / "enso.db"
    database.write_bytes(b"database")
    locked_path = Path(f"{database}{suffix}")
    locked_path.write_bytes(b"sidecar" if suffix else b"database")
    lock_fd = os.open(locked_path, os.O_RDWR)
    probe = """
import errno
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as exc:
    raise SystemExit(0 if exc.errno in (errno.EACCES, errno.EAGAIN) else 2)
raise SystemExit(1)
"""
    try:
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before_hardening = subprocess.run(
            [sys.executable, "-c", probe, str(locked_path)],
            check=False,
        )
        assert before_hardening.returncode == 0

        harden_sqlite_files(str(database))

        after_hardening = subprocess.run(
            [sys.executable, "-c", probe, str(locked_path)],
            check=False,
        )
        assert after_hardening.returncode == 0
    finally:
        os.close(lock_fd)


def test_harden_sqlite_files_tolerates_disappearing_sidecar(tmp_path, monkeypatch):
    database = tmp_path / "enso.db"
    database.write_bytes(b"database")
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"wal")
    real_stat = os.stat

    def stat_then_remove(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.fspath(path) == str(wal):
            wal.unlink()
        return result

    monkeypatch.setattr(os, "stat", stat_then_remove)

    harden_sqlite_files(str(database))

    assert not wal.exists()
