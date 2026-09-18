"""Measure raw CLI events without the chat adapters' presentation-only projection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CODEX_TOOLS = {
    "command_execution",
    "file_change",
    "mcp_tool_call",
    "web_search",
    "collab_tool_call",
    "tool_call",
    "todo_list",
}


def count(value: object) -> int | None:
    """Missing, malformed and negative counts stay unknown instead of becoming zero."""
    return value if type(value) is int and value >= 0 else None


@dataclass
class Measurements:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_tokens: int | None = None
    output_tokens: int | None = None
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    output: str = ""
    completed: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage_source: str = "unavailable"
    raw_usage: dict[str, Any] = field(default_factory=dict)
    permission_denials: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["tool_calls"] = len(self.tools)
        value["tool_failures"] = sum(t.get("failed") is True for t in self.tools.values())
        value["unknown_tool_outcomes"] = sum(t.get("failed") is None for t in self.tools.values())
        value["measurement_complete"] = (
            self.completed
            and self.input_tokens is not None
            and self.output_tokens is not None
            and not self.errors
            and not self.warnings
            and not value["unknown_tool_outcomes"]
        )
        return value


def _codex(result: Measurements, event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "turn.completed":
        usage = event.get("usage", {})
        if result.completed:
            result.warnings.append("Multiple completed turns in a fresh-session run")
        result.completed = True
        result.raw_usage = usage
        result.usage_source = "turn.completed.usage"
        result.input_tokens = count(usage.get("input_tokens"))
        result.cached_input_tokens = count(usage.get("cached_input_tokens"))
        result.output_tokens = count(usage.get("output_tokens"))
    elif kind in {"error", "turn.failed"}:
        result.errors.append(str(event.get("error") or event.get("message") or kind))
    elif kind in {"item.started", "item.updated", "item.completed"}:
        item = event.get("item", {})
        item_type = item.get("type")
        if item_type == "agent_message" and kind == "item.completed":
            result.output = item.get("text", "")
        elif item_type in CODEX_TOOLS:
            key = item.get("id")
            if not isinstance(key, str):
                result.warnings.append("Tool item without an id; cannot count reliably")
                return
            previous = result.tools.get(key, {})
            failed = previous.get("failed")
            if kind == "item.completed":
                failed = bool(
                    item.get("status") in {"failed", "error"}
                    or item.get("error")
                    or item.get("exit_code") not in (None, 0)
                    or (isinstance(item.get("result"), dict) and item["result"].get("isError"))
                )
                if item_type == "command_execution" and item.get("exit_code") is None:
                    failed = True if failed else None
            result.tools[key] = {**previous, **item, "failed": failed}
        elif item_type not in {"agent_message", "reasoning"}:
            warning = f"Unrecognized Codex item type: {item_type}"
            if warning not in result.warnings:
                result.warnings.append(warning)


def _claude_usage(result: Measurements, event: dict[str, Any]) -> None:
    """Use the CLI's complete per-model counters, including cache and auxiliary calls."""
    result.raw_usage = {k: event[k] for k in ("usage", "modelUsage") if k in event}
    models = event.get("modelUsage")
    if isinstance(models, dict) and models:
        result.models = sorted(models)
        fields = ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens")
        totals: list[int | None] = []
        for name in fields:
            values = [count(usage.get(name)) for usage in models.values()]
            totals.append(sum(v for v in values if v is not None) if None not in values else None)
        uncached, cached, creation, output = totals
        result.usage_source = "result.modelUsage (all reported models)"
    else:
        usage = event.get("usage", {})
        uncached = count(usage.get("input_tokens"))
        cached = count(usage.get("cache_read_input_tokens"))
        creation = count(usage.get("cache_creation_input_tokens"))
        output = count(usage.get("output_tokens"))
        result.usage_source = "result.usage (conversation only)"
        result.warnings.append("Complete modelUsage unavailable; only conversation usage reported")
    if uncached is not None and cached is not None and creation is not None:
        result.input_tokens = uncached + cached + creation
    result.cached_input_tokens = cached
    result.cache_creation_tokens = creation
    result.output_tokens = output


def _claude(result: Measurements, event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "result":
        result.completed = True
        result.output = event.get("result", "")
        if event.get("is_error") or event.get("subtype") != "success":
            result.errors.append(str(event.get("errors") or event.get("subtype") or "CLI error"))
        result.permission_denials = event.get("permission_denials", [])
        for denial in result.permission_denials:
            key = denial.get("tool_use_id")
            if isinstance(key, str):
                tool = result.tools.setdefault(key, {"name": denial.get("tool_name")})
                tool["failed"] = True
            else:
                result.warnings.append("Permission denial without a tool id")
        _claude_usage(result, event)
    elif kind in {"assistant", "user"}:
        message = event.get("message", {})
        blocks = message.get("content", [])
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if block.get("type") == "tool_use":
                key = block.get("id")
                if not isinstance(key, str):
                    result.warnings.append("Tool use without an id; cannot count reliably")
                    continue
                previous = result.tools.get(key, {})
                result.tools[key] = {**block, "failed": previous.get("failed")}
            elif block.get("type") == "tool_result":
                key = block.get("tool_use_id")
                if not isinstance(key, str):
                    result.warnings.append("Tool result without an id")
                    continue
                tool = result.tools.setdefault(key, {})
                details = event.get("tool_use_result", {})
                failed = bool(block.get("is_error"))
                if isinstance(details, dict):
                    failed |= details.get("exit_code", details.get("exitCode")) not in (None, 0)
                    failed |= bool(details.get("interrupted"))
                tool.update(result=block, failed=failed)
            elif block.get("type") == "text" and kind == "assistant":
                result.output = block.get("text", "")


def measure(path: Path, provider: str) -> Measurements:
    """Parse a retained JSONL trace; partial traces remain inspectable and incomplete."""
    result = Measurements()
    parser = {"codex": _codex, "claude": _claude}[provider]
    with path.open(encoding="utf-8", errors="replace") as lines:
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
                parser(result, event)
            except (ValueError, TypeError, AttributeError, KeyError) as exc:
                result.warnings.append(f"Event {number}: {exc}")
    return result
