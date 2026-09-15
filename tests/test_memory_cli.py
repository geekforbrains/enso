"""Memory recall and refinement commands against an isolated home and real SQLite state."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from conftest import write_config
from typer.testing import CliRunner

from enso import db, memory
from enso.cli import app
from enso.config import Paths


def source(
    paths: Paths,
    *,
    workspace="personal",
    received="2026-09-14T10:00:00-07:00",
    request="Pause deployment until Friday.",
    response="Deployment paused.",
    status="ok",
    error="",
    thread=None,
) -> int:
    ident = memory.start_turn(
        paths,
        conversation=f"slack:{workspace}",
        workspace=workspace,
        provider="claude",
        model="opus",
        effort="high",
        transport="slack",
        channel="C1",
        channel_name="#plans",
        thread=thread,
        message_id="",
        user_id="U1",
        user_name="Gavin",
        request=request,
        files=(),
        received_at=received,
    )
    assert ident is not None
    memory.finish_turn(
        paths, ident, response=response, status=status, error=error, session_id="session-one"
    )
    return ident


def record(paths: Paths, ident: int, summary: str) -> memory.Entry:
    batch = f"batch-{ident}"
    memory.prepare_batch(paths, batch_id=batch)
    return memory.record_batch(paths, batch, [{"summary": summary, "source_ids": [ident]}])[0]


def invoke(*args: str, input: str | None = None):
    return CliRunner().invoke(app, ["memory", *args], input=input)


def test_recall_workspace_dates_pagination_and_sources(enso_home, raw_config, monkeypatch):
    raw_config["memory"] = {"timezone": "America/Vancouver"}
    write_config(enso_home, raw_config)
    db.migrate(enso_home)
    old = record(enso_home, source(enso_home, received="2026-09-13T23:59:00-07:00"), "Old decision")
    recent = record(enso_home, source(enso_home), "Agent reported deployment paused until Friday.")
    record(enso_home, source(enso_home, workspace="work"), "Work deployment reported complete.")
    monkeypatch.setenv("ENSO_WORKSPACE", "personal")

    listed = invoke("list", "--since", "2026-09-14", "--until", "2026-09-15", "--json")
    assert listed.exit_code == 0, listed.output
    data = json.loads(listed.output)
    assert data["total"] == 1 and data["entries"][0]["ref"] == recent.ref
    assert data["limit"] == 20 and data["offset"] == 0
    limited = invoke("list", "--limit", "1", "--offset", "1", "--json")
    assert json.loads(limited.output)["entries"][0]["ref"] == old.ref
    broad = invoke("list", "--all-workspaces", "--json")
    assert json.loads(broad.output)["total"] == 3
    explicit = invoke("list", "--workspace", "work", "--json")
    assert json.loads(explicit.output)["total"] == 1
    searched = invoke("search", "Friday", "--transport", "slack", "--channel", "C1", "--json")
    assert json.loads(searched.output)["entries"][0]["ref"] == recent.ref
    shown = invoke("show", recent.ref, "--sources", "--json")
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.output)
    assert data["sources"][0]["request"] == "Pause deployment until Friday."
    assert data["sources"][0]["session_id"] == "session-one"
    assert datetime.fromisoformat(data["occurred_at"]).tzinfo == UTC
    assert "2026-09-14 10:00 PDT" in invoke("show", recent.ref).output
    assert "User: Pause deployment" in invoke("show", recent.ref, "--sources").output
    assert recent.ref in invoke("list", "--limit", "1").output


def test_refinement_cli_settles_empty_batches_and_reports_failures(enso_home, raw_config):
    write_config(enso_home, raw_config)
    # Preparation owns migrations; an unused home cleanly reports no work.
    empty = invoke("prepare", "--batch", "empty", "--json")
    assert empty.exit_code == 1 and json.loads(empty.output)["batch"] is None
    ident = source(enso_home)
    prepared = invoke("prepare", "--batch", "run-1", "--json")
    assert prepared.exit_code == 0, prepared.output
    assert json.loads(prepared.output)["turns"][0]["id"] == ident
    pending = invoke("check", "--batch", "run-1")
    assert pending.exit_code == 10 and "still pending" in pending.output
    invalid = invoke("record", "--batch", "run-1", "--file", "-", "--json", input="{}")
    assert invalid.exit_code == 1 and not json.loads(invalid.output)["ok"]
    settled = invoke("record", "--batch", "run-1", "--file", "-", "--json", input="[]")
    assert settled.exit_code == 0 and json.loads(settled.output)["entries"] == []
    assert invoke("check", "--batch", "run-1").exit_code == 0
    assert invoke("prepare", "--batch", "run-2", "--json").exit_code == 1
    assert json.loads(invoke("status", "--json").output)["pending_turns"] == 0
    assert invoke("check", "--batch", "missing", "--json").exit_code == 2
    raw_config["memory"] = {"enabled": False}
    write_config(enso_home, raw_config)
    assert invoke("prepare", "--batch", "disabled", "--json").exit_code == 1
    disabled = invoke("record", "--batch", "run-1", "--file", "-", "--json", input="[]")
    assert disabled.exit_code == 1 and "disabled" in json.loads(disabled.output)["error"]
    enso_home.config.write_text("not json")
    assert invoke("prepare", "--batch", "bad", "--json").exit_code == 2


def test_record_and_forget_require_explicit_scope(enso_home, raw_config):
    write_config(enso_home, raw_config)
    db.migrate(enso_home)
    ident = source(enso_home)
    memory.prepare_batch(enso_home, batch_id="first")
    payload = json.dumps([{"summary": "Deployment reported paused.", "source_ids": [ident]}])
    saved = invoke("record", "--batch", "first", "--file", "-", "--json", input=payload)
    assert saved.exit_code == 0, saved.output
    ref = json.loads(saved.output)["entries"][0]["ref"]
    retry = invoke("record", "--batch", "first", "--file", "-", "--json", input=payload)
    assert json.loads(retry.output)["entries"][0]["ref"] == ref
    refused = invoke("forget", ref, "--json")
    assert refused.exit_code == 1 and "--yes" in json.loads(refused.output)["error"]
    assert memory.get_entry(enso_home, ref) is not None
    forgotten = invoke("forget", ref, "--yes", "--json")
    assert forgotten.exit_code == 0 and json.loads(forgotten.output)["ok"]
    assert memory.status(enso_home)["turns"] == 0
    assert invoke("show", ref, "--json").exit_code == 1


@pytest.mark.parametrize(
    "args",
    [
        ["list", "--workspace", "personal", "--all-workspaces"],
        ["list", "--since", "tomorrowish"],
        ["list", "--since", "2026-09-15", "--until", "2026-09-14"],
        ["show", "bad-reference"],
    ],
)
def test_query_errors_are_single_json_results(enso_home, raw_config, args):
    write_config(enso_home, raw_config)
    result = invoke(*args, "--json")
    assert result.exit_code == 1 and not json.loads(result.output)["ok"]
    assert not enso_home.db.exists()


def test_read_commands_do_not_initialize_missing_database(enso_home, raw_config):
    write_config(enso_home, raw_config)
    assert json.loads(invoke("list", "--json").output)["total"] == 0
    assert json.loads(invoke("status", "--json").output)["turns"] == 0
    assert not enso_home.db.exists()
    enso_home.db.write_bytes(b"invalid sqlite database")
    result = invoke("list", "--json")
    assert result.exit_code == 1 and "error" in json.loads(result.output)


def test_show_sources_text_preserves_failure_context_and_truncation(
    enso_home, raw_config, monkeypatch
):
    raw_config["memory"] = {"timezone": "America/Vancouver"}
    write_config(enso_home, raw_config)
    db.migrate(enso_home)
    monkeypatch.setattr(db, "now", lambda: "2026-09-14T17:01:00.000000+00:00")
    ident = source(
        enso_home,
        request="Inspect these logs: " + "x" * memory.MAX_TEXT_BYTES,
        response="The logs could not be opened.",
        status="error",
        error="Permission denied",
        thread="thread-42",
    )
    entry = record(enso_home, ident, "The requested log inspection failed with permission denied.")
    shown = invoke("show", entry.ref, "--sources")
    assert shown.exit_code == 0, shown.output
    for detail in (
        f"Exchange {ident} · error",
        "Received: 2026-09-14 10:00 PDT",
        "Completed: 2026-09-14 10:01 PDT",
        "Origin: personal · slack · #plans (C1)",
        "Sender: Gavin (U1)",
        "Thread: thread-42",
        "Request truncated in storage.",
        "Error: Permission denied",
        "Agent: The logs could not be opened.",
    ):
        assert detail in shown.output
    assert "Response truncated" not in shown.output


def test_serve_startup_recovers_memory_before_admitting_conversations(
    enso_home, raw_config, monkeypatch
):
    from conftest import FakeTransport

    import enso.cli as cli

    write_config(enso_home, raw_config)
    db.migrate(enso_home)
    ident = memory.start_turn(
        enso_home,
        conversation="slack:D1",
        workspace="default",
        provider="claude",
        model="opus",
        effort="high",
        transport="slack",
        channel="D1",
        channel_name="dm",
        thread=None,
        message_id="interrupted-message",
        user_id="U1",
        user_name="Gavin",
        request="Continue this work after restart.",
        files=(),
        received_at="2026-09-14T10:00:00-07:00",
    )
    assert ident is not None
    monkeypatch.setattr(cli.logsetup, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.audit, "startup_warnings", lambda *args: [])
    monkeypatch.setattr(cli, "build_transports", lambda config: [FakeTransport()])
    entered = []

    async def fake_serve(runtime, transports, runner, heartbeat_runner):
        # The ordinary CLI package also imports its memory command submodule. Startup must
        # call the core recovery API, then admit transports only after it has finished.
        batch = memory.prepare_batch(enso_home, batch_id="startup-proof")
        assert batch is not None and batch.turns[0].id == ident
        assert batch.turns[0].status == "stopped"
        assert batch.turns[0].completed_at is not None
        assert "stopped before" in batch.turns[0].error
        entered.append(True)

    monkeypatch.setattr(cli, "_serve", fake_serve)
    cli._serve_home(enso_home, debug=False)
    assert entered == [True]
