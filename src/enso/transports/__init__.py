"""Transport abstraction — one interface, many channels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TransportContext(ABC):
    """Interface for sending messages back to a user during a conversation.

    Each transport creates a context per incoming message. The runtime
    uses it to deliver status updates and final responses.
    """

    # Whether the runtime should prepend "(Provider / Ns)" to final replies.
    # Transports can set this to False if the prefix is not desired.
    include_prefix: bool = True

    @abstractmethod
    async def reply(self, text: str) -> None:
        """Send a final response message."""

    @abstractmethod
    async def reply_status(self, text: str) -> Any:
        """Send a status message. Returns a handle for editing/deleting."""

    @abstractmethod
    async def edit_status(self, handle: Any, text: str) -> None:
        """Update an existing status message."""

    @abstractmethod
    async def delete_status(self, handle: Any) -> None:
        """Delete a status message."""

    async def send_typing(self) -> None:
        """Send a typing indicator. No-op by default."""


class BaseTransport(ABC):
    """Base class for message transports.

    A transport receives user messages, dispatches them to the runtime,
    and sends responses back. It also supports one-way notifications
    for background job output.
    """

    name: str

    @abstractmethod
    def start(self) -> None:
        """Start the transport event loop (blocking).

        Implementations must also start the runtime's job scheduler
        as a background task within their event loop.
        """

    @abstractmethod
    async def notify(self, text: str) -> None:
        """Send a one-way notification to the user (e.g. job output)."""
