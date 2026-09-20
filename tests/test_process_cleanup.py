"""Cleanup must tolerate an exiting process without hiding live signal failures."""

import asyncio
import errno
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from enso import execution, releases, worktrees


def child():
    return SimpleNamespace(
        pid=123456, returncode=None, wait=AsyncMock(return_value=0), send_signal=Mock()
    )


async def test_group_disappearing_after_permission_error_is_reaped(monkeypatch):
    process = child()
    monkeypatch.setattr(execution.os, "getpgid", lambda _pid: process.pid)
    monkeypatch.setattr(execution.os, "getpgrp", lambda: 654321)
    failure = PermissionError(errno.EPERM, "Operation not permitted")
    kill = Mock(side_effect=[failure, ProcessLookupError(errno.ESRCH, "No such process")])
    monkeypatch.setattr(execution.os, "killpg", kill)

    await execution.terminate_process_tree(process, "exited provider", grace=0.1)

    assert kill.call_args_list == [((process.pid, signal.SIGTERM),), ((process.pid, 0),)]
    process.wait.assert_awaited()
    process.send_signal.assert_not_called()


@pytest.mark.parametrize("leader_exited", [False, True])
async def test_live_group_permission_failure_is_not_hidden(monkeypatch, leader_exited):
    process = child()
    if not leader_exited:
        process.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    monkeypatch.setattr(execution.os, "getpgid", lambda _pid: process.pid)
    monkeypatch.setattr(execution.os, "getpgrp", lambda: 654321)
    kill = Mock(side_effect=PermissionError(errno.EPERM, "Operation not permitted"))
    monkeypatch.setattr(execution.os, "killpg", kill)

    with pytest.raises(PermissionError):
        await execution.terminate_process_tree(process, "protected provider", grace=0.1)

    process.send_signal.assert_not_called()


@pytest.mark.parametrize("group", [0, 654321])
async def test_unsafe_group_signals_only_the_child(monkeypatch, group):
    process = child()
    monkeypatch.setattr(execution.os, "getpgid", lambda _pid: group)
    monkeypatch.setattr(execution.os, "getpgrp", lambda: 654321)
    kill = Mock()
    monkeypatch.setattr(execution.os, "killpg", kill)

    await execution.terminate_process_tree(process, "shared group provider", grace=0.1)

    kill.assert_not_called()
    process.send_signal.assert_called_once_with(signal.SIGTERM)


@pytest.mark.parametrize("group_state", ["gone", "exists", "denied"])
def test_worktree_command_cleanup_preserves_result_only_when_group_disappears(
    tmp_path, monkeypatch, group_state
):
    denied = PermissionError(errno.EPERM, "Original cleanup permission failure")
    probe = {
        "gone": ProcessLookupError(errno.ESRCH, "No such process"),
        "exists": None,
        "denied": PermissionError(errno.EPERM, "Probe permission failure"),
    }[group_state]
    kill = Mock(side_effect=[denied, probe])
    monkeypatch.setattr(execution.os, "killpg", kill)

    def run():
        return worktrees._run(
            [sys.executable, "-c", "print('Git refusal'); raise SystemExit(3)"],
            cwd=tmp_path,
            timeout=5,
        )

    if group_state == "gone":
        assert run() == (3, "Git refusal\n")
    else:
        with pytest.raises(PermissionError) as error:
            run()
        assert error.value is denied
    assert [call.args[1] for call in kill.call_args_list] == [signal.SIGKILL, 0]


def test_sync_cleanup_does_not_hide_permission_failure_when_child_cannot_be_reaped(monkeypatch):
    process = SimpleNamespace(
        pid=123456, wait=Mock(side_effect=subprocess.TimeoutExpired("child", 1))
    )
    denied = PermissionError(errno.EPERM, "Child still running")
    kill = Mock(side_effect=denied)
    monkeypatch.setattr(execution.os, "killpg", kill)

    with pytest.raises(PermissionError) as error:
        execution.kill_process_group(process)

    assert error.value is denied
    kill.assert_called_once_with(process.pid, signal.SIGKILL)


@pytest.mark.parametrize("group_state", ["gone", "exists", "denied"])
async def test_background_cleanup_checks_group_even_after_leader_exits(monkeypatch, group_state):
    process = child()
    process.returncode = 0
    denied = PermissionError(errno.EPERM, "Original cleanup permission failure")
    probe = {
        "gone": ProcessLookupError(errno.ESRCH, "No such process"),
        "exists": None,
        "denied": PermissionError(errno.EPERM, "Probe permission failure"),
    }[group_state]
    kill = Mock(side_effect=[denied, probe])
    monkeypatch.setattr(execution.os, "killpg", kill)

    if group_state == "gone":
        await execution.terminate_background_process(process, "finished command")
    else:
        with pytest.raises(PermissionError) as error:
            await execution.terminate_background_process(process, "finished command")
        assert error.value is denied
    process.wait.assert_awaited()
    assert kill.call_args_list == [((process.pid, signal.SIGKILL),), ((process.pid, 0),)]


@pytest.mark.parametrize("group_state", ["gone", "exists"])
def test_installer_group_permission_error_is_ignored_only_after_proving_absence(
    monkeypatch, group_state
):
    """The release installer keeps its own copy of this dance, outside ``enso.execution``."""
    signals = []
    reaped = []
    denied = PermissionError(errno.EPERM, "installer group permission failure")

    def killpg(pid, sent_signal):
        signals.append(sent_signal)
        if sent_signal:
            raise denied
        if group_state == "gone":
            raise ProcessLookupError(errno.ESRCH, "group reaped")

    monkeypatch.setattr(releases.os, "killpg", killpg)
    process = SimpleNamespace(pid=123, wait=lambda **kwargs: reaped.append(True))

    if group_state == "gone":
        releases._kill_installer_group(process)
    else:
        with pytest.raises(PermissionError) as error:
            releases._kill_installer_group(process)
        assert error.value is denied
    assert reaped == [True]
    assert signals == [releases.signal.SIGKILL, 0]


async def test_cancellation_during_timeout_cleanup_still_reaps_process(tmp_path, monkeypatch):
    """A cancellation landing inside timeout cleanup still reaps the child and leaks no task."""
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
