"""Validate beat definitions and gate files before they can become executable work."""

from __future__ import annotations

import json
import re
import stat
import subprocess
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Agent, Config, Paths, require_workspace
from ..providers import PROVIDER_CLASSES
from ..scheduling import next_cron, schedule_problem
from .models import Beat, Definition, HeartbeatError

DEFINITION_KEYS = tuple(Definition.__dataclass_fields__)
ORIGIN_KEYS = ("transport", "user_id", "user_name", "channel", "channel_name", "thread_ts")
MAX_GATE_BYTES = 64 * 1024


def parse_ref(ref: str) -> int:
    match = re.fullmatch(r"HB-(\d+)", ref.upper())
    if match is None or int(match[1]) < 1:
        raise HeartbeatError("beat references look like HB-001")
    return int(match[1])


def timestamp(value: object, name: str) -> str:
    """Normalize a timestamp to UTC, refusing implicit local time or date-only inputs."""
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        normalized = parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except ValueError, OverflowError:
        raise HeartbeatError(
            f"{name} must be an ISO timestamp with an explicit UTC offset"
        ) from None
    return normalized


def utc(value: datetime | None = None) -> str:
    current = value if value is not None else datetime.now(UTC)
    if current.tzinfo is None:
        raise HeartbeatError("current time must include a timezone")
    return current.astimezone(UTC).isoformat(timespec="microseconds")


def json_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise HeartbeatError(f"{name} must be a JSON object")
    try:
        # Round-tripping both validates and detaches caller-owned mutable containers.
        return json.loads(json.dumps(value, allow_nan=False))
    except TypeError, ValueError, RecursionError:
        raise HeartbeatError(f"{name} must contain valid JSON values") from None


def _text(data: dict[str, Any], key: str, problems: list[str]) -> str | None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{key} must be nonempty text")
        return None
    if "\x00" in value:
        problems.append(f"{key} must not contain NUL characters")
        return None
    return value.strip()


def _agent(data: dict[str, Any], config: Config, problems: list[str]) -> Agent:
    workspace = data.get("workspace")
    override = config.workspaces.get(workspace) if isinstance(workspace, str) else None
    default = override.agent if override and override.agent else config.defaults
    raw = data.get("agent")
    if raw is None:
        return default
    if not isinstance(raw, dict):
        problems.append("agent must be an object with provider, model, and effort")
        return default
    problems.extend(
        f"agent.{key} is not recognized" for key in raw if key not in Agent.__dataclass_fields__
    )
    values = {key: _text(raw, key, problems) or "" for key in Agent.__dataclass_fields__}
    provider = config.providers.get(values["provider"])
    if values["provider"] and provider is None:
        problems.append(f"agent.provider {values['provider']!r} is not configured")
    if provider is not None:
        if values["model"] and values["model"] not in provider.models:
            problems.append(f"agent.model is not in providers.{values['provider']}.models")
        levels = PROVIDER_CLASSES[values["provider"]].effort_levels
        if values["effort"] and values["effort"] not in levels:
            problems.append(f"agent.effort must be one of {', '.join(levels)}")
    return Agent(**values) if all(values.values()) else default


