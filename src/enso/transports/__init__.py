"""Transport contract: inbound ``Turn``s, per-turn ``Reply`` handles, and the ``Transport`` ABC."""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..outbound import OutboundMessage

if TYPE_CHECKING:
    from ..runtime import Runtime


@dataclass
class Turn:
    """An inbound message the runtime should act on."""

    transport: str
    channel: str
    thread: str | None  # the thread replies go to; None for DMs and Telegram chats
    message_id: str
    user_id: str
    user_name: str
    text: str
    files: list[str] = field(default_factory=list)  # local paths of downloaded attachments
    is_dm: bool = False
    mentioned: bool = False
    # Transport-rendered, untrusted history (thread context, forwarded messages)
    # that precedes the user's text in the prompt.
    context: str = ""
    channel_name: str = ""
    # The workspace the message was bound to when it arrived, where the transport prepared
    # its uploads; the runtime resolves the binding itself when this is empty.
    workspace: str = ""


class Reply(ABC):
    """How the runtime talks back for one turn."""

    limit: int = 4000
    # True when send_rich renders enso-message blocks natively; the runtime then
    # offers the agent the rich-format contract.
    rich_format: bool = False

    @abstractmethod
    async def send(self, text: str) -> str:
        """Send markdown text; returns the platform message id."""

    async def send_rich(self, message: OutboundMessage) -> str:
        """Send an ``enso-message`` envelope; transports without blocks send its fallback."""
        return await self.send(message.fallback_text)

    @abstractmethod
    async def send_file(self, path: str, caption: str = "") -> str: ...

    @abstractmethod
    async def status_post(self, text: str) -> str: ...

    @abstractmethod
    async def status_edit(self, message_id: str, text: str) -> None: ...

    @abstractmethod
    async def status_delete(self, message_id: str) -> None: ...

    async def typing(self) -> None:
        return None

    def origin_env(self) -> dict[str, str]:
        """``ENSO_ORIGIN_*`` variables describing the triggering message."""
        return {}


class Transport(ABC):
    """A chat platform connection: turns events into ``Turn``s and sends messages."""

    name: str

    @abstractmethod
    async def start(self, runtime: Runtime) -> None:
        """Connect and run until cancelled."""

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[None]:
        """Make the send methods usable without ``start`` (an ``enso`` CLI process)."""
        yield

    @abstractmethod
    async def send(self, target: str, text: str, *, thread: str | None = None) -> str: ...

    async def send_rich(
        self, target: str, message: OutboundMessage, *, thread: str | None = None
    ) -> str:
        return await self.send(target, message.fallback_text, thread=thread)

    @abstractmethod
    async def send_file(
        self, target: str, path: str, *, caption: str = "", thread: str | None = None
    ) -> str: ...

    async def permalink(self, target: str, message_id: str) -> str | None:
        """A link to a sent message, for platforms that have one."""
        return None

    @abstractmethod
    async def edit(self, target: str, message_id: str, text: str) -> None: ...

    @abstractmethod
    async def delete(self, target: str, message_id: str) -> None: ...

    @abstractmethod
    async def fetch_thread(self, target: str, thread: str) -> list[dict]: ...
