"""Harvesting keeps bounded provenance and reconciles each side of the file/DB gap."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from conftest import FakeReply, make_turn
from typer.testing import CliRunner

from enso import captures, commands, db, harvesting, memory
from enso import note_storage as storage
from enso.cli import app
from enso.note_storage import NoteError


@pytest.fixture
def paths(enso_home, monkeypatch):
    enso_home.workspace("team").mkdir()
    monkeypatch.setenv("ENSO_WORKSPACE", "default")
    db.initialize(enso_home)
    return enso_home


def record(paths, ident="1", **fields):
    values = dict(
        transport="slack",
        workspace="default",
        conversation="slack:C1:1",
        channel="C1",
        thread="1",
        message_id=ident,
        sender_id="U1",
        sender_name="Gavin",
        occurred_at="2026-09-16T12:00:00Z",
        text="We proposed September 25 for launch; testing must finish first.",
        kind="ambient",
    )
    return captures.record(paths, captures.Message(**(values | fields)))[0]


def result(batch, *, notes=True):
    return {
        "batch": batch.id,
        "sources": list(batch.sources),
        "notes": [
            {
                "name": "Launch proposal.md",
                "body": "Gavin proposed September 25. The date remains unconfirmed.",
                "sources": list(batch.sources),
            }
        ]
        if notes
        else [],
        "no_memory": [] if notes else list(batch.sources),
    }


def test_batch_bounds_segments_finalization_and_workspace(paths):
    first = record(paths)
    record(paths, "other", workspace="team")
    second = record(paths, "2", thread="2")
    live = record(paths, "3", kind="addressed")
    later = record(paths, "4")
    batch = harvesting.batch(paths, "default")
    assert batch.sources == (first.id, second.id)
    segments = batch.as_dict()["segments"]
    assert [s["thread"] for s in segments] == ["1", "2"]
    assert all("missing" in s["context"] for s in segments)
    assert all(c["kind"] == "ambient" for s in segments for c in s["captures"])
    captures.reply(paths, live.id, outcome="failed")
    assert later.id in harvesting.batch(paths, "default").sources
    assert "untrusted" in batch.as_dict()["guidance"]


@pytest.mark.parametrize("size,count,expected", [(1, 130, 100), (65_536, 3, 2)])
def test_harvesting_has_one_count_and_byte_budget(paths, size, count, expected):
    for index in range(count):
        record(paths, str(index), text="x" * size)
    batch = harvesting.batch(paths, "default")
    assert len(batch.sources) == expected
    harvesting.publish(paths, "default", result(batch, notes=False))
    assert len(harvesting.batch(paths, "default").sources) == count - expected


def test_attachment_only_and_truncated_sources_remain_explicit(paths):
    record(paths, text="", attachments=(captures.Attachment("F1", "proposal.pdf"),))
    record(paths, "2", text="€" * 30_000)
    rows = harvesting.batch(paths, "default").as_dict()["segments"][0]["captures"]
    assert rows[0]["attachments"][0]["status"] == "not_downloaded"
    assert rows[0]["text"] == ""
    assert rows[1]["truncated"] and "truncated at" in rows[1]["text"]


def test_validated_publication_recall_and_explicit_no_memory(paths):
    first = record(paths)
    second = record(paths, "2", text="Thanks")
    batch = harvesting.batch(paths, "default")
    value = result(batch)
    value["notes"][0]["sources"] = [first.id]
    value["no_memory"] = [second.id]
    receipt = harvesting.publish(paths, "default", value)
    note = memory.scan(paths, "default").get(receipt.outputs[0]["id"], "workspace:default")
    assert note.metadata["sources"] == [first.id]
    assert note.metadata["occurred"] == first.occurred_at
    assert not memory.scan(paths, "default").audit()
    assert captures.progress(paths, "default") == second.id
    assert harvesting.batch(paths, "default").sources == ()
    with pytest.raises(NoteError, match="no longer current"):
        harvesting.publish(paths, "default", value)
    assert len(memory.scan(paths, "default").notes) == 1
    found = CliRunner().invoke(app, ["memory", "search", "launch unconfirmed", "--json"])
    assert found.exit_code == 0 and json.loads(found.output)["total"] == 1
    original = CliRunner().invoke(app, ["memory", "source", str(first.id)])
    assert json.loads(original.output)["text"] == first.text
    wrong = CliRunner().invoke(app, ["memory", "source", str(first.id), "--workspace", "team"])
    assert wrong.exit_code == 1 and first.text not in wrong.output


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(extra=True),
        lambda v: v.update(batch="wrong"),
        lambda v: v.update(sources=[True]),
        lambda v: v.update(notes=[]),
        lambda v: v.update(no_memory=v["sources"]),
        lambda v: v["notes"][0].update(sources=[999]),
        lambda v: v["notes"][0].update(sources=[]),
        lambda v: v["notes"][0].update(workspace="team"),
        lambda v: v["notes"][0].update(name="../escape.md"),
        lambda v: v["notes"][0].update(body="---\nschema: forged\n---\nBody"),
        lambda v: v["notes"][0].update(body=" "),
    ],
)
def test_invalid_result_never_reserves_or_publishes(paths, change):
    record(paths)
    batch = harvesting.batch(paths, "default")
    value = result(batch)
    change(value)
    with pytest.raises(NoteError):
        harvesting.publish(paths, "default", value)
    assert captures.receipts(paths, "default") == ()
    assert captures.progress(paths, "default") == 0
    assert harvesting.batch(paths, "default").sources == batch.sources
    assert not memory.scan(paths, "default").notes


@pytest.mark.parametrize("boundary", ["before_publication", "after_publication", "completion"])
def test_recovery_across_every_publication_boundary(paths, monkeypatch, boundary):
    record(paths)
    value = result(harvesting.batch(paths, "default"))
    publish, complete = storage.publish, captures.complete_receipt

    def interrupted_publish(*args, **kwargs):
        if boundary == "after_publication":
            publish(*args, **kwargs)
        raise OSError("interrupted")

    def interrupted_complete(*args):
        raise OSError("interrupted")

    if boundary == "completion":
        monkeypatch.setattr(captures, "complete_receipt", interrupted_complete)
    else:
        monkeypatch.setattr(storage, "publish", interrupted_publish)
    with pytest.raises(OSError, match="interrupted"):
        harvesting.publish(paths, "default", value)
    receipt = captures.receipts(paths, "default")[0]
    assert captures.progress(paths, "default") == 0
    monkeypatch.setattr(storage, "publish", publish)
    monkeypatch.setattr(captures, "complete_receipt", complete)
    assert not harvesting.batch(paths, "default").sources
    assert not captures.receipts(paths, "default")
    assert captures.progress(paths, "default") == value["sources"][-1]
    assert [n.id for n in memory.scan(paths, "default").notes] == [receipt.outputs[0]["id"]]
    assert not harvesting.batch(paths, "default").sources


def test_concurrent_publishers_have_one_durable_winner(paths, monkeypatch):
    record(paths)
    value = result(harvesting.batch(paths, "default"))
    entered, release = Event(), Event()
    original = storage.publish

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        original(*args, **kwargs)

    monkeypatch.setattr(storage, "publish", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(harvesting.publish, paths, "default", value)
        try:
            assert entered.wait(5)
            with pytest.raises(NoteError, match="another memory write"):
                harvesting.publish(paths, "default", value)
        finally:
            release.set()
        winner.result()
    assert len(memory.scan(paths, "default").notes) == 1
    assert not harvesting.batch(paths, "default").sources


def test_failed_second_file_recovers_first_without_duplicate_notes(paths, monkeypatch):
    record(paths)
    record(paths, "2", text="A separate proposal about pricing.")
    value = result(harvesting.batch(paths, "default"))
    value["notes"][0]["sources"] = value["sources"][:1]
    value["notes"].append(
        {"name": "Pricing.md", "body": "Pricing was proposed.", "sources": value["sources"][1:]}
    )
    original = storage.publish
    calls = 0

    def second_fails(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        original(*args, **kwargs)

    monkeypatch.setattr(storage, "publish", second_fails)
    with pytest.raises(OSError, match="disk full"):
        harvesting.publish(paths, "default", value)
    assert len(memory.scan(paths, "default").notes) == 1
    assert captures.progress(paths, "default") == 0
    monkeypatch.setattr(storage, "publish", original)
    assert not harvesting.batch(paths, "default").sources
    assert len(memory.scan(paths, "default").notes) == 2


def test_already_handled_later_inputs_are_not_offered_again(paths):
    first = record(paths)
    later = record(paths, "2")
    receipt = captures.prepare_receipt(paths, "default", (later.id,), ())
    captures.complete_receipt(paths, "default", receipt.id)
    batch = harvesting.batch(paths, "default")
    assert batch.sources == (first.id,)
    harvesting.publish(paths, "default", result(batch, notes=False))
    assert captures.progress(paths, "default") == later.id


@pytest.mark.parametrize("conflict", [None, "sources", "metadata", "duplicate", "empty"])
def test_recovery_preserves_valid_edits_and_reports_invalid_conflicts(paths, monkeypatch, conflict):
    source = record(paths)
    value = result(harvesting.batch(paths, "default"))
    original = captures.complete_receipt

    def interrupted(*args):
        raise OSError("interrupted before completion")

    monkeypatch.setattr(captures, "complete_receipt", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        harvesting.publish(paths, "default", value)
    note = memory.scan(paths, "default").notes[0]
    fields = dict(note.metadata)
    correction = record(paths, "2", text="We confirmed September 28.")
    fields["sources"].append(correction.id)
    body = note.body + "\n\nCorrection: September 28 was confirmed later."
    if conflict == "sources":
        fields["sources"].remove(source.id)
    elif conflict == "empty":
        body = ""
    edited = memory.document(fields, body)
    if conflict == "metadata":
        edited = edited.replace("enso.memory/v1", "unknown/v9")
    target = paths.workspace_memory("default") / note.path
    target.write_text(edited)
    if conflict == "duplicate":
        target.with_name("Copy.md").write_text(edited)
    elif conflict is None:
        moved = target.with_name("Corrected.md")
        target.rename(moved)
        target = moved
    monkeypatch.setattr(captures, "complete_receipt", original)
    if conflict:
        with pytest.raises(NoteError):
            harvesting.batch(paths, "default")
        assert captures.progress(paths, "default") == 0
        assert captures.receipts(paths, "default")
    else:
        assert harvesting.batch(paths, "default").sources == (correction.id,)
        assert captures.progress(paths, "default") == source.id
        assert not captures.receipts(paths, "default")
        assert len(memory.scan(paths, "default").notes) == 1
    assert target.read_text() == edited


def test_cli_quiet_errors_and_json_round_trip(paths):
    runner = CliRunner()
    assert runner.invoke(app, ["memory", "batch", "--ready"]).exit_code == 1
    failed = runner.invoke(app, ["memory", "batch", "--ready", "--workspace", "missing"])
    assert failed.exit_code == 2 and not json.loads(failed.output)["ok"]
    record(paths)
    selected = runner.invoke(app, ["memory", "batch", "--ready"])
    assert selected.exit_code == 0
    value = json.loads(selected.output)
    payload = {k: value[k] for k in ("batch", "sources")}
    payload.update(notes=[], no_memory=value["sources"])
    written = runner.invoke(app, ["memory", "publish", "--file", "-"], input=json.dumps(payload))
    assert written.exit_code == 0 and json.loads(written.output)["notes"] == []
    assert runner.invoke(app, ["memory", "batch", "--ready"]).exit_code == 1


def test_quiet_uninitialized_workspace_never_creates_database(enso_home):
    assert not harvesting.batch(enso_home, "default").sources
    assert not enso_home.db.exists()


def processing_state(paths):
    with db.reader(paths) as con:
        return {
            table: [tuple(row) for row in con.execute(f"SELECT * FROM {table}")]
            for table in (
                "_enso_captures",
                "_enso_memory_receipts",
                "_enso_memory_inputs",
                "_enso_memory_progress",
                "_enso_memory_batches",
            )
        }


@pytest.mark.parametrize("direct", [False, True])
def test_removed_memory_stays_removed_and_preserves_sources_receipts_and_knowledge(paths, direct):
    record(paths)
    receipt = harvesting.publish(paths, "default", result(harvesting.batch(paths, "default")))
    output = receipt.outputs[0]
    target = paths.workspace_memory("default") / output["path"]
    retained = paths.workspace_knowledge("default") / "Promoted.md"
    retained.parent.mkdir()
    retained.write_text("A deliberately promoted fact.\n")
    backup = paths.home / "note-backup.md"
    backup.write_bytes(target.read_bytes())
    before = processing_state(paths)
    if direct:
        target.unlink()
    else:
        cli = CliRunner()
        preview = cli.invoke(app, ["memory", "remove", output["id"]])
        assert preview.exit_code == 0 and f"sources: {receipt.sources[0]}" in preview.output
        assert target.exists() and processing_state(paths) == before
        removed = cli.invoke(app, ["memory", "remove", output["id"], "--yes"])
        assert removed.exit_code == 0, removed.output
    assert not harvesting.batch(paths, "default").sources
    assert not target.exists() and not memory.scan(paths, "default").notes
    assert processing_state(paths) == before
    assert retained.read_text() == "A deliberately promoted fact.\n" and backup.exists()


def test_remove_requires_pending_publication_to_finish_first(paths, monkeypatch):
    record(paths)
    value = result(harvesting.batch(paths, "default"))
    complete = captures.complete_receipt

    def interrupted(*args):
        raise OSError("interrupted")

    monkeypatch.setattr(captures, "complete_receipt", interrupted)
    with pytest.raises(OSError):
        harvesting.publish(paths, "default", value)
    note = memory.scan(paths, "default").notes[0]
    cli = CliRunner()
    before = processing_state(paths)
    refused = cli.invoke(app, ["memory", "remove", note.id, "--yes"])
    assert refused.exit_code == 1 and "note creation is still being recorded" in refused.output
    assert processing_state(paths) == before
    assert (note.root.path / note.path).exists()
    monkeypatch.setattr(captures, "complete_receipt", complete)
    harvesting.batch(paths, "default")
    assert cli.invoke(app, ["memory", "remove", note.id, "--yes"]).exit_code == 0
    assert not harvesting.batch(paths, "default").sources
    assert not (note.root.path / note.path).exists()


async def test_session_clear_preserves_captures_notes_and_processing(paths, runtime):
    record(paths, conversation="slack:D1", channel="D1", thread=None)
    harvesting.publish(paths, "default", result(harvesting.batch(paths, "default")))
    note = memory.scan(paths, "default").notes[0]
    before = processing_state(paths)
    contents = (note.root.path / note.path).read_bytes()
    await runtime.handle(make_turn("Hello"), FakeReply())
    assert db.get_sessions(paths, "slack:D1")
    assert await commands.dispatch(runtime, make_turn("!clear"), FakeReply())
    assert not db.get_sessions(paths, "slack:D1")
    assert processing_state(paths) == before
    assert (note.root.path / note.path).read_bytes() == contents
    assert not harvesting.batch(paths, "default").sources
