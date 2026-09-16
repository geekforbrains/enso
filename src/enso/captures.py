"""Transport-neutral conversation history and recoverable memory processing receipts.

Human identity and its first snapshot never change. Reply delivery is checkpointed separately;
a pending write after a crash is evidence of an incomplete capture, never permission to resend.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Literal
from uuid import uuid4

from . import db
from .config import Paths, require_workspace
from .note_storage import valid_timestamp

MAX_TEXT_BYTES = 65_536
MAX_BATCH_COUNT = 100
MAX_BATCH_BYTES = 131_072
Outcome = Literal[
    "pending", "completed", "failed", "cancelled", "timed_out", "dropped", "empty", "interrupted"
]
Delivery = Literal["unattempted", "sending", "complete", "partial", "failed", "uncertain"]


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str = ""
    media_type: str | None = None
    size: int | None = None
    status: Literal["not_downloaded", "downloaded", "failed"] = "not_downloaded"
    path: str | None = None

    def __post_init__(self) -> None:
        if self.path is not None:
            parts = PurePosixPath(self.path).parts
            if len(parts) < 2 or parts[0] != "uploads" or ".." in parts or "\\" in self.path:
                raise ValueError("attachment path must be workspace-relative under uploads/")
        if (self.status == "downloaded") != (self.path is not None):
            raise ValueError("only downloaded attachments have a local path")
        if self.size is not None and self.size < 0:
            raise ValueError("attachment size cannot be negative")


@dataclass(frozen=True)
class Message:
    """The authenticated transport's original human message, before prompt preparation."""

    transport: str
    workspace: str
    conversation: str
    channel: str
    thread: str | None
    message_id: str
    sender_id: str
    sender_name: str
    occurred_at: str
    text: str
    kind: Literal["addressed", "ambient"] = "addressed"
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class Part:
    """Character range in the original reply representation, with its known send result.

    Ranges can exceed the stored prefix when text is truncated. A sending part recovered
    after interruption becomes uncertain; an acknowledged ID is never discarded.
    """

    start: int
    end: int
    status: Literal["sending", "sent", "failed", "uncertain"]
    message_id: str | None = None


@dataclass(frozen=True)
class Capture:
    id: int
    transport: str
    workspace: str
    conversation: str
    channel: str
    thread: str | None
    message_id: str | None
    sender_id: str
    sender_name: str
    occurred_at: str
    kind: str
    parent_id: int | None
    text: str
    truncated: bool
    attachments: tuple[Attachment, ...]
    outcome: Outcome
    delivery: Delivery
    parts: tuple[Part, ...]
    finalized: bool
    created_at: str
    updated_at: str

    @property
    def source_text(self) -> str:
        return self.text + (
            "\n[Capture text truncated at 65,536 UTF-8 bytes.]" if self.truncated else ""
        )


def _capture(row: sqlite3.Row) -> Capture:
    fields = dict(row)
    fields["attachments"] = tuple(Attachment(**item) for item in json.loads(row["attachments"]))
    fields["parts"] = tuple(Part(**item) for item in json.loads(row["parts"]))
    fields["truncated"] = bool(row["truncated"])
    fields["finalized"] = bool(row["finalized"])
    return Capture(**fields)


