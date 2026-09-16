"""Permanent capture identity, incomplete delivery, and independent processing progress."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from enso import captures as c
from enso import db
from enso.config import Paths


@pytest.fixture
def paths(tmp_path):
    paths = Paths(tmp_path)
    for name in ("team", "personal"):
        paths.workspace(name).mkdir(parents=True)
    db.initialize(paths)
    return paths


def message(ident="1", **values):
    return replace(
        c.Message(
            "slack",
            "team",
            "slack:C1:1",
            "C1",
            "1",
            ident,
            "U1",
            "Gavin",
            "2026-09-16T12:00:00Z",
            "The original request",
        ),
        **values,
    )


def test_duplicate_event_retains_original_snapshot_and_owner_even_concurrently(paths):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: c.record(paths, message()), range(4)))
    original = results[0][0]
    assert sum(inserted for _, inserted in results) == 1
    assert {row.id for row, _ in results} == {original.id}
    duplicate, inserted = c.record(paths, message(workspace="personal", text="edited"))
    assert duplicate == original and not inserted
    assert c.query(paths, "personal") == ()
    # IDs are scoped by transport and channel, not by workspace or event wrapper IDs.
    assert c.record(paths, message(channel="C2"))[1]
    assert c.record(paths, message(transport="telegram"))[1]


def test_human_reply_and_incremental_delivery_are_separate_and_recoverable(paths):
    source, _ = c.record(paths, message())
    assert not source.finalized and source.outcome == "pending"
    generated = c.reply(paths, source.id, text="Hello world", outcome="completed", final=False)
    assert generated.parent_id == source.id and generated.id != source.id
    assert generated.delivery == "unattempted"
    partial = c.reply(
        paths,
        source.id,
        text="Hello world",
        outcome="completed",
        delivery="sending",
        parts=(c.Part(0, 5, "sent", "m1"), c.Part(6, 11, "sending")),
        final=False,
    )
    assert partial.id == generated.id
    assert c.recover(paths) == 1
    assert c.recover(paths) == 0
    recovered = c.get(paths, "team", partial.id)
    assert recovered.outcome == "completed" and recovered.delivery == "uncertain"
    assert recovered.parts == (c.Part(0, 5, "sent", "m1"), c.Part(6, 11, "uncertain"))
    assert c.get(paths, "team", source.id).outcome == "interrupted"
    with pytest.raises(ValueError, match="finalized"):
        c.reply(paths, source.id, outcome="completed")


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "timed_out", "dropped", "empty"])
def test_no_final_answer_preserves_outcome_without_invented_text(paths, outcome):
    source, _ = c.record(paths, message())
    result = c.reply(paths, source.id, outcome=outcome)
    assert result.text == "" and result.delivery == "unattempted" and result.finalized
    assert c.get(paths, "team", source.id).outcome == outcome
    assert c.get(paths, "personal", result.id) is None


def test_queries_are_ordered_scoped_and_byte_bounded_and_truncation_is_explicit(paths):
    source, _ = c.record(paths, message(text="€" * 30_000, kind="ambient"))
    assert len(source.text.encode()) == 65_535
    assert source.truncated and "truncated" in source.source_text
    second, _ = c.record(paths, message("2", text="x" * 65_536, kind="ambient"))
    third, _ = c.record(paths, message("3", conversation="slack:C1:3", kind="ambient"))
    c.record(paths, message("4", workspace="personal", kind="ambient"))
    assert [r.id for r in c.query(paths, "team")] == [source.id, second.id]
    assert [r.id for r in c.query(paths, "team", after=second.id)] == [third.id]
    assert [r.id for r in c.query(paths, "team", conversation="slack:C1:3")] == [third.id]
    assert len(c.query(paths, "team", limit=1)) == 1
    assert c.query(paths, "team", max_bytes=1) == ()
    for kwargs in ({"limit": 101}, {"limit": 0}, {"max_bytes": 131_073}, {"after": -1}):
        with pytest.raises(ValueError):
            c.query(paths, "team", **kwargs)


def test_attachment_download_updates_only_references(paths):
    attachment = c.Attachment("F1", "report.pdf", "application/pdf", 123)
    source, _ = c.record(paths, message(text="", attachments=(attachment,)))
    downloaded = replace(attachment, status="downloaded", path="uploads/F1/report.pdf")
    c.attachments(paths, source.id, (downloaded,))
    assert c.get(paths, "team", source.id).attachments == (downloaded,)
    with pytest.raises(ValueError, match="first snapshot"):
        c.attachments(paths, source.id, (replace(downloaded, id="F2"),))
    for path in ("/etc/passwd", "uploads/../secret", "uploads\\secret"):
        with pytest.raises(ValueError):
            c.Attachment("F1", status="downloaded", path=path)


def test_receipts_survive_interruption_and_advance_only_contiguous_workspace_inputs(paths):
    one, _ = c.record(paths, message(kind="ambient"))
    other, _ = c.record(paths, message("2", workspace="personal", kind="ambient"))
    three, _ = c.record(paths, message("3", kind="ambient"))
    plan = ({"id": "stable-note", "path": "2026/09/16/note.md", "sha256": "expected"},)
    receipt = c.prepare_receipt(paths, "team", (one.id,), plan)
    assert c.receipts(paths, "team") == (receipt,)  # another connection can recover the plan
    assert c.progress(paths, "team") == 0
    with pytest.raises(ValueError, match="already belongs"):
        c.prepare_receipt(paths, "team", (one.id, three.id), ())
    # The losing writer rolled back its receipt and every reservation.
    assert c.receipts(paths, "team") == (receipt,)
    later = c.prepare_receipt(paths, "team", (three.id,), ())
    c.complete_receipt(paths, "team", later.id)
    assert c.progress(paths, "team") == 0
    c.complete_receipt(paths, "team", receipt.id)
    c.complete_receipt(paths, "team", receipt.id)  # idempotent recovery
    assert c.progress(paths, "team") == three.id
    assert c.progress(paths, "personal") == 0
    assert c.receipts(paths, "team") == ()
    personal = c.prepare_receipt(paths, "personal", (other.id,), ())
    c.complete_receipt(paths, "personal", personal.id)
    assert c.progress(paths, "personal") == other.id
    assert c.query(paths, "team")  # processing does not erase the history


def test_receipts_reject_unfinished_missing_or_cross_workspace_sources(paths):
    pending, _ = c.record(paths, message())
    other, _ = c.record(paths, message("2", workspace="personal", kind="ambient"))
    for ids in ((), (pending.id,), (other.id,), (12345,), (other.id, other.id)):
        with pytest.raises(ValueError):
            c.prepare_receipt(paths, "team", ids, ())
    assert c.receipts(paths, "team") == ()


def test_reply_checkpoint_rolls_back_if_parent_finalization_is_interrupted(paths):
    source, _ = c.record(paths, message())
    with db.transaction(paths) as con:
        con.execute(
            "CREATE TRIGGER fail_parent BEFORE UPDATE ON _enso_captures "
            "WHEN OLD.kind = 'addressed' BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
        c.reply(paths, source.id, text="An answer", outcome="completed")
    assert c.query(paths, "team") == (source,)
    with db.transaction(paths) as con:
        con.execute("DROP TRIGGER fail_parent")
    assert c.recover(paths) == 1
    assert c.get(paths, "team", source.id).outcome == "interrupted"
