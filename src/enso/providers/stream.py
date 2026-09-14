"""Structured provider streams: session identity, bounded reads, and failure diagnostics."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator

from . import BaseProvider, SessionIdError, StreamEvent

log = logging.getLogger(__name__)

READ_CHUNK = 64 * 1024
LINE_KEEP = 10 * 1024 * 1024
DIAGNOSTIC_KEEP = 2000


def text_tail(text: str, keep: int) -> str:
    """Keep a UTF-8 tail without exceeding the byte limit or splitting a character."""
    return text[-keep:].encode()[-keep:].decode(errors="ignore")


class ProtocolError(ValueError):
    """A provider's stream cannot safely be interpreted as the requested turn."""

    def __init__(self, message: str) -> None:
        super().__init__(text_tail(message, DIAGNOSTIC_KEEP))


class ProviderStream:
    """Interpret one turn without owning its process, presentation, or persistence."""

    def __init__(
        self, provider: BaseProvider, expected_id: str | None, *, new_session: bool
    ) -> None:
        self.provider = provider
        self.expected_id = (
            provider.check_session_id(expected_id) if expected_id is not None else None
        )
        # An assigned id becomes resumable only after a recognized CLI event.
        self.session_id = None if new_session else self.expected_id
        self.events = 0
        self.error = ""
        self.unparsed = ""
        self.stderr = b""

    def feed(self, line: bytes) -> list[StreamEvent]:
        """Validate every session announcement before delivering any events on this line."""
        text = line.decode(errors="replace").strip()
        if not text:
            return []
        try:
            raw = self.provider.parse_line(text)
            events = self.provider.parse_event(raw) if raw is not None else []
            if not events:
                self.unparsed = text_tail(self.unparsed + text + "\n", DIAGNOSTIC_KEEP)
                return []
            self.events += len(events)
            if self.expected_id and not self.session_id:
                self.session_id = self.expected_id
            for event in events:
                if event.kind == "session" and event.session_id is not None:
                    announced = self.provider.check_session_id(event.session_id)
                    if self.expected_id is not None and announced != self.expected_id:
                        raise ProtocolError(
                            f"{self.provider.name} announced a different session from the "
                            "one requested; refusing to change sessions"
                        )
                    self.expected_id = self.session_id = announced
                elif event.kind == "error":
                    event.text = (
                        text_tail(event.text, DIAGNOSTIC_KEEP) or "provider reported an error"
                    )
                    self.error = event.text
            return events
        except SessionIdError as exc:
            raise ProtocolError(str(exc)) from exc
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError(f"invalid {self.provider.name} event: {exc}") from exc

    def exit_error(self, rc: int) -> str:
        """Prefer provider errors, then diagnostics; an empty successful stream still fails."""
        diagnostic = text_tail(
            self.stderr.decode(errors="replace").strip() or self.unparsed.strip(), DIAGNOSTIC_KEEP
        )
        if self.error or rc:
            return self.error or diagnostic or f"{self.provider.name} exited with status {rc}"
        if not self.events:
            error = f"{self.provider.name} returned no recognized provider events"
            if diagnostic:
                error += ": " + text_tail(diagnostic, DIAGNOSTIC_KEEP - len(error.encode()) - 2)
            return error
        return ""

    async def read(
        self, process: asyncio.subprocess.Process, *, debug: bool = False
    ) -> AsyncGenerator[StreamEvent]:
        """Drain both pipes, yielding validated events and any otherwise unreported failure.

        The caller closes this iterator and terminates the process on cancellation or a
        protocol error. Stderr is drained concurrently but never interpreted as stdout;
        adapters that require merged output arrange that when launching the process.
        """
        assert process.stdout is not None
        stderr_task = (
            asyncio.create_task(self._read_stderr(process.stderr))
            if process.stderr is not None
            else None
        )
        try:
            async with contextlib.aclosing(_lines(process.stdout)) as lines:
                async for line in lines:
                    if debug:
                        log.debug("provider line %s", line.decode(errors="replace").strip()[:2000])
                    for event in self.feed(line):
                        yield event
            rc = await process.wait()
            if stderr_task is not None:
                await stderr_task
            if not self.error:
                self.error = self.exit_error(rc)
                if self.error:
                    yield StreamEvent(kind="error", text=self.error)
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(READ_CHUNK):
            self.stderr = (self.stderr + chunk)[-DIAGNOSTIC_KEEP:]


async def _lines(stream: asyncio.StreamReader) -> AsyncGenerator[bytes]:
    """Read complete lines with a bound even when a provider never emits a newline."""
    buffer = bytearray()
    while chunk := await stream.read(READ_CHUNK):
        buffer.extend(chunk)
        start = 0
        while (end := buffer.find(b"\n", start)) >= 0:
            if end - start > LINE_KEEP:
                raise ProtocolError(f"provider event exceeds {LINE_KEEP} bytes")
            yield bytes(buffer[start:end])
            start = end + 1
        if start:
            del buffer[:start]
        if len(buffer) > LINE_KEEP:
            raise ProtocolError(f"provider event exceeds {LINE_KEEP} bytes")
    if buffer:
        yield bytes(buffer)
