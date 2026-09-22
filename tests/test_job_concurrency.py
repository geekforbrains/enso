"""Group admission across tasks/processes, cancellation races, and abandoned waiters."""

from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal

import pytest

from enso import db
from enso.config import Paths
from enso.jobs import Job, JobConcurrency, concurrency
from enso.locks import LockPathError, acquire_file_lock


@pytest.fixture
def paths(enso_home: Paths) -> Paths:
    db.initialize(enso_home)
    return enso_home


def job(
    paths: Paths,
    name: str,
    *,
    policy: Literal["wait", "skip"] = "wait",
    max_wait: int | None = None,
) -> Job:
    return Job(
        dir_name=name,
        path=paths.job(f"default:{name}"),
        name=name,
        schedule="* * * * *",
        workspace="default",
        enabled=True,
        prompt="",
        command="true",
        concurrency=JobConcurrency("reports", policy, max_wait),
    )


def queued(paths: Paths) -> list[str]:
    with db.reader(paths) as con:
        return [
            row["run_id"]
            for row in con.execute("SELECT run_id FROM _enso_job_waiters ORDER BY sequence")
        ]


async def wait_queued(paths: Paths, expected: list[str]) -> None:
    async with asyncio.timeout(5):
        while await asyncio.to_thread(queued, paths) != expected:
            await asyncio.sleep(0.01)


def assert_unlocked(paths: Paths, group: str = "reports") -> None:
    lock = concurrency.acquire_group_lock(paths, group)
    assert lock is not None
    lock.close()


async def test_group_is_optional(paths: Paths):
    result = await concurrency.acquire(paths, replace(job(paths, "free"), concurrency=None), "free")
    assert result == concurrency.Admission("acquired")
    assert queued(paths) == []


async def test_waiters_take_turns_and_skip_cannot_jump_queue(paths: Paths):
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    first = asyncio.create_task(concurrency.acquire(paths, job(paths, "first"), "first"))
    await wait_queued(paths, ["first"])
    second = asyncio.create_task(concurrency.acquire(paths, job(paths, "second"), "second"))
    await wait_queued(paths, ["first", "second"])

    owner.close()
    # A skip caller must defer to registered waiters even if neither has polled yet.
    skipped = await concurrency.acquire(paths, job(paths, "skip", policy="skip"), "skip")
    assert skipped == concurrency.Admission("busy")
    admitted = await asyncio.wait_for(first, 5)
    assert admitted.reason == "acquired" and admitted.lock is not None
    assert not second.done()
    assert queued(paths) == ["second"]
    admitted.lock.close()
    admitted_second = await asyncio.wait_for(second, 5)
    assert admitted_second.reason == "acquired" and admitted_second.lock is not None
    admitted_second.lock.close()
    assert queued(paths) == []
    assert_unlocked(paths)


async def test_skip_busy_group_does_not_register_waiter(paths: Paths):
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    try:
        assert await concurrency.acquire(paths, job(paths, "skip", policy="skip"), "skip") == (
            concurrency.Admission("busy")
        )
        assert queued(paths) == []
    finally:
        owner.close()


async def test_independent_groups_can_run_together(paths: Paths):
    first = await concurrency.acquire(paths, job(paths, "first"), "first")
    other_job = replace(job(paths, "other"), concurrency=JobConcurrency("backups", "skip"))
    second = await concurrency.acquire(paths, other_job, "other")
    assert first.lock is not None and second.lock is not None
    first.lock.close()
    second.lock.close()


async def test_expiration_removes_waiter_and_lease(paths: Paths):
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    try:
        result = await asyncio.wait_for(
            concurrency.acquire(paths, job(paths, "expires", max_wait=1), "expires"), 5
        )
        assert result == concurrency.Admission("expired")
        assert queued(paths) == []
        lease = acquire_file_lock(paths.lock("job-waiters", "default", "expires"))
        assert lease is not None
        lease.close()
    finally:
        owner.close()


async def test_cancelled_head_does_not_block_next_waiter(paths: Paths):
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    first = asyncio.create_task(concurrency.acquire(paths, job(paths, "first"), "first"))
    await wait_queued(paths, ["first"])
    second = asyncio.create_task(concurrency.acquire(paths, job(paths, "second"), "second"))
    await wait_queued(paths, ["first", "second"])
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert queued(paths) == ["second"]
    owner.close()
    result = await asyncio.wait_for(second, 5)
    assert result.lock is not None
    result.lock.close()
    assert queued(paths) == []


