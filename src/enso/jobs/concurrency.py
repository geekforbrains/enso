"""Fair, cross-process admission to job groups, with crash-safe waiting leases.

The per-job lock belongs to the runner and remains held while admission waits. SQLite
orders waiters; advisory locks establish whether each waiter and the group owner still
exist. No polling sleep or job execution holds a database transaction or worker thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import IO, Literal

from .. import db, execution
from ..config import Paths
from ..locks import acquire_file_lock
from ..maintenance import paused
from . import Job

POLL_SECONDS = 0.2


@dataclass(frozen=True)
class Admission:
    """The caller owns an acquired lock until the job finishes, including its hooks."""

    reason: Literal["acquired", "busy", "expired", "paused"]
    lock: IO[str] | None = None


def acquire_group_lock(paths: Paths, group: str) -> IO[str] | None:
    """Take a group's advisory lock without waiting; names cannot escape the lock directory.

    Job callers use ``acquire`` so an immediate acquisition cannot bypass the queue.
    """
    return acquire_file_lock(paths.lock("groups", hashlib.sha256(group.encode()).hexdigest()))


@dataclass
class _Waiter:
    paths: Paths
    job: Job
    run_id: str
    deadline: float | None
    lease: IO[str] | None = None
    group_lock: IO[str] | None = None
    registered: bool = False

    def _head(self, con: sqlite3.Connection, group: str) -> str | None:
        # Only the head can block admission. Recover dead heads lazily instead of
        # opening every waiter's lease on every poll (quadratic work for a busy group).
        while True:
            row = con.execute(
                "SELECT run_id, workspace, job FROM _enso_job_waiters WHERE group_name = ? "
                "ORDER BY sequence LIMIT 1",
                (group,),
            ).fetchone()
            if row is None:
                return None
            if row["run_id"] == self.run_id:
                return self.run_id
            lease = acquire_file_lock(self.paths.lock("job-waiters", row["workspace"], row["job"]))
            if lease is None:
                return str(row["run_id"])
            try:
                con.execute("DELETE FROM _enso_job_waiters WHERE run_id = ?", (row["run_id"],))
            finally:
                lease.close()

    def attempt(self) -> Admission | None:
        """One short transaction: register, reconcile stale leases, and try the head."""
        policy = self.job.concurrency
        assert policy is not None
        if policy.on_busy == "wait" and paused(self.paths):
            return Admission("paused")
        if policy.on_busy == "wait" and self.lease is None:
            self.lease = acquire_file_lock(
                self.paths.lock("job-waiters", self.job.workspace, self.job.dir_name)
            )
            if self.lease is None:
                raise RuntimeError(f"run {self.run_id} is already waiting for a group")
        with db.transaction(self.paths) as con:
            if policy.on_busy == "wait" and paused(self.paths):
                return Admission("paused")
            # Getting SQLite's writer lock may itself take time. Never acquire a group
            # after the wait deadline simply because its previous owner just finished.
            if self.deadline is not None and time.monotonic() >= self.deadline:
                return Admission("expired")
            if policy.on_busy == "wait" and not self.registered:
                # Acquiring the reusable job lease proves previous generations are dead.
                # Clear their rows before registering: our live lease must not keep an
                # older run of this job at the head forever. The runner's per-job lock
                # ensures that no legitimate generation can be displaced here.
                con.execute(
                    "DELETE FROM _enso_job_waiters WHERE workspace = ? AND job = ?",
                    (self.job.workspace, self.job.dir_name),
                )
                con.execute(
                    "INSERT INTO _enso_job_waiters (run_id, workspace, job, group_name) "
                    "VALUES (?, ?, ?, ?)",
                    (self.run_id, self.job.workspace, self.job.dir_name, policy.group),
                )
                self.registered = True
            head = self._head(con, policy.group)
            if head is None or head == self.run_id:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    return Admission("expired")
                self.group_lock = acquire_group_lock(self.paths, policy.group)
                if self.group_lock is not None:
                    con.execute("DELETE FROM _enso_job_waiters WHERE run_id = ?", (self.run_id,))
                    self.registered = False
                    return Admission("acquired", self.group_lock)
            return Admission("busy") if policy.on_busy == "skip" else None

    def cleanup(self) -> None:
        """Remove our queue entry before dropping its lease, even after interrupted admission."""
        try:
            if self.registered:
                with db.transaction(self.paths) as con:
                    con.execute("DELETE FROM _enso_job_waiters WHERE run_id = ?", (self.run_id,))
                self.registered = False
        finally:
            if self.lease is not None:
                self.lease.close()
                self.lease = None


async def acquire(paths: Paths, job: Job, run_id: str) -> Admission:
    """Wait fairly or skip, as the job explicitly requests; return ownership or a reason.

    This runs before execution budgets start. The runner must already hold the per-job
    lock, so repeated triggers cannot accumulate another pending run of the same job.
    Waiting jobs consume neither a database transaction nor a worker during each pause.
    """
    policy = job.concurrency
    if policy is None:
        return Admission("acquired")
    deadline = time.monotonic() + policy.max_wait if policy.max_wait is not None else None
    waiter = _Waiter(paths, job, run_id, deadline)
    try:
        try:
            while True:
                result = await execution.run_sync(waiter.attempt)
                if result is not None:
                    break
                pause = POLL_SECONDS
                if deadline is not None:
                    pause = min(pause, max(0, deadline - time.monotonic()))
                await asyncio.sleep(pause)
        finally:
            await execution.run_sync(waiter.cleanup)
    except BaseException:
        if waiter.group_lock is not None:
            waiter.group_lock.close()
        raise
    return result
