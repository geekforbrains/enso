"""Workspace, job, runs, message, slack, telegram, and logs commands; multi-transport startup."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import importlib.util
import json
import os
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeSlack, FakeTransport, load_job, write_config, write_job
from slack_sdk.errors import SlackApiError
from typer.testing import CliRunner

from enso import db, runs, workspaces
from enso.cli import _serve, app, build_transports
from enso.cli.common import INPUT_LIMIT, InputError, read_input
from enso.config import Config, Paths
from enso.heartbeat.runner import HeartbeatRunner
from enso.jobs.runner import JobRunner, acquire_group_lock
from enso.runtime import Runtime
from enso.transports.slack import SlackTransport
from enso.transports.telegram import TelegramTransport

ORIGIN = {
    "ENSO_ORIGIN_TRANSPORT": "slack",
    "ENSO_ORIGIN_CHANNEL": "C3",
    "ENSO_ORIGIN_THREAD_TS": "7.0",
    "ENSO_ORIGIN_CHANNEL_NAME": "#dev",
}


def test_workspace_create_and_list(enso_home: Paths, raw_config: dict) -> None:
    write_config(enso_home, raw_config)
    runner = CliRunner()
    assert runner.invoke(app, ["workspace", "create", "meteor"]).exit_code == 0
    root = enso_home.workspace("meteor")
    assert (root / "AGENTS.md").read_text().startswith("# meteor\n")
    assert os.readlink(root / "CLAUDE.md") == "AGENTS.md"
    assert sorted(entry.name for entry in root.iterdir()) == [
        ".agents", ".claude", "AGENTS.md", "CLAUDE.md", "drafts", "jobs", "knowledge",
        "memory", "projects", "skills", "uploads",
    ]  # fmt: skip
    assert all((root / name).is_dir() for name in workspaces.WORKSPACE_DIRS)
    for link in (root / ".claude" / "skills", root / ".agents" / "skills"):
        assert os.readlink(link) == "../skills" and link.resolve() == (root / "skills").resolve()
    assert runner.invoke(app, ["workspace", "create", "meteor"]).exit_code == 1
    assert runner.invoke(app, ["workspace", "create", "Bad Name"]).exit_code == 1

    result = runner.invoke(app, ["workspace", "list"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "WORKSPACE  BINDINGS               JOBS  AUDIT",
        "default    slack:C1, slack:dm:U1  -     11 errors",  # the fixture's bare directory
        "meteor     -                      -     2 warnings",  # untouched template, orphan
    ]
    assert result.stderr.startswith("home: ")  # not seeded either


def test_workspace_audit_command(enso_home: Paths, raw_config: dict) -> None:
    write_config(enso_home, raw_config)
    workspaces.seed_home(enso_home)
    root = enso_home.workspace("default")
    (root / "AGENTS.md").write_text("# default\n")
    runner = CliRunner()

    broken = runner.invoke(app, ["workspace", "audit"])
    lines = broken.stdout.splitlines()
    assert broken.exit_code == 1 and lines[0] == f"home {enso_home.home}: ok"
    assert lines[1:3] == ["default: 10 errors", "  bindings: slack:C1, slack:dm:U1"]
    assert lines[3] == "  error: skills/ is missing (repairable with --fix)"
    assert runner.invoke(app, ["workspace", "audit", "meteor"]).exit_code == 1  # no such workspace

    fixed = runner.invoke(app, ["workspace", "audit", "--fix"])
    assert fixed.exit_code == 0 and "  fixed: created " in fixed.stdout
    assert fixed.stdout.splitlines()[1] == "default: ok"
    clean = runner.invoke(app, ["workspace", "audit", "--json"])
    payload = json.loads(clean.stdout)
    assert clean.exit_code == 0 and payload["ok"] and payload["home"]["status"] == "ok"
    assert [(w["name"], w["status"], w["findings"]) for w in payload["workspaces"]] == [
        ("default", "ok", [])
    ]

    runner.invoke(app, ["workspace", "create", "lonely"])
    (root / "uploads" / "x").mkdir()
    (root / "uploads" / "x" / "f").write_bytes(b"x" * 2048)
    warned = runner.invoke(app, ["workspace", "audit", "lonely"])
    assert warned.exit_code == 0 and warned.stdout.splitlines()[1:] == [
        "lonely: 2 warnings",
        "  warning: AGENTS.md is still the untouched template; say what the workspace is for",
        "  warning: nothing is bound to this workspace and no job names it",
    ]
    assert "  uploads: 2.0 KB" in runner.invoke(app, ["workspace", "audit", "default"]).stdout
    listed = json.loads(runner.invoke(app, ["workspace", "audit", "--json"]).stdout)
    assert [w["name"] for w in listed["workspaces"]] == ["default", "lonely"]


def test_build_transports_covers_every_configured_entry(config_both: Config) -> None:
    assert [t.name for t in build_transports(config_both)] == ["slack", "telegram"]


async def test_serve_starts_every_transport(config_both: Config) -> None:
    transports = [FakeTransport("slack"), FakeTransport("telegram")]
    await _serve(
        Runtime(config_both), transports, JobRunner(config_both), HeartbeatRunner(config_both)
    )
    assert all(t.started for t in transports)


async def test_serve_cancellation_stops_transports_and_closes_running_jobs(
    enso_home: Paths, fake_config: Config
) -> None:
    write_job(enso_home, prompt="sleep 5")
    nightly = load_job(enso_home, fake_config)
    runner = JobRunner(fake_config)
    transports = [FakeTransport("slack", block=True), FakeTransport("telegram", block=True)]
    serving = asyncio.create_task(
        _serve(Runtime(fake_config), transports, runner, HeartbeatRunner(fake_config))
    )
    await asyncio.sleep(0.05)
    assert all(t.started for t in transports)
    task = runner.start(nightly, trigger="schedule")
    await asyncio.sleep(0.3)
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving
    assert task.done() and runs.list_runs(enso_home)[0].status == "error"
    assert all(t.cleaned_up for t in transports)


async def test_serve_shares_the_clock_and_stops_both_runners(config, monkeypatch):
    ready = asyncio.Event()
    stopped = set()

    class Background:
        def __init__(self, name):
            self.name = name

        async def tick(self, now):
            pass

        async def stop(self):
            stopped.add(self.name)

    async def clock(callbacks):
        assert set(callbacks) == {"jobs", "heartbeat"}
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("enso.cli.scheduling.minute_loop", clock)
    transports = [FakeTransport("slack", block=True)]
    serving = asyncio.create_task(
        _serve(Runtime(config), transports, Background("jobs"), Background("heartbeat"))
    )
    await asyncio.wait_for(ready.wait(), 2)
    serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving
    assert stopped == {"jobs", "heartbeat"} and transports[0].cleaned_up


def test_a_newer_database_stops_startup_before_any_transport(
    enso_home: Paths, raw_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older Enso against a newer home reports it and stops, rather than migrating it."""
    write_config(enso_home, raw_config)
    con = sqlite3.connect(enso_home.db, isolation_level=None)
    con.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    con.close()
    before = enso_home.db.read_bytes()
    monkeypatch.setattr(
        "enso.cli.build_transports",
        lambda config: pytest.fail("serve reached the transports past a newer database"),
    )

    runner = CliRunner()
    for command in (["serve"], ["job", "list"], ["message", "list"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 1, command
        assert "Traceback" not in result.stderr and result.stdout == ""
        assert result.stderr.splitlines()[-1].endswith(
            f"knows up to {db.SCHEMA_VERSION}: upgrade Enso to the version that wrote it"
        )
    assert enso_home.db.read_bytes() == before  # no schema, no WAL, no run or message row


@pytest.mark.parametrize(
    "command",
    [
        ["job", "list"],
        ["job", "show", "missing"],
        ["job", "run", "missing"],
        [
            "job",
            "create",
            "--name",
            "Example",
            "--provider",
            "claude",
            "--model",
            "opus",
            "--effort",
            "high",
            "--schedule",
            "0 9 * * *",
            "--workspace",
            "default",
        ],
        ["runs", "list"],
        ["runs", "show", "missing"],
        ["message", "send", "example"],
        ["message", "attach", "example.txt"],
        ["message", "list"],
        ["telegram", "send", "example"],
        ["telegram", "attach", "example.txt"],
        ["slack", "send", "-c", "C1", "example"],
        ["slack", "upload", "-c", "C1", "example.txt"],
        ["slack", "edit", "-c", "C1", "--ts", "1.0", "example"],
        ["slack", "delete", "-c", "C1", "--ts", "1.0"],
        ["slack", "react", "-c", "C1", "--ts", "1.0", "eyes"],
        ["slack", "unreact", "-c", "C1", "--ts", "1.0", "eyes"],
        ["slack", "thread", "C1", "1.0"],
        ["slack", "history", "C1"],
        ["slack", "lookup-user", "example"],
        ["slack", "lookup-channel", "example"],
        ["slack", "whois", "U1"],
        ["slack", "open-dm", "U1"],
        ["slack", "refresh"],
        ["table", "list"],
        ["table", "register", "example", "--description", "Example"],
        ["table", "schema", "example"],
    ],
)
def test_json_commands_report_missing_config(enso_home: Paths, command: list[str]) -> None:
    result = CliRunner().invoke(app, [*command, "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "ok": False,
        "error": f"{enso_home.config} is missing; run `enso setup` first",
    }
    assert not enso_home.db.exists()


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "failure", ["config", "newer_database", "corrupt_database", "database_dir"]
)
def test_loading_errors_respect_output_mode(
    enso_home: Paths, raw_config: dict, as_json: bool, failure: str
) -> None:
    write_config(enso_home, raw_config)
    if failure == "config":
        enso_home.config.write_text("{")
        expected = "could not read"
    elif failure == "newer_database":
        with sqlite3.connect(enso_home.db) as con:
            con.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
        expected = "upgrade Enso to the version that wrote it"
    elif failure == "corrupt_database":
        enso_home.db.write_text("not a SQLite database")
        expected = "file is not a database"
    else:
        enso_home.db.mkdir()
        expected = "unable to open database file"
    before = enso_home.db.read_bytes() if enso_home.db.is_file() else None
    result = CliRunner().invoke(app, ["message", "list", *(["--json"] if as_json else [])])
    assert result.exit_code == 1
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False and expected in payload["error"]
        assert result.stderr == ""
    else:
        assert result.stdout == "" and expected in result.stderr
        assert result.stderr.startswith("error: ") and "Traceback" not in result.stderr
    if before is not None:
        assert enso_home.db.read_bytes() == before


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (["job", "show", "missing"], "no job named missing"),
        (["job", "run", "missing"], "no job named missing"),
        (["runs", "show", "missing"], "no run matches missing"),
    ],
)
def test_json_lookup_failures(
    enso_home: Paths, raw_config: dict, command: list[str], error: str
) -> None:
    write_config(enso_home, raw_config)
    result = CliRunner().invoke(app, [*command, "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {"ok": False, "error": error}


@pytest.mark.parametrize("command", ["show", "run"])
def test_json_job_commands_report_malformed_frontmatter(
    enso_home: Paths, raw_config: dict, command: str
) -> None:
    write_config(enso_home, raw_config)
    write_job(enso_home, enabled="yes", timeout=0)
    result = CliRunner().invoke(app, ["job", command, "nightly", "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "ok": False,
        "error": "enabled must be true or false; timeout must be a positive integer",
    }
    assert runs.list_runs(enso_home) == []


def test_job_and_runs_commands(enso_home: Paths, raw_config: dict, fake_claude: str) -> None:
    raw_config["providers"]["claude"]["path"] = fake_claude
    write_config(enso_home, raw_config)
    runner = CliRunner()
    create = [
        "job", "create", "--name", "Nightly Digest", "--provider", "claude", "--model", "opus",
        "--effort", "high", "--schedule", "0 9 * * *", "--workspace", "default",
    ]  # fmt: skip
    made = runner.invoke(app, [*create, "--json"])
    job = json.loads(made.stdout)
    assert made.exit_code == 0 and job["dir_name"] == "nightly-digest" and job["enabled"] is False
    assert job["path"] == str(enso_home.jobs / "nightly-digest" / "JOB.md")
    assert runner.invoke(app, create).exit_code == 1  # exists (text mode)
    duplicate = runner.invoke(app, [*create, "--json"])
    assert duplicate.exit_code == 1 and duplicate.stderr == ""
    assert json.loads(duplicate.stdout) == {
        "ok": False,
        "error": f"job nightly-digest already exists at {enso_home.jobs / 'nightly-digest'}",
    }
    bad = runner.invoke(app, [*create[:7], "gpt", *create[8:], "--json"])
    assert bad.exit_code == 1 and bad.stderr == ""
    assert json.loads(bad.stdout)["ok"] is False and "gpt" in json.loads(bad.stdout)["error"]

    listed = json.loads(runner.invoke(app, ["job", "list", "--json"]).stdout)
    assert [(j["dir_name"], j["enabled"], j["last_run"]) for j in listed] == [
        ("nightly-digest", False, None)
    ]
    assert "nightly-digest  0 9 * * *" in runner.invoke(app, ["job", "list"]).stdout

    ran = runner.invoke(app, ["job", "run", "nightly-digest", "--json"])
    payload = json.loads(ran.stdout)
    assert ran.exit_code == 0 and payload["ok"] and payload["status"] == "ok"
    assert "job=nightly-digest" in payload["output"]
    assert runner.invoke(app, ["job", "run", "missing"]).exit_code == 1

    shown = json.loads(runner.invoke(app, ["job", "show", "nightly-digest", "--json"]).stdout)
    assert shown["last_run"]["status"] == "ok" and shown["next_run"] and shown["problems"] == []

    history = json.loads(runner.invoke(app, ["runs", "list", "--json"]).stdout)
    assert [run["id"] for run in history] == [payload["run_id"]]
    one = runner.invoke(app, ["runs", "show", payload["run_id"][:6]])
    assert one.exit_code == 0 and "status: ok" in one.stdout and "job=nightly-digest" in one.stdout
    assert runner.invoke(app, ["runs", "show", "zzz"]).exit_code == 1


def test_job_create_refuses_an_invalid_schedule(enso_home: Paths, raw_config: dict) -> None:
    """The schedule is validated before anything is written, so a refusal leaves no directory."""
    write_config(enso_home, raw_config)
    runner = CliRunner()
    for schedule in ("@daily", "* * * * * *", "0 9 * *"):
        made = runner.invoke(app, [
            "job", "create", "--name", "Odd Hours", "--provider", "claude", "--model", "opus",
            "--effort", "high", "--schedule", schedule, "--workspace", "default",
        ])  # fmt: skip
        assert made.exit_code == 1 and made.stdout == ""
        assert "must be exactly five fields" in made.stderr
        assert not (enso_home.jobs / "odd-hours").exists()


def test_job_show_and_list_carry_a_schedule_problem(enso_home: Paths, raw_config: dict) -> None:
    write_config(enso_home, raw_config)
    write_job(enso_home, "hourly", schedule="@hourly")
    db.migrate(enso_home)
    runner = CliRunner()

    shown = runner.invoke(app, ["job", "show", "hourly", "--json"])
    payload = json.loads(shown.stdout)
    assert shown.exit_code == 0 and payload["next_run"] is None
    assert payload["problems"] == [
        "schedule '@hourly' must be exactly five fields, "
        "minute hour day-of-month month day-of-week; Enso schedules at minute resolution, "
        "so a seconds or year field and aliases such as @daily are not accepted"
    ]
    listed = json.loads(runner.invoke(app, ["job", "list", "--json"]).stdout)
    assert [entry["problems"] for entry in listed] == [payload["problems"]]
    assert runner.invoke(app, ["job", "run", "hourly"]).exit_code == 1
    refused = runner.invoke(app, ["job", "run", "hourly", "--json"])
    assert refused.exit_code == 1 and refused.stderr == ""
    assert json.loads(refused.stdout) == {"ok": False, "error": "; ".join(payload["problems"])}
    assert runs.list_runs(enso_home) == []


@pytest.mark.parametrize(
    ("status", "exit_code", "prerun", "prompt"),
    [
        ("ok", 1, "exit 0", "Say hi."),
        ("error", 1, "exit 0", "fail provider failure"),
        ("timeout", 1, "exit 0", "sleep 10"),
        ("no_work", 1, "exit 1", "Say hi."),
        ("prerun_error", 1, "exit 2", "Say hi."),
        ("skipped", 1, "exit 0", "Say hi."),
    ],
)
def test_job_run_reports_failing_postrun_for_every_outcome(
    enso_home: Paths,
    raw_config: dict,
    fake_claude: str,
    status: str,
    exit_code: int,
    prerun: str,
    prompt: str,
) -> None:
    raw_config["providers"]["claude"]["path"] = fake_claude
    write_config(enso_home, raw_config)
    path = write_job(
        enso_home,
        enabled=False,
        prerun="prerun.sh",
        postrun="postrun.sh",
        concurrency_group="cli-test",
        timeout=1 if status == "timeout" else 30,
        prompt=prompt,
    )
    path.with_name("prerun.sh").write_text(prerun)
    path.with_name("postrun.sh").write_text('echo "ENSO_ERROR: example hook failure" >&2\nexit 2')
    runner = CliRunner()
    with contextlib.ExitStack() as stack:
        if status == "skipped":
            lock = acquire_group_lock(enso_home, "cli-test")
            assert lock is not None
            stack.enter_context(lock)
        ran = runner.invoke(app, ["job", "run", "nightly"])
        assert ran.exit_code == exit_code
        assert ran.stderr.count("postrun failed: example hook failure\n") == 1
        if status in ("ok", "no_work", "skipped"):
            assert ran.stderr == "postrun failed: example hook failure\n"
        serialized = runner.invoke(app, ["job", "run", "nightly", "--json"])
        assert serialized.exit_code == exit_code and serialized.stderr == ""
        payload = json.loads(serialized.stdout)
        final_status = "error" if status in ("ok", "no_work", "skipped") else status
        assert (payload["status"], payload["postrun_error"]) == (
            final_status,
            "example hook failure",
        )
        assert payload["ok"] is (exit_code == 0) and payload["run_id"]
    history = runs.list_runs(enso_home)
    assert len(history) == 2 and all(run.status == final_status for run in history)
    assert all(run.error == (payload["error"] or None) for run in history)
    assert all(run.postrun_error == "example hook failure" for run in history)


def test_job_run_reports_a_failing_postrun(
    enso_home: Paths, raw_config: dict, fake_claude: str
) -> None:
    raw_config["providers"]["claude"]["path"] = fake_claude
    write_config(enso_home, raw_config)
    write_job(enso_home, postrun="postrun.sh")
    (enso_home.jobs / "nightly" / "postrun.sh").write_text("exit 2")
    runner = CliRunner()
    ran = runner.invoke(app, ["job", "run", "nightly"])
    assert ran.exit_code == 1 and ran.stderr == "postrun failed: postrun exited with status 2\n"
    payload = json.loads(runner.invoke(app, ["job", "run", "nightly", "--json"]).stdout)
    assert (payload["status"], payload["postrun_error"]) == (
        "error",
        "postrun exited with status 2",
    )


def test_runs_show_includes_attempts_without_loading_them_in_lists(
    enso_home: Paths, config: Config, raw_config: dict
) -> None:
    write_config(enso_home, raw_config)
    db.migrate(enso_home)
    write_job(enso_home)
    run_id = runs.start(enso_home, load_job(enso_home, config), "manual", effort="high")
    runs.record_attempt(
        enso_home,
        run_id,
        number=1,
        status="ok",
        exit_code=0,
        output="Initial answer",
        error="",
        session_id="same-session",
        postrun_exit_code=10,
        postrun_output="Commit these changes.",
        duration_ms=123,
    )
    runs.record_attempt(
        enso_home,
        run_id,
        number=2,
        status="ok",
        exit_code=0,
        output="Committed work",
        error="",
        session_id="same-session",
        postrun_exit_code=2,
        postrun_error="Cleanup failed",
        duration_ms=456,
    )
    runs.finish(
        enso_home,
        run_id,
        status="error",
        output="Committed work",
        error="Cleanup failed",
        postrun_error="Cleanup failed",
        session_id="same-session",
    )
    cli = CliRunner()
    shown = cli.invoke(app, ["runs", "show", run_id[:6], "--json"])
    assert shown.exit_code == 0 and shown.stderr == ""
    payload = json.loads(shown.stdout)
    assert payload["session_id"] == "same-session" and payload["postrun_error"] == "Cleanup failed"
    assert [attempt["number"] for attempt in payload["attempts"]] == [1, 2]
    assert payload["attempts"][0]["postrun_output"] == "Commit these changes."
    text = cli.invoke(app, ["runs", "show", run_id])
    assert text.exit_code == 0 and "Attempt 1: ok" in text.stdout and "Attempt 2: ok" in text.stdout
    assert "Commit these changes." in text.stdout and "Initial answer" in text.stdout
    listed = cli.invoke(app, ["runs", "list", "--json"])
    assert "attempts" not in json.loads(listed.stdout)[0]
    assert "Initial answer" not in listed.stdout


# -- transport tools, outbox, logs ---------------------------------------------


def test_message_help_describes_destination_priority() -> None:
    result = CliRunner().invoke(app, ["message", "--help"])
    assert result.exit_code == 0
    assert (
        "Send to --to, else the conversation that asked, else the transport's notify target."
        in " ".join(result.stdout.split())
    )


@pytest.mark.parametrize("transport", ["slack", "telegram"])
@pytest.mark.parametrize("as_json", [False, True])
def test_message_origin_with_unconfigured_transport(
    enso_home: Paths,
    raw_config_both: dict,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    as_json: bool,
) -> None:
    raw_config_both["transports"].pop(transport)
    raw_config_both["bindings"] = {}
    write_config(enso_home, raw_config_both)
    monkeypatch.setenv("ENSO_ORIGIN_TRANSPORT", transport)
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C1" if transport == "slack" else "123")
    commands = [["message", "send", "example"]]
    if transport == "slack":
        commands.append(["slack", "send", "-c", "C1", "example"])
    for command in commands:
        result = CliRunner().invoke(app, [*command, *(["--json"] if as_json else [])])
        assert result.exit_code == 1
        error = f"transports.{transport} is not configured"
        if as_json:
            assert json.loads(result.stdout) == {"ok": False, "error": error}
            assert result.stderr == ""
        else:
            assert result.stdout == "" and result.stderr == f"error: {error}\n"


@pytest.mark.parametrize("transport", ["slack", "telegram"])
@pytest.mark.parametrize("generic", [False, True])
def test_json_transport_import_failure(
    enso_home: Paths,
    raw_config_both: dict,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    generic: bool,
) -> None:
    write_config(enso_home, raw_config_both)
    importing = builtins.__import__
    error = f"Missing transport dependencies; install enso[{transport}]"

    def missing_transport(name, globals=None, locals=None, fromlist=(), level=0):
        package = globals["__package__"] if level else ""
        if (
            importlib.util.resolve_name("." * level + name, package)
            == f"enso.transports.{transport}"
        ):
            raise ImportError(error)
        return importing(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", missing_transport)
    if generic:
        target = "slack:C1" if transport == "slack" else "telegram:123"
        command = ["message", "send", "example", "--to", target]
    elif transport == "slack":
        command = ["slack", "send", "-c", "C1", "example"]
    else:
        command = ["telegram", "send", "example"]
    result = CliRunner().invoke(app, [*command, "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {"ok": False, "error": error}


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch, enso_home: Paths, raw_config: dict) -> FakeSlack:
    """Config on disk plus one fake Slack client behind every SlackTransport."""
    write_config(enso_home, raw_config)
    fake = FakeSlack()
    monkeypatch.setattr(SlackTransport, "client", property(lambda self: fake))
    return fake


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.documents: list[tuple[int | str, str, str | None]] = []

    async def send_message(self, chat_id: int | str, text: str, **kwargs: Any) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text})
        return SimpleNamespace(message_id=len(self.sent))

    async def send_document(self, chat_id: int | str, handle: Any, caption: str | None) -> Any:
        self.documents.append((chat_id, os.path.basename(handle.name), caption))
        return SimpleNamespace(message_id=len(self.documents))


def test_read_input_bounds_files_and_requires_utf8(tmp_path: Path) -> None:
    path = tmp_path / "in.txt"
    path.write_bytes(b"a" * 8)
    assert read_input(path, limit=8) == "aaaaaaaa"
    path.write_bytes(b"a" * 9)
    with pytest.raises(InputError, match=r"^input exceeds 8 bytes$"):
        read_input(path, limit=8)
    path.write_bytes(b"\xff")
    with pytest.raises(InputError, match="is not UTF-8"):
        read_input(path, limit=8)
    with pytest.raises(InputError, match=r"^could not read "):
        read_input(tmp_path / "missing", limit=8)
    assert read_input("-", literal=True) == "-"
    assert read_input("é" * 4, literal=True, limit=8) == "é" * 4
    with pytest.raises(InputError, match=r"^input exceeds 8 bytes$"):
        read_input("é" * 4 + "x", literal=True, limit=8)
    with pytest.raises(InputError, match=r"^input is not UTF-8$"):
        read_input("\udcff", literal=True)


def test_message_input_over_the_limit_is_refused_before_sending(
    slack: FakeSlack, tmp_path: Path
) -> None:
    runner = CliRunner()
    body_file = tmp_path / "body.md"
    body_file.write_bytes(b"x" * (INPUT_LIMIT + 1))
    result = runner.invoke(app, ["slack", "send", "-c", "C1", "--file", str(body_file), "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {"ok": False, "error": f"input exceeds {INPUT_LIMIT} bytes"}
    result = runner.invoke(app, ["message", "send", "-"], input="x" * (INPUT_LIMIT + 1))
    assert result.exit_code == 1 and result.stderr == f"error: input exceeds {INPUT_LIMIT} bytes\n"
    result = runner.invoke(app, ["message", "send", "é" * (INPUT_LIMIT // 2) + "x", "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {"ok": False, "error": f"input exceeds {INPUT_LIMIT} bytes"}
    assert slack.sent("chat_postMessage") == []


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("invalid_utf8", [False, True])
def test_rich_input_is_bounded_and_requires_utf8(
    slack: FakeSlack, tmp_path: Path, as_json: bool, invalid_utf8: bool
) -> None:
    envelope = tmp_path / "rich.json"
    content = b'{"version":1,"fallback_text":"A: 1","blocks":[{"type":"table","rows":[["A",1]]}]}'
    envelope.write_bytes(b"\xff" if invalid_utf8 else content.ljust(INPUT_LIMIT + 1, b" "))
    error = f"{envelope} is not UTF-8" if invalid_utf8 else f"input exceeds {INPUT_LIMIT} bytes"
    args = ["slack", "send", "-c", "C1", "--rich", str(envelope)]
    result = CliRunner().invoke(app, [*args, *(["--json"] if as_json else [])])
    assert result.exit_code == 1
    if as_json:
        assert result.stderr == ""
        assert json.loads(result.stdout) == {"ok": False, "error": error}
    else:
        assert result.stdout == "" and result.stderr == f"error: {error}\n"
    assert slack.sent("chat_postMessage") == []


def test_slack_writes_follow_the_json_contract_and_fill_the_outbox(
    slack: FakeSlack, tmp_path: Path
) -> None:
    runner = CliRunner()
    sent = json.loads(runner.invoke(app, ["slack", "send", "-c", "C1", "hello", "--json"]).stdout)
    assert sent == {
        "ok": True,
        "transport": "slack",
        "channel": "C1",
        "ts": "100.1",
        "thread_ts": None,
        "permalink": "https://x.slack.com/archives/C1/p1001",
    }
    # Chained the way a job would: the reply goes into the first message's thread.
    reply = runner.invoke(
        app, ["slack", "send", "-c", "C1", "-t", sent["ts"], "-", "--json"], input="from stdin\n"
    )
    assert json.loads(reply.stdout)["thread_ts"] == "100.1"
    assert slack.sent("chat_postMessage")[-1] == {
        "channel": "C1",
        "text": "from stdin\n",
        "thread_ts": "100.1",
    }
    body_file = tmp_path / "body.md"
    body_file.write_text("# Report")
    plain = runner.invoke(app, ["slack", "send", "-c", "C1", "--file", str(body_file)])
    assert plain.exit_code == 0 and plain.stdout.startswith("transport=slack channel=C1 ts=100.1 ")
    assert (
        runner.invoke(app, ["slack", "send", "-c", "C1", "x", "--file", str(body_file)]).exit_code
        == 1
    )
    assert runner.invoke(app, ["slack", "send", "-c", "C1"]).exit_code == 1

    envelope = tmp_path / "rich.json"
    envelope.write_text(
        '{"version":1,"fallback_text":"A: 1","blocks":[{"type":"table","rows":[["A",1]]}]}'.ljust(
            INPUT_LIMIT
        )
    )
    rich = runner.invoke(app, ["slack", "send", "-c", "C1", "--rich", str(envelope), "--json"])
    assert json.loads(rich.stdout)["ok"]
    assert slack.sent("chat_postMessage")[-1]["blocks"][0]["type"] == "table"
    envelope.write_text('{"version": 1}')
    bad = runner.invoke(app, ["slack", "send", "-c", "C1", "--rich", str(envelope), "--json"])
    assert bad.exit_code == 1 and "fallback_text" in json.loads(bad.stdout)["error"]

    chart = tmp_path / "chart.png"
    chart.write_bytes(b"png")
    upload = ["slack", "upload", "-c", "C1", "-t", "100.1", str(chart), "--caption", "Chart"]
    uploaded = json.loads(runner.invoke(app, [*upload, "--json"]).stdout)
    assert uploaded == {
        "ok": True,
        "transport": "slack",
        "channel": "C1",
        "ts": None,
        "thread_ts": "100.1",
        "file": "F1",
        "permalink": None,
    }
    assert slack.sent("files_upload_v2") == [
        {"channel": "C1", "file": str(chart), "initial_comment": "Chart", "thread_ts": "100.1"}
    ]
    assert (
        runner.invoke(app, ["slack", "upload", "-c", "C1", str(tmp_path / "no.png")]).exit_code == 1
    )

    edited = runner.invoke(app, ["slack", "edit", "-c", "C1", "--ts", "100.1", "fixed", "--json"])
    assert json.loads(edited.stdout)["ts"] == "100.1"
    assert slack.sent("chat_update") == [{"channel": "C1", "ts": "100.1", "text": "fixed"}]
    assert json.loads(
        runner.invoke(app, ["slack", "delete", "-c", "C1", "--ts", "100.1", "--json"]).stdout
    )["ok"]
    assert slack.sent("chat_delete") == [{"channel": "C1", "ts": "100.1"}]
    reacted = runner.invoke(
        app, ["slack", "react", "-c", "C1", "--ts", "100.1", ":eyes:", "--json"]
    )
    assert json.loads(reacted.stdout)["reaction"] == "eyes"
    assert slack.sent("reactions_add") == [{"channel": "C1", "timestamp": "100.1", "name": "eyes"}]
    unreacted = runner.invoke(
        app, ["slack", "unreact", "-c", "C1", "--ts", "100.1", ":eyes:", "--json"]
    )
    assert json.loads(unreacted.stdout)["reaction"] == "eyes"
    assert slack.sent("reactions_remove") == [
        {"channel": "C1", "timestamp": "100.1", "name": "eyes"}
    ]

    # Slack's own error code is the error; the failed send is still recorded.
    slack.responses["chat_postMessage"] = SlackApiError(
        "no", {"ok": False, "error": "channel_not_found"}
    )
    failed = runner.invoke(app, ["slack", "send", "-c", "C9", "x", "--json"])
    assert failed.exit_code == 1
    assert json.loads(failed.stdout) == {"ok": False, "error": "channel_not_found"}
    rows = json.loads(runner.invoke(app, ["message", "list", "--json"]).stdout)
    assert [(r["status"], r["target"], r["thread"], r["text"]) for r in rows] == [
        ("failed", "C9", None, "x"),
        ("sent", "C1", "100.1", "[file chart.png] Chart"),
        ("sent", "C1", None, "A: 1"),
        ("sent", "C1", None, "# Report"),
        ("sent", "C1", "100.1", "from stdin\n"),
        ("sent", "C1", None, "hello"),
    ]
    assert all(r["source"] == "cli" and r["consumed_at"] is None for r in rows)
    listed = runner.invoke(app, ["message", "list", "-n", "1"]).stdout.splitlines()
    assert listed[0].startswith("ID  CREATED") and listed[1].split()[2:5] == [
        "failed",
        "slack:C9",
        "cli",
    ]


def test_slack_reads_render_names_and_trim_threads(slack: FakeSlack) -> None:
    thread = [
        {"ts": "1.0", "user": "U1", "text": "hi <@U2> &amp; all", "reply_count": 2},
        {"ts": "2.0", "user": "U2", "subtype": "channel_join", "text": "joined"},
        {"ts": "3.0", "user": "U2", "text": "yo"},
        {"ts": "4.0", "user": "U2", "text": "latest"},
    ]
    slack.responses["conversations_replies"] = {"messages": thread}
    slack.responses["users_info"] = lambda kw: {
        "user": {"id": kw["user"], "real_name": "Gavin" if kw["user"] == "U1" else "Ann"}
    }
    runner = CliRunner()
    shown = runner.invoke(app, ["slack", "thread", "C1", "1.0", "-n", "2"])
    lines = shown.stdout.splitlines()
    assert lines[0].endswith("  Gavin (U1)  ts=1.0  [2 replies]") and lines[1] == "  hi @Ann & all"
    assert "latest" in shown.stdout and "yo" not in shown.stdout and "joined" not in shown.stdout
    assert "1 earlier reply" in shown.stderr or "1 earlier replies" in shown.stderr
    assert len(slack.sent("users_info")) == 2

    slack.responses["conversations_history"] = {
        "messages": [
            {"ts": "9.0", "user": "U1", "text": "newest"},
            {"ts": "8.0", "user": "U1", "text": "older"},
        ]
    }
    history = runner.invoke(app, ["slack", "history", "C1", "--since", "24h", "-n", "2", "--json"])
    assert [(v["ts"], v["name"], v["text"]) for v in json.loads(history.stdout)] == [
        ("8.0", "Gavin", "older"),
        ("9.0", "Gavin", "newest"),
    ]
    assert len(slack.sent("users_info")) == 2  # names came from the cache this time
    call = slack.sent("conversations_history")[0]
    assert call["limit"] == 2 and float(call["oldest"]) > 0
    assert runner.invoke(app, ["slack", "history", "C1", "--since", "soon"]).exit_code == 1


def test_slack_directory_commands(slack: FakeSlack) -> None:
    slack.responses["users_list"] = {
        "members": [
            {"id": "U1", "name": "gavin", "real_name": "Gavin V", "profile": {"email": "g@x.io"}}
        ]
    }
    slack.responses["conversations_list"] = {
        "channels": [{"id": "C1", "name": "general", "is_member": True}]
    }
    slack.responses["conversations_open"] = {"channel": {"id": "D1"}}
    runner = CliRunner()
    refreshed = json.loads(runner.invoke(app, ["slack", "refresh", "--json"]).stdout)
    assert refreshed == {"ok": True, "users": 1, "channels": 1}
    assert (
        runner.invoke(app, ["slack", "lookup-user", "gav"]).stdout
        == "U1  Gavin V  (@gavin)  g@x.io\n"
    )
    assert runner.invoke(app, ["slack", "lookup-channel", "#GEN"]).stdout == "C1  #general\n"
    assert runner.invoke(app, ["slack", "lookup-user", "nobody"]).exit_code == 1
    assert (
        json.loads(runner.invoke(app, ["slack", "whois", "U1", "--json"]).stdout)[0]["email"]
        == "g@x.io"
    )
    assert runner.invoke(app, ["slack", "open-dm", "gavin"]).stdout == "user=U1 channel=D1\n"
    assert slack.sent("conversations_open") == [{"users": "U1"}]
    assert runner.invoke(app, ["slack", "open-dm", "nobody", "--json"]).exit_code == 1


@pytest.mark.parametrize(
    ("args", "env", "expected", "source"),
    [
        (["--to", "slack:C2"], {}, ("C2", None), "cli"),
        (["--to", "C2"], {}, ("C2", None), "cli"),  # bare id: one transport is configured
        ([], ORIGIN, ("C3", "7.0"), "turn:slack:C3:7.0"),  # back to the asking thread
        (["--to", "C2"], ORIGIN, ("C2", None), "turn:slack:C3:7.0"),
        ([], {"ENSO_JOB": "nightly"}, ("C1", None), "job:nightly"),  # notify
    ],
)
def test_message_send_destination(
    slack: FakeSlack,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    env: dict[str, str],
    expected: tuple[str, str | None],
    source: str,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    runner = CliRunner()
    payload = json.loads(runner.invoke(app, ["message", "send", "note", *args, "--json"]).stdout)
    assert (payload["channel"], payload["thread_ts"]) == expected
    row = json.loads(runner.invoke(app, ["message", "list", "--json"]).stdout)[0]
    assert (row["target"], row["thread"], row["source"]) == (*expected, source)


def test_message_send_without_destination_fails(
    enso_home: Paths, raw_config: dict, slack: FakeSlack
) -> None:
    raw_config["transports"]["slack"].pop("notify")
    write_config(enso_home, raw_config)
    result = CliRunner().invoke(app, ["message", "send", "note", "--json"])
    assert result.exit_code == 1 and json.loads(result.stdout)["error"].startswith("no destination")
    assert slack.calls == []


@pytest.mark.parametrize("as_json", [False, True])
def test_telegram_send_without_the_transport_configured_fails_cleanly(
    enso_home: Paths, raw_config: dict, as_json: bool
) -> None:
    """A Slack-only home used to raise a bare KeyError out of destination()."""
    write_config(enso_home, raw_config)
    args = ["telegram", "send", "hi"] + (["--json"] if as_json else [])
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1 and not isinstance(result.exception, KeyError)
    if as_json:
        assert json.loads(result.stdout) == {
            "ok": False,
            "error": "transport telegram is not configured",
        }
    else:
        assert result.stderr == "error: transport telegram is not configured\n"


def test_message_attach_and_telegram_send(
    enso_home: Paths, raw_config_both: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_config(enso_home, raw_config_both)
    bot = FakeBot()

    @contextlib.asynccontextmanager
    async def connect(self: TelegramTransport) -> AsyncIterator[None]:
        self._bot = bot  # type: ignore[assignment]
        yield
        self._bot = None

    monkeypatch.setattr(TelegramTransport, "connect", connect)
    runner = CliRunner()
    sent = json.loads(runner.invoke(app, ["telegram", "send", "hi", "--json"]).stdout)
    assert sent == {"ok": True, "transport": "telegram", "chat_id": "123", "message_id": "1"}
    assert bot.sent == [{"chat_id": 123, "text": "hi"}]
    attachment = tmp_path / "a.txt"
    attachment.write_text("x")
    attach = ["message", "attach", str(attachment), "see", "--to", "telegram:456", "--json"]
    assert json.loads(runner.invoke(app, attach).stdout) == {
        "ok": True,
        "transport": "telegram",
        "chat_id": "456",
        "message_id": "1",
    }
    assert bot.documents == [(456, "a.txt", "see")]
    assert runner.invoke(app, ["telegram", "send", "hi", "--to", "slack:C1"]).exit_code == 1
    rows = json.loads(runner.invoke(app, ["message", "list", "--json"]).stdout)
    assert [(r["transport"], r["target"], r["text"]) for r in rows] == [
        ("telegram", "456", "[file a.txt] see"),
        ("telegram", "123", "hi"),
    ]


def test_logs_filters_across_rotated_files(enso_home: Paths) -> None:
    enso_home.log.with_name("enso.log.1").write_text(
        "10:00:00 INFO  slack     [t:abc] older\n10:00:01 INFO  runner    [j:nightly] job line\n"
    )
    enso_home.log.write_text(
        "10:00:02 INFO  runtime   [t:abc] spawn\n10:00:03 INFO  runtime   [t:def] other\n"
        "10:00:04 INFO  runtime   [t:abc] done\n"
    )
    runner = CliRunner()
    assert [
        line[-5:]
        for line in runner.invoke(app, ["logs", "--turn", "abc", "-n", "2"]).stdout.splitlines()
    ] == ["spawn", " done"]
    assert [
        line[-5:] for line in runner.invoke(app, ["logs", "--turn", "abc"]).stdout.splitlines()
    ] == ["older", "spawn", " done"]
    assert (
        runner.invoke(app, ["logs", "--job", "nightly", "--grep", "job"])
        .stdout.strip()
        .endswith("job line")
    )
    assert runner.invoke(app, ["logs", "--grep", "nothing"]).stdout == ""
    enso_home.log.unlink()
    assert runner.invoke(app, ["logs"]).exit_code == 1


def test_config_set_and_unset_text_output(enso_home: Paths, raw_config: dict) -> None:
    write_config(enso_home, raw_config)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "set", "logging.level", "DEBUG"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "configuration applied",
        "restart the service to apply the transports or logging change",
    ]
    # The viewer is its own process, so a web change names it, not the service.
    result = runner.invoke(app, ["config", "set", "web.port", "9000"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "configuration applied",
        "restart the viewer (enso web stop, then enso web start) to apply the web change",
    ]
    result = runner.invoke(app, ["config", "unset", "logging.level"])
    assert result.exit_code == 0 and result.stdout.splitlines()[0] == "configuration applied"
    result = runner.invoke(app, ["config", "unset", "logging.level"])
    assert result.exit_code == 1 and result.stdout == ""
    assert result.stderr == "error: logging.level is not set\n"
    result = runner.invoke(app, ["config", "set", "agent.timeout", "--", "-1"])
    assert result.exit_code == 1 and result.stderr.startswith("error: agent.timeout")
    assert json.loads(enso_home.config.read_text())["agent"] == {"timeout": 30}
    # A change to both reports both processes; web.port 9000 is dropped by this document.
    raw_config["transports"]["slack"]["bot_token"] = "xoxb-rotated"
    result = runner.invoke(app, ["config", "apply", "--file", "-"], input=json.dumps(raw_config))
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "configuration applied",
        "restart the service to apply the transports or logging change",
        "restart the viewer (enso web stop, then enso web start) to apply the web change",
    ]
