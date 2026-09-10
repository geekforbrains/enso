"""Shared bounded subprocesses, advisory locks, and provider turns for background work."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import signal
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from . import log as logctx
from .providers import BaseProvider, SessionIdError
from .runs import OUTPUT_KEEP
from .runtime import terminate_process_tree

log = logging.getLogger(__name__)

DRAIN_SECONDS = 5.0
DIAGNOSTIC_LIMIT = 500
NOTIFY_LIMIT = 4000

READ_CHUNK = 64 * 1024
LINE_KEEP = 10 * 1024 * 1024
DIAGNOSTIC_KEEP = 2000


@dataclass(frozen=True)
class ProviderTurn:
    """One provider response and its validated, resumable session identity, if known."""

    status: Literal["ok", "error", "timeout"]
    output: str = ""
    error: str = ""
    exit_code: int | None = None
    session_id: str | None = None


def _tail(text: str, keep: int) -> str:
    """Keep a UTF-8 tail without exceeding the byte limit or splitting a character."""
    return text[-keep:].encode()[-keep:].decode(errors="ignore")


class _ProtocolError(ValueError):
    """A provider's stream cannot safely be interpreted as the requested turn."""


class _Collected:
    """Keep the final answer separately from early session events and diagnostics."""

    def __init__(self, provider: BaseProvider, expected_id: str | None, *, new_session: bool):
        self.provider = provider
        self.expected_id = expected_id
        # An assigned id becomes resumable only after the adapter recognizes a CLI event.
        self.session_id = None if new_session else expected_id
        self.events = 0
        self.output = ""
        self.error = ""
        self.unparsed = ""
        self.stderr = b""

    def feed(self, line: bytes) -> None:
        text = line.decode(errors="replace").strip()
        if not text:
            return
        try:
            raw = self.provider.parse_line(text)
            events = self.provider.parse_event(raw) if raw is not None else []
            if not events:
                self.unparsed = _tail(self.unparsed + text + "\n", DIAGNOSTIC_KEEP)
                return
            self.events += len(events)
            if self.expected_id and not self.session_id:
                self.session_id = self.expected_id
            for event in events:
                if event.kind == "session" and event.session_id is not None:
                    announced = self.provider.check_session_id(event.session_id)
                    if self.expected_id is not None and announced != self.expected_id:
                        raise _ProtocolError(
                            f"{self.provider.name} announced a different session from the "
                            "one requested; refusing to change sessions"
                        )
                    self.expected_id = self.session_id = announced
                elif event.kind == "response":
                    # Adapters emit complete answers; format_response keeps the last one.
                    # Retaining every event would let a long job grow memory indefinitely.
                    self.output = _tail(self.provider.format_response([event.text]), OUTPUT_KEEP)
                elif event.kind == "error":
                    self.error = _tail(event.text, DIAGNOSTIC_KEEP) or "provider reported an error"
        except SessionIdError as exc:
            raise _ProtocolError(str(exc)) from exc
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, _ProtocolError):
                raise
            raise _ProtocolError(f"invalid {self.provider.name} event: {exc}") from exc


async def _read_stdout(stream: asyncio.StreamReader, collected: _Collected) -> None:
    """Parse complete lines while bounding even a provider that never emits a newline."""
    buffer = bytearray()
    while chunk := await stream.read(READ_CHUNK):
        buffer.extend(chunk)
        start = 0
        while (end := buffer.find(b"\n", start)) >= 0:
            if end - start > LINE_KEEP:
                raise _ProtocolError(f"provider event exceeds {LINE_KEEP} bytes")
            collected.feed(bytes(buffer[start:end]))
            start = end + 1
        if start:
            del buffer[:start]
        if len(buffer) > LINE_KEEP:
            raise _ProtocolError(f"provider event exceeds {LINE_KEEP} bytes")
    if buffer:
        collected.feed(bytes(buffer))


async def _read_stderr(stream: asyncio.StreamReader, collected: _Collected) -> None:
    while chunk := await stream.read(READ_CHUNK):
        collected.stderr = (collected.stderr + chunk)[-DIAGNOSTIC_KEEP:]


async def terminate_background_process(process: asyncio.subprocess.Process, label: str) -> None:
    """Stop a subprocess group, including descendants whose leader already exited."""
    await terminate_process_tree(process, label)
    # The group leader can exit before a descendant closes an inherited output pipe.
    # Kill any survivors too, including when the leader had already exited at timeout.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


