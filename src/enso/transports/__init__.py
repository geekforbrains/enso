"""Transport abstraction — one interface, many channels."""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core import Runtime

log = logging.getLogger(__name__)


def safe_filename(name: str) -> str:
    """Sanitise an attachment filename to prevent path traversal."""
    return os.path.basename(name).lstrip(".")


class TransportContext(ABC):
    """Interface for sending messages back to a user during a conversation.

    Each transport creates a context per incoming message. The runtime
    uses it to deliver status updates and final responses.
    """

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
        return None

    def get_origin_env(self) -> dict[str, str]:
        """Return ``ENSO_ORIGIN_*`` env vars describing the triggering message.

        Injected into the provider subprocess so commands like
        ``enso message send`` can auto-route back to the origin. An empty
        dict means no origin context (e.g. scheduled jobs, CLI triggers);
        outbound commands then fall through to ``notify_channel``.
        """
        return {}


class BaseTransport(ABC):
    """Base class for message transports.

    A transport receives user messages, dispatches them to the runtime,
    and sends responses back. It also supports one-way notifications
    for background job output.
    """

    name: str
    message_limit: int = 4096
    runtime: Runtime

    @abstractmethod
    def start(self) -> None:
        """Start the transport event loop (blocking).

        Implementations must also start the runtime's job scheduler
        as a background task within their event loop.
        """

    @abstractmethod
    async def notify(self, text: str, *, destination: str | None = None) -> None:
        """Send a one-way notification to the user (e.g. job output)."""

    def _start_background_tasks(self) -> None:
        """Start the job scheduler and update-confirmation background tasks.

        Must be called from within the transport's running event loop.
        """
        self._scheduler_task = asyncio.create_task(self.runtime.run_job_scheduler())
        self._update_confirmation_task = asyncio.create_task(
            self._confirm_pending_update()
        )

    async def _confirm_pending_update(self) -> None:
        """Confirm that a newly installed process and services came up."""
        from ..updater import (
            clear_update_confirmation,
            pending_update_confirmation,
            update_confirmation_message,
            wait_for_service_settle,
        )

        pending = pending_update_confirmation(self.name)
        if not pending:
            return
        await wait_for_service_settle()
        try:
            sent = await self._send_update_confirmation(
                pending, update_confirmation_message(pending)
            )
        except Exception:
            log.exception("Failed to send %s update confirmation", self.name)
            return
        if sent:
            clear_update_confirmation(str(pending.get("id", "")))

    async def _send_update_confirmation(self, pending: dict, text: str) -> bool:
        """Deliver the post-update confirmation message. Returns True when sent.

        A False return leaves the confirmation queued for the next start
        (e.g. the transport's client isn't ready yet).
        """
        raise NotImplementedError
