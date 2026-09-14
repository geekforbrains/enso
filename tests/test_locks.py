"""The hardened advisory lock open shared by every lock site."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from enso import locks


def test_lock_refuses_links_and_special_files(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("someone else's file")
    link = tmp_path / "link.lock"
    link.symlink_to(outside)
    with pytest.raises(locks.LockPathError, match="symbolic link"):
        locks.acquire(link)
    fifo = tmp_path / "fifo.lock"
    os.mkfifo(fifo)
    with pytest.raises(locks.LockPathError, match="regular file"):
        locks.acquire(fifo)  # returns at once: nothing ever opens the other end
    directory = tmp_path / "dir.lock"
    directory.mkdir()
    with pytest.raises(locks.LockPathError, match="regular file"):
        locks.acquire(directory)
    assert outside.read_text() == "someone else's file"


def test_lock_contention_shared_readers_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with pytest.raises(FileNotFoundError):
        locks.open_lock(path, create=False)
    holder = locks.acquire(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(BlockingIOError):
        locks.acquire(path)
    with pytest.raises(BlockingIOError):
        locks.acquire(path, shared=True)
    assert locks.acquire_file_lock(path) is None
    os.close(holder)
    readers = [locks.acquire(path, shared=True) for _ in range(2)]
    with pytest.raises(BlockingIOError):
        locks.acquire(path)
    for fd in readers:
        os.close(fd)
    handle = locks.acquire_file_lock(path)
    assert handle is not None
    with handle:
        assert locks.acquire_file_lock(path) is None
    released = locks.acquire_file_lock(path)
    assert released is not None
    released.close()
