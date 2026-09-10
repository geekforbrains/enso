"""Heartbeat CLI persists finite work and reports bounded, unambiguous JSON failures."""

from __future__ import annotations

import json
import sqlite3

import pytest
import typer
from conftest import write_config
from typer.testing import CliRunner

from enso import heartbeat
from enso.cli.heartbeat import INPUT_LIMIT, heartbeat_app
from enso.config import load_config

app = typer.Typer()
app.add_typer(heartbeat_app, name="heartbeat")
runner = CliRunner()


@pytest.fixture(autouse=True)
def cli_home(enso_home, raw_config):
    write_config(enso_home, raw_config)
    return enso_home


def invoke(*args, input=None):
    return runner.invoke(app, ["heartbeat", *args], input=input)


def packet(*args, input=None):
    result = invoke(*args, "--json", input=input)
    assert result.exit_code == 0, (result.stdout, result.stderr, result.exception)
    assert result.stderr == ""
    return json.loads(result.stdout)


def create(**fields):
    value = {
        "title": "Thank Bob",
        "instructions": "Send Bob a thank-you email about yesterday's meeting.",
        "completion": "The email is sent and its receipt recorded.",
        "allowed_actions": "Send the requested thank-you email to bob@example.test.",
        "at": "2030-09-08T08:30:00+00:00",
        **fields,
    }
    return packet("create", "--file", "-", input=json.dumps(value))


def test_create_defaults_and_origin_are_saved(cli_home, monkeypatch):
    monkeypatch.setenv("ENSO_ORIGIN_TRANSPORT", "slack")
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C3")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "7.0")
    monkeypatch.setenv("ENSO_ORIGIN_USER_ID", "U7")
    monkeypatch.setenv("ENSO_ORIGIN_USER_NAME", "Gavin")
    beat = create()
    assert beat["state"] == "paused" and beat["workspace"] == "default"
    assert beat["agent"] == {"provider": "claude", "model": "opus", "effort": "xhigh"}
    assert beat["notify"] == "slack:C3" and beat["notify_thread"] == "7.0"
    assert beat["origin"]["user_id"] == "U7"
    assert beat["directory"] == str(cli_home.heartbeat / beat["ref"])
    events = packet("history", beat["ref"])
    assert len(events) == 1 and events[0]["actor"] == "slack:U7"
    assert [item["ref"] for item in packet("list")] == [beat["ref"]]


def test_update_merges_fields_and_preserves_explicit_destination(tmp_path):
    beat = create(notify="slack:C9")
    path = tmp_path / "patch.json"
    path.write_text(json.dumps({"title": "Thank Bob tomorrow"}))
    updated = packet("update", beat["ref"], "--file", str(path))
    assert updated["title"] == "Thank Bob tomorrow"
    assert updated["instructions"] == beat["instructions"]
    assert updated["notify"] == "slack:C9"
    assert updated["revision"] > beat["revision"]
    stale = invoke(
        "update", beat["ref"], "--file", str(path), "--if-revision", str(beat["revision"]), "--json"
    )
    assert stale.exit_code == 1 and json.loads(stale.stdout)["ok"] is False


def test_gate_creation_stays_paused_and_resume_only_checks_syntax(cli_home):
    beat = create(gate="gate.sh")
    missing = invoke("resume", beat["ref"], "--json")
    assert missing.exit_code == 1 and "gate" in json.loads(missing.stdout)["error"]
    script = cli_home.heartbeat / beat["ref"] / "gate.sh"
    script.parent.mkdir(parents=True)
    script.write_text("touch should-never-run\nexit 1\n")
    resumed = packet("resume", beat["ref"])
    assert resumed["state"] == "active"
    assert not script.with_name("should-never-run").exists()
    paused = packet("pause", beat["ref"], "--message", "Wait for my instructions")
    assert paused["state"] == "paused"


def test_history_pagination_and_source_time():
    beat = create()
    first = packet("note", beat["ref"], "We met yesterday", "--occurred-at", "2026-09-06T09:00:00Z")
    second = packet("note", beat["ref"], "Send a short message")
    assert first["payload"]["occurred_at"].startswith("2026-09-06T09:00:00")
    assert first["payload"]["occurred_at"] != first["created_at"]
    assert [event["id"] for event in packet("history", beat["ref"], "--limit", "1")] == [
        second["id"]
    ]
    assert [
        event["id"] for event in packet("history", beat["ref"], "--after", str(first["id"]))
    ] == [second["id"]]
    assert all(
        event["id"] < second["id"]
        for event in packet("history", beat["ref"], "--before", str(second["id"]))
    )
    notes = packet("history", beat["ref"], "--kind", "noted")
    assert {event["id"] for event in notes} == {first["id"], second["id"]}


