"""Source-backed episodic memory, independent of Markdown knowledge.

Capture saves only the conversation Enso actually handles. Refinement commits summaries
and source acknowledgements together; a model can never supply event dates or origin.
Read APIs share one bounded, read-only query path for the CLI and viewer.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db
from .config import Config, Paths

MAX_TEXT_BYTES = 64 * 1024
MAX_BATCH_BYTES = 48 * 1024
MAX_EXCERPT_BYTES = 4 * 1024
MAX_BATCH_TURNS = 40
MAX_SUMMARY_CHARS = 2000
MAX_PAGE = 100
_BATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class MemoryError(ValueError):
    """A memory request or refinement does not satisfy its source contract."""


@dataclass(frozen=True)
class TurnRecord:
    id: int
    conversation: str
    workspace: str
    provider: str
    model: str
    effort: str
    transport: str
    channel: str
    channel_name: str
    thread: str | None
    message_id: str
    user_id: str
    user_name: str
    request: str
    response: str
    files: tuple[str, ...]
    received_at: str
    completed_at: str | None
    session_id: str | None
    status: str
    error: str
    request_truncated: bool
    response_truncated: bool
    processed_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Entry:
    id: int
    batch_id: str
    summary: str
    occurred_at: str
    ended_at: str
    created_at: str
    workspace: str
    transport: str
    channel: str
    channel_name: str
    thread: str | None
    conversation: str
    source_count: int

    @property
    def ref(self) -> str:
        return f"MEM-{self.id:06d}"

    def as_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, **asdict(self)}


@dataclass(frozen=True)
class MemoryPage:
    entries: tuple[Entry, ...]
    total: int


@dataclass(frozen=True)
class Batch:
    id: str
    turns: tuple[TurnRecord, ...]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_enabled(config: Config, workspace: str) -> bool:
    """One switch stops future capture/refinement while keeping existing recall available."""
    return config.memory.enabled


def _bounded(text: str, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore"), len(encoded) > limit


def _instant(value: str) -> str:
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError) as exc:
        raise MemoryError("timestamps must be ISO timestamps with a timezone") from exc


def start_turn(
    paths: Paths,
    *,
    conversation: str,
    workspace: str,
    provider: str,
    model: str,
    effort: str,
    transport: str,
    channel: str,
    channel_name: str,
    thread: str | None,
    message_id: str,
    user_id: str,
    user_name: str,
    request: str,
    files: Sequence[str],
    received_at: str,
) -> int | None:
    """Save accepted input once; a repeated transport delivery never replaces its source."""
    context = (
        conversation,
        workspace,
        provider,
        model,
        effort,
        transport,
        channel,
        channel_name,
        thread or "",
        message_id,
        user_id,
        user_name,
    )
    if any(len(value) > 1024 for value in context) or len(json.dumps(context).encode()) > 8192:
        raise MemoryError("conversation context exceeds the capture limit")
    text, truncated = _bounded(request)
    with db.transaction(paths) as con:
        cursor = con.execute(
            """INSERT INTO _enso_memory_turns
            (conversation,workspace,provider,model,effort,transport,channel,channel_name,
             thread,message_id,user_id,user_name,request,files,received_at,request_truncated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (
                conversation,
                workspace,
                provider,
                model,
                effort,
                transport,
                channel,
                channel_name,
                thread,
                message_id,
                user_id,
                user_name,
                text,
                json.dumps(list(files)),
                _instant(received_at),
                truncated,
            ),
        )
        return cursor.lastrowid if cursor.rowcount else None


def finish_turn(
    paths: Paths,
    turn_id: int,
    *,
    response: str = "",
    status: str = "ok",
    error: str = "",
    session_id: str | None = None,
) -> None:
    """Finalize a captured source, including interrupted turns, without storing tool output."""
    if status not in {"ok", "error", "timeout", "stopped"}:
        raise MemoryError("invalid conversation outcome")
    text, truncated = _bounded(response)
    with db.transaction(paths) as con:
        con.execute(
            """UPDATE _enso_memory_turns
            SET response=?,status=?,error=?,session_id=?,completed_at=?,response_truncated=?
            WHERE id=? AND completed_at IS NULL""",
            (text, status, _bounded(error, 2000)[0], session_id, db.now(), truncated, turn_id),
        )


