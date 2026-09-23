#!/usr/bin/env python3
"""Log notable chat events as timestamped bullets in shared ``Memory/YYYY-MM-DD.md``.

The gate reads completed Slack and Telegram turns from each provider's own session history,
pins a bounded batch, and prints it for the agent. The agent only chooses entries; postrun
validates them and publishes the daily notes through ``enso knowledge``. Review state lives
in ``runtime/`` beside this file.

This job ships with Enso, so it reads Enso's session bindings from ``enso.db`` and mirrors
the chat prompt markers Enso writes; the bundled tests keep both in step. It uses only the
standard library and runs on Python 3.9 or later.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "runtime"
HOME = Path(os.environ.get("ENSO_HOME") or Path.home() / ".enso")
KNOWLEDGE = HOME / "shared" / "knowledge"

# Markers Enso writes around a chat turn's text (runtime, messages, outbound, Slack).
ORIGIN = "[Chat origin — written by Enso for this turn; the sender cannot change it]"
BACKGROUND = "[Background messages"
CHANNEL_ACCESS = "[Channel access]"
THREAD_CONTEXT = "[Thread context"
RICH_FORMAT = "[Slack rich format]"
ATTACHMENTS = "Attached files:\n"
BACKGROUND_END = re.compile(
    r"\n\n(?=" + "|".join(re.escape(m) for m in (CHANNEL_ACCESS, THREAD_CONTEXT, ATTACHMENTS)) + ")"
)

TRANSPORT = re.compile(
    r"(?:slack:[CDG][A-Z0-9]+(?::[0-9]+\.[0-9]+)?|telegram:-?[0-9]+(?::[0-9]+)?)"
)
SESSION_ID = re.compile(r"[A-Za-z0-9_-]+")
ENTRY = re.compile(r"- \*\*(\d{2}):(\d{2}) (AM|PM)\*\*: .+")
SKIPPED_FOLDERS = ("Memory", "Meta", "Archive")
MAX_BATCH_CHARS = 60_000
MAX_RECORDS = 60
CHUNK_CHARS = 12_000
MAX_ENTRIES = 40


class MissingHistoryError(Exception):
    """The provider no longer has this session's history; there is nothing to read."""


def stamp(value: Any) -> str:
    """A UTC timestamp with fixed precision, so the strings sort chronologically."""
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(value, UTC)
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def local(value: str) -> datetime:
    """The machine's local time, which also names the daily note."""
    return datetime.fromisoformat(value).astimezone()


