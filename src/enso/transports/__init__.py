"""Transport abstraction — one interface, many channels."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import stat
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, BinaryIO

if TYPE_CHECKING:
    from ..core import Runtime
    from ..outbound import OutboundMessage, SurfacePublication

log = logging.getLogger(__name__)


def safe_filename(name: str) -> str:
    """Sanitise an attachment filename to prevent path traversal."""
    return os.path.basename(name).lstrip(".")


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_CREATE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_SECURE_UPLOADS_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and all(
        function in os.supports_dir_fd
        for function in (os.mkdir, os.open, os.stat, os.unlink)
    )
    and os.stat in os.supports_follow_symlinks
)


def _safe_path_component(value: str, *, label: str) -> str:
    """Validate one name used relative to an already-open directory."""
    if not value or value in {".", ".."} or os.sep in value:
        raise ValueError(f"Invalid {label}")
    if os.altsep and os.altsep in value:
        raise ValueError(f"Invalid {label}")
    return value


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


class SecureUploadDirectory:
    """A pinned, symlink-resistant ``<workspace>/uploads/<turn>`` directory."""

    def __init__(
        self,
        *,
        workspace_path: str,
        turn_id: str,
        workspace_fd: int,
        uploads_fd: int,
        turn_fd: int,
    ) -> None:
        self.workspace_path = workspace_path
        self.turn_id = turn_id
        self.path = os.path.join(workspace_path, "uploads", turn_id)
        self._workspace_fd = workspace_fd
        self._uploads_fd = uploads_fd
        self._turn_fd = turn_fd
        self._completed_files: dict[str, os.stat_result] = {}

    @classmethod
    def create(cls, workspace_path: str, turn_id: str) -> SecureUploadDirectory:
        """Create and pin a fresh upload directory without following symlinks."""
        if not _SECURE_UPLOADS_SUPPORTED:
            raise OSError(errno.ENOTSUP, "Secure upload directories are unsupported")
        turn_id = _safe_path_component(turn_id, label="upload turn id")
        workspace_path = os.path.abspath(os.path.expanduser(workspace_path))
        workspace_fd = uploads_fd = turn_fd = -1
        try:
            workspace_fd = os.open(workspace_path, _DIRECTORY_OPEN_FLAGS)
            with contextlib.suppress(FileExistsError):
                os.mkdir("uploads", mode=0o700, dir_fd=workspace_fd)
            uploads_fd = os.open("uploads", _DIRECTORY_OPEN_FLAGS, dir_fd=workspace_fd)
            os.mkdir(turn_id, mode=0o700, dir_fd=uploads_fd)
            turn_fd = os.open(turn_id, _DIRECTORY_OPEN_FLAGS, dir_fd=uploads_fd)
            return cls(
                workspace_path=workspace_path,
                turn_id=turn_id,
                workspace_fd=workspace_fd,
                uploads_fd=uploads_fd,
                turn_fd=turn_fd,
            )
        except BaseException:
            for fd in (turn_fd, uploads_fd, workspace_fd):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            raise

    @classmethod
    def create_for_path(cls, path: str) -> SecureUploadDirectory:
        """Create a secure upload directory from its expected full path."""
        path = os.path.abspath(os.path.expanduser(path))
        turn_id = os.path.basename(path)
        uploads_path = os.path.dirname(path)
        if os.path.basename(uploads_path) != "uploads":
            raise ValueError("Upload directory must be <workspace>/uploads/<turn>")
        return cls.create(os.path.dirname(uploads_path), turn_id)

    def __enter__(self) -> SecureUploadDirectory:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the pinned descriptors without removing retained uploads."""
        for attr in ("_turn_fd", "_uploads_fd", "_workspace_fd"):
            fd = getattr(self, attr)
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, attr, -1)

    def file_path(self, filename: str) -> str:
        filename = _safe_path_component(filename, label="upload filename")
        return os.path.join(self.path, filename)

    def verified_file_path(self, filename: str) -> str | None:
        """Return the path only while it names the exact file created here."""
        filename = _safe_path_component(filename, label="upload filename")
        created_file = self._completed_files.get(filename)
        if created_file is None:
            return None
        if not self._paths_are_current(filename, created_file):
            return None
        return self.file_path(filename)

    def _paths_are_current(
        self,
        filename: str,
        created_file: os.stat_result,
    ) -> bool:
        """Check that names still resolve to the pinned directories and file."""
        try:
            workspace_now = os.stat(self.workspace_path, follow_symlinks=False)
            uploads_now = os.stat(
                "uploads", dir_fd=self._workspace_fd, follow_symlinks=False
            )
            turn_now = os.stat(
                self.turn_id, dir_fd=self._uploads_fd, follow_symlinks=False
            )
            file_now = os.stat(filename, dir_fd=self._turn_fd, follow_symlinks=False)
        except OSError:
            return False
        return all(
            (
                stat.S_ISDIR(workspace_now.st_mode),
                stat.S_ISDIR(uploads_now.st_mode),
                stat.S_ISDIR(turn_now.st_mode),
                stat.S_ISREG(file_now.st_mode),
                _same_file(workspace_now, os.fstat(self._workspace_fd)),
                _same_file(uploads_now, os.fstat(self._uploads_fd)),
                _same_file(turn_now, os.fstat(self._turn_fd)),
                _same_file(file_now, created_file),
            )
        )

    def _unlink_if_created(self, filename: str, created_file: os.stat_result) -> None:
        """Remove only the exact file this instance created."""
        try:
            current = os.stat(filename, dir_fd=self._turn_fd, follow_symlinks=False)
            if _same_file(current, created_file):
                os.unlink(filename, dir_fd=self._turn_fd)
        except OSError:
            pass

    @contextlib.contextmanager
    def open_file(self, filename: str) -> Iterator[BinaryIO]:
        """Exclusively create one file relative to the pinned turn directory."""
        filename = _safe_path_component(filename, label="upload filename")
        fd = os.open(
            filename,
            _FILE_CREATE_FLAGS,
            mode=0o600,
            dir_fd=self._turn_fd,
        )
        created_file = os.fstat(fd)
        try:
            stream = os.fdopen(fd, "wb")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            self._unlink_if_created(filename, created_file)
            raise
        try:
            with stream:
                yield stream
            if not self._paths_are_current(filename, created_file):
                raise OSError("Upload path changed while the file was being written")
            self._completed_files[filename] = created_file
        except BaseException:
            self._completed_files.pop(filename, None)
            if not stream.closed:
                with contextlib.suppress(OSError):
                    stream.close()
            self._unlink_if_created(filename, created_file)
            raise


