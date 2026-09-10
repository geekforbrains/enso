"""Shared process cleanup remains reliable when cancellation interrupts a timeout."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from enso import execution
from enso.providers import ClaudeProvider


async def test_cancellation_during_timeout_cleanup_still_reaps_process(tmp_path, monkeypatch):
    terminating = asyncio.Event()
    stopped = asyncio.Event()
    calls = []

    async def wait():
        await stopped.wait()

    process = SimpleNamespace(
        stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader(), wait=wait, returncode=None
    )

    async def create(*args, **kwargs):
        return process

    async def terminate(child, label):
        assert child is process
        calls.append(label)
        if len(calls) == 1:
            terminating.set()
            await asyncio.Event().wait()
        child.returncode = -9
        stopped.set()

    monkeypatch.setattr(execution.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(execution, "terminate_background_process", terminate)
    baseline = asyncio.all_tasks()
    running = asyncio.create_task(
        execution.run_process(
            ["fake"], cwd=tmp_path, env={}, timeout=0.01, merge_stderr=False, label="gate"
        )
    )
    await asyncio.wait_for(terminating.wait(), 1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped.is_set() and len(calls) == 2
    assert asyncio.all_tasks() <= baseline


@pytest.mark.parametrize("timeout", [0, -1])
async def test_exhausted_batch_budget_does_not_launch(tmp_path, monkeypatch, timeout):
    async def create(*args, **kwargs):
        pytest.fail("must not launch an exhausted turn")

    monkeypatch.setattr(execution.asyncio, "create_subprocess_exec", create)
    result = await execution.execute_batch(
        ClaudeProvider("fake"),
        "hello",
        "opus",
        "high",
        [],
        cwd=tmp_path,
        env={},
        timeout=timeout,
        label="beat",
    )
    assert result.status == "timeout" and result.exit_code is None
