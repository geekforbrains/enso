"""Job execution against the fake CLI: prerun contract, run rows, overlap, scheduling, alerts."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import signal
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (
    FakeReply,
    FakeTransport,
    load_job,
    make_turn,
    script,
    write_config,
    write_job,
    write_workspace,
)

from enso import db, messages, runs
from enso.config import Config, Paths, parse_config
from enso.execution import NOTIFY_LIMIT, ProviderTurn
from enso.jobs import Job
from enso.jobs import runner as runner_module
from enso.jobs.runner import (
    POSTRUN_FEEDBACK_LIMIT,
    JobRunner,
    Postrun,
    acquire_group_lock,
    acquire_lock,
    decide,
)
from enso.locks import LockPathError
from enso.runtime import ORIGIN_HEADER, Runtime


@pytest.fixture(autouse=True)
def local_zone(request: pytest.FixtureRequest) -> Iterator[str]:
    """Pin the C library's zone, which cron arithmetic runs in; UTC unless parametrized."""
    zone = getattr(request, "param", "UTC")
    saved = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    yield zone
    if saved is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = saved
    time.tzset()


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport("slack")


@pytest.fixture
def runner(fake_config: Config, transport: FakeTransport) -> JobRunner:
    return JobRunner(fake_config, {"slack": transport})


def job(
    paths: Paths,
    config: Config,
    *,
    script: str | None = None,
    hook: str | None = None,
    **fields: str,
) -> Job:
    """A ``nightly`` job; ``script`` becomes ``prerun.sh`` and ``hook`` becomes ``postrun.sh``."""
    write_job(paths, **fields)
    if script is not None:
        (paths.workspace_jobs("default") / "nightly" / "prerun.sh").write_text(script)
    if hook is not None:
        (paths.workspace_jobs("default") / "nightly" / "postrun.sh").write_text(hook)
    return load_job(paths, config)


# Records what a postrun sees: stdin to seen.txt, the outcome variables to env.txt.
RECORDER = "cat > seen.txt; env | grep '^ENSO_RUN_' | sort > env.txt"


def recorded(nightly: Job) -> tuple[str, dict[str, str]]:
    seen = (nightly.job_dir / "seen.txt").read_text()
    lines = (nightly.job_dir / "env.txt").read_text().splitlines()
    return seen, dict(line.split("=", 1) for line in lines)


async def test_manual_run_records_the_run_and_stays_quiet(
    runner: JobRunner, enso_home: Paths, fake_config: Config, transport: FakeTransport
) -> None:
    result = await runner.run(job(enso_home, fake_config, prompt="hello"), trigger="manual")
    assert (result.status, result.exit_code) == ("ok", 0) and result.run_id
    assert result.output.endswith(
        f"batch job=default:nightly run={result.run_id} workspace=default prompt=hello"
    )
    run = runs.get(enso_home, result.run_id)
    assert run is not None and (run.status, run.trigger, run.output) == (
        "ok",
        "manual",
        result.output,
    )
    assert db.job_state(enso_home, "default:nightly").last_run is None
    assert transport.sent == []


async def test_a_job_prompt_never_gains_a_chat_origin(
    runner: JobRunner, runtime: Runtime, enso_home: Paths, fake_config: Config
) -> None:
    """Nobody sent a job, so it gets no origin block — not even after a chat turn ran."""
    await runtime.handle(make_turn("hello"), FakeReply())
    result = await runner.run(job(enso_home, fake_config, prompt="hello"), trigger="manual")
    assert result.status == "ok"
    assert result.output.endswith(
        f"batch job=default:nightly run={result.run_id} workspace=default prompt=hello"
    )
    assert ORIGIN_HEADER not in result.output
    # A turn builds its child environment from a copy, so nothing chat-shaped is left behind.
    assert [key for key in os.environ if key.startswith("ENSO_ORIGIN_")] == []


async def test_run_records_the_clamped_effort(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    nightly = job(enso_home, fake_config, model="haiku", effort="max")
    result = await runner.run(nightly, trigger="manual")
    run = runs.get(enso_home, result.run_id or "")
    assert result.status == "ok" and run is not None
    assert (run.model, run.effort) == ("haiku", "high")  # the effective level, not the request


@pytest.mark.parametrize(
    ("model", "effort", "expected_effort"),
    [
        ("gemini-3.8-flash-low", "high", "low"),
        ("gemini-3.8-flash-high", "low", "high"),
    ],
)
async def test_agy_job_pins_the_workspace_project(
    enso_home: Paths,
    raw_config_both: dict,
    fake_agy: str,
    transport: FakeTransport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    effort: str,
    expected_effort: str,
) -> None:
    """A scheduled Antigravity run resolves its project instead of registering another one."""
    catalog = tmp_path / "gemini-home/.gemini/config/projects"
    catalog.mkdir(parents=True)
    catalog.joinpath("ws.json").write_text(
        json.dumps(
            {
                "id": "ws-project",
                "projectResources": {
                    "resources": [{"folderUri": f"file://{enso_home.workspace('default')}"}]
                },
            }
        )
    )
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path / "gemini-home"))
    monkeypatch.setenv("FAKE_AGY_ARGV", str(argv_log))
    raw_config_both["providers"]["agy"] = {"path": fake_agy, "models": [model]}
    config, problems, _ = parse_config(raw_config_both, enso_home)
    assert config is not None, problems
    db.initialize(enso_home)

    nightly = job(enso_home, config, provider="agy", model=model, effort=effort)
    result = await JobRunner(config, {"slack": transport}).run(nightly, trigger="manual")
    assert result.status == "ok", result.error
    run = runs.get(enso_home, result.run_id or "")
    assert run is not None and (run.model, run.effort) == (model, expected_effort)

    # The job path passes the workspace too, so a run finds the pin the chat turns created.
    (launch,) = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert launch[launch.index("--model") + 1] == model
    assert "--effort" not in launch
    assert launch[-3:-1] == ["--project", "ws-project"]
    # Nothing is registered, and a job never resumes: every run is a fresh conversation.
    assert "--new-project" not in launch and "--conversation" not in launch