async def test_freed_group_cannot_be_acquired_after_wait_deadline(paths: Paths, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(concurrency, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    pending = asyncio.create_task(
        concurrency.acquire(paths, job(paths, "expires", max_wait=1), "expires")
    )
    await wait_queued(paths, ["expires"])
    clock[0] += 2
    owner.close()
    assert await asyncio.wait_for(pending, 5) == concurrency.Admission("expired")
    assert queued(paths) == []
    assert_unlocked(paths)


async def test_maintenance_releases_waiters_without_waiting_for_group_owner(paths: Paths):
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    try:
        pending = asyncio.create_task(concurrency.acquire(paths, job(paths, "waiting"), "waiting"))
        await wait_queued(paths, ["waiting"])
        paths.maintenance.write_text("{}")
        assert await asyncio.wait_for(pending, 5) == concurrency.Admission("paused")
        assert queued(paths) == []
    finally:
        owner.close()


async def test_cancel_during_thread_acquisition_joins_and_releases_lock(paths: Paths, monkeypatch):
    acquired = threading.Event()
    finish = threading.Event()
    original = concurrency.acquire_group_lock

    def slow_acquire(paths, group):
        lock = original(paths, group)
        acquired.set()
        assert finish.wait(5)
        return lock

    monkeypatch.setattr(concurrency, "acquire_group_lock", slow_acquire)
    pending = asyncio.create_task(concurrency.acquire(paths, job(paths, "racy"), "racy"))
    assert await asyncio.to_thread(acquired.wait, 5)
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()  # shutdown may cancel the same task again during its cleanup
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert queued(paths) == []
    assert_unlocked(paths)


async def test_cancel_during_cleanup_releases_acquired_group(paths: Paths, monkeypatch):
    cleaning = threading.Event()
    finish = threading.Event()
    original = concurrency._Waiter.cleanup

    def slow_cleanup(waiter):
        cleaning.set()
        assert finish.wait(5)
        original(waiter)

    monkeypatch.setattr(concurrency._Waiter, "cleanup", slow_cleanup)
    pending = asyncio.create_task(concurrency.acquire(paths, job(paths, "racy"), "racy"))
    assert await asyncio.to_thread(cleaning.wait, 5)
    pending.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert queued(paths) == []
    assert_unlocked(paths)


async def test_many_waiters_do_not_exhaust_default_executor(paths: Paths):
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    pending = [
        asyncio.create_task(concurrency.acquire(paths, job(paths, f"job-{i}"), f"run-{i}"))
        for i in range(40)
    ]
    try:
        async with asyncio.timeout(10):
            while len(await asyncio.to_thread(queued, paths)) < len(pending):
                await asyncio.sleep(0.01)
            # Releasing running work through the same pool must remain possible.
            await asyncio.to_thread(owner.close)
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            assert any(task.result().lock is not None for task in done)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*pending, return_exceptions=True)
        for result in results:
            if isinstance(result, concurrency.Admission) and result.lock is not None:
                result.lock.close()
        owner.close()
    assert queued(paths) == []
    assert_unlocked(paths)


CHILD = """
import asyncio
import sys
from pathlib import Path
from enso.config import Paths
from enso.jobs import Job, JobConcurrency, concurrency

async def main():
    paths = Paths(Path(sys.argv[1]))
    name = sys.argv[2]
    job = Job(dir_name=name, path=paths.job('default:' + name), name=name,
              schedule='* * * * *', workspace='default', enabled=True, prompt='',
              command='true', concurrency=JobConcurrency('reports', 'wait'))
    result = await concurrency.acquire(paths, job, name)
    assert result.lock is not None
    print('acquired', flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    result.lock.close()

asyncio.run(main())
"""


@pytest.mark.parametrize("crash_while", ["waiting", "holding"])
async def test_process_crashes_do_not_strand_fifo(paths: Paths, crash_while: str):
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    children = []
    try:
        for name in ("first", "second"):
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                CHILD,
                str(paths.home),
                name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            children.append(child)
            await wait_queued(paths, ["first"] if name == "first" else ["first", "second"])
        first, second = children
        assert first.stdout is not None and second.stdout is not None
        if crash_while == "holding":
            owner.close()
            assert await asyncio.wait_for(first.stdout.readline(), 5) == b"acquired\n"
            assert queued(paths) == ["second"]
        first.kill()
        await asyncio.wait_for(first.wait(), 5)
        owner.close()
        assert await asyncio.wait_for(second.stdout.readline(), 5) == b"acquired\n"
        assert queued(paths) == []
        assert second.stdin is not None
        second.stdin.write(b"done\n")
        await second.stdin.drain()
        assert await asyncio.wait_for(second.wait(), 5) == 0
    finally:
        owner.close()
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
    assert_unlocked(paths)


async def test_waiter_lock_refuses_symlink_without_touching_target(paths: Paths, tmp_path):
    target = tmp_path / "untouched"
    target.write_text("private")
    paths.lock("job-waiters", "default", "unsafe").symlink_to(target)
    with pytest.raises(LockPathError):
        await concurrency.acquire(paths, job(paths, "unsafe"), "unsafe")
    assert target.read_text() == "private"
    assert queued(paths) == []


async def test_lock_files_remain_empty_and_keep_their_inode(paths: Paths):
    first = await concurrency.acquire(paths, job(paths, "one"), "one")
    assert first.lock is not None
    first.lock.close()
    locks = {path: path.stat().st_ino for path in paths.runtime_dir.rglob("*.lock")}
    second = await concurrency.acquire(paths, job(paths, "one"), "new-run")
    assert second.lock is not None
    second.lock.close()
    assert set(paths.runtime_dir.rglob("*.lock")) == set(locks)
    for path, inode in locks.items():
        assert path.stat().st_ino == inode
        assert path.read_bytes() == b""


async def test_restarted_job_replaces_its_dead_generation_before_waiting(paths: Paths):
    owner = concurrency.acquire_group_lock(paths, "reports")
    assert owner is not None
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CHILD,
        str(paths.home),
        "same-job",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await wait_queued(paths, ["same-job"])
        child.kill()
        await asyncio.wait_for(child.wait(), 5)
        pending = asyncio.create_task(
            concurrency.acquire(paths, job(paths, "same-job"), "restarted")
        )
        await wait_queued(paths, ["restarted"])
        owner.close()
        result = await asyncio.wait_for(pending, 5)
        assert result.lock is not None
        result.lock.close()
        assert queued(paths) == []
    finally:
        owner.close()
        if child.returncode is None:
            child.kill()
            await child.wait()
    assert_unlocked(paths)