def recover_interrupted(paths: Paths) -> int:
    """Called only before the daemon accepts work; retain the input of interrupted turns."""
    with db.transaction(paths) as con:
        return con.execute(
            """UPDATE _enso_memory_turns SET status='stopped',completed_at=?,
            error='Enso stopped before this conversation finished.' WHERE completed_at IS NULL""",
            (db.now(),),
        ).rowcount


def _turn(row: sqlite3.Row) -> TurnRecord:
    fields = dict(row)
    fields["files"] = tuple(json.loads(fields["files"]))
    for name in ("request_truncated", "response_truncated"):
        fields[name] = bool(fields[name])
    return TurnRecord(**fields)


@contextmanager
def _reader(paths: Paths) -> Iterator[sqlite3.Connection | None]:
    """An absent/older database is empty memory, never a reason for a read to migrate it."""
    try:
        with db.reader(paths) as con:
            present = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_enso_memories'"
            ).fetchone()
            yield con if present else None
    except db.MissingDatabaseError:
        yield None


_ENTRY_SELECT = """SELECT m.*, (SELECT count(*) FROM _enso_memory_sources s
                   WHERE s.memory_id=m.id) AS source_count FROM _enso_memories m"""


def _entry_id(ref: str) -> int:
    match = re.fullmatch(r"(?:MEM-)?([0-9]+)", str(ref), re.IGNORECASE)
    if not match or len(match[1]) > 18 or int(match[1]) < 1:
        raise MemoryError("memory must be a reference such as MEM-000001")
    return int(match[1])


def _entries(con: sqlite3.Connection, clause: str, params: Sequence[object]) -> tuple[Entry, ...]:
    return tuple(Entry(**dict(row)) for row in con.execute(_ENTRY_SELECT + clause, params))


