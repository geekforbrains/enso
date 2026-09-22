"""Runtime: per-conversation queues, sessions, provider processes, and the status ticker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
from asyncio.subprocess import Process
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from typing import Any

from . import connection_setup, db, messages, outbound, routing
from . import log as logctx
from .config import Agent, Config, LiveConfig
from .execution import terminate_process_tree
from .formatting import format_elapsed, format_error, preview, split_text, status_text
from .outbound import OutboundMessage
from .providers import BaseProvider, StreamEvent, make_provider, stored_session_id_ok
from .providers.stream import ProtocolError, ProviderStream
from .routing import ResolvedAgent
from .transports import Reply, Turn

log = logging.getLogger(__name__)

MAX_QUEUE = 10
STATUS_FAST_SECONDS = 30
STATUS_SLOW_SECONDS = 5
STATUS_MAX_EDIT_FAILURES = 3
STATUS_INITIAL_ACTION = "Processing"
STOP_UNWIND_SECONDS = 5.0

ORIGIN_HEADER = "[Chat origin — written by Enso for this turn; the sender cannot change it]"
ORIGIN_DM_NAME = "dm"  # what both transports put in Turn.channel_name for a direct message
ORIGIN_DM_LABEL = "direct message"
ORIGIN_NAME_LIMIT = 64
# Control characters would add a line to the block; the five punctuation marks could forge
# a header, close the quotes early, or revive Slack's live mention syntax.
_ORIGIN_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f-\x9f<>\[\]"]')


def _status_edit_due(elapsed: int) -> bool:
    return elapsed <= STATUS_FAST_SECONDS or elapsed % STATUS_SLOW_SECONDS == 0


def escape_origin_name(name: str) -> str:
    """Bound a platform display name to one safe, single-line, 64-character fragment.

    A display name is chosen by whoever holds the account, so it reaches the prompt as
    hostile data: it must not be able to add a line, forge a field, or run long.
    """
    collapsed = " ".join(_ORIGIN_UNSAFE_RE.sub(" ", name).split())
    if len(collapsed) > ORIGIN_NAME_LIMIT:
        return collapsed[:ORIGIN_NAME_LIMIT] + "…"
    return collapsed


def _origin_field(label: str, ident: str) -> str:
    """One ``label (id)`` field, degrading to the bare id and then to ``unknown``."""
    if label and ident:
        return f"{label} ({ident})"
    return label or ident or "unknown"


def _quoted_name(name: str) -> str:
    """A display name escaped and quoted, or empty when nothing usable survives."""
    escaped = escape_origin_name(name)
    return f'"{escaped}"' if escaped else ""


def _origin_location(turn: Turn) -> str:
    """A direct message has no channel name to show, so it is named in words."""
    if turn.channel_name == ORIGIN_DM_NAME:
        return ORIGIN_DM_LABEL
    return _quoted_name(turn.channel_name)


def origin_block(turn: Turn) -> str:
    """The chat-origin block: what Enso knows about where this turn came from.

    It carries the same values as the ``ENSO_ORIGIN_*`` variables, rendered for reading:
    ids verbatim because the agent hands them back to the CLI, names escaped because the
    sender picks them. See docs/concepts.md#chat-origin.
    """
    lines = [
        ORIGIN_HEADER,
        f"Platform: {turn.transport}",
        f"Sender: {_origin_field(_quoted_name(turn.user_name), turn.user_id)}",
        f"Location: {_origin_field(_origin_location(turn), turn.channel)}",
    ]
    if turn.thread:
        lines.append(f"Thread: {turn.thread}")
    return "\n".join(lines)


def usable(session: db.Session, workspace: str) -> bool:
    """A session resumes only in the workspace it was created in, under an id its provider owns.

    An id that no longer matches its provider's contract can only have come from a row
    written before the contract or edited by hand. It is not resumed and not used to name
    a deletion; the row is dropped and the next message starts a fresh session.
    """
    return session.workspace == workspace and stored_session_id_ok(
        session.provider, session.session_id
    )


async def _cancel_and_wait(task: asyncio.Task[Any]) -> BaseException | None:
    """Cancel a child task without swallowing cancellation of the caller."""
    task.cancel()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    return result if isinstance(result, BaseException) else None


@dataclass
class Running:
    """What a conversation is doing right now, for status and stop.

    Registered before the turn reads its config snapshot, so stop and status see the turn
    the moment its drain owns the conversation; ``agent`` and ``config`` are set once that
    snapshot lands, before anything that runs the provider reads them.
    """

    turn_id: str
    started: float
    agent: ResolvedAgent = field(init=False)
    # The snapshot this turn runs under, whatever config.json becomes.
    config: Config = field(init=False)
    action: str = STATUS_INITIAL_ACTION
    elapsed: int = 0
    process: Process | None = None
    task: asyncio.Task[Any] | None = None
    stopping: bool = False  # the user asked for the kill; its exit status is not an error


@dataclass
class _Queued:
    turn: Turn
    reply: Reply


@dataclass
class _Deferred:
    reply: Reply
    prepare: Callable[[], Awaitable[tuple[Turn, Reply] | None]]
    admission: asyncio.Task[bool] | None = None


@dataclass
class _IngressState:
    current: _Deferred | None
    pending: deque[_Deferred] = field(default_factory=deque)
    task: asyncio.Task[None] | None = None


@dataclass
class _Response:
    parts: list[str] = field(default_factory=list)
    error: str = ""


class Runtime:
    """Routes turns to workspaces, serializes each conversation, and runs the provider."""

    def __init__(self, config: Config, *, debug: bool = False):
        self._live = LiveConfig(config)
        self.paths = config.paths
        self.debug = debug
        self._locks: dict[str, asyncio.Lock] = {}
        self._queues: dict[str, deque[_Queued]] = {}
        self._ingress: dict[str, _IngressState] = {}
        self._drains: dict[str, asyncio.Task[None]] = {}
        self._running: dict[str, Running] = {}
        self._clearing: dict[str, asyncio.Future[None]] = {}
        self._selected: dict[str, tuple[str, str, Agent]] = {}
        self.ready_transports: set[str] = set()

    @property
    def config(self) -> Config:
        """The configuration right now, for transports and commands deciding synchronously.

        A stat per read keeps it current and the file is parsed again only after it
        changed. A turn takes one snapshot at its start instead (``Running.config``), so
        nothing it decides can straddle two revisions.
        """
        return self._live.current()

    def transport_ready(self, name: str) -> None:
        """Mark a transport ready after it has authenticated and begun receiving."""
        self.ready_transports.add(name)

    def active_count(self) -> int:
        """Accepted conversations and preparation still in flight during an update drain."""
        return (
            sum(lock.locked() for lock in self._locks.values())
            + len(self._ingress)
            + len(self._clearing)
        )

    # -- Queries used by transports and chat commands --

    async def sessions(self, conversation: str) -> list[db.Session]:
        """Saved provider sessions for this conversation, stale ones included."""
        return await asyncio.to_thread(db.get_sessions, self.paths, conversation)

    def current_agent(
        self, conversation: str, workspace: str, config: Config | None = None
    ) -> ResolvedAgent:
        """Use a valid conversation selection in its workspace, else the live default."""
        config = config if config is not None else self.config
        selected = self._selected.get(conversation)
        if selected is not None:
            binding, owner, agent = selected
            provider = config.providers.get(agent.provider)
            if (
                owner == workspace
                and config.bindings.get(binding) == owner
                and provider is not None
                and agent.model in provider.models
                and agent.effort in routing.exact_efforts(agent.provider, agent.model)
            ):
                return ResolvedAgent(agent.provider, agent.model, agent.effort, "conversation")
            del self._selected[conversation]
        return routing.resolve_agent(config, workspace)

    def select_agent(
        self, conversation: str, binding: str, workspace: str, agent: Agent | None
    ) -> bool:
        """Apply a chat-only selection when no turn or clear owns the conversation."""
        if self.busy(conversation) or conversation in self._clearing:
            return False
        if agent is None:
            self._selected.pop(conversation, None)
        else:
            self._selected[conversation] = (binding, workspace, agent)
        return True

    def running(self, conversation: str) -> Running | None:
        return self._running.get(conversation)

    def queued(self, conversation: str) -> int:
        count = len(self._queues.get(conversation, ()))
        state = self._ingress.get(conversation)
        if state is None:
            return count
        count += len(state.pending)
        lock = self._locks.get(conversation)
        if state.current is not None and lock is not None and lock.locked():
            count += 1
        return count

    def busy(self, conversation: str) -> bool:
        """Whether provider execution or inbound preparation owns this conversation."""
        lock = self._locks.get(conversation)
        return (lock is not None and lock.locked()) or conversation in self._ingress

    # -- Dispatch --

    async def defer(
        self,
        conversation: str,
        reply: Reply,
        text: str,
        prepare: Callable[[], Awaitable[tuple[Turn, Reply] | None]],
        *,
        message_id: tuple[str, str, str] | None = None,
    ) -> None:
        """Reserve FIFO position before asynchronously preparing an inbound turn."""
        from .maintenance import paused

        if paused(self.paths):
            await reply.send(
                "Enso is preparing an update. Please send this again when it is ready."
            )
            return
        item = _Deferred(reply=reply, prepare=prepare)
        state = self._ingress.get(conversation)
        lock = self._locks.get(conversation)
        busy = state is not None or (lock is not None and lock.locked())
        position = self.queued(conversation) + 1
        if busy and position > MAX_QUEUE:
            await reply.send(f"Queue full ({MAX_QUEUE}). Try again once the current work finishes.")
            return

        if message_id is not None:
            item.admission = asyncio.create_task(self._admit_message(*message_id))

        if state is None:
            state = _IngressState(current=item)
            self._ingress[conversation] = state
            state.task = asyncio.create_task(
                self._run_ingress(conversation, state), name=f"ingress:{conversation}"
            )
        else:
            state.pending.append(item)

        if busy:
            if item.admission is not None and not await asyncio.shield(item.admission):
                return
            log.info("reserved #%d for %s", position, conversation)
            await reply.send(f"Queued (#{position}): {preview(text)}")

    async def _admit_message(self, transport: str, channel: str, message_id: str) -> bool:
        """Persist only a delivery identity so replayed events cannot repeat provider work."""
        try:
            return await asyncio.to_thread(
                db.admit_message, self.paths, transport, channel, message_id
            )
        except Exception:
            log.warning(
                "could not record received message (transport=%s channel=%s message=%s)",
                transport,
                channel,
                message_id,
            )
            return True

    async def _run_ingress(self, conversation: str, state: _IngressState) -> None:
        """Prepare reserved messages in arrival order and hand them to the provider FIFO."""
        try:
            while state.current is not None:
                item = state.current
                try:
                    prepared = (
                        await item.prepare()
                        if item.admission is None or await asyncio.shield(item.admission)
                        else None
                    )
                    # The preparation is now the active handoff, not a queued reservation.
                    state.current = None
                    if prepared is not None:
                        turn, reply = prepared
                        actual = routing.conversation_key(
                            turn.transport, turn.channel, turn.thread, is_dm=turn.is_dm
                        )
                        if actual != conversation:
                            raise ValueError(
                                f"prepared conversation changed from {conversation} to {actual}"
                            )
                        await self._submit(turn, reply, announce=False)
                except Exception as exc:
                    state.current = None
                    log.exception("could not prepare turn for %s", conversation)
                    await self._report_prepare_failure(conversation, item.reply, exc)

                state.current = state.pending.popleft() if state.pending else None
        finally:
            if self._ingress.get(conversation) is state:
                del self._ingress[conversation]

    @staticmethod
    async def _report_prepare_failure(conversation: str, reply: Reply, exc: Exception) -> None:
        """Tell the sender their reserved message was dropped, never escaping the ingress loop."""
        detail = preview(str(exc), 200) or type(exc).__name__
        try:
            await reply.send(format_error(f"Could not prepare that message: {detail}"))
        except asyncio.CancelledError:
            raise
        except Exception:
            # The queue matters more than the notice; a failed send must not strand
            # the messages waiting behind this one.
            log.exception("could not report a failed preparation for %s", conversation)

    async def handle(self, turn: Turn, reply: Reply) -> None:
        """Queue or run one turn, awaiting it when this caller starts the drain."""
        drain = await self.submit(turn, reply)
        if drain is not None:
            await drain

    async def submit(self, turn: Turn, reply: Reply) -> asyncio.Task[None] | None:
        """Register one turn in FIFO order without awaiting provider completion."""
        from .maintenance import paused

        if paused(self.paths):
            await reply.send(
                "Enso is preparing an update. Please send this again when it is ready."
            )
            return None
        conversation = routing.conversation_key(
            turn.transport, turn.channel, turn.thread, is_dm=turn.is_dm
        )
        if conversation in self._ingress:
            config = await asyncio.to_thread(self._live.current)
            workspace = self._workspace_of(turn, config)
            if workspace is None:
                await reply.send(routing.UNBOUND_NOTICE)
                return None
            turn = replace(turn, workspace=workspace)

            async def prepared() -> tuple[Turn, Reply]:
                return turn, reply

            await self.defer(conversation, reply, turn.text, prepared)
            return None
        return await self._submit(turn, reply, announce=True)

    async def _submit(
        self, turn: Turn, reply: Reply, *, announce: bool
    ) -> asyncio.Task[None] | None:
        config = await asyncio.to_thread(self._live.current)
        workspace = self._workspace_of(turn, config)
        if workspace is None:
            # The binding went away while the message was being prepared; the sender
            # gets the same notice a queued message gets.
            await reply.send(routing.UNBOUND_NOTICE)
            return None
        turn = replace(turn, workspace=workspace)
        conversation = routing.conversation_key(
            turn.transport, turn.channel, turn.thread, is_dm=turn.is_dm
        )
        lock = self._locks.setdefault(conversation, asyncio.Lock())
        if lock.locked():
            queue = self._queues.setdefault(conversation, deque())
            if announce and len(queue) >= MAX_QUEUE:
                await reply.send(
                    f"Queue full ({MAX_QUEUE}). Try again once the current work finishes."
                )
                return None
            queue.append(_Queued(turn, reply))
            log.info("queued #%d for %s", len(queue), conversation)
            if announce:
                await reply.send(f"Queued (#{len(queue)}): {preview(turn.text)}")
            return None

        # Acquire before returning so an ingress sequencer can safely admit the
        # next prepared turn without it overtaking this one.
        await lock.acquire()
        started = asyncio.Event()
        drain = asyncio.create_task(
            self._drain(conversation, turn, reply, lock, started),
            name=f"conversation:{conversation}",
        )
        try:
            await started.wait()
        except BaseException:
            await _cancel_and_wait(drain)
            raise
        self._drains[conversation] = drain

        def forget(done: asyncio.Task[None]) -> None:
            if self._drains.get(conversation) is done:
                del self._drains[conversation]

        drain.add_done_callback(forget)
        return drain

    async def _drain(
        self,
        conversation: str,
        turn: Turn,
        reply: Reply,
        lock: asyncio.Lock,
        started: asyncio.Event,
    ) -> None:
        """Run the accepted turn and every later turn queued behind it."""
        try:
            started.set()
            while True:
                try:
                    await self._run_turn(conversation, turn, reply)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    log.warning("turn canceled without canceling the conversation drain")
                except Exception:
                    # Keep ownership of the conversation until older queued turns
                    # have run; releasing here would let a newer arrival overtake.
                    log.exception("turn failed before the conversation queue drained")

                pending = self._queues.get(conversation)
                if not pending:
                    break
                item = pending.popleft()
                log.info("dequeued for %s (%d left)", conversation, len(pending))
                turn, reply = item.turn, item.reply
        finally:
            lock.release()
            self._forget_idle(conversation, lock)

    def _forget_idle(self, conversation: str, lock: asyncio.Lock) -> None:
        """Drop the coordination state of a conversation whose drain just finished.

        Called from the drain's release, where nothing has yielded since the drain saw an
        empty queue: ``_submit`` never awaits between reading ``_locks`` and either queueing
        behind the lock or acquiring it, so an arrival is already in that queue or has not
        looked yet, and a later one simply installs a fresh lock. The lock never has waiters
        for the same reason, so an unlocked lock has no owner to strand. The identity check
        keeps this drain from evicting coordination state that belongs to a newer one.
        """
        if self._locks.get(conversation) is not lock or lock.locked():
            return
        if self._queues.get(conversation):
            return
        del self._locks[conversation]
        self._queues.pop(conversation, None)

    async def stop(self, conversation: str) -> str:
        """Cancel preparation and execution, then drop everything already queued."""
        # Detach inbound preparation before yielding so messages that arrive after
        # this stop are placed in a fresh ingress queue and are not dropped with
        # the messages that preceded it.
        current = asyncio.current_task()
        ingress = self._ingress.get(conversation)
        dropped = self.queued(conversation)
        ingress_task: asyncio.Task[None] | None = None
        stopped_preparing = False
        if ingress is not None:
            ingress.pending.clear()
            if ingress.task is not None and ingress.task is not current:
                stopped_preparing = not ingress.task.done()
                if self._ingress.get(conversation) is ingress:
                    del self._ingress[conversation]
                ingress_task = ingress.task
                ingress_task.cancel()

        queue = self._queues.get(conversation)
        if queue:
            queue.clear()
        running = self._running.get(conversation)

        # Stop provider work before waiting for attachment/network cleanup from
        # the canceled ingress task; cleanup must not delay the user's stop.
        if running is not None:
            running.stopping = True
            if running.process is not None:
                await terminate_process_tree(running.process, f"stop {conversation}", grace=0.5)
            if running.task is not None:
                running.task.cancel()
                # Let the turn unwind before reporting, so "Stopped" is true and a
                # follow-up command (clear, status) finds the conversation released.
                await asyncio.wait({running.task}, timeout=STOP_UNWIND_SECONDS)

        if ingress_task is not None:
            await asyncio.gather(ingress_task, return_exceptions=True)

        parts = []
        if running is None:
            parts.append(
                "Stopped preparing a message." if stopped_preparing else "Nothing is running."
            )
        else:
            parts.append(
                f"Stopped after {format_elapsed(int(time.monotonic() - running.started))}."
            )
        if dropped:
            parts.append(f"Dropped {dropped} queued message{'s' if dropped != 1 else ''}.")
        return " ".join(parts)

    async def clear(self, conversation: str) -> str:
        """Forget every provider session for a conversation, deleting local session data."""
        while True:
            if self.busy(conversation):
                # A preparing or running turn may read or refresh its session row, so
                # clearing now would race that turn.
                return "A message is running. Stop it first (or wait), then clear."
            earlier = self._clearing.get(conversation)
            if earlier is None:
                break
            await earlier
        # Nothing yields between the check above and this marker, and a turn admitted
        # from here on waits for it before reading its session (_run_turn_inner), so
        # no turn can resume a session mid-delete and then write it back.
        clearing: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._clearing[conversation] = clearing
        try:
            sessions = await asyncio.to_thread(db.delete_sessions, self.paths, conversation)
            had_selection = self._selected.pop(conversation, None) is not None
            if not sessions:
                return (
                    "Cleared selection. The next message uses the configured default."
                    if had_selection
                    else "No session to clear."
                )
            lines = ["Cleared. The next message starts a fresh session."]
            for session in sessions:
                summary = await asyncio.to_thread(self._forget_local, session)
                lines.append(f"{session.provider}: {summary}")
            log.info("cleared %d session(s) for %s", len(sessions), conversation)
            return "\n".join(lines)
        finally:
            if self._clearing.get(conversation) is clearing:
                del self._clearing[conversation]
            clearing.set_result(None)

    # -- One turn --

    async def _run_turn(self, conversation: str, turn: Turn, reply: Reply) -> None:
        turn_id = logctx.new_turn_id()
        with logctx.context(f"t:{turn_id}"):
            # Registered before anything yields, so a stop or status between the drain
            # taking this turn and its config snapshot landing still finds it.
            running = Running(turn_id=turn_id, started=time.monotonic())
            self._running[conversation] = running
            task = asyncio.create_task(self._run_turn_inner(conversation, turn, reply, running))
            running.task = task
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                log.info("turn stopped")
            finally:
                if self._running.get(conversation) is running:
                    del self._running[conversation]

    def _forget_local(self, session: db.Session) -> str:
        """Delete a session's local data where it was created; returns a one-line summary."""
        cwd = str(self.paths.workspace(session.workspace))
        try:
            return self._provider(self.config, session.provider).clear_session(
                session.session_id, cwd
            )
        except Exception as exc:
            return f"could not delete local session data ({exc})"

    def _session_for(self, conversation: str, provider: str, workspace: str) -> db.Session | None:
        """The session to resume; rows an earlier binding or a broken id left behind are dropped."""
        current = None
        for session in db.get_sessions(self.paths, conversation):
            if usable(session, workspace):
                if session.provider == provider:
                    current = session
                continue
            reason = (
                f"workspace changed {session.workspace} -> {workspace}"
                if session.workspace != workspace
                else "session id outside the provider's contract"
            )
            db.delete_session(self.paths, conversation, session.provider)
            log.info(
                "dropping %s session (%s): %s",
                session.provider,
                reason,
                self._forget_local(session),
            )
        return current

    @staticmethod
    def _provider(config: Config, name: str) -> BaseProvider:
        return make_provider(name, config.providers[name].path)

    @staticmethod
    def assemble_prompt(turn: Turn, *, background: str = "", rich: bool = False) -> str:
        """Origin, background sends, context, attachments, the text, and the rich contract.

        The origin block leads because it is the only part Enso wrote itself; everything
        after it is data somebody else supplied.
        """
        parts = [origin_block(turn), background, turn.context]
        if turn.files:
            parts.append("Attached files:\n" + "\n".join(turn.files))
        parts.append(turn.text)
        if rich:
            parts.append(outbound.CONTRACT)
        return "\n\n".join(part for part in parts if part)

    def _env(self, reply: Reply, workspace: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(reply.origin_env())
        env["ENSO_WORKSPACE"] = workspace
        env["ENSO_HOME"] = str(self.paths.home)
        return env

    def _workspace_of(self, turn: Turn, config: Config) -> str | None:
        """Recheck admission without changing a turn's selected workspace."""
        key = routing.binding_key(
            turn.transport, turn.channel, is_dm=turn.is_dm, user_id=turn.user_id
        )
        return routing.workspace_for(config, key, workspace=turn.workspace)

    async def _run_turn_inner(
        self, conversation: str, turn: Turn, reply: Reply, running: Running
    ) -> None:
        # One snapshot per turn: the agent, launch arguments, timeout, and receipt
        # hash all come from it, read here rather than at admission because the turn may
        # have waited in the queue.
        config = await asyncio.to_thread(self._live.current)
        workspace = self._workspace_of(turn, config)
        if workspace is None:
            await reply.send(routing.UNBOUND_NOTICE)
            return
        running.agent = self.current_agent(conversation, workspace, config)
        running.config = config
        try:
            await self._turn(conversation, workspace, turn, reply, running)
        finally:
            # The agent knows what it sent; those rows must not return as context.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    messages.consume_own, self.paths, f"turn:{conversation}", workspace=workspace
                )

    async def _turn(
        self, conversation: str, workspace: str, turn: Turn, reply: Reply, running: Running
    ) -> None:
        clearing = self._clearing.get(conversation)
        if clearing is not None:
            # Admitted while a clear was in flight: let it finish before reading the session.
            await clearing
        agent, config = running.agent, running.config
        provider = self._provider(config, agent.provider)
        background = await asyncio.to_thread(
            messages.take_background,
            self.paths,
            turn.transport,
            turn.channel,
            None if turn.is_dm else turn.thread,
            workspace=workspace,
        )
        prompt = self.assemble_prompt(
            turn, background=messages.render(background), rich=reply.rich_format
        )
        log.info(
            "received %s from %s (%s) in %s len=%d files=%d background=%d",
            turn.transport,
            turn.user_name or turn.user_id,
            turn.user_id,
            conversation,
            len(turn.text),
            len(turn.files),
            len(background),
        )
        if self.debug:
            log.debug("prompt:\n%s", prompt)
        await reply.typing()
        status_id = await self._post_status(reply, status_text(agent, 0, STATUS_INITIAL_ACTION))
        stop = asyncio.Event()
        ticker = asyncio.create_task(self._ticker(reply, status_id, running, stop))
        deadline = time.monotonic() + config.agent_timeout if config.agent_timeout else None

        async def collect(prompt: str) -> tuple[_Response, bool]:
            return await self._collect(
                provider, prompt, conversation, workspace, turn, reply, running, deadline
            )

        try:
            collected, timed_out = await collect(prompt)
            text = provider.format_response(collected.parts)
            rich: OutboundMessage | None = None
            response_ok = not collected.error and bool(text.strip())
            if reply.rich_format and not timed_out and not collected.error:
                rich, text, timed_out, response_ok = await self._rich_response(
                    text, collect, provider, running
                )
            await self._stop_ticker(ticker, stop)
            if timed_out:
                log.warning("turn timed out after %ss", config.agent_timeout)
                await self._finish_status(
                    reply, status_id, self._timeout_notice(config.agent_timeout)
                )
                return
            await self._finish_status(reply, status_id, None)
            log.info(
                "done parts=%d response_len=%d rich=%s error=%s elapsed=%ds",
                len(collected.parts),
                len(text),
                rich is not None,
                bool(collected.error),
                running.elapsed,
            )
            if collected.error:
                await reply.send(format_error(collected.error[:4000]))
            elif not response_ok and text == outbound.FAILURE_NOTICE:
                await reply.send(text)
            elif rich is not None or text.strip():
                await reply.deliver(text, rich)
            else:
                await reply.send("(No response)")
            if response_ok:
                try:
                    await asyncio.to_thread(
                        connection_setup.record_reply,
                        self.paths,
                        {"provider": agent.provider, "model": agent.model, "effort": agent.effort},
                        turn.transport,
                        turn.user_id,
                        turn.channel,
                        config.source_hash or "unpersisted",
                    )
                except OSError, ValueError, KeyError, TypeError, connection_setup.PairingError:
                    log.warning("could not record onboarding reply confirmation")
        except asyncio.CancelledError:
            await self._stop_ticker(ticker, stop)
            await self._finish_status(reply, status_id, "Stopped.")
            raise
        except Exception as exc:
            await self._stop_ticker(ticker, stop)
            log.exception("turn failed")
            await self._finish_status(reply, status_id, None)
            for chunk in split_text(format_error(str(exc)), reply.limit):
                await reply.send(chunk)

    @staticmethod
    async def _rich_response(
        text: str,
        collect: Callable[[str], Awaitable[tuple[_Response, bool]]],
        provider: BaseProvider,
        running: Running,
    ) -> tuple[OutboundMessage | None, str, bool, bool]:
        """Parse an ``enso-message`` reply, asking the provider once to fix an invalid one.

        Returns the message to send (else the text to send) and whether the correction
        turn timed out, then whether the provider produced a successful response.
        """
        try:
            return outbound.parse_outbound_message(text), text, False, bool(text.strip())
        except outbound.EnvelopeError as exc:
            first = exc
        log.info("invalid enso-message (%s); asking for a correction", first.reason)
        running.action = "Correcting response formatting"
        collected, timed_out = await collect(outbound.repair_prompt(first.reason))
        if timed_out:
            return None, text, True, False
        fallback = first.fallback
        repaired = provider.format_response(collected.parts)
        if collected.error:
            log.warning("correction turn failed: %s", collected.error[:200])
        elif not repaired.strip():
            log.warning("correction turn returned no text")
        else:
            try:
                return outbound.parse_outbound_message(repaired), repaired, False, True
            except outbound.EnvelopeError as exc:
                log.warning("enso-message still invalid after correction: %s", exc.reason)
                fallback = exc.fallback or fallback
        return None, fallback or outbound.FAILURE_NOTICE, False, False

    @staticmethod
    def _timeout_notice(seconds: int) -> str:
        duration = f"{seconds // 60}-minute" if seconds % 60 == 0 else f"{seconds}-second"
        return f"Stopped after reaching the {duration} timeout."

    async def _collect(
        self,
        provider: BaseProvider,
        prompt: str,
        conversation: str,
        workspace: str,
        turn: Turn,
        reply: Reply,
        running: Running,
        deadline: float | None,
    ) -> tuple[_Response, bool]:
        """Stream the provider to completion under the deadline, retrying once if transient."""
        collected = _Response()
        config = running.config
        args = config.provider_args(workspace, provider.name)
        env = self._env(reply, workspace)
        cwd = str(self.paths.workspace(workspace))

        async def consume() -> None:
            async for event in self._run_provider(
                provider, prompt, conversation, workspace, running, args, env, cwd
            ):
                if event.kind == "response":
                    collected.parts.append(event.text)
                elif event.kind == "status":
                    running.action = event.text
                elif event.kind == "error":
                    collected.error = event.text

        timed_out = await self._run_until(consume(), deadline)
        if not timed_out and collected.error and provider.retryable_error(collected.error):
            log.info("retrying once after transient error: %s", collected.error[:200])
            collected = _Response()
            timed_out = await self._run_until(consume(), deadline)
        return collected, timed_out

    async def _run_until(self, work: Coroutine[Any, Any, None], deadline: float | None) -> bool:
        """Run ``work``; True only when our deadline expired first."""
        if deadline is None:
            await work
            return False
        task = asyncio.create_task(work)
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
            if task in done:
                await task
                return False
            await _cancel_and_wait(task)
            return True
        except BaseException:
            if not task.done():
                await _cancel_and_wait(task)
            raise

    # -- Provider process --

    async def _run_provider(
        self,
        provider: BaseProvider,
        prompt: str,
        conversation: str,
        workspace: str,
        running: Running,
        args: tuple[str, ...],
        env: dict[str, str],
        cwd: str,
    ) -> AsyncIterator[StreamEvent]:
        """Spawn the provider CLI, stream its events, and keep the session row current."""
        agent = running.agent
        session = await asyncio.to_thread(self._session_for, conversation, provider.name, workspace)
        session_id = session.session_id if session else None
        new_session = session is None and provider.assigns_session_id
        if new_session:
            session_id = str(uuid.uuid4())
        stream = ProviderStream(provider, session_id, new_session=new_session)
        cmd = provider.command(
            prompt,
            agent.model,
            agent.effort,
            args,
            session_id=session_id,
            new_session=new_session,
            cwd=cwd,
        )
        log.info(
            "spawn %s %s %s session=%s%s cwd=%s",
            provider.name,
            agent.model,
            agent.effort,
            (session_id or "-")[:8],
            " (new)" if new_session else "",
            cwd,
        )
        log.debug("command %s", logctx.redacted_command(cmd))
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
        running.process = process
        stored_id = session.session_id if session else None

        async def persist_session() -> None:
            nonlocal stored_id
            if stream.session_id is not None and stream.session_id != stored_id:
                await asyncio.to_thread(
                    db.set_session,
                    self.paths,
                    conversation,
                    provider.name,
                    stream.session_id,
                    workspace,
                )
                stored_id = stream.session_id

        try:
            async with contextlib.aclosing(stream.read(process, debug=self.debug)) as events:
                async for event in events:
                    await persist_session()
                    if event.kind == "error":
                        if running.stopping:
                            continue
                        log.error("provider error: %s", event.text)
                    yield event
        except ProtocolError as exc:
            log.error("provider error: %s", exc)
            yield StreamEvent(kind="error", text=str(exc))
        finally:
            try:
                if process.returncode is None:
                    await terminate_process_tree(process, f"{provider.name} {conversation}")
            finally:
                # A conflicting announcement can fail before an event is delivered;
                # retain the original identity even on that path.
                await persist_session()
                if session is not None:
                    await asyncio.to_thread(
                        db.touch_session, self.paths, conversation, provider.name
                    )
                log.info("exit=%s events=%d", process.returncode, stream.events)
                running.process = None

    # -- Status message --

    @staticmethod
    async def _post_status(reply: Reply, text: str) -> str | None:
        try:
            return await reply.status_post(text)
        except Exception:
            log.warning("could not post status message", exc_info=True)
            return None

    @staticmethod
    async def _finish_status(reply: Reply, status_id: str | None, text: str | None) -> None:
        """Delete the status message, or replace it with a final line."""
        if status_id is None:
            if text:
                with contextlib.suppress(Exception):
                    await reply.send(text)
            return
        try:
            if text:
                await reply.status_edit(status_id, text)
            else:
                await reply.status_delete(status_id)
        except Exception:
            log.warning("could not finalize status message", exc_info=True)
            if text:
                with contextlib.suppress(Exception):
                    await reply.send(text)

    @staticmethod
    async def _stop_ticker(ticker: asyncio.Task[Any], stop: asyncio.Event) -> None:
        stop.set()
        error = await _cancel_and_wait(ticker)
        if error is not None and not isinstance(error, asyncio.CancelledError):
            log.warning("status ticker failed: %s", error)

    @staticmethod
    async def _ticker(
        reply: Reply, status_id: str | None, running: Running, stop: asyncio.Event
    ) -> None:
        """Edit the status every second for 30s, then every 5s; refresh typing every 4s."""
        edits_enabled = status_id is not None
        failures = 0
        while not stop.is_set():
            await asyncio.sleep(1)
            if stop.is_set():
                break
            running.elapsed += 1
            elapsed = running.elapsed
            if edits_enabled and _status_edit_due(elapsed):
                assert status_id is not None
                text = status_text(running.agent, elapsed, running.action)
                try:
                    await asyncio.wait_for(reply.status_edit(status_id, text), timeout=5.0)
                    failures = 0
                except Exception:
                    failures += 1
                    log.debug("status edit failed (%d)", failures, exc_info=True)
                    if failures >= STATUS_MAX_EDIT_FAILURES:
                        edits_enabled = False
                        log.warning("status edits disabled after %d failures", failures)
            if elapsed % 4 == 0:
                with contextlib.suppress(Exception):
                    await reply.typing()