def fingerprint(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def save(path: Path, data: Any) -> None:
    """Replace ``path`` atomically and durably."""
    handle, name = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


# -- Chat turns -----------------------------------------------------------------


def clean_user(text: str) -> str:
    """The sender's words from an Enso chat prompt, or empty for anything else.

    Thread history has no unambiguous end marker, so it stays labelled for the reviewer
    rather than being passed off as newly spoken text.
    """
    if not text.startswith(ORIGIN + "\n"):
        return ""
    _, separator, text = text.partition("\n\n")
    if not separator:
        return ""
    text = text.split("\n\n" + RICH_FORMAT + "\n", 1)[0]
    if text.startswith(BACKGROUND):
        # Background sends can span paragraphs; only the next Enso block is a safe end.
        boundary = BACKGROUND_END.search(text)
        if boundary:
            text = text[boundary.end() :]
    while text.startswith((CHANNEL_ACCESS, ATTACHMENTS)):
        _, separator, text = text.partition("\n\n")
        if not separator:
            return ""
    return text.strip()


def sender(text: str) -> str:
    """The origin block's sender: an escaped display name and platform id."""
    for line in text.partition("\n\n")[0].splitlines():
        if line.startswith("Sender: "):
            return line[len("Sender: ") :]
    return ""


def clean_reply(text: str) -> str:
    text = text.strip()
    if text.startswith("```enso-message\n") and text.endswith("```"):
        try:
            envelope = json.loads(text[len("```enso-message\n") : -3])
        except ValueError:
            return text
        if isinstance(envelope, dict) and isinstance(envelope.get("fallback_text"), str):
            return str(envelope["fallback_text"]).strip()
    return text


def unit(native_id: str, user: str, reply: str, started: Any, ended: Any) -> dict[str, Any]:
    return {
        "native_id": native_id,
        "started_at": stamp(started),
        "completed_at": stamp(ended),
        "sender": sender(user),
        "user": clean_user(user),
        "assistant": clean_reply(reply),
    }


# -- Provider history -------------------------------------------------------------


def claude_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and not any(c.get("type") == "tool_result" for c in content):
        return "\n".join(c["text"] for c in content if c.get("type") == "text")
    return ""


def claude_chain(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The active conversation, excluding side agents and abandoned branches."""
    nodes = {
        r["uuid"]: r
        for r in records
        if r.get("uuid") and not r.get("isSidechain") and not r.get("teamName")
    }
    leaves = [
        r
        for r in records
        if r.get("uuid") in nodes and not r.get("isMeta") and r.get("type") in ("user", "assistant")
    ]
    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    current = leaves[-1]["uuid"] if leaves else None
    while current and current in nodes:
        if current in visited:
            raise ValueError("cyclic Claude transcript")
        visited.add(current)
        record = nodes[current]
        chain.append(record)
        current = record.get("parentUuid")
        if not current and record.get("subtype") == "compact_boundary":
            # Follow the bridge to the original turns; the synthetic summary is not new text.
            current = record.get("logicalParentUuid")
    chain.reverse()
    return chain


def claude_units(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    human: dict[str, Any] | None = None
    answers: list[str] = []
    ended = None
    for record in claude_chain(records):
        message = record.get("message", {})
        kind = record.get("type")
        if (
            kind == "user"
            and not record.get("isMeta")
            and not record.get("isCompactSummary")
            and clean_user(claude_text(message))
        ):
            if human is not None and ended is not None:
                units.append(
                    unit(
                        human["uuid"],
                        claude_text(human["message"]),
                        "\n\n".join(answers),
                        human["timestamp"],
                        ended,
                    )
                )
            human, answers, ended = record, [], None
        elif kind == "assistant" and human is not None and message.get("stop_reason") == "end_turn":
            for item in message.get("content", []):
                if (
                    item.get("type") == "text"
                    and (text := clean_reply(item["text"])) not in answers
                ):
                    answers.append(text)
            ended = record["timestamp"]
    if human is not None and ended is not None:
        units.append(
            unit(
                human["uuid"],
                claude_text(human["message"]),
                "\n\n".join(answers),
                human["timestamp"],
                ended,
            )
        )
    return units


def read_claude(session_id: str) -> list[dict[str, Any]]:
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    paths = list(root.glob(f"*/{session_id}.jsonl"))
    if not paths:
        raise MissingHistoryError(session_id)
    if len(paths) > 1:
        raise ValueError("ambiguous Claude session file")
    records = []
    for line in paths[0].read_bytes().splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # a line still being written waits for the next scan
        if line.strip():
            records.append(json.loads(line))
    return claude_units(records)


class CodexReader:
    """Codex's local app-server reads its own paginated history; no model runs."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self.process: subprocess.Popen[str] | None = None
        self.ident = 0
        self.incoming: queue.Queue[dict[str, Any]] = queue.Queue()

    def start(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [self.executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.process = process

        def receive() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    self.incoming.put(json.loads(line))
                except ValueError:
                    continue
            self.incoming.put({"reader_closed": True})

        threading.Thread(target=receive, daemon=True).start()
        client = {"name": "enso_memory", "version": "1"}
        self.call("initialize", {"clientInfo": client, "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized", "params": {}})
        return process

    def send(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method: str, params: dict[str, Any]) -> Any:
        self.ident += 1
        self.send({"id": self.ident, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while True:
            try:
                reply = self.incoming.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError("Codex history read timed out") from None
            if reply.get("reader_closed"):
                raise RuntimeError("Codex history reader exited")
            if reply.get("id") == self.ident:
                if "error" in reply:
                    raise RuntimeError("Codex could not read the session")
                return reply["result"]

    def read(self, session_id: str) -> list[dict[str, Any]]:
        if self.process is None:
            self.start()
        params = {"threadId": session_id, "includeTurns": True}
        units = []
        for turn in self.call("thread/read", params)["thread"]["turns"]:
            if turn["status"] not in ("completed", "interrupted", "failed"):
                continue
            if not turn.get("completedAt"):
                continue
            if turn.get("itemsView") != "full":
                raise RuntimeError("Codex returned incomplete turn history")
            users = [
                "\n".join(c["text"] for c in item["content"] if c.get("type") == "text")
                for item in turn["items"]
                if item["type"] == "userMessage"
            ]
            users = [text for text in users if text.startswith(ORIGIN + "\n")]
            if not users:
                continue
            replies = [
                clean_reply(item["text"])
                for item in turn["items"]
                if item["type"] == "agentMessage" and item.get("phase") in ("final_answer", None)
            ]
            reply = "\n\n".join(dict.fromkeys(replies))
            units.append(
                unit(turn["id"], "\n\n".join(users), reply, turn["startedAt"], turn["completedAt"])
            )
        return units

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=3)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=3)


class Readers:
    """One reader per supported provider; other providers are skipped, not guessed at."""

    SUPPORTED = ("claude", "codex")

    def __init__(self) -> None:
        self.codex: CodexReader | None = None

    def read(self, provider: str, session_id: str) -> list[dict[str, Any]]:
        if provider == "claude":
            return read_claude(session_id)
        if self.codex is None:
            configured = (load(HOME / "config.json") or {}).get("providers", {})
            self.codex = CodexReader(
                os.path.expanduser(configured.get("codex", {}).get("path", "codex"))
            )
        return self.codex.read(session_id)

    def close(self) -> None:
        if self.codex is not None:
            self.codex.close()


# -- Collection -------------------------------------------------------------------


def bindings() -> dict[str, dict[str, Any]]:
    """Enso's current chat sessions, keyed by provider and session id."""
    database = HOME / "enso.db"
    if not database.exists():
        return {}
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT conversation, provider, session_id, workspace, last_active FROM sessions"
        ).fetchall()
    found = {}
    for conversation, provider, session_id, workspace, last_active in rows:
        if TRANSPORT.fullmatch(conversation) and SESSION_ID.fullmatch(session_id):
            found[f"{provider}:{session_id}"] = {
                "provider": provider,
                "session_id": session_id,
                "workspace": workspace,
                "last_active": stamp(last_active),
            }
    return found


def note_paths() -> list[str]:
    if not KNOWLEDGE.is_dir():
        return []
    paths = []
    for path in KNOWLEDGE.rglob("*.md"):
        parts = path.relative_to(KNOWLEDGE).parts
        if parts[0] not in SKIPPED_FOLDERS and not any(p.startswith(".") for p in parts):
            paths.append(path.relative_to(KNOWLEDGE).as_posix())
    return sorted(paths)


def note_candidates(text: str, paths: list[str]) -> list[str]:
    """Existing notes whose whole title appears in the turn; titles are hints, not evidence."""
    text = text.casefold()
    words = set(re.findall(r"[a-z0-9]+", text))
    common = {"the", "as", "a", "and", "for", "of", "to", "in"}
    scored = []
    for path in paths:
        title = Path(path).stem
        terms = set(re.findall(r"[a-z0-9]+", title.casefold())) - common
        exact = path.casefold() in text or (len(title) > 6 and title.casefold() in text)
        overlap = len(terms & words) / max(1, len(terms))
        named = any(not term.isdigit() for term in terms)  # dated notes match any date
        if exact or (named and len(terms) >= 2 and overlap == 1):
            scored.append((2 if exact else overlap, len(terms), path))
    return [path for _, _, path in sorted(scored, reverse=True)[:5]]


def records(
    key: str, binding: dict[str, Any], units: list[dict[str, Any]], start: str, paths: list[str]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """New-evidence records, split into parts, each with its review checkpoint."""
    found = []
    previous = None
    for source in sorted(units, key=lambda u: (u["started_at"], u["native_id"])):
        if not source["user"]:
            continue
        if source["completed_at"] >= start:
            parts = max(
                1, math.ceil(max(len(source["user"]), len(source["assistant"])) / CHUNK_CHARS)
            )
            related = note_candidates(source["user"] + "\n" + source["assistant"], paths)
            for part in range(parts):
                window = slice(part * CHUNK_CHARS, (part + 1) * CHUNK_CHARS)
                data = {
                    "started_at": source["started_at"],
                    "completed_at": source["completed_at"],
                    "user": source["user"][window],
                    "assistant": source["assistant"][window],
                    "part": part + 1,
                    "parts": parts,
                }
                native_id = f"{source['native_id']}:{part + 1}"
                record = {
                    "source_id": fingerprint([key, native_id])[:24],
                    "new": True,
                    "workspace": binding["workspace"],
                    "sender": source["sender"],
                    **data,
                    "related_notes": related,
                }
                if previous:
                    record["prior_context"] = {
                        "user": previous["user"][-1000:],
                        "assistant": previous["assistant"][-1500:],
                    }
                found.append(
                    (record, {"session": key, "native_id": native_id, "digest": fingerprint(data)})
                )
        previous = source
    return found


def forget(state: dict[str, Any], key: str) -> None:
    for section in ("sessions", "reviewed", "read"):
        state[section].pop(key, None)


def collect(
    state: dict[str, Any],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, Any]]:
    """Unreviewed completed turns across known sessions, oldest first.

    A session is reread only after Enso records new activity in it. Sessions Enso has
    since forgotten are kept until their remaining turns are reviewed.
    """
    current = bindings()
    state["sessions"].update(current)
    paths: list[str] | None = None
    stats: dict[str, Any] = {"sessions_read": 0, "unsupported": 0, "failed": 0}
    candidates = []
    readers = Readers()
    try:
        for key, binding in list(state["sessions"].items()):
            bound = key in current
            if (
                binding["last_active"] < state["start_at"]
                or state["read"].get(key) == binding["last_active"]
            ):
                if not bound:
                    forget(state, key)
                continue
            if binding["provider"] not in Readers.SUPPORTED:
                stats["unsupported"] += 1
                if bound:
                    state["read"][key] = binding["last_active"]
                else:
                    forget(state, key)
                continue
            try:
                units = readers.read(binding["provider"], binding["session_id"])
            except MissingHistoryError:
                units = []
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                stats["failed"] += 1
                reason = f"{binding['provider']} session ({type(error).__name__})"
                print(f"memory: could not read a {reason}", file=sys.stderr)
                continue
            stats["sessions_read"] += 1
            reviewed = state["reviewed"].get(key, {})
            paths = note_paths() if paths is None else paths
            found = [
                pair
                for pair in records(key, binding, units, state["start_at"], paths)
                if reviewed.get(pair[1]["native_id"]) != pair[1]["digest"]
            ]
            candidates.extend(found)
            if found:
                continue
            if bound:
                state["read"][key] = binding["last_active"]
            else:
                forget(state, key)
    finally:
        readers.close()
    candidates.sort(key=lambda pair: (pair[0]["completed_at"], pair[0]["source_id"]))
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    size = 0
    for record, checkpoint in candidates:
        length = len(json.dumps(record, ensure_ascii=False))
        if selected and (size + length > MAX_BATCH_CHARS or len(selected) >= MAX_RECORDS):
            break
        selected.append((record, checkpoint))
        size += length
    stats.update(unreviewed=len(candidates), selected=len(selected), checked_at=stamp(time.time()))
    return selected, stats


# -- Daily notes ------------------------------------------------------------------


def knowledge(*args: str, body: str | None = None) -> dict[str, Any]:
    result = subprocess.run(
        ["enso", "knowledge", *args, "--shared", "--json"],
        input=body,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"enso knowledge {args[0]} failed for {args[1]}")
    return dict(json.loads(result.stdout))


def read_note(path: str) -> dict[str, Any] | None:
    return knowledge("show", path) if (KNOWLEDGE / path).is_file() else None


def write_note(path: str, body: str, prior: dict[str, Any] | None) -> None:
    if prior is None:
        knowledge("create", path, "--file", "-", body=body)
    else:
        knowledge("update", path, "--file", "-", "--expected-hash", prior["sha256"], body=body)


def clock(moment: datetime) -> str:
    """``03:17 PM`` without depending on the locale's AM/PM names."""
    hour = moment.hour % 12 or 12
    return f"{hour:02d}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def minutes(line: str) -> int:
    match = ENTRY.fullmatch(line)
    assert match is not None
    hour, minute, period = match.groups()
    return (int(hour) % 12 + (12 if period == "PM" else 0)) * 60 + int(minute)


def merge(body: str, additions: list[str]) -> str:
    """Add new entries in time order, keeping anyone's edits to the note.

    A note holding only entries is kept sorted. Other content stays where it is and new
    entries follow it.
    """
    lines = body.splitlines()
    new = [line for line in dict.fromkeys(additions) if line not in lines]
    content = [line for line in lines if line.strip()]
    if all(ENTRY.fullmatch(line) for line in content):
        return "\n".join(sorted(content + new, key=minutes)) + "\n"
    return "\n".join([*body.rstrip("\n").splitlines(), *new]) + "\n"


def existing_entries(batch_records: list[dict[str, Any]], start: str) -> dict[str, str]:
    days = sorted(
        {
            local(record[moment]).date().isoformat()
            for record in batch_records
            for moment in ("started_at", "completed_at")
            if record[moment] >= start
        }
    )
    return {day: (read_note(f"Memory/{day}.md") or {}).get("body", "") for day in days}


# -- Review results ---------------------------------------------------------------


def parse_result(output: str) -> Any:
    """The agent's JSON object, tolerating a Markdown fence around it."""
    text = output.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(text)


def validate(result: Any, batch: dict[str, Any]) -> list[dict[str, str]]:
    """Turn the agent's picks into note lines; any mistake is returned to the agent."""
    if not isinstance(result, dict) or set(result) != {"version", "batch_id", "entries"}:
        raise ValueError("return only version, batch_id, and entries")
    if result["version"] != 1 or result["batch_id"] != batch["batch_id"]:
        raise ValueError("copy version 1 and the current batch_id")
    if not isinstance(result["entries"], list) or len(result["entries"]) > MAX_ENTRIES:
        raise ValueError(f"entries must be a list of at most {MAX_ENTRIES}")
    sources = {record["source_id"]: record for record in batch["records"]}
    return [entry_line(entry, sources, batch["start_at"]) for entry in result["entries"]]


def entry_line(entry: Any, sources: dict[str, dict[str, Any]], start: str) -> dict[str, str]:
    if not isinstance(entry, dict) or set(entry) != {"source_id", "moment", "text", "notes"}:
        raise ValueError("each entry needs exactly source_id, moment, text, and notes")
    source = sources.get(entry["source_id"]) if isinstance(entry["source_id"], str) else None
    if source is None:
        raise ValueError("an entry cites an unknown source_id")
    if entry["moment"] not in ("user", "assistant"):
        raise ValueError("moment must be user or assistant")
    at = source["started_at" if entry["moment"] == "user" else "completed_at"]
    if at < start or not source[entry["moment"]]:
        raise ValueError("the chosen moment has no new text in that record")
    text = entry["text"]
    if (
        not isinstance(text, str)
        or not 10 <= len(text) <= 900
        or text != text.strip()
        or any(ord(c) < 32 for c in text)
        or re.search(r"https?://|\[|\]|<|>", text)
    ):
        raise ValueError("text must be one plain sentence without links or control characters")
    notes = entry["notes"]
    if not isinstance(notes, list) or len(notes) > 3:
        raise ValueError("notes must list at most three paths")
    links = []
    for path in dict.fromkeys(notes):
        if path not in source["related_notes"]:
            raise ValueError("a note path was not offered for that record")
        if not (KNOWLEDGE / path).is_file():
            raise ValueError(f"{path} has moved; omit that link")
        links.append(f"[[shared:{path[:-3]}|{Path(path).stem}]]")
    when = local(at)
    suffix = " (" + "; ".join(links) + ")" if links else ""
    return {"day": when.date().isoformat(), "line": f"- **{clock(when)}**: {text}{suffix}"}


# -- Hooks ------------------------------------------------------------------------


def new_state() -> dict[str, Any]:
    """Start with today's chats: no archive dump on first install."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "version": 1,
        "start_at": stamp(midnight.isoformat()),
        "sessions": {},
        "reviewed": {},
        "read": {},
    }


def load_state() -> dict[str, Any]:
    state = load(STATE / "state.json") or new_state()
    if state.get("version") != 1:
        raise ValueError("runtime/state.json has an unsupported version")
    return dict(state)


def publish(journal: dict[str, Any], state: dict[str, Any]) -> None:
    """Replay a saved publication: notes first, then the review checkpoints."""
    days: dict[str, list[str]] = {}
    for entry in journal["entries"]:
        days.setdefault(entry["day"], []).append(entry["line"])
    for day, lines in sorted(days.items()):
        path = f"Memory/{day}.md"
        prior = read_note(path)
        before = prior["body"] if prior else ""
        after = merge(before, lines)
        if after != before:
            write_note(path, after, prior)
    for point in journal["checkpoints"]:
        state["reviewed"].setdefault(point["session"], {})[point["native_id"]] = point["digest"]
    save(STATE / "state.json", state)
    (STATE / "pending.json").unlink(missing_ok=True)
    (STATE / "journal.json").unlink()


def gate() -> int:
    state = load_state()
    if (journal := load(STATE / "journal.json")) is not None:
        publish(journal, state)
    batch = load(STATE / "pending.json")
    if batch is None:
        selected, stats = collect(state)
        save(STATE / "state.json", state)
        save(STATE / "last-scan.json", stats)
        if not selected:
            if stats["failed"]:
                print(
                    f"ENSO_ERROR: memory could not read {stats['failed']} chat session(s)",
                    file=sys.stderr,
                )
                return 2
            return 1
        chosen = [record for record, _ in selected]
        checkpoints = [checkpoint for _, checkpoint in selected]
        batch = {
            "version": 1,
            "batch_id": fingerprint(checkpoints)[:24],
            "start_at": state["start_at"],
            "records": chosen,
            "checkpoints": checkpoints,
            "existing_daily_entries": existing_entries(chosen, state["start_at"]),
        }
    batch["run_id"] = os.environ.get("ENSO_RUN_ID", "manual")
    save(STATE / "pending.json", batch)
    shown = {key: batch[key] for key in ("batch_id", "records", "existing_daily_entries")}
    print(json.dumps(shown, ensure_ascii=False, separators=(",", ":")))
    return 0


def postrun() -> int:
    if os.environ.get("ENSO_RUN_STATUS") != "ok":
        return 0  # the pinned batch waits for the next run
    state = load_state()
    if (journal := load(STATE / "journal.json")) is not None:
        publish(journal, state)
        return 0
    batch = load(STATE / "pending.json")
    if not batch or batch["run_id"] != os.environ.get("ENSO_RUN_ID"):
        raise RuntimeError("no pinned batch for this run")
    try:
        entries = validate(parse_result(sys.stdin.read()), batch)
    except (ValueError, TypeError, KeyError) as error:
        print(f"Result rejected: {error}. Return corrected JSON only for {batch['batch_id']}.")
        return 10
    journal = {
        "batch_id": batch["batch_id"],
        "entries": entries,
        "checkpoints": batch["checkpoints"],
    }
    save(STATE / "journal.json", journal)
    publish(journal, state)
    print(json.dumps({"entries_written": len(entries), "records_reviewed": len(batch["records"])}))
    return 0


def status() -> int:
    state = load(STATE / "state.json") or {}
    summary = {
        "start_at": state.get("start_at"),
        "sessions": len(state.get("sessions", {})),
        "reviewed_turns": sum(len(turns) for turns in state.get("reviewed", {}).values()),
        "pending_batch": (load(STATE / "pending.json") or {}).get("batch_id"),
        "publication_pending": (STATE / "journal.json").exists(),
        "last_scan": load(STATE / "last-scan.json"),
    }
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Enso's daily chat memory log.")
    parser.add_argument("mode", choices=("gate", "postrun", "status"))
    mode = parser.parse_args().mode
    if mode == "status":
        return status()
    os.umask(0o077)
    STATE.mkdir(exist_ok=True)
    try:
        with open(STATE / ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return gate() if mode == "gate" else postrun()
    except Exception as error:
        # Never quote transcript text or subprocess output: either can hold private data.
        print(
            f"ENSO_ERROR: memory {mode} failed ({type(error).__name__}): {error}", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
