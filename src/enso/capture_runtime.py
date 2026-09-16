"""Best-effort live capture checkpoints; failures never retry provider work or delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from . import captures
from .config import Paths

log = logging.getLogger(__name__)


@dataclass
class CaptureWriter:
    paths: Paths
    id: int | None = None
    message: captures.Message | None = None
    text: str = ""
    sender_id: str = ""
    sender_name: str = "Enso"
    outcome: captures.Outcome = "pending"
    parts: list[captures.Part] = field(default_factory=list)
    finalized: bool = False
    failed: bool = False
    rejected: Callable[[Exception], bool] = field(default=lambda _: False)

    admission: asyncio.Task[bool] | None = None

    @classmethod
    def start(cls, paths: Paths, message: captures.Message) -> CaptureWriter:
        """Start capture without yielding the transport's FIFO reservation to a later event."""
        writer = cls(paths, message=message)
        writer.admission = asyncio.create_task(writer._admit())
        return writer

    async def _admit(self) -> bool:
        result = await self._write(captures.record, self.message)
        if result is not None:
            record, inserted = result
            if not inserted:
                self.finalized = True
                return False
            self.id = record.id
        return True  # failed storage still permits ordinary conversation handling

    async def ready(self) -> bool:
        return await asyncio.shield(self.admission) if self.admission else True

    async def _write(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self.failed:
            return None
        try:
            return await asyncio.to_thread(function, self.paths, *args, **kwargs)
        except Exception:
            # Exceptions can quote message bodies or authenticated URLs. Identifiers alone
            # suffice, and one failed operation disables further writes for this capture.
            self.failed = True
            log.warning(
                "capture storage failed (capture=%s transport=%s message=%s)",
                self.id,
                self.message.transport if self.message else "-",
                self.message.message_id if self.message else "-",
            )
            return None

    async def attachments(self, items: tuple[captures.Attachment, ...]) -> None:
        if self.id is not None:
            await self._write(captures.attachments, self.id, items)

    async def begin(self, text: str, outcome: captures.Outcome) -> None:
        self.text, self.outcome = text, outcome
        await self._checkpoint(final=False)

    def _delivery(self) -> captures.Delivery:
        states = {part.status for part in self.parts}
        if "sending" in states:
            return "sending"
        if "uncertain" in states:
            return "uncertain"
        if "failed" in states:
            return "partial" if "sent" in states else "failed"
        if not states:
            return "unattempted"
        return "complete" if self.parts[-1].end == len(self.text) else "partial"

    async def _checkpoint(
        self, *, final: bool, handling_outcome: captures.Outcome | None = None
    ) -> None:
        if self.id is not None:
            await self._write(
                captures.reply,
                self.id,
                text=self.text,
                outcome=self.outcome,
                handling_outcome=handling_outcome,
                sender_id=self.sender_id,
                sender_name=self.sender_name,
                delivery=self._delivery(),
                parts=tuple(self.parts),
                final=final,
            )

    async def finish(self, outcome: captures.Outcome | None = None) -> None:
        await self.ready()
        if self.finalized:
            return
        self.finalized = True
        if self.outcome == "pending":
            self.outcome = outcome or "failed"
        self.parts = [
            replace(p, status="uncertain") if p.status == "sending" else p for p in self.parts
        ]
        await self._checkpoint(final=True, handling_outcome=outcome)

    async def send(
        self,
        text: str,
        sender: Callable[[], Awaitable[str]],
        *,
        fallback: bool = False,
    ) -> str:
        """Checkpoint the intended part before sending; preserve acknowledgments individually."""
        if fallback:
            # A rich-message rejection confirms nothing was sent; fallback replaces that
            # representation, rather than making the rejected envelope part of the answer.
            self.text, self.parts = text, []
        offset = self.parts[-1].end if self.parts else 0
        start = self.text.find(text, offset)
        if start < 0:
            raise ValueError("delivered part is absent from the reply representation")
        self.parts.append(captures.Part(start, start + len(text), "sending"))
        await self._checkpoint(final=False)
        try:
            ident = await sender()
        except BaseException as exc:
            status: Literal["failed", "uncertain"] = (
                "failed" if isinstance(exc, Exception) and self.rejected(exc) else "uncertain"
            )
            self.parts[-1] = replace(self.parts[-1], status=status)
            await self._checkpoint(final=False)
            raise
        self.parts[-1] = replace(self.parts[-1], status="sent", message_id=ident)
        await self._checkpoint(final=False)
        return ident