def list_entries(
    paths: Paths,
    *,
    workspace: str | None = None,
    transport: str | None = None,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    query: str = "",
    limit: int = 20,
    offset: int = 0,
) -> MemoryPage:
    """Newest event time first, with exact scope filters and literal summary substring search."""
    if (
        type(limit) is not int
        or not 1 <= limit <= MAX_PAGE
        or type(offset) is not int
        or not 0 <= offset <= 2**63 - 1
    ):
        raise MemoryError(
            f"limit must be 1-{MAX_PAGE}; offset must be a non-negative 64-bit integer"
        )
    if len(query) > 500:
        raise MemoryError("search is limited to 500 characters")
    clauses: list[str] = []
    params: list[object] = []
    for key, value in (("workspace", workspace), ("transport", transport), ("channel", channel)):
        if value:
            clauses.append(f"m.{key}=?")
            params.append(value)
    if since:
        since = _instant(since)
        clauses.append("m.occurred_at>=?")
        params.append(since)
    if until:
        until = _instant(until)
        clauses.append("m.occurred_at<?")
        params.append(until)
    if since and until and since >= until:
        raise MemoryError("since must be before until")
    if query.strip():
        clauses.append("m.summary LIKE ? ESCAPE '\\'")
        params.append(
            "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _reader(paths) as con:
        if con is None:
            return MemoryPage((), 0)
        # The count and rows share a snapshot while capture/refinement continues in WAL.
        con.execute("BEGIN")
        total = con.execute("SELECT count(*) FROM _enso_memories m" + where, params).fetchone()[0]
        entries = _entries(
            con,
            where + " ORDER BY m.occurred_at DESC,m.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return MemoryPage(entries, total)


def get_entry(paths: Paths, ref: str) -> Entry | None:
    ident = _entry_id(ref)
    with _reader(paths) as con:
        rows = _entries(con, " WHERE m.id=?", (ident,)) if con is not None else ()
        return rows[0] if rows else None


def sources(paths: Paths, ref: str) -> tuple[TurnRecord, ...]:
    ident = _entry_id(ref)
    with _reader(paths) as con:
        if con is None:
            return ()
        return tuple(
            _turn(row)
            for row in con.execute(
                """SELECT t.* FROM _enso_memory_turns t JOIN _enso_memory_sources s
            ON s.turn_id=t.id WHERE s.memory_id=? ORDER BY t.received_at,t.id LIMIT ?""",
                (ident, MAX_BATCH_TURNS),
            )
        )


def facets(paths: Paths) -> dict[str, Any]:
    with _reader(paths) as con:
        result: dict[str, Any] = {
            key + "s": [
                row[0]
                for row in con.execute(f"SELECT DISTINCT {key} FROM _enso_memories ORDER BY {key}")
            ]
            if con is not None
            else []
            for key in ("workspace", "transport", "channel")
        }
        names: dict[str, str] = {}
        if con is not None:
            for row in con.execute(
                """SELECT channel,channel_name FROM (
                SELECT channel,channel_name,row_number() OVER (
                    PARTITION BY channel ORDER BY occurred_at DESC,id DESC) AS position
                FROM _enso_memories WHERE channel_name != '') WHERE position=1"""
            ):
                names[row["channel"]] = row["channel_name"]
        result["channel_names"] = names
        return result


def timezone_info(name: str) -> tzinfo | None:
    """None tells datetime.astimezone to use the host's zone at that instant."""
    if name == "local":
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise MemoryError("timezone must be local or an IANA timezone") from exc


def parse_since(
    value: str | None, timezone: str = "local", now: datetime | None = None
) -> str | None:
    """Resolve calendar starts (Monday weeks), durations, dates and timezone-aware instants."""
    if not value:
        return None
    zone = timezone_info(timezone)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise MemoryError("current time must have a timezone")
    value = value.strip()
    relative = re.fullmatch(r"([1-9][0-9]{0,5})([hdw])", value)
    if relative:
        hours = int(relative[1]) * {"h": 1, "d": 24, "w": 168}[relative[2]]
        try:
            return (current.astimezone(UTC) - timedelta(hours=hours)).isoformat(
                timespec="microseconds"
            )
        except OverflowError as exc:
            raise MemoryError("date range is too large") from exc
    local = current.astimezone(zone).replace(tzinfo=None)
    if value in {"today", "week", "month"}:
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if value == "week":
            local -= timedelta(days=local.weekday())
        elif value == "month":
            local = local.replace(day=1)
    else:
        try:
            local = datetime.fromisoformat(value)
        except ValueError as exc:
            raise MemoryError("use today, week, month, 7d, 24h, an ISO date or timestamp") from exc
        if local.tzinfo is not None:
            return _instant(value)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise MemoryError("timestamps must include a timezone; dates use memory.timezone")
    try:
        return local.replace(tzinfo=zone).astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, OverflowError) as exc:
        raise MemoryError("date is outside the supported range") from exc


def _batch_id(value: str) -> str:
    if not isinstance(value, str) or not _BATCH_ID.fullmatch(value):
        raise MemoryError("invalid memory batch identifier")
    return value


def _batch_turns(con: sqlite3.Connection, ids: Sequence[int]) -> tuple[TurnRecord, ...]:
    if not ids:
        return ()
    placeholders = ",".join("?" for _ in ids)
    return tuple(
        _turn(row)
        for row in con.execute(
            f"SELECT * FROM _enso_memory_turns WHERE id IN ({placeholders}) "
            "ORDER BY received_at,id",
            ids,
        )
    )


def _excerpt(text: str, limit: int) -> tuple[str, bool]:
    """Keep both the request's introduction and the reply's conclusion when reducing input."""
    raw = text.encode()
    if len(raw) <= limit:
        return text, False
    half = limit // 2
    return (
        raw[:half].decode(errors="ignore")
        + "\n[... excerpt omitted ...]\n"
        + raw[-half:].decode(errors="ignore")
    ), True


def _batch_preview(turn: TurnRecord) -> TurnRecord:
    """Bound model evidence, separately from retained source text and its permanent identity."""
    limit = MAX_EXCERPT_BYTES
    while True:
        request, request_cut = _excerpt(turn.request, limit)
        response, response_cut = _excerpt(turn.response, limit)
        result = replace(
            turn,
            request=request,
            response=response,
            request_truncated=turn.request_truncated or request_cut,
            response_truncated=turn.response_truncated or response_cut,
            # The summarizer never opens attachments; the source detail retains all paths.
            files=(),
        )
        if len(json.dumps(result.as_dict(), indent=2).encode()) < MAX_BATCH_BYTES - 1024:
            return result
        if limit <= 128:
            raise MemoryError("conversation context is too large for a memory batch")
        limit //= 2


def prepare_batch(paths: Paths, *, batch_id: str, limit: int = MAX_BATCH_TURNS) -> Batch | None:
    """Freeze bounded source IDs; abandoned batches never hide inputs from the next job."""
    _batch_id(batch_id)
    if isinstance(limit, bool) or not 1 <= limit <= MAX_BATCH_TURNS:
        raise MemoryError(f"batch limit must be 1-{MAX_BATCH_TURNS}")
    with db.transaction(paths) as con:
        row = con.execute("SELECT * FROM _enso_memory_batches WHERE id=?", (batch_id,)).fetchone()
        if row:
            if row["recorded_at"]:
                return None
            ids = json.loads(row["turn_ids"])
            turns = _batch_turns(con, ids)
            if len(turns) != len(ids) or any(turn.processed_at for turn in turns):
                raise MemoryError(
                    "batch sources were already processed or forgotten; prepare a new batch"
                )
            return Batch(batch_id, tuple(_batch_preview(turn) for turn in turns), row["created_at"])
        selected: list[TurnRecord] = []
        created = db.now()
        for row in con.execute(
            """SELECT * FROM _enso_memory_turns WHERE processed_at IS NULL
            AND completed_at IS NOT NULL ORDER BY received_at,id LIMIT ?""",
            (limit,),
        ):
            turn = _batch_preview(_turn(row))
            candidate = Batch(batch_id, (*selected, turn), created)
            if len(json.dumps(candidate.as_dict(), indent=2).encode()) > MAX_BATCH_BYTES:
                break
            selected.append(turn)
        if not selected:
            return None
        con.execute(
            "INSERT INTO _enso_memory_batches(id,created_at,turn_ids) VALUES (?,?,?)",
            (batch_id, created, json.dumps([turn.id for turn in selected])),
        )
        # Obsolete, unrecorded batches carry only IDs and are safe to replace. Successful
        # receipts stay for idempotency; raw conversation text is never copied into a batch.
        con.execute(
            "DELETE FROM _enso_memory_batches WHERE recorded_at IS NULL AND id != ?", (batch_id,)
        )
        return Batch(batch_id, tuple(selected), created)


def _payload(entries: list[dict[str, Any]], allowed: set[int]) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or len(entries) > MAX_BATCH_TURNS:
        raise MemoryError(f"record a JSON array of at most {MAX_BATCH_TURNS} memories")
    result: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"summary", "source_ids"}:
            raise MemoryError("each memory must contain only summary and source_ids")
        summary, ids = item["summary"], item["source_ids"]
        if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY_CHARS:
            raise MemoryError(f"summary must be 1-{MAX_SUMMARY_CHARS} characters")
        if (
            not isinstance(ids, list)
            or not ids
            or len(ids) > MAX_BATCH_TURNS
            or any(type(ident) is not int or ident not in allowed for ident in ids)
            or len(set(ids)) != len(ids)
        ):
            raise MemoryError("source_ids must be distinct IDs from this batch")
        result.append({"summary": summary.strip(), "source_ids": sorted(ids)})
    if len({json.dumps(item, sort_keys=True) for item in result}) != len(result):
        raise MemoryError("duplicate memories in one batch")
    return result