async def execute_turn(
    provider: BaseProvider,
    prompt: str,
    model: str,
    effort: str,
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    session_id: str | None = None,
) -> ProviderTurn:
    """Run one structured turn, creating or strictly resuming the supplied session.

    No fresh-session fallback is attempted. A successful first turn from a CLI that never
    announces its own id can have no resumable session; the caller decides whether it needs
    another turn. Cancellation terminates the process group before propagating.
    """
    if timeout <= 0:
        return ProviderTurn(
            "timeout", error="provider time budget exhausted", session_id=session_id
        )
    started = time.monotonic()
    new_session = session_id is None and provider.assigns_session_id
    try:
        if new_session:
            session_id = str(uuid.uuid4())
        if session_id is not None:
            provider.check_session_id(session_id)
        cmd = provider.command(
            prompt,
            model,
            effort,
            args,
            session_id=session_id,
            new_session=new_session,
            batch=False,
            cwd=str(cwd),
        )
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
            if provider.stderr_to_stdout()
            else asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProviderTurn("error", error=_tail(str(exc), DIAGNOSTIC_KEEP))

    collected = _Collected(provider, session_id, new_session=new_session)
    assert process.stdout is not None
    readers = [asyncio.create_task(_read_stdout(process.stdout, collected))]
    if process.stderr is not None:
        readers.append(asyncio.create_task(_read_stderr(process.stderr, collected)))
    waiting = asyncio.create_task(process.wait())
    status: Literal["ok", "error", "timeout"] = "ok"
    error = ""
    completed = False
    try:
        await asyncio.wait_for(
            asyncio.gather(*readers, waiting), max(0, timeout - (time.monotonic() - started))
        )
        completed = True
        if collected.error or process.returncode:
            status = "error"
            error = (
                collected.error
                or collected.stderr.decode(errors="replace").strip()
                or collected.unparsed.strip()
                or f"{provider.name} exited with status {process.returncode}"
            )
        elif not collected.events:
            status = "error"
            error = f"{provider.name} returned no recognized provider events"
            diagnostic = (
                collected.stderr.decode(errors="replace").strip() or collected.unparsed.strip()
            )
            if diagnostic:
                error += ": " + _tail(diagnostic, DIAGNOSTIC_KEEP - len(error.encode()) - 2)
    except TimeoutError:
        status = "timeout"
        error = "provider time budget exhausted"
    except (_ProtocolError, OSError) as exc:
        status = "error"
        error = str(exc)
    finally:
        try:
            if not completed:
                await terminate_background_process(process, f"{provider.name} background turn")
        finally:
            for task in [*readers, waiting]:
                task.cancel()
            await asyncio.gather(*readers, waiting, return_exceptions=True)
    return ProviderTurn(
        status,
        output=collected.output,
        error=_tail(error, DIAGNOSTIC_KEEP),
        exit_code=process.returncode,
        session_id=collected.session_id,
    )


async def execute_batch(
    provider: BaseProvider,
    prompt: str,
    model: str,
    effort: str,
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    label: str,
) -> ProviderTurn:
    """Run one fresh batch response with bounded output and process-tree cleanup."""
    if timeout <= 0:
        return ProviderTurn("timeout", error="provider time budget exhausted")
    try:
        cmd = provider.command(prompt, model, effort, args, batch=True, cwd=str(cwd))
        log.info(
            "spawn %s %s %s cwd=%s prompt_len=%d", provider.name, model, effort, cwd, len(prompt)
        )
        log.debug("command %s", logctx.redacted_command(cmd))
        stdout, _, rc, timed_out = await run_process(
            cmd,
            cwd=cwd,
            env=env,
            timeout=timeout,
            merge_stderr=True,
            label=label,
        )
    except OSError as exc:
        return ProviderTurn("error", error=f"could not start {provider.name}: {exc}")
    output = provider.format_batch_output(stdout)
    if timed_out:
        error = f"timed out after {timeout:g}s"
        log.warning(error)
        return ProviderTurn("timeout", output=output, error=error, exit_code=rc)
    if rc != 0:
        error = f"{provider.name} exited with status {rc}"
        log.warning(error)
        return ProviderTurn("error", output=output, error=error, exit_code=rc)
    return ProviderTurn("ok", output=output, exit_code=0)


