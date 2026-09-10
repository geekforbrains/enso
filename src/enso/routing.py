"""Bindings → workspace, conversation keys, and defaults → agent resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .providers import provider_class

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedAgent:
    """The provider/model/effort a conversation runs with and where it came from."""

    provider: str
    model: str
    effort: str
    source: str  # "defaults" | "workspace"


def binding_key(transport: str, channel: str, *, is_dm: bool = False, user_id: str = "") -> str:
    """The ``bindings`` key for a message's location."""
    if transport == "slack" and is_dm:
        return f"slack:dm:{user_id}"
    return f"{transport}:{channel}"


def conversation_key(
    transport: str, channel: str, thread: str | None, *, is_dm: bool = False
) -> str:
    """Keyed per thread; a DM (threads included) or Telegram chat is one conversation."""
    if is_dm or not thread:
        return f"{transport}:{channel}"
    return f"{transport}:{channel}:{thread}"


def workspace_for(config: Config, key: str) -> str | None:
    return config.bindings.get(key)


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
