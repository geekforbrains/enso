"""Shared command handlers — transport-agnostic logic for bot commands."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .config import CONFIG_DIR
from .providers import PROVIDER_NAMES, provider_class

if TYPE_CHECKING:
    from .core import ExecutionContext, Runtime
    from .teams import Policy
    from .updater import UpdateResult

log = logging.getLogger(__name__)


async def cmd_stop_async(runtime: Runtime, conv_id: str) -> str:
    """Stop any running process, clear the queue, and describe what happened."""
    queued_count = await runtime.clear_queue(conv_id)
    had, error = await runtime.stop_chat(conv_id)
    if not had and not queued_count:
        return "Nothing running."
    if error:
        return f"Error stopping: {error}"
    parts = []
    if had:
        parts.append("Stopped.")
    if queued_count:
        parts.append(f"Cleared {queued_count} queued message(s).")
    return " ".join(parts)


def cmd_status(runtime: Runtime, settings_key: str, *, policy: Policy) -> str:
    """Return the effective route settings and where each value came from."""
    resolved = runtime.resolve_route_settings(settings_key, policy)
    labels = {
        "route": "route selection",
        "policy_default": "policy default",
        "provider_default": "provider default",
        "cli_default": "CLI default",
    }
    lines = [
        f"Provider: {resolved.provider} ({labels[resolved.provider_source]})",
        f"Model: {resolved.model} ({labels[resolved.model_source]})",
    ]
    if resolved.effort is None:
        lines.append(f"Effort: {labels[resolved.effort_source]}")
    else:
        lines.append(
            f"Effort: {resolved.effort} ({labels[resolved.effort_source]})"
        )
    return "\n".join(lines)


def cmd_use(
    runtime: Runtime,
    settings_key: str,
    choice: str | None,
    *,
    policy: Policy,
    providers: list[str] | None = None,
) -> tuple[str | None, list[tuple[str, bool]]]:
    """Switch provider or list available providers.

    If choice is given and valid, switches and returns (response_text, []).
    If no choice, returns (None, [(name, is_active), ...]) for the transport
    to render in its native UI. ``providers`` restricts both the picker and
    accepted choices — routed workspaces pass their usable allowlist, so a
    disallowed provider is refused rather than selected-and-failed later.
    """
    candidates = policy.providers if providers is None else providers
    available = [
        name
        for name in candidates
        if name in PROVIDER_NAMES and name in policy.providers
    ]
    if choice:
        normalized = choice.strip().lower()
        if normalized == "default":
            runtime.set_route_provider(settings_key, None)
            runtime.save_state()
            provider = runtime.resolve_route_settings(settings_key, policy).provider
            return f"Provider reset to policy default ({provider}).", []
        if normalized in available:
            runtime.set_route_provider(settings_key, normalized)
            runtime.save_state()
            return f"Provider set to {normalized}.", []
        if normalized in PROVIDER_NAMES:
            return f"Provider {normalized} is not available here.", []

    active = runtime.resolve_route_settings(settings_key, policy).provider
    options = [(p, p == active) for p in available]
    return None, options


def cmd_model(
    runtime: Runtime,
    settings_key: str,
    choice: str | None,
    *,
    policy: Policy,
) -> tuple[str | None, list[tuple[str, bool]]]:
    """Switch model or list available models.

    If choice is given and valid, switches and returns (response_text, []).
    If no choice, returns (None, [(name, is_active), ...]) for the transport
    to render in its native UI.
    """
    resolved = runtime.resolve_route_settings(settings_key, policy)
    provider = resolved.provider
    models = runtime.models.get(provider, [])

    if choice:
        normalized = choice.strip()
        if normalized.lower() == "default":
            runtime.set_route_model(settings_key, provider, None)
            runtime.save_state()
            model = runtime.resolve_route_settings(settings_key, policy).model
            return f"{provider} model reset to provider default ({model}).", []
        # Support numeric index
        if normalized.isdigit():
            idx = int(normalized) - 1
            if not (0 <= idx < len(models)):
                return f"Invalid index. Use 1-{len(models)}.", []
            selected = models[idx]
        elif normalized in models:
            selected = normalized
        else:
            return f"Unknown model '{choice}'.", []
        runtime.set_route_model(settings_key, provider, selected)
        runtime.save_state()
        return f"{provider} model \u2192 {selected}", []

    if not models:
        return f"No models configured for {provider}.", []

    active = resolved.model
    options = [(m, m == active) for m in models]
    return None, options


def cmd_effort(
    runtime: Runtime,
    settings_key: str,
    choice: str | None,
    *,
    policy: Policy,
) -> tuple[str | None, list[tuple[str, bool]]]:
    """Switch reasoning effort or list levels supported by the active model.

    ``choice`` may be a level name, a 1-based index, or ``default`` to
    clear the per-chat override and fall back to the CLI's own default.
    When no choice is given, returns options for the transport to render
    in its native picker UI — only levels the current model supports are
    included.
    """
    resolved = runtime.resolve_route_settings(settings_key, policy)
    provider = resolved.provider
    provider_cls = provider_class(provider)
    levels = provider_cls.effort_levels
    model = resolved.model

    if not levels:
        if provider == "agy":
            return (
                "Agy effort is selected through /model; choose an "
                "effort-qualified model variant.",
                [],
            )
        return f"{provider} does not support configurable effort.", []

    if choice:
        normalized = choice.strip().lower()
        if normalized == "default":
            runtime.set_route_effort(settings_key, provider, model, None)
            runtime.save_state()
            return f"Effort cleared (using {provider} CLI config/default).", []

        supported = provider_cls.supported_efforts(model)
        if normalized.isdigit():
            idx = int(normalized) - 1
            if not (0 <= idx < len(supported)):
                return f"Invalid index. Use 1-{len(supported)}.", []
            selected = supported[idx]
        elif normalized in levels:
            selected = normalized
        else:
            opts = ", ".join(levels)
            return f"Unknown effort '{choice}'. Choose: {opts}, or 'default'.", []

        runtime.set_route_effort(settings_key, provider, model, selected)
        runtime.save_state()
        effective = provider_cls.clamp_effort(selected, model)
        if effective != selected:
            return (
                f"Effort \u2192 {selected} "
                f"(clamped to {effective} for {model}).",
                [],
            )
        return f"Effort \u2192 {selected}", []

    active = resolved.effort
    options = [(level, level == active) for level in provider_cls.supported_efforts(model)]
    return None, options


async def cmd_compact_async(
    runtime: Runtime, conv_id: str, *, context: ExecutionContext,
) -> str:
    """Compact the active provider's session: summarise → clear → stash seed.

    Hidden summarisation runs through the live session; the summary becomes
    a seed prepended to the next user message in a fresh session. The user
    never sees the summary itself — only a brief confirmation.

    Refuses with guidance if a message is currently running for this chat;
    we don't want to interleave a destructive compact with in-flight work.
    """
    lock = runtime.get_chat_lock(conv_id)
    if lock.locked():
        return (
            "A message is currently running. Stop it (!stop) or wait for it "
            "to finish, then try again."
        )

    provider = context.provider
    if not runtime.session_by_chat_provider.get((conv_id, provider)):
        return f"Nothing to compact — no active {provider} session for this chat."

    summary = await runtime.run_compaction(conv_id, provider, context=context)
    if not summary:
        return "Compaction failed — no summary produced. Session left untouched."

    # Clear only the active provider; cmd_clear without clear_all does that.
    cmd_clear(runtime, conv_id, context=context)
    runtime.compact_seed_by_chat[conv_id] = summary
    runtime.save_state()
    log.info(
        "Compacted %s session for chat %s (%d-char summary stashed)",
        provider, conv_id, len(summary),
    )
    # Messages that queued while compaction held the chat lock would
    # otherwise wait for the next user message.
    runtime.kick_queue(conv_id)
    return "Compacted. Continue the conversation — context will be preserved as a summary."


async def cmd_update_async(runtime: Runtime) -> UpdateResult:
    """Run the deterministic stable updater when no agent work is active."""
    import asyncio

    from .updater import UpdateResult, update_enso

    if runtime.update_in_progress:
        return UpdateResult("blocked", "Another Enso update is already running.")

    runtime.update_in_progress = True
    restart_pending = False
    try:
        active_chats = [
            task for task in runtime.running_task_by_chat.values()
            if not task.done()
        ]
        active_jobs = runtime.jobs.running_here()
        # Jobs triggered from the dashboard or CLI run in other processes;
        # their cross-process run locks are the only visible signal here.
        external_jobs = runtime.jobs.running_elsewhere()
        if active_chats or active_jobs or external_jobs:
            return UpdateResult(
                "blocked",
                "Enso is busy with active agent work. Wait for it to finish "
                "or stop it, then update.",
            )
        result = await asyncio.to_thread(update_enso, runtime.config)
        restart_pending = result.restart_required
        return result
    finally:
        if not restart_pending:
            runtime.update_in_progress = False


def cmd_clear(
    runtime: Runtime,
    conv_id: str,
    *,
    context: ExecutionContext,
    clear_all: bool = False,
) -> list[str]:
    """Clear policy-allowed sessions and return one summary per provider."""
    parts = []
    allowed = [name for name in context.policy.providers if name in PROVIDER_NAMES]
    for prov_name in allowed:
        if clear_all or context.provider == prov_name:
            sid = runtime.session_by_chat_provider.pop((conv_id, prov_name), None)
            provider = runtime.make_provider(prov_name, context=context)
            summary = provider.clear_session(
                sid, context.path, policy_dir=context.policy.policy_dir
            )
            parts.append(f"{prov_name.capitalize()}: {summary}")
    runtime.save_state()
    return parts


def cmd_logs() -> str:
    """Return the last 25 log lines."""
    log_path = os.path.join(CONFIG_DIR, "enso.log")
    if not os.path.exists(log_path):
        return "No log file found."
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 32768))
            tail = f.read().decode(errors="replace")
        lines = tail.splitlines()[-25:]
        return "\n".join(lines) if lines else "(empty)"
    except Exception as exc:
        return f"Error reading logs: {exc}"


def cmd_help(commands: list[tuple[str, str]], prefix: str = "/") -> str:
    """Format a help message from a list of (name, description) tuples."""
    return "\n".join(f"{prefix}{name} \u2014 {desc}" for name, desc in commands)