@pytest.mark.parametrize("operation", ["cancel", "expire", "complete"])
def test_closed_beats_hidden_until_requested(operation):
    beat = create()
    closed = packet(operation, beat["ref"], "--message", "The requested outcome was resolved.")
    assert closed["state"] in {"cancelled", "expired", "fulfilled"}
    assert packet("list") == []
    assert [item["ref"] for item in packet("list", "--all")] == [beat["ref"]]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("[]", "must be an object"),
        ("{", "invalid JSON"),
        ('{"title":"a","title":"b"}', "duplicate"),
        ('{"agent":{"model":"a","model":"b"}}', "duplicate"),
        ('{"at":NaN}', "non-finite"),
        ('{"x":"' + "x" * INPUT_LIMIT + '"}', "exceeds"),
    ],
)
def test_bad_json_is_one_error_document(text, error):
    result = invoke("create", "--file", "-", "--json", input=text)
    assert result.exit_code == 1 and result.stderr == ""
    value = json.loads(result.stdout)
    assert value["ok"] is False and error in value["error"]


def test_file_failures_and_core_validation_are_reported(tmp_path):
    for path in (tmp_path / "missing.json", tmp_path):
        result = invoke("create", "--file", str(path), "--json")
        assert result.exit_code == 1 and json.loads(result.stdout)["ok"] is False
        assert result.stderr == ""
    invalid = invoke("create", "--file", "-", "--json", input='{"title":3,"unexpected":true}')
    assert invalid.exit_code == 1
    error = json.loads(invalid.stdout)["error"]
    assert "title" in error and "unexpected" in error


def test_database_errors_have_no_traceback(monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(heartbeat, "list_beats", broken)
    result = invoke("list", "--json")
    assert result.exit_code == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {"ok": False, "error": "database is locked"}


def test_other_beat_cannot_be_mutated_from_a_run(cli_home, monkeypatch):
    current = create()
    other = create(title="Other work")
    monkeypatch.setenv("ENSO_BEAT", current["ref"])
    monkeypatch.setenv("ENSO_BEAT_RUN_ID", "running-beat-id")
    result = invoke("complete", other["ref"], "--message", "Done", "--json")
    assert result.exit_code == 1 and "own beat" in json.loads(result.stdout)["error"]
    assert heartbeat.get(cli_home, other["ref"]).state == "paused"


def test_run_records_action_outcome_then_waits_with_checkpoint(cli_home, monkeypatch):
    beat = create()
    packet("resume", beat["ref"])
    run = heartbeat.start_run(load_config(cli_home), beat["ref"], trigger="manual")
    monkeypatch.setenv("ENSO_BEAT", beat["ref"])
    monkeypatch.setenv("ENSO_BEAT_RUN_ID", run.id)
    # Job identifiers inherited from a caller must never replace a beat's own run identity.
    monkeypatch.setenv("ENSO_RUN_ID", "unrelated-job-run")
    action = packet("action", beat["ref"], "thank-bob", "--message", "Send the thank-you email")
    assert action["run_id"] == run.id and action["actor"] == f"beat:{beat['ref']}"
    outcome = packet(
        "action-result",
        beat["ref"],
        "thank-bob",
        "--status",
        "succeeded",
        "--message",
        "Email sent",
        "--receipt",
        "mail:sent-123",
    )
    assert outcome["receipt"] == "mail:sent-123" and outcome["action_status"] == "succeeded"
    waiting = packet(
        "wait",
        beat["ref"],
        "--message",
        "Waiting for Bob to acknowledge receipt",
        "--followup-at",
        "2030-09-09T08:30:00+00:00",
        "--checkpoint",
        '{"message_id":"123"}',
    )
    assert waiting["state"] == "active" and waiting["checkpoint"] == {"message_id": "123"}


def test_wait_and_action_require_run_ownership():
    beat = create()
    for args in (
        ("wait", beat["ref"], "--message", "No running provider"),
        ("action", beat["ref"], "send", "--message", "Send mail"),
    ):
        result = invoke(*args, "--json")
        assert result.exit_code == 1 and json.loads(result.stdout)["ok"] is False
        assert result.stderr == ""


def test_stale_run_cannot_replace_instructions_or_create_another_beat(cli_home, monkeypatch):
    beat = create()
    config = load_config(cli_home)
    packet("resume", beat["ref"])
    run = heartbeat.start_run(config, beat["ref"], trigger="manual")
    heartbeat.update(config, beat["ref"], {"instructions": "Do not send the email yet."})
    monkeypatch.setenv("ENSO_BEAT", beat["ref"])
    monkeypatch.setenv("ENSO_BEAT_RUN_ID", run.id)
    for command in (("update", beat["ref"]), ("create",)):
        result = invoke(*command, "--file", "-", "--json", input='{"instructions":"Send it now"}')
        assert result.exit_code == 1
        assert "during a heartbeat run use note or wait" in json.loads(result.stdout)["error"]
    assert heartbeat.get(cli_home, beat["ref"]).instructions == "Do not send the email yet."
    assert len(heartbeat.list_beats(cli_home)) == 1


def test_status_reports_disabled_without_starting_work(cli_home, raw_config):
    raw_config["heartbeat"] = {"enabled": False, "retention_days": 14}
    write_config(cli_home, raw_config)
    assert packet("status") == {"enabled": False, "retention_days": 14}
    refused = invoke("create", "--file", "-", "--json", input="{}")
    assert refused.exit_code == 1