def _text(text: str) -> tuple[str, bool]:
    data = text.encode("utf-8")
    return data[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore"), len(data) > MAX_TEXT_BYTES


def record(paths: Paths, message: Message) -> tuple[Capture, bool]:
    """Return the first snapshot and whether it is new, including across binding changes."""
    require_workspace(paths, message.workspace)
    stamp = valid_timestamp(message.occurred_at)
    if stamp is None:
        raise ValueError("capture occurrence must have a timezone")
    if not all((message.transport, message.conversation, message.channel, message.message_id)):
        raise ValueError("capture requires transport, conversation, channel, and message identity")
    text, truncated = _text(message.text)
    now = db.now()
    with db.transaction(paths) as con:
        inserted = (
            con.execute(
                "INSERT INTO _enso_captures (transport, workspace, conversation, channel, thread, "
                "message_id, sender_id, sender_name, occurred_at, kind, text, truncated, "
                "attachments, "
                "outcome, delivery, finalized, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unattempted', ?, ?, ?) "
                "ON CONFLICT (transport, channel, message_id) DO NOTHING",
                (
                    message.transport,
                    message.workspace,
                    message.conversation,
                    message.channel,
                    message.thread,
                    message.message_id,
                    message.sender_id,
                    message.sender_name,
                    stamp,
                    message.kind,
                    text,
                    truncated,
                    json.dumps([asdict(a) for a in message.attachments]),
                    "completed" if message.kind == "ambient" else "pending",
                    message.kind == "ambient",
                    now,
                    now,
                ),
            ).rowcount
            == 1
        )
        row = con.execute(
            "SELECT * FROM _enso_captures WHERE transport = ? AND channel = ? AND message_id = ?",
            (message.transport, message.channel, message.message_id),
        ).fetchone()
    return _capture(row), inserted


def attachments(paths: Paths, capture_id: int, items: tuple[Attachment, ...]) -> None:
    """Checkpoint normal turn downloads without changing the first message snapshot."""
    with db.transaction(paths) as con:
        row = con.execute("SELECT * FROM _enso_captures WHERE id = ?", (capture_id,)).fetchone()
        if row is None or row["kind"] != "addressed" or row["finalized"]:
            raise ValueError("attachments require an unfinished addressed capture")
        old = _capture(row).attachments
        if [(a.id, a.filename, a.media_type, a.size) for a in old] != [
            (a.id, a.filename, a.media_type, a.size) for a in items
        ]:
            raise ValueError("attachment metadata must preserve the first snapshot")
        con.execute(
            "UPDATE _enso_captures SET attachments = ?, updated_at = ? WHERE id = ?",
            (json.dumps([asdict(a) for a in items]), db.now(), capture_id),
        )


def reply(
    paths: Paths,
    parent_id: int,
    *,
    text: str = "",
    outcome: Outcome,
    handling_outcome: Outcome | None = None,
    sender_id: str = "",
    sender_name: str = "Enso",
    delivery: Delivery = "unattempted",
    parts: tuple[Part, ...] = (),
    final: bool = True,
) -> Capture:
    """Checkpoint one logical reply; finalize its addressed parent only after delivery ends."""
    if final and (outcome == "pending" or delivery == "sending"):
        raise ValueError("a final reply requires a settled outcome and delivery state")
    text, truncated = _text(text)
    now = db.now()
    with db.transaction(paths) as con:
        parent = con.execute("SELECT * FROM _enso_captures WHERE id = ?", (parent_id,)).fetchone()
        if parent is None or parent["kind"] != "addressed":
            raise ValueError("reply requires an existing addressed capture")
        if parent["finalized"]:
            raise ValueError("the addressed capture is already finalized")
        con.execute(
            "INSERT INTO _enso_captures (transport, workspace, conversation, channel, thread, "
            "sender_id, sender_name, occurred_at, kind, parent_id, text, truncated, outcome, "
            "delivery, parts, finalized, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reply', ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (parent_id) DO UPDATE SET text = excluded.text, "
            "truncated = excluded.truncated, outcome = excluded.outcome, "
            "delivery = excluded.delivery, parts = excluded.parts, finalized = excluded.finalized, "
            "updated_at = excluded.updated_at",
            (
                parent["transport"],
                parent["workspace"],
                parent["conversation"],
                parent["channel"],
                parent["thread"],
                sender_id,
                sender_name,
                now,
                parent_id,
                text,
                truncated,
                outcome,
                delivery,
                json.dumps([asdict(part) for part in parts]),
                final,
                now,
                now,
            ),
        )
        if final:
            con.execute(
                "UPDATE _enso_captures SET outcome = ?, finalized = 1, updated_at = ? WHERE id = ?",
                (handling_outcome or outcome, now, parent_id),
            )
        row = con.execute(
            "SELECT * FROM _enso_captures WHERE parent_id = ?", (parent_id,)
        ).fetchone()
    return _capture(row)


def recover(paths: Paths) -> int:
    """Close interrupted captures at service startup, without executing or sending anything."""
    with db.transaction(paths) as con:
        rows = con.execute(
            "SELECT * FROM _enso_captures WHERE kind = 'reply' AND finalized = 0"
        ).fetchall()
        for row in rows:
            parts = json.loads(row["parts"])
            for part in parts:
                if part["status"] == "sending":
                    part["status"] = "uncertain"
            con.execute(
                "UPDATE _enso_captures SET parts = ?, delivery = CASE "
                "WHEN delivery = 'sending' THEN 'uncertain' ELSE delivery END, "
                "finalized = 1, updated_at = ? WHERE id = ?",
                (json.dumps(parts), db.now(), row["id"]),
            )
        return con.execute(
            "UPDATE _enso_captures SET outcome = 'interrupted', finalized = 1, "
            "updated_at = ? WHERE kind = 'addressed' AND finalized = 0",
            (db.now(),),
        ).rowcount


def get(paths: Paths, workspace: str, capture_id: int) -> Capture | None:
    with db.reader(paths) as con:
        row = con.execute(
            "SELECT * FROM _enso_captures WHERE workspace = ? AND id = ?", (workspace, capture_id)
        ).fetchone()
    return _capture(row) if row else None


def query(
    paths: Paths,
    workspace: str,
    *,
    after: int = 0,
    conversation: str | None = None,
    limit: int = MAX_BATCH_COUNT,
    max_bytes: int = MAX_BATCH_BYTES,
) -> tuple[Capture, ...]:
    """Read a bounded page in stable ID order; no content crosses workspace ownership."""
    if not 1 <= limit <= MAX_BATCH_COUNT or not 1 <= max_bytes <= MAX_BATCH_BYTES or after < 0:
        raise ValueError("capture query exceeds the count or byte bounds")
    with db.reader(paths) as con:
        sql = "SELECT * FROM _enso_captures WHERE workspace = ? AND id > ?"
        args: list[str | int] = [workspace, after]
        if conversation is not None:
            sql += " AND conversation = ?"
            args.append(conversation)
        rows = con.execute(sql + " ORDER BY id LIMIT ?", (*args, limit)).fetchall()
    result = []
    used = 0
    for row in rows:
        used += len(row["text"].encode("utf-8"))
        if used > max_bytes:
            break
        result.append(_capture(row))
    return tuple(result)


def source_problems(paths: Paths, workspace: str, sources: list[int]) -> tuple[str, ...]:
    """Check memory provenance without initializing a database or exposing source bodies."""
    try:
        with db.reader(paths) as con:
            problems = []
            for source in sources:
                row = con.execute(
                    "SELECT workspace FROM _enso_captures WHERE id = ?", (source,)
                ).fetchone()
                if row is None:
                    problems.append(f"capture {source} does not exist")
                elif row["workspace"] != workspace:
                    problems.append(f"capture {source} belongs to another workspace")
            return tuple(problems)
    except db.MissingDatabaseError:
        return tuple(f"capture {source} does not exist" for source in sources)
    except db.UnreadableDatabaseError:
        return ("capture references cannot be verified: database is unreadable",)


@dataclass(frozen=True)
class Receipt:
    id: str
    workspace: str
    sources: tuple[int, ...]
    # Publication plans, including stable note identities and expected content hashes.
    # The harvester validates their schema and reconciles files before complete_receipt.
    outputs: tuple[dict[str, str], ...]
    completed_at: str | None


def _receipt(con: sqlite3.Connection, row: sqlite3.Row) -> Receipt:
    sources = tuple(
        r[0]
        for r in con.execute(
            "SELECT capture_id FROM _enso_memory_inputs WHERE receipt_id = ? ORDER BY capture_id",
            (row["id"],),
        )
    )
    return Receipt(
        row["id"], row["workspace"], sources, tuple(json.loads(row["outputs"])), row["completed_at"]
    )


def prepare_receipt(
    paths: Paths,
    workspace: str,
    sources: tuple[int, ...],
    outputs: tuple[dict[str, str], ...],
) -> Receipt:
    """Reserve finished inputs atomically; empty outputs explicitly mean no useful memory."""
    if not sources or len(sources) > MAX_BATCH_COUNT or len(set(sources)) != len(sources):
        raise ValueError("a receipt requires distinct bounded capture IDs")
    ident = str(uuid4())
    with db.transaction(paths) as con:
        size = 0
        for source in sources:
            row = con.execute(
                "SELECT workspace, finalized, text FROM _enso_captures WHERE id = ?", (source,)
            ).fetchone()
            if row is None or row["workspace"] != workspace or not row["finalized"]:
                raise ValueError("receipt sources must be finished captures in its workspace")
            size += len(row["text"].encode("utf-8"))
        if size > MAX_BATCH_BYTES:
            raise ValueError("receipt source text exceeds the batch limit")
        con.execute(
            "INSERT INTO _enso_memory_receipts (id, workspace, outputs, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ident, workspace, json.dumps(outputs), db.now()),
        )
        try:
            con.executemany(
                "INSERT INTO _enso_memory_inputs VALUES (?, ?)",
                [(source, ident) for source in sources],
            )
        except sqlite3.IntegrityError:
            raise ValueError("capture already belongs to a processing receipt") from None
    return Receipt(ident, workspace, tuple(sorted(sources)), outputs, None)


def receipts(paths: Paths, workspace: str, *, limit: int = MAX_BATCH_COUNT) -> tuple[Receipt, ...]:
    """Bounded pending publication plans for recovery; completed history stays in SQLite."""
    if not 1 <= limit <= MAX_BATCH_COUNT:
        raise ValueError("receipt query exceeds the count bound")
    with db.reader(paths) as con:
        return tuple(
            _receipt(con, row)
            for row in con.execute(
                "SELECT * FROM _enso_memory_receipts WHERE workspace = ? AND completed_at IS NULL "
                "ORDER BY created_at, id LIMIT ?",
                (workspace, limit),
            )
        )


def progress(paths: Paths, workspace: str) -> int:
    with db.reader(paths) as con:
        row = con.execute(
            "SELECT capture_id FROM _enso_memory_progress WHERE workspace = ?", (workspace,)
        ).fetchone()
    return row[0] if row else 0


def complete_receipt(paths: Paths, workspace: str, receipt_id: str) -> None:
    """Acknowledge durable publication and advance only through contiguous handled captures."""
    with db.transaction(paths) as con:
        changed = con.execute(
            "UPDATE _enso_memory_receipts SET completed_at = coalesce(completed_at, ?) "
            "WHERE id = ? AND workspace = ?",
            (db.now(), receipt_id, workspace),
        )
        if not changed.rowcount:
            raise ValueError("receipt does not exist in this workspace")
        first_gap = con.execute(
            "SELECT min(c.id) FROM _enso_captures c "
            "LEFT JOIN _enso_memory_inputs i ON i.capture_id = c.id "
            "LEFT JOIN _enso_memory_receipts r ON r.id = i.receipt_id "
            "WHERE c.workspace = ? AND r.completed_at IS NULL",
            (workspace,),
        ).fetchone()[0]
        high = con.execute(
            "SELECT coalesce(max(id), 0) FROM _enso_captures "
            "WHERE workspace = ? AND (? IS NULL OR id < ?)",
            (workspace, first_gap, first_gap),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO _enso_memory_progress VALUES (?, ?) "
            "ON CONFLICT (workspace) DO UPDATE SET capture_id = excluded.capture_id",
            (workspace, high),
        )
