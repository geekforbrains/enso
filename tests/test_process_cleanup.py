"""Cleanup must tolerate an exiting process without hiding live signal failures."""

import asyncio
import errno
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from enso import execution


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


@pytest.mark.parametrize("group", [0, -1, 654321])
async def test_unsafe_group_signals_only_the_child(monkeypatch, group):
    process = child()
    monkeypatch.setattr(execution.os, "getpgid", lambda _pid: group)
    monkeypatch.setattr(execution.os, "getpgrp", lambda: 654321)
    kill = Mock()
    monkeypatch.setattr(execution.os, "killpg", kill)

    await execution.terminate_process_tree(process, "shared group provider", grace=0.1)

    kill.assert_not_called()
    process.send_signal.assert_called_once_with(signal.SIGTERM)
