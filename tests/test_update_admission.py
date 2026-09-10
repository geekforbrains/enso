"""In-flight CLI work and busy drains remain safe across update transitions."""

import errno
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from test_updates import managed as managed
from test_updates import queue

from enso import maintenance, updates
from enso.maintenance import UpdateError, read_json, write_json


@pytest.mark.parametrize("gate_present", [False, True])
def test_interrupted_drain_defers_without_stopping_busy_work(managed, gate_present):
    state = queue(managed)
    updates._save(managed.paths, state, "draining")
    if gate_present:
        write_json(managed.paths.maintenance, {"operation_id": state["id"]})
    managed.events.clear()
    updates.run_update(managed.paths, state["id"])
    assert read_json(managed.paths.update_state)["status"] == "deferred"
    assert not maintenance.paused(managed.paths)
    assert managed.events == []
    assert updates.installed(managed.paths)["version"] == "0.1.0"


def test_cli_holds_home_access_while_waiting_for_input(managed, raw_config, tmp_path):
    paths = managed.paths
    fifo = tmp_path / "config.fifo"
    os.mkfifo(fifo)
    command = [sys.executable, "-m", "enso.cli", "config", "apply", "--file", str(fifo), "--json"]
    process = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=dict(os.environ, ENSO_HOME=str(paths.home)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    writer = None
    try:
        deadline = time.monotonic() + 10
        while writer is None:
            try:
                writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno != errno.ENXIO or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        # Opening the write end proves the CLI passed admission and reached its
        # blocking input read. An update cannot snapshot until that CLI finishes.
        with (
            pytest.raises(UpdateError, match="another command"),
            maintenance.exclusive_access(paths, timeout=0),
        ):
            pytest.fail("snapshot admission succeeded while a CLI was awaiting input")
        write_json(paths.maintenance, {"operation_id": "b" * 32})
        write_json(paths.update_state, {"id": "b" * 32, "status": "draining"})
        os.write(writer, json.dumps(raw_config).encode())
        os.close(writer)
        writer = None
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0, (output, error)
        assert json.loads(output)["applied"] is True
        with maintenance.exclusive_access(paths, timeout=0):
            assert read_json(paths.config) == raw_config
    finally:
        if writer is not None:
            os.close(writer)
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_new_cli_cannot_mutate_during_exclusive_update(managed, raw_config, tmp_path):
    with maintenance.exclusive_access(managed.paths, timeout=0):
        result = subprocess.run(
            [sys.executable, "-m", "enso.cli", "config", "apply", "--file", "-", "--json"],
            input=json.dumps(raw_config),
            cwd=tmp_path,
            env=dict(os.environ, ENSO_HOME=str(managed.paths.home)),
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False
    assert not managed.paths.config.exists()
