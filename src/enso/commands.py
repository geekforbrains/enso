"""Chat commands shared by Slack and Telegram."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import routing
from .config import Agent, Config
from .formatting import format_elapsed, model_label
from .runtime import Runtime, usable
from .service import restart_command
from .transport_registry import TRANSPORTS
from .transports import Reply, Turn

log = logging.getLogger(__name__)

COMMANDS: tuple[tuple[str, str], ...] = (
    ("stop", "Stop the running process and drop queued messages"),
    ("clear", "Forget this conversation's selection and sessions"),
    ("status", "Workspace, agent, session, and queue for this conversation"),
    ("use", "Choose this conversation's provider, model, and effort"),
    ("help", "List these commands"),
    ("restart", "Restart the Enso service"),
)
# Telegram clients send /start when a chat is opened; answer it like /help.
ALIASES = {"start": "help"}
RESTART_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class Command:
    name: str
    args: str = ""


@dataclass
class Result:
    text: str
    after: Callable[[], None] | None = None  # runs once the reply has been delivered


def parse(text: str, transport: str) -> Command | None:
    """``!stop`` / ``/status@bot`` → ``Command``; anything else is a prompt."""
    prefix = TRANSPORTS[transport].prefix
    stripped = text.strip()
    if not stripped.startswith(prefix):
        return None
    body = stripped[len(prefix) :]
    # "!!!" or "! wow" are prose, not commands.
    if not body or not body[0].isalpha():
        return None
    parts = body.split(None, 1)
    name = parts[0].split("@", 1)[0].lower()
    return Command(ALIASES.get(name, name), parts[1].strip() if len(parts) > 1 else "")


def help_text(prefix: str) -> str:
    return "\n".join(f"{prefix}{name} — {description}" for name, description in COMMANDS)


def _age(stamp: str) -> str:
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return "?"
    return format_elapsed(max(0, int((datetime.now(UTC) - then).total_seconds())))


async def status_text(runtime: Runtime, conversation: str, workspace: str) -> str:
    """Workspace, effective agent and its source, sessions, running work, queue depth."""
    agent = runtime.current_agent(conversation, workspace)
    source = (
        "conversation selection"
        if agent.source == "conversation"
        else "defaults"
        if agent.source == "defaults"
        else f"workspaces.{workspace}.agent"
    )
    lines = [
        f"Workspace: {workspace}",
        f"Agent: {agent.provider} · {model_label(agent.model)} · {agent.effort} (from {source})",
    ]
    sessions = await runtime.sessions(conversation)
    if sessions:
        lines.extend(
            f"Session: {s.provider} {s.session_id[:8]} · started {_age(s.created_at)} ago"
            f" · active {_age(s.last_active)} ago"
            + ("" if usable(s, workspace) else f" · stale (created in workspace {s.workspace})")
            for s in sessions
        )
    else:
        lines.append("Session: none")
    running = runtime.running(conversation)
    if running is None:
        lines.append("Running: nothing")
    else:
        elapsed = format_elapsed(int(time.monotonic() - running.started))
        lines.append(f"Running: {elapsed} · ↳ {running.action}")
    lines.append(f"Queued: {runtime.queued(conversation)}")
    return "\n".join(lines)


def _use_help(runtime: Runtime, conversation: str, workspace: str, prefix: str) -> str:
    config = runtime.config
    current = runtime.current_agent(conversation, workspace, config)
    source = "selected here" if current.source == "conversation" else "configured default"
    return "\n".join(
        (
            f"Using {current.provider}:{current.model}:{current.effort} ({source}).",
            f"{prefix}use PROVIDER:MODEL:EFFORT — choose all three",
            f"{prefix}use model:MODEL — change only the model",
            f"{prefix}use effort:EFFORT — change only the effort",
            f"{prefix}use default — return to the configured default",
            f"Providers: {', '.join(config.providers)}",
        )
    )


def _use_choice(
    config: Config, current: routing.ResolvedAgent, spec: str, prefix: str
) -> Agent | str:
    """An exact triple, or a human-readable reason it cannot be selected."""
    partial = ""
    if spec.startswith("model:"):
        partial = "model"
        agent = Agent(current.provider, spec[len("model:") :], current.effort)
    elif spec.startswith("effort:"):
        partial = "effort"
        agent = Agent(current.provider, current.model, spec[len("effort:") :])
    else:
        provider, separator, remainder = spec.partition(":")
        model, final_separator, effort = remainder.rpartition(":")
        if not separator or not final_separator or not provider or not model or not effort:
            return f"Choose a complete triple, for example {prefix}use codex:sol:medium."
        agent = Agent(provider, model, effort)
    if not agent.provider or not agent.model or not agent.effort:
        return f"Use {prefix}use PROVIDER:MODEL:EFFORT, model:MODEL, effort:EFFORT, or default."
    provider_config = config.providers.get(agent.provider)
    if provider_config is None:
        return (
            f"Provider {agent.provider!r} is not configured. Choose: {', '.join(config.providers)}."
        )
    if agent.model not in provider_config.models:
        return (
            f"Model {agent.model!r} is not configured for {agent.provider}. "
            f"Choose: {', '.join(provider_config.models)}."
        )
    exact = routing.exact_efforts(agent.provider, agent.model)
    if agent.effort not in exact:
        direction = (
            f" Use {prefix}use {agent.provider}:{agent.model}:EFFORT to change both."
            if partial == "model"
            else ""
        )
        return (
            f"Effort {agent.effort!r} cannot be used with {agent.provider}:{agent.model}. "
            f"Choose: {', '.join(exact)}.{direction}"
        )
    return agent


async def _use_text(
    runtime: Runtime, conversation: str, binding: str, workspace: str, spec: str, prefix: str
) -> str:
    if not spec:
        return _use_help(runtime, conversation, workspace, prefix)
    config = runtime.config
    current = runtime.current_agent(conversation, workspace, config)
    if spec == "default":
        selected: Agent | None = None
    else:
        choice = _use_choice(config, current, spec, prefix)
        if isinstance(choice, str):
            return choice
        selected = choice
    if not runtime.select_agent(conversation, binding, workspace, selected):
        return (
            "A message is running or being cleared. Wait or stop it before changing the selection."
        )
    agent = runtime.current_agent(conversation, workspace, config)
    sessions = await runtime.sessions(conversation)
    resumed = any(s.provider == agent.provider and usable(s, workspace) for s in sessions)
    source = "configured default" if selected is None else "conversation selection"
    context = (
        f"Resuming the earlier {agent.provider} session; "
        "it may miss turns handled by other providers."
        if resumed
        else f"The next {agent.provider} turn starts a fresh provider session."
    )
    return f"Using {agent.provider}:{agent.model}:{agent.effort} ({source}). {context}"


def restart_service() -> None:
    """Restart through the service manager that runs this process, else re-exec ``serve``."""
    cmd = restart_command(os.getpid())
    if cmd is not None:
        log.info("restarting service: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("service restart failed (%s); re-executing instead", exc)
        else:
            if result.returncode == 0:
                return  # the service manager is stopping this process now
            log.warning(
                "service restart failed (exit %d): %s; re-executing instead",
                result.returncode,
                result.stderr.strip() or result.stdout.strip(),
            )
    argv = [sys.executable, "-m", "enso.cli", *sys.argv[1:]]
    log.info("re-executing: %s", " ".join(argv))
    os.execv(sys.executable, argv)


def _schedule_restart() -> None:
    # A short delay lets the transport acknowledge the event that carried the command.
    asyncio.get_running_loop().call_later(RESTART_DELAY_SECONDS, lambda: restart_service())


async def run(
    runtime: Runtime,
    command: Command,
    *,
    conversation: str,
    binding: str,
    workspace: str,
    transport: str,
) -> Result:
    """Execute one parsed command for a bound conversation."""
    prefix = TRANSPORTS[transport].prefix
    if command.name == "stop":
        return Result(await runtime.stop(conversation))
    if command.name == "clear":
        return Result(await runtime.clear(conversation))
    if command.name == "status":
        return Result(await status_text(runtime, conversation, workspace))
    if command.name == "use":
        return Result(
            await _use_text(runtime, conversation, binding, workspace, command.args, prefix)
        )
    if command.name == "help":
        return Result(help_text(prefix))
    if command.name == "restart":
        return Result("Restarting…", after=_schedule_restart)
    return Result(f"Unknown command {prefix}{command.name}. Try {prefix}help.")


async def dispatch(runtime: Runtime, turn: Turn, reply: Reply) -> bool:
    """Run ``turn`` as a command when it is one; True when it was handled."""
    command = parse(turn.text, turn.transport)
    if command is None:
        return False
    from .maintenance import paused

    key = routing.binding_key(turn.transport, turn.channel, is_dm=turn.is_dm, user_id=turn.user_id)
    workspace = routing.workspace_for(runtime.config, key, workspace=turn.workspace)
    if workspace is None:
        await reply.send(routing.UNBOUND_NOTICE)
        return True
    if paused(runtime.paths) and command.name not in {"status", "help", "stop"}:
        await reply.send("Enso is updating; wait for it to report ready before changing its state.")
        return True
    conversation = routing.conversation_key(
        turn.transport, turn.channel, turn.thread, is_dm=turn.is_dm
    )
    log.info(
        "command %s%s from %s in %s",
        TRANSPORTS[turn.transport].prefix,
        command.name,
        turn.user_name or turn.user_id,
        conversation,
    )
    result = await run(
        runtime,
        command,
        conversation=conversation,
        binding=key,
        workspace=workspace,
        transport=turn.transport,
    )
    await reply.send(result.text)
    if result.after is not None:
        result.after()
    return True