def _origin(turn: TurnRecord) -> tuple[str, ...]:
    return turn.workspace, turn.conversation, turn.transport, turn.channel, turn.thread or ""


def record_batch(paths: Paths, batch_id: str, entries: list[dict[str, Any]]) -> tuple[Entry, ...]:
    """Validate model prose and source links, then publish and acknowledge in one transaction."""
    _batch_id(batch_id)
    with db.transaction(paths) as con:
        batch = con.execute("SELECT * FROM _enso_memory_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise MemoryError("no prepared batch with that ID")
        ids = json.loads(batch["turn_ids"])
        payload = _payload(entries, set(ids))
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if batch["recorded_at"]:
            if batch["payload_hash"] != digest:
                raise MemoryError("this batch was already recorded with different memories")
            return _entries(con, " WHERE m.batch_id=? ORDER BY m.id", (batch_id,))
        turns = {turn.id: turn for turn in _batch_turns(con, ids)}
        if len(turns) != len(ids) or any(turn.processed_at for turn in turns.values()):
            raise MemoryError("batch sources were already processed or forgotten")
        stamp = db.now()
        for item in payload:
            source = sorted(
                (turns[ident] for ident in item["source_ids"]),
                key=lambda turn: (turn.received_at, turn.id),
            )
            first = source[0]
            if any(_origin(turn) != _origin(first) for turn in source):
                raise MemoryError(
                    "one memory must stay within one workspace and conversation location"
                )
            ended = max(turn.completed_at or turn.received_at for turn in source)
            cursor = con.execute(
                """INSERT INTO _enso_memories
                (batch_id,summary,occurred_at,ended_at,created_at,workspace,transport,
                 channel,channel_name,thread,conversation) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    item["summary"],
                    first.received_at,
                    ended,
                    stamp,
                    first.workspace,
                    first.transport,
                    first.channel,
                    first.channel_name,
                    first.thread,
                    first.conversation,
                ),
            )
            con.executemany(
                "INSERT INTO _enso_memory_sources(memory_id,turn_id) VALUES (?,?)",
                [(cursor.lastrowid, turn.id) for turn in source],
            )
        con.executemany(
            "UPDATE _enso_memory_turns SET processed_at=? WHERE id=?",
            [(stamp, ident) for ident in ids],
        )
        con.execute(
            "UPDATE _enso_memory_batches SET recorded_at=?,payload_hash=? WHERE id=?",
            (stamp, digest, batch_id),
        )
        return _entries(con, " WHERE m.batch_id=? ORDER BY m.id", (batch_id,))


def batch_status(paths: Paths, batch_id: str) -> dict[str, Any]:
    _batch_id(batch_id)
    with _reader(paths) as con:
        row = (
            con.execute("SELECT * FROM _enso_memory_batches WHERE id=?", (batch_id,)).fetchone()
            if con is not None
            else None
        )
        if row is None or con is None:
            raise MemoryError("no prepared batch with that ID")
        count = con.execute(
            "SELECT count(*) FROM _enso_memories WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        return {
            "id": batch_id,
            "status": "recorded" if row["recorded_at"] else "pending",
            "turn_ids": json.loads(row["turn_ids"]),
            "entry_count": count,
        }


def status(paths: Paths) -> dict[str, Any]:
    result: dict[str, Any] = {
        "turns": 0,
        "pending_turns": 0,
        "entries": 0,
        "last_recorded_at": None,
    }
    with _reader(paths) as con:
        if con is None:
            return result
        result["turns"] = con.execute("SELECT count(*) FROM _enso_memory_turns").fetchone()[0]
        result["pending_turns"] = con.execute("""SELECT count(*) FROM _enso_memory_turns
            WHERE completed_at IS NOT NULL AND processed_at IS NULL""").fetchone()[0]
        result["entries"] = con.execute("SELECT count(*) FROM _enso_memories").fetchone()[0]
        result["last_recorded_at"] = con.execute(
            "SELECT max(recorded_at) FROM _enso_memory_batches"
        ).fetchone()[0]
    return result


def forget(paths: Paths, ref: str) -> bool:
    """Erase the connected set of summaries and shared source text; never leave hidden copies."""
    ident = _entry_id(ref)
    with db.transaction(paths) as con:
        if not con.execute("SELECT 1 FROM _enso_memories WHERE id=?", (ident,)).fetchone():
            return False
        # Traverse both sides: one exchange may support multiple summaries and a summary
        # may combine several exchanges. Deleting only one side would retain forgotten text.
        rows = con.execute(
            """WITH RECURSIVE related(id) AS (
            SELECT ? UNION SELECT b.memory_id FROM related r
            JOIN _enso_memory_sources a ON a.memory_id=r.id
            JOIN _enso_memory_sources b ON b.turn_id=a.turn_id)
            SELECT DISTINCT s.turn_id FROM related r
            JOIN _enso_memory_sources s ON s.memory_id=r.id""",
            (ident,),
        ).fetchall()
        ids = [row[0] for row in rows]
        for turn_id in ids:
            con.execute(
                """DELETE FROM _enso_memories WHERE id IN
                (SELECT memory_id FROM _enso_memory_sources WHERE turn_id=?)""",
                (turn_id,),
            )
        con.execute("DELETE FROM _enso_memories WHERE id=?", (ident,))
        con.executemany("DELETE FROM _enso_memory_turns WHERE id=?", [(item,) for item in ids])
        # Batch receipts contain only IDs and hashes. Keeping them prevents a delayed
        # retry from regenerating the forgotten summaries from its old model output.
        return True
