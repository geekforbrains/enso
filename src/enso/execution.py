"""Shared bounded subprocesses, advisory locks, and provider turns for background work."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import log as logctx
from .providers import BaseProvider
from .providers.stream import DIAGNOSTIC_KEEP, ProtocolError, ProviderStream, text_tail
from .runs import OUTPUT_KEEP

log = logging.getLogger(__name__)

DRAIN_SECONDS = 5.0
DIAGNOSTIC_LIMIT = 500
NOTIFY_LIMIT = 4000

READ_CHUNK = 64 * 1024
TERMINATE_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class ProviderTurn:
    """One provider response and its validated, resumable session identity, if known."""

    status: Literal["ok", "error", "timeout"]
    output: str = ""
    error: str = ""
    exit_code: int | None = None
    session_id: str | None = None


async def terminate_process_tree(
    process: asyncio.subprocess.Process, label: str, *, grace: float = TERMINATE_GRACE_SECONDS
) -> None:
    """SIGTERM the process group, wait ``grace`` seconds, then SIGKILL it."""
    if process.returncode is not None:
        return
    try:
        pgid: int | None = os.getpgid(process.pid)
    except ProcessLookupError:
        pgid = None
    if pgid is not None and (pgid <= 0 or pgid == os.getpgrp()):
        pgid = None

    async def send(sig: signal.Signals) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                process.send_signal(sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            if pgid is None:
                raise
            # macOS can report EPERM for a group containing only an unreaped
            # zombie. Let asyncio reap our child, but hide the error only when
            # the group has disappeared; a live permission failure must surface.
            try:
                await asyncio.wait_for(process.wait(), timeout=grace)
            except TimeoutError:
                raise exc from None
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            raise

    log.info("terminating %s pid=%s pgid=%s", label, process.pid, pgid)
    await send(signal.SIGTERM)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=grace)
        return
    log.info("%s did not exit after %.1fs; killing", label, grace)
    await send(signal.SIGKILL)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=grace)


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
        stream = ProviderStream(provider, session_id, new_session=new_session)
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
        return ProviderTurn("error", error=text_tail(str(exc), DIAGNOSTIC_KEEP))

    output = ""

    async def collect() -> None:
        nonlocal output
        async with contextlib.aclosing(stream.read(process)) as events:
            async for event in events:
                if event.kind == "response":
                    # Adapters emit complete answers; keep only the last one, bounded.
                    output = text_tail(provider.format_response([event.text]), OUTPUT_KEEP)

    status: Literal["ok", "error", "timeout"] = "ok"
    error = ""
    completed = False
    try:
        await asyncio.wait_for(collect(), max(0, timeout - (time.monotonic() - started)))
        completed = True
        if stream.error:
            status = "error"
            error = stream.error
    except TimeoutError:
        status = "timeout"
        error = "provider time budget exhausted"
    except (ProtocolError, OSError) as exc:
        status = "error"
        error = str(exc)
    finally:
        if not completed:
            await terminate_background_process(process, f"{provider.name} background turn")
    return ProviderTurn(
        status,
        output=output,
        error=text_tail(error, DIAGNOSTIC_KEEP),
        exit_code=process.returncode,
        session_id=stream.session_id,
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