async def test_opencode_job_extracts_the_answer_from_jsonl(
    enso_home: Paths,
    raw_config_both: dict,
    fake_opencode: str,
    transport: FakeTransport,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv_log = tmp_path / "opencode-argv.jsonl"
    monkeypatch.setenv("FAKE_OPENCODE_ARGV", str(argv_log))
    # The job environment inherits the service's PWD, which OpenCode would otherwise take
    # as its project root in preference to the workspace the runner starts it in.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("PWD", str(elsewhere))
    workspace = enso_home.workspace("default")
    model = "openrouter/deepseek/deepseek-v4-flash"
    raw_config_both["providers"]["opencode"] = {
        "path": fake_opencode,
        "models": [model],
        "args": ["--auto"],
    }
    config, problems, _ = parse_config(raw_config_both, enso_home)
    assert config is not None, problems
    db.initialize(enso_home)

    nightly = job(
        enso_home,
        config,
        provider="opencode",
        model=model,
        effort="high",
        prompt="hello",
    )
    result = await JobRunner(config, {"slack": transport}).run(nightly, trigger="manual")
    assert result.status == "ok" and result.run_id
    assert result.output == (
        f"batch job=default:nightly run={result.run_id} workspace=default "
        f"root={workspace.resolve()} prompt=hello"
    )
    assert not result.output.startswith("{")

    (launch,) = [json.loads(line) for line in argv_log.read_text().splitlines()]
    assert launch == [
        "run", "-m", model, "--variant", "high", "--auto", "--format", "json",
        "--dir", str(workspace), "--", "hello",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("script", "status", "error", "provider_ran"),
    [
        ("echo 'fresh data'", "ok", "", True),
        ("exit 1", "no_work", "", False),
        (
            "echo 'ENSO_ERROR: feed down' >&2; echo scraped; exit 2",
            "prerun_error",
            "feed down",
            False,
        ),
        ("exit 3", "prerun_error", "prerun exited with status 3", False),
        ("sleep 5", "prerun_error", "prerun timed out after 1s", False),
        (None, "prerun_error", "prerun script not found: prerun.sh", False),
    ],
)
async def test_prerun_contract(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    script: str | None,
    status: str,
    error: str,
    provider_ran: bool,
) -> None:
    nightly = job(
        enso_home,
        fake_config,
        script=script,
        prompt="Use: {{prerun_output}}",
        prerun="prerun.sh",
        prerun_timeout=1,
    )
    result = await runner.run(nightly, trigger="manual")
    assert (result.status, result.error) == (status, error)
    assert result.output.endswith("prompt=Use: fresh data") if provider_ran else not result.output
    run = runs.get(enso_home, result.run_id or "")
    assert run is not None and run.status == status


async def test_provider_failure_and_timeout(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    failed = await runner.run(job(enso_home, fake_config, prompt="fail"), trigger="manual")
    assert (failed.status, failed.exit_code, failed.error) == (
        "error",
        1,
        "claude exited with status 1",
    )
    assert "fake: boom" in failed.output  # stderr is merged into the captured output
    late = await runner.run(
        job(enso_home, fake_config, prompt="sleep 5", timeout=1), trigger="manual"
    )
    assert (late.status, late.error, late.output) == (
        "timeout",
        "timed out after 1s",
        "batch: working",
    )


async def test_postrun_gets_the_output_on_stdin_and_the_outcome_in_env(
    runner: JobRunner, enso_home: Paths, fake_config: Config, transport: FakeTransport
) -> None:
    nightly = job(enso_home, fake_config, hook=RECORDER, prompt="hello", postrun="postrun.sh")
    result = await runner.run(nightly, trigger="schedule")
    seen, env = recorded(nightly)
    assert result.status == "ok" and result.postrun_error == "" and seen == result.output
    assert env["ENSO_RUN_ID"] == result.run_id
    assert (env["ENSO_RUN_STATUS"], env["ENSO_RUN_EXIT_CODE"]) == ("ok", "0")
    run = runs.get(enso_home, result.run_id or "")
    assert run is not None and run.duration_ms is not None
    assert 0 <= int(env["ENSO_RUN_DURATION_MS"]) <= run.duration_ms
    assert (env["ENSO_RUN_ATTEMPT"], env["ENSO_RUN_FOLLOWUPS_REMAINING"]) == ("1", "2")
    assert transport.sent == []
    assert "postrun_error" in result.as_dict()


@pytest.mark.parametrize(
    ("script", "status", "exit_code"),
    [("exit 1", "no_work", "1"), ("exit 3", "prerun_error", "3"), ("echo go", "ok", "0")],
)
async def test_postrun_runs_for_every_outcome(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    script: str,
    status: str,
    exit_code: str,
) -> None:
    nightly = job(
        enso_home,
        fake_config,
        script=script,
        hook=RECORDER,
        prerun="prerun.sh",
        postrun="postrun.sh",
    )
    result = await runner.run(nightly, trigger="manual")
    seen, env = recorded(nightly)
    assert result.status == status and seen == result.output
    assert (env["ENSO_RUN_STATUS"], env["ENSO_RUN_EXIT_CODE"]) == (status, exit_code)


@pytest.mark.parametrize(
    ("hook", "diagnostic"),
    [
        ("echo 'ENSO_ERROR: archive full' >&2; exit 2", "archive full"),
        ("exit 4", "postrun exited with status 4"),
        ("sleep 5", "postrun timed out after 1s"),
        (None, "postrun script not found: postrun.sh"),
    ],
)
async def test_failing_postrun_marks_the_run_failed_and_alerts_once(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    transport: FakeTransport,
    hook: str | None,
    diagnostic: str,
) -> None:
    nightly = job(enso_home, fake_config, hook=hook, postrun="postrun.sh", postrun_timeout=1)
    result = await runner.run(nightly, trigger="schedule")
    assert (result.status, result.exit_code, result.postrun_error) == ("error", 0, diagnostic)
    run = runs.get(enso_home, result.run_id or "")
    assert run is not None and (run.status, run.error, run.postrun_error) == (
        "error",
        diagnostic,
        diagnostic,
    )
    assert transport.sent == [("C1", f"⚠️ [default:nightly] postrun failed\n{diagnostic}")]
    manual = await runner.run(nightly, trigger="manual")
    assert manual.postrun_error == diagnostic and len(transport.sent) == 1


async def test_postrun_that_ignores_or_half_reads_stdin_does_not_hang(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(tmp_path, monkeypatch, "x" * runs.OUTPUT_KEEP, "y" * runs.OUTPUT_KEEP)
    ignoring = job(enso_home, fake_config, hook="exit 0", postrun="postrun.sh")
    started = time.monotonic()
    result = await runner.run(ignoring, trigger="manual")
    assert (result.status, result.postrun_error) == ("ok", "") and len(result.output) > 100_000
    partial = job(enso_home, fake_config, hook="head -c 10 > part.txt", postrun="postrun.sh")
    result = await runner.run(partial, trigger="manual")
    assert (result.status, result.postrun_error) == ("ok", "")
    assert (partial.job_dir / "part.txt").read_text() == "y" * 10
    assert time.monotonic() - started < 5


async def test_postrun_continues_the_same_session_with_feedback_and_only_one_prerun(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    transport: FakeTransport,
) -> None:
    nightly = job(
        enso_home,
        fake_config,
        script="echo selected >> selections.txt; echo task-42",
        prerun="prerun.sh",
        prompt="Work on {{prerun_output}}",
        hook="""cat > "seen-$ENSO_RUN_ATTEMPT.txt"
printf '%s:%s\n' "$ENSO_RUN_ATTEMPT" "$ENSO_RUN_FOLLOWUPS_REMAINING" >> checks.txt
if [ "$ENSO_RUN_ATTEMPT" = 1 ]; then
    printf 'Commit the work for task-42.\nKeep its existing changes.\n'
    exit 10
fi
""",
        postrun="postrun.sh",
    )
    result = await runner.run(nightly, trigger="schedule")
    assert result.status == "ok" and result.run_id and result.session_id
    attempts = runs.attempts(enso_home, result.run_id)
    assert [attempt.number for attempt in attempts] == [1, 2]
    first, second = attempts
    assert first.session_id == second.session_id == result.session_id
    assert first.output.endswith("prompt=Work on task-42")
    feedback = "Commit the work for task-42.\nKeep its existing changes.\n"
    assert first.postrun_exit_code == 10 and first.postrun_output == feedback
    assert second.output == f"resumed {result.session_id} workspace=default prompt={feedback}"
    assert second.postrun_exit_code == 0 and result.output == second.output
    assert (nightly.job_dir / "seen-1.txt").read_text() == first.output
    assert (nightly.job_dir / "seen-2.txt").read_text() == second.output
    assert (nightly.job_dir / "checks.txt").read_text().splitlines() == ["1:2", "2:1"]
    assert (nightly.job_dir / "selections.txt").read_text().splitlines() == ["selected"]
    assert transport.sent == []
    fresh = await runner.run(nightly, trigger="manual")
    assert fresh.status == "ok" and fresh.session_id != result.session_id
    assert runs.attempts(enso_home, fresh.run_id or "")[0].output.startswith("new ")


@pytest.mark.parametrize("limit", [None, 0, 1, 3])
async def test_postrun_followup_limit_is_default_two_or_the_job_override(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    transport: FakeTransport,
    limit: int | None,
) -> None:
    fields = {"max_followups": limit} if limit is not None else {}
    nightly = job(
        enso_home,
        fake_config,
        hook=(
            'echo "$ENSO_RUN_ATTEMPT:$ENSO_RUN_FOLLOWUPS_REMAINING" >> checks.txt; '
            "echo repair; exit 10"
        ),
        postrun="postrun.sh",
        **fields,
    )
    result = await runner.run(nightly, trigger="schedule")
    cap = 2 if limit is None else limit
    assert result.status == "error" and f"max_followups={cap}" in result.postrun_error
    attempts = runs.attempts(enso_home, result.run_id or "")
    assert len(attempts) == cap + 1
    assert attempts[-1].postrun_error == result.postrun_error
    assert len({attempt.session_id for attempt in attempts}) == 1
    assert (nightly.job_dir / "checks.txt").read_text().splitlines() == [
        f"{number}:{cap + 1 - number}" for number in range(1, cap + 2)
    ]
    assert len(transport.sent) == 1 and "exit 0" not in transport.sent[0][1]
    run = runs.get(enso_home, result.run_id or "")
    assert run is not None and run.status == "error" and run.postrun_error == result.postrun_error


@pytest.mark.parametrize(
    ("hook", "error"),
    [
        ("exit 10", "without a feedback message"),
        ("printf ' \\n\\t'; exit 10", "without a feedback message"),
        (
            f"head -c {POSTRUN_FEEDBACK_LIMIT + 1} /dev/zero | tr '\\0' x; exit 10",
            "exceeds 65536 bytes",
        ),
        (
            # A huge prefix followed by plausible instructions must not become a valid tail.
            f"head -c {2 * POSTRUN_FEEDBACK_LIMIT} /dev/zero | tr '\\0' x; echo repair; exit 10",
            "exceeds 65536 bytes",
        ),
    ],
)
async def test_invalid_postrun_feedback_fails_without_resuming(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    hook: str,
    error: str,
) -> None:
    nightly = job(enso_home, fake_config, hook=hook, postrun="postrun.sh")
    result = await runner.run(nightly, trigger="manual")
    assert result.status == "error" and error in result.postrun_error
    (attempt,) = runs.attempts(enso_home, result.run_id or "")
    assert attempt.postrun_exit_code == 10 and attempt.postrun_error == result.postrun_error


@pytest.mark.parametrize(("gate", "status"), [("exit 1", "error"), ("exit 2", "prerun_error")])
async def test_postrun_cannot_open_a_closed_prerun_gate(
    runner: JobRunner, enso_home: Paths, fake_config: Config, gate: str, status: str
) -> None:
    nightly = job(
        enso_home,
        fake_config,
        script=gate,
        prerun="prerun.sh",
        hook=RECORDER + "; echo repair; exit 10",
        postrun="postrun.sh",
    )
    result = await runner.run(nightly, trigger="manual")
    assert result.status == status and "no provider ran" in result.postrun_error
    seen, env = recorded(nightly)
    assert seen == "" and env["ENSO_RUN_ATTEMPT"] == "0"
    assert env["ENSO_RUN_FOLLOWUPS_REMAINING"] == "2"
    (attempt,) = runs.attempts(enso_home, result.run_id or "")
    assert attempt.number == 0 and attempt.session_id is None


async def test_postrun_cannot_resume_a_group_collision(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    nightly = job(
        enso_home,
        fake_config,
        hook=RECORDER + "; echo repair; exit 10",
        postrun="postrun.sh",
        concurrency_group="shared",
    )
    held = acquire_group_lock(enso_home, "shared")
    assert held is not None
    try:
        result = await runner.run(nightly, trigger="manual")
    finally:
        held.close()
    assert result.status == "error" and "no provider ran" in result.postrun_error
    assert recorded(nightly)[1]["ENSO_RUN_STATUS"] == "skipped"
    (attempt,) = runs.attempts(enso_home, result.run_id or "")
    assert (attempt.number, attempt.status) == (0, "skipped")


@pytest.mark.parametrize(
    ("prompt", "bad_session", "status"),
    [("fail", False, "error"), ("sleep 5", False, "timeout"), ("hello", True, "error")],
)
async def test_postrun_reacts_to_provider_failure_without_retrying(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    transport: FakeTransport,
    prompt: str,
    bad_session: bool,
    status: str,
) -> None:
    if bad_session:
        monkeypatch.setenv("FAKE_SESSION_ID", "../not-a-session")
    nightly = job(
        enso_home,
        fake_config,
        prompt=prompt,
        timeout=1,
        hook=RECORDER + "; echo repair; exit 10",
        postrun="postrun.sh",
    )
    result = await runner.run(nightly, trigger="schedule")
    assert result.status == status and f"after provider {status}" in result.postrun_error
    assert result.error != result.postrun_error
    assert recorded(nightly)[1]["ENSO_RUN_STATUS"] == status
    assert len(runs.attempts(enso_home, result.run_id or "")) == 1
    assert len(transport.sent) == 1


async def test_followup_requires_a_session_id(
    runner: JobRunner, enso_home: Paths, fake_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing_session(*args, **kwargs):
        return ProviderTurn("ok", output="done", exit_code=0)

    monkeypatch.setattr(runner_module.execution, "execute_turn", missing_session)
    nightly = job(enso_home, fake_config, hook="echo repair; exit 10", postrun="postrun.sh")
    result = await runner.run(nightly, trigger="manual")
    assert result.status == "error" and "no session id" in result.postrun_error
    assert len(runs.attempts(enso_home, result.run_id or "")) == 1


async def test_postrun_exit_zero_does_not_resume_even_with_stdout(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    nightly = job(enso_home, fake_config, hook="echo archived", postrun="postrun.sh")
    result = await runner.run(nightly, trigger="manual")
    assert result.status == "ok" and result.postrun_error == ""
    (attempt,) = runs.attempts(enso_home, result.run_id or "")
    assert attempt.postrun_exit_code == 0 and attempt.postrun_output == "archived\n"


async def test_feedback_at_byte_limit_is_delivered_completely(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    nightly = job(
        enso_home,
        fake_config,
        hook=(
            'if [ "$ENSO_RUN_ATTEMPT" = 1 ]; then '
            f"head -c {POSTRUN_FEEDBACK_LIMIT} /dev/zero | tr '\\0' x; exit 10; fi"
        ),
        postrun="postrun.sh",
    )
    result = await runner.run(nightly, trigger="manual")
    assert result.status == "ok" and result.output.endswith(
        "prompt=" + "x" * POSTRUN_FEEDBACK_LIMIT
    )
    first, second = runs.attempts(enso_home, result.run_id or "")
    assert first.postrun_output == "x" * POSTRUN_FEEDBACK_LIMIT
    assert first.session_id == second.session_id


@pytest.mark.parametrize("spent", [3, 10])
async def test_followups_share_provider_timeout_but_hook_time_does_not_spend_it(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    spent: int,
) -> None:
    clock = [100.0]
    timeouts = []
    durations = []

    async def execute(*args, timeout, **kwargs):
        timeouts.append(timeout)
        clock[0] += spent
        return ProviderTurn("ok", output="done", exit_code=0, session_id="session")

    async def check(job, result, duration_ms, *, attempt):
        durations.append(duration_ms)
        clock[0] += 100  # excludes even a long postrun invocation from provider budget
        return Postrun(10, "repair") if attempt == 1 else Postrun(0)

    monkeypatch.setattr(runner_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runner_module.execution, "execute_turn", execute)
    monkeypatch.setattr(runner, "_postrun", check)
    nightly = job(enso_home, fake_config, hook="exit 0", postrun="postrun.sh", timeout=10)
    result = await runner.run(nightly, trigger="manual")
    if spent == 3:
        assert result.status == "ok" and timeouts == [10, 7]
        assert durations == [3000, 106000]
    else:
        assert result.status == "timeout" and timeouts == [10]
        assert "time budget was exhausted" in result.postrun_error


async def test_postrun_keeps_both_locks_and_run_row_until_followups_finish(
    runner: JobRunner, enso_home: Paths, fake_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    checking = asyncio.Event()
    proceed = asyncio.Event()
    hook = runner._postrun

    async def pause_check(job, result, duration_ms, *, attempt):
        if attempt == 1:
            checking.set()
            await proceed.wait()
        return await hook(job, result, duration_ms, attempt=attempt)

    monkeypatch.setattr(runner, "_postrun", pause_check)
    nightly = job(
        enso_home,
        fake_config,
        hook='if [ "$ENSO_RUN_ATTEMPT" = 1 ]; then echo repair; exit 10; fi',
        postrun="postrun.sh",
        concurrency_group="shared",
    )
    write_job(enso_home, workspace="team", concurrency_group="shared")
    teammate = load_job(enso_home, fake_config, "team:nightly")
    task = runner.start(nightly, trigger="manual")
    try:
        await asyncio.wait_for(checking.wait(), 3)
        (active,) = runs.list_runs(enso_home)
        assert active.status == "running" and active.ended_at is None
        assert acquire_lock(nightly.job_dir) is None
        assert acquire_group_lock(enso_home, "shared") is None
        (attempt,) = runs.attempts(enso_home, active.id)
        assert attempt.status == "ok" and attempt.postrun_exit_code is None
        assert (await runner.run(teammate, trigger="manual")).status == "skipped"
        assert (enso_home.runtime_dir / runner_module.GROUP_LOCK_DIRNAME).is_dir()
    finally:
        proceed.set()
        result = await task
    assert result.status == "ok" and len(runs.attempts(enso_home, result.run_id or "")) == 2
    held = acquire_group_lock(enso_home, "shared")
    assert held is not None
    held.close()
    assert (await runner.run(teammate, trigger="manual")).status == "ok"


async def test_cancel_during_postrun_preserves_completed_attempt_and_closes_row(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    monkeypatch: pytest.MonkeyPatch,
    transport: FakeTransport,
) -> None:
    checking = asyncio.Event()

    async def paused(*args, **kwargs):
        checking.set()
        await asyncio.Future()

    monkeypatch.setattr(runner, "_postrun", paused)
    nightly = job(
        enso_home, fake_config, hook="exit 10", postrun="postrun.sh", concurrency_group="shared"
    )
    task = runner.start(nightly, trigger="schedule")
    await asyncio.wait_for(checking.wait(), 3)
    await runner.stop()
    assert task.cancelled() and transport.sent == []
    (run,) = runs.list_runs(enso_home)
    assert run.status == "error" and "cancelled" in (run.error or "")
    (attempt,) = runs.attempts(enso_home, run.id)
    assert attempt.status == "ok" and attempt.session_id and attempt.output
    for held in (acquire_lock(nightly.job_dir), acquire_group_lock(enso_home, "shared")):
        assert held is not None
        held.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_hook_cleanup_kills_descendant_after_the_leader_has_exited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    """A child retains stdout and ignores SIGTERM; parent death signals when it is ready."""
    hook = tmp_path / "hook.py"
    hook.write_text(
        """import os, subprocess, sys
ready_read, ready_write = os.pipe()
lifetime_read, lifetime_write = os.pipe()
subprocess.Popen([
    sys.executable, '-c',
    'import os, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'os.write(int(sys.argv[1]), b"1"); os.close(int(sys.argv[1])); '
    'os.read(int(sys.argv[2]), 1); print("ready", flush=True); signal.pause()',
    str(ready_write), str(lifetime_read)
], pass_fds=(ready_write, lifetime_read))
os.close(ready_write)
os.close(lifetime_read)
os.read(ready_read, 1)
# Exiting closes lifetime_write, so the child prints only after its parent dies.
"""
    )
    ready = asyncio.Event()
    processes = []
    create_process = asyncio.create_subprocess_exec
    read_tail = runner_module.execution.read_tail
    terminate = runner_module.execution.terminate_process_tree

    async def create(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        processes.append(process)
        return process

    async def read(stream, keep, label):
        first = await stream.read(1)
        if first:
            ready.set()
        return first + await read_tail(stream, keep - len(first), label)

    async def terminate_quickly(process, label):
        await terminate(process, label, grace=0.05)

    monkeypatch.setattr(runner_module.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(runner_module.execution, "read_tail", read)
    monkeypatch.setattr(runner_module.execution, "DRAIN_SECONDS", 0.1)
    monkeypatch.setattr(runner_module.execution, "terminate_process_tree", terminate_quickly)
    running = asyncio.create_task(
        runner_module.execution.run_process(
            [sys.executable, str(hook)],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=10 if cancel else 1,
            merge_stderr=False,
            label="postrun descendant cleanup",
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), 3)
        if cancel:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            stdout, _, exit_code, timed_out = await asyncio.wait_for(running, 3)
            assert timed_out and exit_code == 0 and stdout == "ready\n"
        process = processes[0]
        assert process.returncode == 0 and process.stdout is not None
        # EOF proves the SIGTERM-ignoring child no longer holds the inherited pipe.
        assert await asyncio.wait_for(process.stdout.read(), 1) == b""
    finally:
        for process in processes:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_output_keeps_only_the_final_tail(
    runner: JobRunner,
    enso_home: Paths,
    fake_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(tmp_path, monkeypatch, "x" * runs.OUTPUT_KEEP + "\nlast line")
    result = await runner.run(job(enso_home, fake_config), trigger="manual")
    assert result.status == "ok"
    assert result.output == "x" * (runs.OUTPUT_KEEP - 11) + "\nlast line"
    run = runs.get(enso_home, result.run_id or "")
    assert run is not None and run.output == result.output


async def test_overlapping_trigger_is_skipped(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    nightly = job(enso_home, fake_config, prompt="sleep 0.5")
    held = acquire_lock(nightly.job_dir)  # another process is running the job
    assert held is not None
    skipped = await runner.run(nightly, trigger="schedule")
    assert (skipped.status, skipped.run_id) == ("skipped", None)
    assert runs.list_runs(enso_home) == []
    held.close()

    db.set_last_run(enso_home, "default:nightly", "2026-09-01T08:59:00+00:00")
    task = runner.start(nightly, trigger="schedule")
    await asyncio.sleep(0.05)
    await runner.tick(datetime.fromisoformat("2026-09-01T09:00:30+00:00"))  # due, but running
    assert runner.running() == ["default:nightly"]
    assert (await task).status == "ok"
    assert len(runs.list_runs(enso_home)) == 1


async def test_concurrency_group_serializes_provider_runs_after_prerun(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    nightly = job(
        enso_home,
        fake_config,
        script="touch selected.txt",
        prerun="prerun.sh",
        concurrency_group="dev-EN",
    )
    held = acquire_group_lock(enso_home, "dev-EN")
    assert held is not None

    skipped = await runner.run(nightly, trigger="schedule")
    run = runs.get(enso_home, skipped.run_id or "")
    assert (skipped.status, skipped.output) == ("skipped", "")
    assert run is not None and run.status == "skipped"
    assert (nightly.job_dir / "selected.txt").exists()  # selection happens before the group lock

    held.close()
    completed = await runner.run(nightly, trigger="manual")
    assert completed.status == "ok"


async def test_stop_closes_the_running_row_without_alerting(
    runner: JobRunner, enso_home: Paths, fake_config: Config, transport: FakeTransport
) -> None:
    nightly = job(enso_home, fake_config, prompt="sleep 5")
    task = runner.start(nightly, trigger="schedule")
    await asyncio.sleep(0.3)
    await runner.stop()
    assert task.cancelled() and runner.running() == []
    run = runs.list_runs(enso_home)[0]
    assert run.status == "error" and run.error
    assert run.ended_at is not None and run.duration_ms is not None
    assert transport.sent == []
    held = acquire_lock(nightly.job_dir)  # the lock was released with the task
    assert held is not None
    held.close()


def test_recover_closes_orphaned_rows_only(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    nightly = job(enso_home, fake_config)
    write_job(enso_home, "other")
    other = load_job(enso_home, fake_config, "other")
    orphan = runs.start(enso_home, nightly, "schedule", effort=nightly.effort)
    live = runs.start(enso_home, other, "manual", effort=other.effort)
    gone = runs.start(enso_home, other, "manual", effort=other.effort)
    with db.transaction(enso_home) as con:  # its job directory was deleted since
        con.execute("UPDATE runs SET job = 'gone' WHERE id = ?", (gone,))
    held = acquire_lock(other.job_dir)  # an ``enso job run`` in another process
    assert held is not None

    def status(run_id: str) -> str:
        run = runs.get(enso_home, run_id)
        assert run is not None
        return run.status

    assert runner.recover() == 2
    assert [status(run_id) for run_id in (orphan, gone, live)] == ["error", "error", "running"]
    closed = runs.get(enso_home, orphan)  # nobody saw it end, so no end time or duration
    assert closed is not None and (closed.ended_at, closed.duration_ms) == (None, None)
    held.close()
    assert runner.recover() == 1
    assert runner.recover() == 0


NOW = datetime.fromisoformat("2026-09-01T09:00:30+00:00")
LA = "America/Los_Angeles"


@pytest.mark.parametrize(
    ("local_zone", "last_run", "now", "catch_up", "expect"),
    [
        ("UTC", None, "2026-09-01T09:00:30+00:00", False, "first"),
        ("UTC", "2026-09-01T08:59:00+00:00", "2026-09-01T09:00:30+00:00", False, "fire"),
        ("UTC", "2026-09-01T09:00:10+00:00", "2026-09-01T09:00:30+00:00", False, "wait"),
        ("UTC", "2026-08-31T09:00:30+00:00", "2026-09-01T10:00:30+00:00", False, "misfire"),
        ("UTC", "2026-08-31T09:00:30+00:00", "2026-09-01T10:00:30+00:00", True, "fire"),
        # spring-forward 2026-03-08: 09:00 stays 09:00 PDT, not 09:00 PST (10:00 PDT)
        (LA, "2026-03-07T09:00:30-08:00", "2026-03-08T09:00:30-07:00", False, "fire"),
        (LA, "2026-03-07T09:00:30-08:00", "2026-03-08T10:00:30-07:00", False, "misfire"),
        # fall-back 2026-11-01: 09:00 PDT (08:00 PST) is not the slot; 09:00 PST is
        (LA, "2026-10-31T09:00:30-07:00", "2026-11-01T08:00:30-08:00", False, "wait"),
        (LA, "2026-10-31T09:00:30-07:00", "2026-11-01T09:00:30-08:00", False, "fire"),
    ],
    indirect=["local_zone"],
)
def test_decide(
    enso_home: Paths,
    config: Config,
    local_zone: str,
    last_run: str | None,
    now: str,
    catch_up: bool,
    expect: str,
) -> None:
    nightly = job(enso_home, config, catch_up=catch_up, misfire_grace_seconds=300)
    when = datetime.fromisoformat(last_run) if last_run else None
    assert decide(nightly, when, datetime.fromisoformat(now)) == expect


async def test_tick_fires_due_jobs_only(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    job(enso_home, fake_config)
    write_job(enso_home, "off", enabled=False)
    write_job(enso_home, "broken", model="gpt")
    write_job(enso_home, "hourly", schedule="@hourly")  # invalid: never due, never dispatched
    await runner.tick(NOW)  # first sighting: remembered, not fired
    assert (
        runner.running() == []
        and db.job_state(enso_home, "default:nightly").last_run == NOW.isoformat()
    )
    assert db.job_state(enso_home, "default:off").last_run is None
    assert db.job_state(enso_home, "default:hourly").last_run is None
    db.set_last_run(enso_home, "default:nightly", "2026-09-01T08:59:00+00:00")
    db.set_last_run(enso_home, "default:hourly", "2026-09-01T08:59:00+00:00")
    await runner.tick(NOW)
    assert runner.running() == ["default:nightly"]  # a broken neighbour does not hold up a due job
    await asyncio.gather(*runner._running.values())
    assert runs.latest(enso_home)["default:nightly"].status == "ok"
    assert (
        db.job_state(enso_home, "default:nightly").last_run == NOW.isoformat()
    )  # the dispatch stamp


async def test_a_scheduled_run_keeps_its_dispatch_stamp(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    """Finishing must not re-stamp last_run: the anchor stays the slot that fired the run."""
    job(
        enso_home,
        fake_config,
        schedule="* * * * *",
        misfire_grace_seconds=30,
        prompt="sleep 1",
    )
    db.set_last_run(enso_home, "default:nightly", "2026-09-01T08:59:30+00:00")
    await runner.tick(NOW)  # the 09:00 slot is exactly 30s old: at the grace edge, so it fires
    assert runner.running() == ["default:nightly"]
    await runner.tick(datetime.fromisoformat("2026-09-01T09:01:30+00:00"))  # the 09:01 slot; busy
    await asyncio.gather(*runner._running.values())
    assert (
        db.job_state(enso_home, "default:nightly").last_run == NOW.isoformat()
    )  # not the finish time
    await runner.tick(datetime.fromisoformat("2026-09-01T09:01:40+00:00"))
    assert (
        db.job_state(enso_home, "default:nightly").last_run == "2026-09-01T09:01:40+00:00"
    )  # misfired


async def test_a_tick_reads_config_changes_without_a_restart(
    runner: JobRunner, enso_home: Paths, fake_config: Config
) -> None:
    write_job(enso_home, model="gpt")  # not one of claude's models, so the job is invalid
    db.set_last_run(enso_home, "default:nightly", "2026-09-01T08:59:00+00:00")
    await runner.tick(NOW)
    assert runner.running() == []
    raw = copy.deepcopy(fake_config.raw)
    raw["providers"]["claude"]["models"].append("gpt")
    write_config(enso_home, raw)
    await runner.tick(NOW)
    assert runner.running() == ["default:nightly"]
    assert (await runner._running["default:nightly"]).status == "ok"


async def test_scheduled_alerts_suppress_repeats_and_recover(
    runner: JobRunner, enso_home: Paths, fake_config: Config, transport: FakeTransport
) -> None:
    broken = "echo 'ENSO_ERROR: feed down' >&2; exit 2"
    nightly = job(enso_home, fake_config, script=broken, prerun="prerun.sh")
    await runner.run(nightly, trigger="schedule")
    await runner.run(nightly, trigger="schedule")
    assert transport.sent == [("C1", "⚠️ [default:nightly] prerun failed\nfeed down")]
    assert messages.list_messages(enso_home, 1)[0].workspace == nightly.workspace
    (nightly.job_dir / "prerun.sh").write_text("exit 3")
    await runner.run(nightly, trigger="schedule")
    assert transport.sent[-1] == (
        "C1",
        "⚠️ [default:nightly] prerun failed\nprerun exited with status 3",
    )
    (nightly.job_dir / "prerun.sh").write_text("exit 1")
    await runner.run(nightly, trigger="schedule")
    assert transport.sent[-1] == ("C1", "✅ [default:nightly] prerun recovered")
    assert db.job_state(enso_home, "default:nightly").failure_fingerprint is None
    await runner.run(nightly, trigger="schedule")
    assert len(transport.sent) == 3

    failing = job(enso_home, fake_config, prompt="fail " + "x" * NOTIFY_LIMIT, notify="slack:C9")
    await runner.run(failing, trigger="schedule")
    target, text = transport.sent[-1]
    assert target == "C9" and text.startswith("⚠️ [default:nightly (exit 1)]\n…")
    assert text.endswith("fake: boom") and len(text) == NOTIFY_LIMIT


@pytest.mark.parametrize("args", [(), ("--permission-mode", "dontAsk")])
async def test_jobs_preserve_provider_arguments_without_policy_prerequisites(
    enso_home, fake_config, transport, tmp_path, monkeypatch, args
):
    launch_log = tmp_path / "launches.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LAUNCHES", str(launch_log))
    write_workspace(
        enso_home,
        "default",
        {
            "agent": {"provider": "codex", "model": "sol", "effort": "high"},
            "providers": {"claude": {"args": list(args)}},
        },
    )
    config, problems, _ = parse_config(fake_config.raw, enso_home)
    assert config is not None, problems
    result = await JobRunner(config, {"slack": transport}).run(
        job(enso_home, config, prompt="hello"), trigger="schedule"
    )
    assert result.status == "ok" and result.run_id
    run = runs.get(enso_home, result.run_id)
    assert run is not None and run.status == "ok"
    (launch,) = [json.loads(line) for line in launch_log.read_text().splitlines()]
    argv = launch["args"]
    assert argv[argv.index("--output-format") + 2 : argv.index("--model")] == list(args)
    assert launch["cwd"] == str(enso_home.workspace("default").resolve())
    assert not (enso_home.workspace("default") / ".claude/settings.json").exists()
    assert transport.sent == []


def test_job_locks_refuse_symbolic_links(
    enso_home: Paths, fake_config: Config, tmp_path: Path
) -> None:
    nightly = job(enso_home, fake_config)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.lock").touch()
    (nightly.job_dir / runner_module.LOCK_FILENAME).symlink_to(outside / "run.lock")
    with pytest.raises(LockPathError, match="symbolic link"):
        acquire_lock(nightly.job_dir)
    groups = enso_home.runtime_dir / runner_module.GROUP_LOCK_DIRNAME
    groups.parent.mkdir(parents=True, exist_ok=True)
    groups.symlink_to(outside, target_is_directory=True)
    with pytest.raises(LockPathError, match="symbolic link"):
        acquire_group_lock(enso_home, "shared")
    assert sorted(entry.name for entry in outside.iterdir()) == ["run.lock"]


async def test_same_named_jobs_have_independent_execution_and_history(
    enso_home, fake_config, monkeypatch, tmp_path
):
    """Both schedulers and manual runs use the owner in every persisted/visible identity."""
    from enso.jobs import find_job
    from enso.web import heartbeat as activity

    enso_home.workspace("team").mkdir()
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LAUNCHES", str(calls))
    selected = []
    for workspace in ("default", "team"):
        path = write_job(
            enso_home,
            "digest",
            workspace=workspace,
            prerun="prerun.sh",
            postrun="postrun.sh",
            prompt="{{prerun_output}}",
        )
        (path.parent / "prerun.sh").write_text(
            'printf "%s|%s|%s" "$PWD" "$ENSO_WORKSPACE" "$ENSO_JOB"'
        )
        (path.parent / "postrun.sh").write_text(
            'cat > "result-$ENSO_RUN_ATTEMPT.txt"; printf "%s" "$ENSO_JOB" > owner.txt\n'
            'if [ "$ENSO_RUN_ATTEMPT" = 1 ]; then\n'
            '  printf "Finish %s in %s" "$ENSO_JOB" "$ENSO_WORKSPACE"; exit 10\n'
            "fi\n"
        )
        job, problems = find_job(enso_home, fake_config, f"{workspace}:digest")
        assert job is not None and not problems
        selected.append(job)
        db.set_last_run(enso_home, job.ref, "2026-09-01T08:59:00+00:00")
    runner = JobRunner(fake_config)
    await runner.tick(NOW)
    active = list(runner._running.values())
    assert runner.running() == ["default:digest", "team:digest"]
    results = await asyncio.gather(*active)
    assert all(result.status == "ok" for result in results)
    assert {json.loads(line)["cwd"] for line in calls.read_text().splitlines()} == {
        str(enso_home.workspace("default")),
        str(enso_home.workspace("team")),
    }
    for job in selected:
        assert (job.job_dir / "owner.txt").read_text() == job.ref
        assert (
            f"{job.job_dir}|{job.workspace}|{job.ref}" in (job.job_dir / "result-1.txt").read_text()
        )
        run = runs.list_runs(enso_home, job=job.ref)[0]
        first, second = runs.attempts(enso_home, run.id)
        assert first.session_id == second.session_id
        assert (
            f"workspace={job.workspace} prompt=Finish {job.ref} in {job.workspace}" in second.output
        )
        assert db.job_state(enso_home, job.ref).last_run == NOW.isoformat()
        assert len(runs.list_runs(enso_home, job=job.ref)) == 1
        assert runs.count(enso_home, job=job.ref) == 1
    assert (
        set(runs.latest(enso_home))
        == set(runs.latest_summaries(enso_home))
        == {"default:digest", "team:digest"}
    )
    assert runs.job_names(enso_home) == ["default:digest", "team:digest"]
    assert [row.job for row in activity.activity(enso_home, job="team:digest")] == ["team:digest"]
    db.set_failure(enso_home, "default:digest", "failure", db.now())
    assert db.job_state(enso_home, "team:digest").failure_fingerprint is None
    with db.reader(enso_home) as con:
        assert {tuple(row) for row in con.execute("SELECT workspace, job FROM runs")} == {
            ("default", "digest"),
            ("team", "digest"),
        }
    held = acquire_lock(selected[0].job_dir)
    assert held is not None
    try:
        assert (await runner.run(selected[0], trigger="manual")).status == "skipped"
        assert (await runner.run(selected[1], trigger="manual")).status == "ok"
        # Recovery checks the same qualified lock, including when another workspace has its name.
        orphan = runs.start(enso_home, selected[1], "manual", effort="high")
        busy = runs.start(enso_home, selected[0], "manual", effort="high")
        assert JobRunner(fake_config).recover() == 1
        assert runs.get(enso_home, orphan).status == "error"
        assert runs.get(enso_home, busy).status == "running"
    finally:
        held.close()
    assert JobRunner(fake_config).recover() == 1