def _timing(data: dict[str, Any], problems: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("at", "expires_at", "followup_at"):
        if data.get(key) is not None:
            try:
                result[key] = timestamp(data[key], key)
            except HeartbeatError as exc:
                problems.extend(exc.problems)
    if (data.get("at") is None) == (data.get("schedule") is None):
        problems.append("give exactly one of at or schedule")
    if data.get("schedule") is not None:
        schedule = _text(data, "schedule", problems)
        if schedule:
            problem = schedule_problem(schedule)
            if problem:
                problems.append(problem)
            result["schedule"] = schedule
    zone = data.get("timezone", "UTC")
    try:
        if not isinstance(zone, str):
            raise ValueError
        ZoneInfo(zone)
    except ZoneInfoNotFoundError, ValueError:
        problems.append("timezone must be an IANA timezone such as America/Vancouver or UTC")
    else:
        result["timezone"] = zone
    expiry = result.get("expires_at")
    if expiry:
        for key in ("at", "followup_at"):
            if result.get(key) and result[key] >= expiry:
                problems.append(f"{key} must be before expires_at")
    return result


def _routing(data: dict[str, Any], config: Config, problems: list[str]) -> dict[str, Any]:
    raw = data.get("origin")
    if raw is None:
        raw = {}
    origin: dict[str, str] = {}
    if not isinstance(raw, dict):
        problems.append("origin must be an object of text fields")
    else:
        for key, value in raw.items():
            if key not in ORIGIN_KEYS:
                problems.append(f"origin.{key} is not recognized")
            elif not isinstance(value, str) or "\x00" in value:
                problems.append(f"origin.{key} must be text without NUL characters")
            else:
                origin[key] = value
    origin_target = None
    if origin.get("transport") and origin.get("channel"):
        origin_target = f"{origin['transport']}:{origin['channel']}"
    notify = data.get("notify")
    if notify is None:
        default_target = config.default_notify()
        notify = origin_target or (
            f"{default_target[0]}:{default_target[1]}" if default_target else None
        )
    if notify is not None:
        try:
            if not isinstance(notify, str):
                raise ValueError("must be text")
            transport, target = config.resolve_target(notify)
            notify = f"{transport}:{target}"
        except ValueError as exc:
            problems.append(f"notify: {exc}")
    thread = data.get("notify_thread")
    if thread is None and notify == origin_target:
        thread = origin.get("thread_ts") or None
    if thread is not None:
        if not isinstance(thread, str) or not re.fullmatch(r"\d+\.\d+", thread):
            problems.append("notify_thread must be a Slack thread timestamp")
        if not isinstance(notify, str) or not notify.startswith("slack:"):
            problems.append("notify_thread requires a Slack notify target")
    return {"notify": notify, "notify_thread": thread, "origin": origin}


def validate_definition(raw: object, config: Config, *, at_consumed: bool = False) -> Definition:
    """Report all independent definition problems before returning a usable snapshot."""
    data = json_object(raw, "beat definition")
    problems = [
        f"{key} is not a recognized beat field" for key in data if key not in DEFINITION_KEYS
    ]
    result: dict[str, Any] = {}
    for key in ("title", "instructions", "completion", "allowed_actions", "workspace"):
        value = _text(data, key, problems)
        if value is not None:
            result[key] = value
    workspace = result.get("workspace")
    if workspace:
        try:
            require_workspace(config.paths, workspace)
        except ValueError as exc:
            problems.append(str(exc))
    result["agent"] = _agent(data, config, problems)
    result.update(_timing(data, problems))
    if (
        not at_consumed
        and result.get("at")
        and result.get("followup_at")
        and result["followup_at"] < result["at"]
    ):
        problems.append("followup_at must not be earlier than the first assessment at")
    result.update(_routing(data, config, problems))
    for key, default in (("timeout", 900), ("gate_timeout", 120)):
        value = data.get(key, default)
        if type(value) is not int or value < 1:
            problems.append(f"{key} must be a positive integer")
        else:
            result[key] = value
    gate = data.get("gate")
    if gate is not None and gate != "gate.sh":
        problems.append("gate must be gate.sh, a file inside this beat's directory")
    result["gate"] = gate
    llm_checks = data.get("llm_checks", False)
    if type(llm_checks) is not bool:
        problems.append("llm_checks must be a boolean")
    elif result.get("schedule") and gate is None and not llm_checks:
        problems.append(
            "ungated recurring beats require llm_checks: true (an LLM on every due check)"
        )
    result["llm_checks"] = llm_checks
    if problems:
        raise HeartbeatError(problems)
    return Definition(**result)


def next_check(definition: Definition, now: str) -> str | None:
    """The next ordinary check or explicit follow-up, whichever comes first."""
    due = definition.at
    if definition.schedule:
        try:
            due = utc(
                next_cron(definition.schedule, datetime.fromisoformat(now), definition.timezone)
            )
        except ValueError, OverflowError:
            raise HeartbeatError("schedule has no reachable next time") from None
    choices = [value for value in (due, definition.followup_at, definition.expires_at) if value]
    return min(choices) if choices else None


def validate_gate(paths: Paths, beat: Beat) -> None:
    """Check a confined shell gate's syntax without running it or reading shell startup files."""
    if beat.gate is None:
        return
    try:
        require_workspace(paths, beat.workspace)
    except ValueError as exc:
        raise HeartbeatError(str(exc)) from exc
    root = paths.workspace_heartbeat(beat.workspace)
    directory = root / beat.ref
    gate = directory / beat.gate
    try:
        if root.is_symlink() or directory.is_symlink() or gate.is_symlink():
            raise HeartbeatError("gate.sh and its heartbeat directory must not be symlinks")
        if (
            gate.resolve().parent != directory.resolve()
            or directory.resolve().parent != root.resolve()
        ):
            raise HeartbeatError("gate.sh must stay inside its heartbeat directory")
        details = gate.stat()
        if not stat.S_ISREG(details.st_mode):
            raise HeartbeatError("gate.sh must be a regular file")
        if details.st_size > MAX_GATE_BYTES:
            raise HeartbeatError("gate.sh must be at most 64 KiB")
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-n", "gate.sh"],
            cwd=directory,
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise HeartbeatError(f"could not validate {gate}: {exc}") from exc
    if result.returncode:
        raise HeartbeatError("gate.sh has invalid shell syntax; check it with bash -n")
