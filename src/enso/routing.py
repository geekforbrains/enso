"""Bindings → workspace, conversation keys, and defaults → agent resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config, require_workspace
from .providers import provider_class
from .transport_registry import TRANSPORTS

log = logging.getLogger(__name__)

UNBOUND_NOTICE = "This conversation is not bound to an available workspace."


@dataclass(frozen=True)
class ResolvedAgent:
    """The provider/model/effort a conversation runs with and where it came from."""

    provider: str
    model: str
    effort: str
    source: str  # "defaults" | "workspace"


def binding_key(transport: str, channel: str, *, is_dm: bool = False, user_id: str = "") -> str:
    """The ``bindings`` key for a message's location."""
    return TRANSPORTS[transport].binding_key(channel, is_dm=is_dm, user_id=user_id)


def conversation_key(
    transport: str, channel: str, thread: str | None, *, is_dm: bool = False
) -> str:
    """Keyed per thread; a DM (threads included) or Telegram chat is one conversation."""
    if is_dm or not thread:
        return f"{transport}:{channel}"
    return f"{transport}:{channel}:{thread}"


def workspace_for(config: Config, key: str, *, workspace: str = "") -> str | None:
    """Admit an existing binding, retaining a queued turn's original workspace.

    Both the current binding and a previously selected workspace must still exist;
    neither a broken binding nor a removed directory may select a fallback.
    """
    bound = config.bindings.get(key)
    if bound is None:
        log.info("dropping turn: %s is no longer bound", key)
        return None
    try:
        require_workspace(config.paths, bound)
        if workspace and workspace != bound:
            require_workspace(config.paths, workspace)
    except ValueError as exc:
        log.warning("dropping turn: %s: %s", key, exc)
        return None
    return workspace or bound


def clamp_effort(provider: str, model: str, effort: str) -> str:
    """The effort the model actually supports, logged when it differs from the request."""
    clamped = provider_class(provider).clamp_effort(effort, model)
    if clamped != effort:
        log.info("effort %s clamped to %s for %s %s", effort, clamped, provider, model)
    return clamped


def resolve_agent(config: Config, workspace: str) -> ResolvedAgent:
    """``defaults`` unless the workspace has its own ``agent`` block; effort clamped."""
    override = config.workspaces.get(workspace)
    if override is not None and override.agent is not None:
        agent, source = override.agent, "workspace"
    else:
        agent, source = config.defaults, "defaults"
    effort = clamp_effort(agent.provider, agent.model, agent.effort)
    return ResolvedAgent(agent.provider, agent.model, effort, source)
