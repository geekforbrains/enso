"""Shared command handlers — transport-agnostic logic for bot commands."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .config import CONFIG_DIR
from .providers import PROVIDER_NAMES

if TYPE_CHECKING:
    from .core import Runtime

log = logging.getLogger(__name__)


def cmd_stop(runtime: Runtime, conv_id: str) -> tuple[bool, str | None]:
    """Stop a running process. Returns (had_something, error_msg).

    Queue clearing is handled by the caller (transport/runtime dispatch layer).
    """
    import asyncio

    loop = asyncio.get_event_loop()
    return loop.run_until_complete(runtime.stop_chat(conv_id))


async def cmd_stop_async(runtime: Runtime, conv_id: str) -> tuple[bool, str | None]:
    """Async version of cmd_stop."""
    return await runtime.stop_chat(conv_id)


def cmd_status(runtime: Runtime, conv_id: str) -> str:
    """Return provider and model info for a conversation."""
    provider = runtime.get_active_provider(conv_id)
    model = runtime.get_active_model(conv_id, provider)
    return f"Provider: {provider}\nModel: {model}"


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