def _warn_if_task_died(task: asyncio.Task, label: str) -> None:
    """Surface a background task that died — silence here hides real outages."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("%s task died: %s", label, exc, exc_info=exc)


class TransportContext(ABC):
    """Interface for sending messages back to a user during a conversation.

    Each transport creates a context per incoming message. The runtime
    uses it to deliver status updates and final responses.
    """

    # Set by transports that can render markdown themselves (Slack). When it
    # is False the runtime keeps ownership of splitting long plain text.
    rich_markdown_enabled: bool = False

    @abstractmethod
    async def reply(self, text: str) -> None:
        """Send a final response message."""

    async def reply_markdown(self, text: str) -> None:
        """Send markdown-formatted text, falling back safely on plain text."""
        await self.reply(text)

    async def reply_message(self, message: OutboundMessage) -> None:
        """Send a structured response, falling back safely on plain text."""
        await self.reply(message.fallback_text)

    async def offer_surface_draft(
        self,
        publication: SurfacePublication,
        source_text: str,
    ) -> None:
        """Offer a persistent-surface draft, falling back safely on plain text."""
        await self.reply(publication.fallback_text)

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

    def get_output_instructions(self) -> str:
        """Return transport-specific final-output instructions for the agent."""
        return ""

    def get_surface_instructions(self) -> str:
        """Return instructions for persistent-surface drafts."""
        return ""


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
        self._scheduler_task = asyncio.create_task(self.runtime.jobs.run_scheduler())
        self._scheduler_task.add_done_callback(
            lambda task: _warn_if_task_died(task, "Job scheduler")
        )
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

    @abstractmethod
    async def _send_update_confirmation(self, pending: dict, text: str) -> bool:
        """Deliver the post-update confirmation message. Returns True when sent.

        A False return leaves the confirmation queued for the next start
        (e.g. the transport's client isn't ready yet).
        """
