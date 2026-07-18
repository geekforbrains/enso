"""Shared command handlers — transport-agnostic logic for bot commands."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .config import CONFIG_DIR, claude_cfg, save_config
from .providers import PROVIDER_NAMES, provider_class

if TYPE_CHECKING:
    from .core import Runtime
    from .updater import UpdateResult

log = logging.getLogger(__name__)


def _get_claude_runner(runtime: Runtime, *, key: str = "runner") -> str:
    """Return the effective Claude runner for the given config key.

    ``key`` is ``"runner"`` for interactive chat or ``"job_runner"`` for
    background jobs — the two are configured independently.
    """
    return "kage" if claude_cfg(runtime.config).get(key) == "kage" else "print"


def _runner_label(runner: str) -> str:
    """Return the user-facing label for a Claude runner value."""
    return "kage" if runner == "kage" else "claude -p"


def _kage_menu(runtime: Runtime) -> list[tuple[str, str, bool]]:
    """Build the kage toggle menu: (callback_data, label, active) per option."""
    chat = _get_claude_runner(runtime, key="runner")
    jobs = _get_claude_runner(runtime, key="job_runner")
    return [
        ("kage:on", "Interactive: kage", chat == "kage"),
        ("kage:off", "Interactive: claude -p", chat != "kage"),
        ("kage:jobs:on", "Jobs: kage", jobs == "kage"),
        ("kage:jobs:off", "Jobs: claude -p", jobs != "kage"),
    ]


async def cmd_stop_async(runtime: Runtime, conv_id: str) -> str:
    """Stop any running process, clear the queue, and describe what happened."""
    queued_count = runtime.clear_queue(conv_id)
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


def cmd_status(runtime: Runtime, conv_id: str) -> str:
    """Return provider, model, and effort info for a conversation."""
    provider = runtime.get_active_provider(conv_id)
    model = runtime.get_active_model(conv_id, provider)
    lines = [f"Provider: {provider}", f"Model: {model}"]
    if provider == "claude":
        lines.append(f"Runner: {_runner_label(_get_claude_runner(runtime))}")
        lines.append(
            f"Job runner: {_runner_label(_get_claude_runner(runtime, key='job_runner'))}"
        )
    effort = runtime.get_active_effort(conv_id, provider, model)
    if effort:
        lines.append(f"Effort: {effort}")
    return "\n".join(lines)


def cmd_use(
    runtime: Runtime, conv_id: str, choice: str | None,
) -> tuple[str | None, list[tuple[str, bool]]]:
    """Switch provider or list available providers.

    If choice is given and valid, switches and returns (response_text, []).
    If no choice, returns (None, [(name, is_active), ...]) for the transport
    to render in its native UI.
    """
    if choice and choice in PROVIDER_NAMES:
        runtime.active_provider_by_chat[conv_id] = choice
        runtime.save_state()
        return f"Provider set to {choice}.", []

    active = runtime.get_active_provider(conv_id)
    options = [(p, p == active) for p in PROVIDER_NAMES]
    return None, options


def cmd_model(
    runtime: Runtime, conv_id: str, choice: str | None,
) -> tuple[str | None, list[tuple[str, bool]]]:
    """Switch model or list available models.

    If choice is given and valid, switches and returns (response_text, []).
    If no choice, returns (None, [(name, is_active), ...]) for the transport
    to render in its native UI.
    """
    provider = runtime.get_active_provider(conv_id)
    models = runtime.models.get(provider, [])

    if choice:
        # Support numeric index
        if choice.isdigit():
            idx = int(choice) - 1
            if not (0 <= idx < len(models)):
                return f"Invalid index. Use 1-{len(models)}.", []
            selected = models[idx]
        elif choice in models:
            selected = choice
        else:
            return f"Unknown model '{choice}'.", []
        runtime.active_model_by_chat_provider[(conv_id, provider)] = selected
        runtime.save_state()
        return f"{provider} model \u2192 {selected}", []

    if not models:
        return f"No models configured for {provider}.", []

    active = runtime.get_active_model(conv_id, provider)
    options = [(m, m == active) for m in models]
    return None, options


def cmd_effort(
    runtime: Runtime, conv_id: str, choice: str | None,
) -> tuple[str | None, list[tuple[str, bool]]]:
    """Switch reasoning effort or list levels supported by the active model.

    ``choice`` may be a level name, a 1-based index, or ``default`` to
    clear the per-chat override and fall back to the CLI's own default.
    When no choice is given, returns options for the transport to render
    in its native picker UI — only levels the current model supports are
    included.
    """
    provider = runtime.get_active_provider(conv_id)
    provider_cls = provider_class(provider)
    levels = provider_cls.effort_levels
    if not levels:
        return f"Effort is only supported for Claude and Codex (current: {provider}).", []

    model = runtime.get_active_model(conv_id, provider)
    key = (conv_id, provider, model)

    if choice:
        normalized = choice.strip().lower()
        if normalized == "default":
            runtime.effort_by_chat_provider_model.pop(key, None)
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

        runtime.effort_by_chat_provider_model[key] = selected
        runtime.save_state()
        effective = provider_cls.clamp_effort(selected, model)
        if effective != selected:
            return (
                f"Effort \u2192 {selected} "
                f"(clamped to {effective} for {model}).",
                [],
            )
        return f"Effort \u2192 {selected}", []

    active = runtime.effort_by_chat_provider_model.get(key)
    options = [(level, level == active) for level in provider_cls.supported_efforts(model)]
    return None, options


def cmd_kage(
    runtime: Runtime, conv_id: str, choice: str | None,
) -> tuple[str | None, list[tuple[str, str, bool]]]:
    """Toggle whether Claude runs through kage or native claude -p.

    Interactive chat and background jobs are configured independently:
    ``runner`` governs chat, ``job_runner`` governs jobs. ``choice`` is the
    raw argument string. Supported forms (optionally prefixed with ``jobs``):
    empty/``show`` \u2192 menu; ``on``/``off``/``toggle``/``status``.

    Returns ``(response_text | None, options)`` where each option is
    ``(callback_data, label, active)`` for rendering toggle buttons.
    """
    del conv_id  # Runner mode is global config, not per-chat state.

    tokens = (choice or "").strip().lower().split()
    if tokens and tokens[0] in {"job", "jobs"}:
        key, label = "job_runner", "Job runner"
        tokens = tokens[1:]
    else:
        key, label = "runner", "Interactive runner"

    action = tokens[0] if tokens else ""
    runner = _get_claude_runner(runtime, key=key)

    if action in {"", "show"}:
        return None, _kage_menu(runtime)

    if action == "status":
        return f"{label}: {_runner_label(runner)}.", []

    if action == "toggle":
        target = "print" if runner == "kage" else "kage"
    elif action in {"on", "kage", "enable", "enabled", "true"}:
        target = "kage"
    elif action in {"off", "print", "native", "claude", "disable", "disabled", "false"}:
        target = "print"
    else:
        return (
            "Unknown kage mode. Use: on, off, toggle, or status "
            "(optionally prefixed with 'jobs').",
            [],
        )

    providers = runtime.config.setdefault("providers", {})
    claude_cfg = providers.setdefault("claude", {})
    claude_cfg[key] = target
    save_config(runtime.config)

    return (
        f"{label} \u2192 {_runner_label(target)}. "
        "Existing session state was left unchanged.",
        [],
    )


async def cmd_compact_async(runtime: Runtime, conv_id: str) -> str:
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

    provider = runtime.get_active_provider(conv_id)
    if not runtime.session_by_chat_provider.get((conv_id, provider)):
        return f"Nothing to compact — no active {provider} session for this chat."

    summary = await runtime.run_compaction(conv_id, provider)
    if not summary:
        return "Compaction failed — no summary produced. Session left untouched."

    # Clear only the active provider; cmd_clear without clear_all does that.
    cmd_clear(runtime, conv_id)
    runtime.compact_seed_by_chat[conv_id] = summary
    runtime.save_state()
    log.info(
        "Compacted %s session for chat %s (%d-char summary stashed)",
        provider, conv_id, len(summary),
    )
    return "Compacted. Continue the conversation — context will be preserved as a summary."


async def cmd_update_async(runtime: Runtime) -> UpdateResult:
    """Run the deterministic stable updater when no agent work is active."""
    import asyncio

    from .updater import UpdateResult, update_enso

    if runtime._update_in_progress:
        return UpdateResult("blocked", "Another Enso update is already running.")

    runtime._update_in_progress = True
    restart_pending = False
    try:
        active_chats = [
            task for task in runtime.running_task_by_chat.values()
            if not task.done()
        ]
        active_jobs = [
            task for task in runtime._running_job_tasks.values()
            if not task.done()
        ]
        if active_chats or active_jobs:
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
            runtime._update_in_progress = False


def cmd_clear(runtime: Runtime, conv_id: str, *, clear_all: bool = False) -> list[str]:
    """Clear sessions and return summary lines per provider."""
    parts = []
    for prov_name in PROVIDER_NAMES:
        if clear_all or runtime.get_active_provider(conv_id) == prov_name:
            sid = runtime.session_by_chat_provider.pop((conv_id, prov_name), None)
            provider = runtime.make_provider(prov_name)
            summary = provider.clear_session(sid, runtime.working_dir)
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