def acquire_file_lock(path: Path) -> IO[str] | None:
    """Take ``path``'s advisory lock, or None when another process holds it."""
    handle = open(path, "a")  # noqa: SIM115 - released by the caller
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


async def read_tail(stream: asyncio.StreamReader, keep: int, label: str) -> bytes:
    """Drain the stream to EOF, holding only its final ``keep`` bytes."""
    buffer = bytearray()
    total = 0
    while chunk := await stream.read(READ_CHUNK):
        buffer += chunk
        total += len(chunk)
        if len(buffer) > 2 * keep:  # trim in batches so the copying stays linear
            del buffer[:-keep]
    if total > keep:
        # Trimmed stdout can become a prompt that starts mid-document, so say so.
        log.warning("%s: %d bytes of output; keeping only the final %d", label, total, keep)
    return bytes(buffer[-keep:])


async def _feed(process: asyncio.subprocess.Process, data: bytes) -> None:
    """Write ``data`` to the child's stdin and close it; a child that never reads is fine."""
    assert process.stdin is not None
    # The pipe closes under us when the child exits without reading everything; its exit
    # code, not this, says what happened.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        process.stdin.write(data)
        await process.stdin.drain()
    process.stdin.close()
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        await process.stdin.wait_closed()


async def _collect(
    process: asyncio.subprocess.Process, keep: int, label: str, stdin: bytes | None = None
) -> tuple[bytes, bytes]:
    """Read both pipes to EOF (tails only) while feeding stdin, then reap, like ``communicate``.

    Feeding runs beside the reads: a script that fills stdout before touching stdin, or
    never reads it at all, would otherwise deadlock one end against the other.
    """
    assert process.stdout is not None
    streams = [process.stdout] + ([process.stderr] if process.stderr is not None else [])
    feeding = [_feed(process, stdin)] if stdin is not None else []
    results = await asyncio.gather(*(read_tail(s, keep, label) for s in streams), *feeding)
    await process.wait()
    tails = [tail or b"" for tail in results[: len(streams)]]  # the feed's slot is None
    return tails[0], (tails[1] if len(tails) > 1 else b"")


async def run_process(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    merge_stderr: bool,
    label: str,
    stdin: bytes | None = None,
    output_keep: int = OUTPUT_KEEP,
) -> tuple[str, str, int | None, bool]:
    """Run to completion or ``timeout`` seconds, then kill the tree; keeps only the output tail.

    ``stdin`` is written to the child and closed; without it the child reads nothing.
    """
    # No stream ``limit``: the chunked reader never calls readline, and the default
    # buffer with its flow control keeps the pipe bounded while the tail is trimmed.
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    reading = asyncio.create_task(_collect(process, output_keep, label, stdin))
    try:
        done, _ = await asyncio.wait({reading}, timeout=timeout)
        timed_out = reading not in done
        if timed_out:
            await terminate_background_process(process, label)
        try:
            # A descendant that outlived the kill could hold the pipe open; do not wait on it.
            stdout, stderr = await asyncio.wait_for(reading, DRAIN_SECONDS)
        except TimeoutError:
            stdout, stderr = b"", b""
    except BaseException:
        reading.cancel()
        try:
            await terminate_background_process(process, label)
        finally:
            await asyncio.gather(reading, return_exceptions=True)
        raise
    return (
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
        process.returncode,
        timed_out,
    )


def enso_error(stderr: str) -> str:
    """The ``ENSO_ERROR: <summary>`` line a prerun may print, bounded; else empty."""
    for line in stderr.splitlines():
        if line.lstrip().startswith("ENSO_ERROR:"):
            summary = " ".join(line.split(":", 1)[1].split())
            return (
                summary[: DIAGNOSTIC_LIMIT - 1] + "…"
                if len(summary) > DIAGNOSTIC_LIMIT
                else summary
            )
    return ""


def tail(text: str, limit: int) -> str:
    """The end of the output, where a failing CLI leaves its error."""
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def alert_text(headline: str, body: str) -> str:
    """Headline plus the end of the body, within NOTIFY_LIMIT; the headline is never cut."""
    room = NOTIFY_LIMIT - len(headline) - 1
    if not body or room < 2:  # tail() needs room for "…" plus one character
        return headline
    return f"{headline}\n{tail(body, room)}"
