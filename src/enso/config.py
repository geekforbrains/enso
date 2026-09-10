"""ENSO_HOME paths and the ``config.json`` schema: load, validate, save."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from .providers import PROVIDER_CLASSES

log = logging.getLogger(__name__)

CONFIG_VERSION = 1
TRANSPORT_NAMES = ("slack", "telegram")
WORKSPACE_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Slack DM bindings use the message's user id: ``U…`` on standalone workspaces, ``W…`` for
# Enterprise Grid org-wide ids. DM targets are deliberately not accepted by resolve_target;
# posting to a DM needs its D… conversation id, not the user id.
BINDING_KEY_RE = re.compile(r"(?:slack:(?:dm:[UW]|[CG])[A-Z0-9]+|telegram:[0-9]+)")
SLACK_TARGET_RE = re.compile(r"[CGD][A-Z0-9]+")
TARGET_FORMS = {
    "slack": "a Slack conversation id (C…, G…, or D…)",
    "telegram": "a positive numeric Telegram user id",
}
DEFAULT_AGENT_TIMEOUT = 3600
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 5
DEFAULT_RUNS_KEEP = 500
DEFAULT_RUNS_MAX_AGE_DAYS = 30
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8787
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Every project has these stages besides its own; a project may not name one of them.
BUILTIN_STAGES = ("backlog", "blocked", "done", "cancelled")
PROJECT_KEY_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}")
STAGE_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")


class ConfigError(Exception):
    """config.json cannot be used; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class ConfigConflictError(ConfigError):
    """Another writer owns the configuration, or the caller's revision is stale."""


@dataclass(frozen=True)
class Paths:
    """Every filesystem location Enso uses, derived from one home directory."""

    home: Path

    @classmethod
    def from_env(cls) -> Paths:
        """``ENSO_HOME`` when set, else ``~/.enso``."""
        return cls(Path(os.environ.get("ENSO_HOME") or "~/.enso").expanduser())

    @property
    def config(self) -> Path:
        return self.home / "config.json"

    @property
    def config_lock(self) -> Path:
        """Stable advisory lock shared by configuration writers."""
        return self.home / ".config.lock"

    @property
    def config_example(self) -> Path:
        return self.home / "config.example.json"

    @property
    def db(self) -> Path:
        return self.home / "enso.db"

    @property
    def log(self) -> Path:
        return self.home / "enso.log"

    @property
    def launchd_log(self) -> Path:
        return self.home / "launchd.log"

    @property
    def web_pid(self) -> Path:
        """The web viewer's pidfile, locked by the viewer process for its lifetime."""
        return self.home / "web.pid"

    @property
    def web_log(self) -> Path:
        return self.home / "web.log"

    @property
    def workspaces(self) -> Path:
        return self.home / "workspaces"

    @property
    def jobs(self) -> Path:
        return self.home / "jobs"

    @property
    def heartbeat(self) -> Path:
        """Beat gate scripts and execution locks, separate from scheduled job files."""
        return self.home / "heartbeat"

    @property
    def worktrees(self) -> Path:
        """Per-task Git worktrees for repo projects: ``worktrees/<KEY>/<REF>``."""
        return self.home / "worktrees"

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def runtime_dir(self) -> Path:
        """Managed releases, update receipts and restart coordination for this home."""
        return self.home / "runtime"

    @property
    def update_state(self) -> Path:
        return self.runtime_dir / "update.json"

    @property
    def maintenance(self) -> Path:
        return self.runtime_dir / "maintenance.json"

    @property
    def daemon_state(self) -> Path:
        return self.runtime_dir / "daemon.json"

    @property
    def connection_dir(self) -> Path:
        """Private, expiring state for the pre-service connection receiver."""
        return self.cache / "connect"

    @property
    def slack_cache(self) -> Path:
        return self.cache / "slack.json"

    @property
    def models_cache(self) -> Path:
        return self.cache / "models.json"

    @property
    def skills(self) -> Path:
        return self.home / "skills"

    @property
    def skill_lock(self) -> Path:
        """Serialize publication of official skills without depending on runtime setup."""
        return self.home / ".skills.lock"

    @property
    def agents_md(self) -> Path:
        return self.home / "AGENTS.md"

    @property
    def secrets(self) -> Path:
        """``*.env`` files loaded into the service environment at start."""
        return self.home / "secrets"

    def workspace(self, name: str) -> Path:
        return self.workspaces / name


@dataclass(frozen=True)
class Agent:
    """One explicit provider/model/effort triple."""

    provider: str
    model: str
    effort: str


@dataclass(frozen=True)
class ProviderConfig:
    path: str
    models: tuple[str, ...]
    args: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceConfig:
    """Per-workspace overrides; absent keys fall back to the global config.

    ``restricted`` makes a launch in the workspace require the provider's own policy file
    first; see ``policy.py``. False keeps the default: whatever the provider's args allow.
    """

    agent: Agent | None = None
    provider_args: dict[str, tuple[str, ...]] = field(default_factory=dict)
    restricted: bool = False


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str
    app_token: str
    notify: str = ""
    mention_required: bool = False
    thread_mention_required: bool = False


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    allowed_users: tuple[str, ...] = ()
    notify: str = ""


@dataclass(frozen=True)
class LoggingConfig:
    level: str = DEFAULT_LOG_LEVEL
    max_bytes: int = DEFAULT_LOG_MAX_BYTES
    backups: int = DEFAULT_LOG_BACKUPS


@dataclass(frozen=True)
class RunsConfig:
    keep: int = DEFAULT_RUNS_KEEP
    max_age_days: int = DEFAULT_RUNS_MAX_AGE_DAYS


@dataclass(frozen=True)
class HeartbeatConfig:
    """Whether finite background work runs, and how long closed beats are retained."""

    enabled: bool = True
    retention_days: int = 30


@dataclass(frozen=True)
class WebConfig:
    """Where ``enso web start`` listens unless its flags say otherwise."""

    host: str = DEFAULT_WEB_HOST
    port: int = DEFAULT_WEB_PORT


@dataclass(frozen=True)
class Stage:
    """One step of a project's pipeline: served by a stage job, or waiting on a person."""

    name: str
    human: bool = False


@dataclass(frozen=True)
class ProjectConfig:
    """One ``projects`` entry: where its tasks are worked and the stages they pass through."""

    key: str
    name: str
    workspace: str
    repo: Path | None
    stages: tuple[Stage, ...]
    setup: str | None = None  # bash command run once inside a fresh worktree
    copy: tuple[str, ...] = ()  # relative paths copied from the main checkout into a worktree

    @property
    def stage_names(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages)

    @property
    def agent_stages(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages if not stage.human)

    @property
    def human_stages(self) -> tuple[str, ...]:
        return tuple(stage.name for stage in self.stages if stage.human)

    @property
    def last_agent_stage(self) -> str:
        """The agent stage that lands a task's branch; validation guarantees there is one."""
        return self.agent_stages[-1]

    def stage(self, name: str) -> Stage | None:
        return next((stage for stage in self.stages if stage.name == name), None)

    def index(self, name: str) -> int:
        """Zero-based position of a project stage; ``ValueError`` for anything else."""
        return self.stage_names.index(name)

    def next_stage(self, name: str) -> str:
        """The stage after ``name``, or ``done`` after the last one."""
        index = self.index(name) + 1
        return self.stage_names[index] if index < len(self.stages) else "done"

    def previous_stage(self, name: str) -> str | None:
        """The stage before ``name``, or None from the first one."""
        index = self.index(name)
        return self.stage_names[index - 1] if index else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "workspace": self.workspace,
            "repo": str(self.repo) if self.repo is not None else None,
            "stages": [{"name": stage.name, "human": stage.human} for stage in self.stages],
            "setup": self.setup,
            "copy": list(self.copy),
        }


@dataclass(frozen=True)
class Config:
    """A validated config.json."""

    paths: Paths
    raw: dict[str, Any]
    slack: SlackConfig | None
    telegram: TelegramConfig | None
    bindings: dict[str, str]
    defaults: Agent
    workspaces: dict[str, WorkspaceConfig]
    providers: dict[str, ProviderConfig]
    agent_timeout: int
    logging: LoggingConfig
    runs: RunsConfig
    web: WebConfig
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    heartbeat: HeartbeatConfig = field(default_factory=HeartbeatConfig)
    source_hash: str | None = None

    def provider_args(self, workspace: str, provider: str) -> tuple[str, ...]:
        """Workspace override when present, else the provider's global args."""
        override = self.workspaces.get(workspace)
        if override is not None and provider in override.provider_args:
            return override.provider_args[provider]
        return self.providers[provider].args

    @property
    def transports(self) -> dict[str, SlackConfig | TelegramConfig]:
        """Configured transports by name, Slack first."""
        configured: dict[str, SlackConfig | TelegramConfig] = {}
        if self.slack is not None:
            configured["slack"] = self.slack
        if self.telegram is not None:
            configured["telegram"] = self.telegram
        return configured

    def resolve_target(self, value: str) -> tuple[str, str]:
        """``slack:C…`` / ``telegram:123`` → (transport, id); a bare id needs one transport."""
        if not value:
            raise ValueError("target is empty")
        transport, sep, target = value.partition(":")
        if not sep:
            if len(self.transports) != 1:
                raise ValueError(f"{value!r} is ambiguous; prefix it with slack: or telegram:")
            transport, target = next(iter(self.transports)), value
        elif transport not in TRANSPORT_NAMES:
            raise ValueError(f"{value!r}: unknown transport {transport!r}; use slack: or telegram:")
        elif transport not in self.transports:
            raise ValueError(f"transport {transport} is not configured")
        if canonical_target(transport, target) is None:
            raise ValueError(f"{target!r} is not {TARGET_FORMS[transport]}")
        return transport, target

    def default_notify(self) -> tuple[str, str] | None:
        """The first configured transport with a ``notify`` target, if any."""
        for name, transport in self.transports.items():
            if transport.notify:
                return name, transport.notify
        return None


def valid_workspace_name(name: object) -> bool:
    return (
        isinstance(name, str) and len(name) <= 64 and WORKSPACE_NAME_RE.fullmatch(name) is not None
    )


# -- Parsing ------------------------------------------------------------------

# Every closed object in the version-1 schema, by the path it is reported under. Dynamic maps
# are absent on purpose: bindings, workspaces, providers, and workspace provider overrides are
# named by the user and validated by their own name rules, not by this table.
ROOT_KEYS = (
    "version",
    "transports",
    "bindings",
    "defaults",
    "workspaces",
    "providers",
    "agent",
    "logging",
    "runs",
    "web",
    "projects",
    "heartbeat",
)
PROJECT_KEYS = ("name", "workspace", "repo", "stages", "setup", "copy")
SLACK_KEYS = ("bot_token", "app_token", "notify", "mention_required", "thread_mention_required")
TELEGRAM_KEYS = ("bot_token", "allowed_users", "notify")
PROVIDER_KEYS = ("path", "models", "args")
AGENT_KEYS = ("provider", "model", "effort")
WORKSPACE_KEYS = ("agent", "providers", "restricted")
WORKSPACE_PROVIDER_KEYS = ("args",)
SETTINGS_KEYS = {
    "agent": ("timeout",),
    "logging": ("level", "max_bytes", "backups"),
    "runs": ("keep", "max_age_days"),
    "web": ("host", "port"),
    "heartbeat": ("enabled", "retention_days"),
}
# JSON member names are arbitrary text. An ordinary one prints as itself; anything else is
# JSON-escaped so a newline or a bidirectional control character in config.json cannot forge
# a line of terminal output.
_PLAIN_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def _key_text(key: str) -> str:
    """One member name, safe to print inside a problem."""
    return key if _PLAIN_KEY_RE.fullmatch(key) else json.dumps(key)


def _unknown_keys(raw: dict, allowed: tuple[str, ...], where: str, unknown: list[str]) -> None:
    """Report every member of a closed object the schema does not define, in document order.

    Values are never quoted: any of them may be a token. Callers apply this only to a value
    that really is an object, so a malformed subtree reports its own type problem alone, and
    they choose the sink, so a document of some other schema version collects nothing.
    """
    prefix = f"{where}." if where else ""
    for key in raw:
        if key not in allowed:
            unknown.append(f"{prefix}{_key_text(key)} is not a recognized key")


def _str_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _positive_int(raw: dict, key: str, default: int, where: str, problems: list[str]) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        problems.append(f"{where}.{key} must be a non-negative integer")
        return default
    return value


def _parse_slack(raw: object, problems: list[str], unknown: list[str]) -> SlackConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("transports.slack must be an object")
        return None
    _unknown_keys(raw, SLACK_KEYS, "transports.slack", unknown)
    tokens: dict[str, str] = {}
    for key in ("bot_token", "app_token"):
        token = raw.get(key)
        if not isinstance(token, str) or not token:
            problems.append(f"transports.slack.{key} is required")
            token = ""
        tokens[key] = token
    flags: dict[str, bool] = {}
    for key in ("mention_required", "thread_mention_required"):
        flag = raw.get(key, False)
        if not isinstance(flag, bool):
            problems.append(f"transports.slack.{key} must be true or false")
            flag = False
        flags[key] = flag
    return SlackConfig(
        bot_token=tokens["bot_token"],
        app_token=tokens["app_token"],
        notify=_parse_notify(raw, "slack", problems),
        mention_required=flags["mention_required"],
        thread_mention_required=flags["thread_mention_required"],
    )


def _telegram_user_id(value: object) -> str | None:
    """A positive Telegram user id: an int, or its exact decimal string."""
    if isinstance(value, bool):
        return None
    # ``isdecimal`` and not ``isdigit``: ``"\u00b2".isdigit()`` is true but ``int`` rejects it,
    # which would escape parsing as a ValueError instead of landing in ``problems``.
    if isinstance(value, str) and value.isdecimal() and str(int(value)) == value:
        value = int(value)
    if isinstance(value, int) and value > 0:
        return str(value)
    return None


# One definition of "a place a transport can post to", shared by config parsing, job
# validation, and the CLI's ``--to``, so a bad destination fails before anything is sent.
def canonical_target(transport: str, value: object) -> str | None:
    """``value`` as the id ``transport`` can post to, or None when it is not one."""
    if transport == "telegram":
        return _telegram_user_id(value)
    return value if isinstance(value, str) and SLACK_TARGET_RE.fullmatch(value) else None


def _parse_notify(raw: dict, transport: str, problems: list[str]) -> str:
    """The transport's default destination as a canonical id; absent or empty is ''."""
    value = raw.get("notify")
    if value is None or value == "":
        return ""
    target = canonical_target(transport, value)
    if target is None:
        problems.append(f"transports.{transport}.notify must be {TARGET_FORMS[transport]}")
        return ""
    return target


def _parse_telegram(raw: object, problems: list[str], unknown: list[str]) -> TelegramConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("transports.telegram must be an object")
        return None
    _unknown_keys(raw, TELEGRAM_KEYS, "transports.telegram", unknown)
    token = raw.get("bot_token")
    if not isinstance(token, str) or not token:
        problems.append("transports.telegram.bot_token is required")
        token = ""
    users_raw = raw.get("allowed_users", [])
    if not isinstance(users_raw, list):
        problems.append("transports.telegram.allowed_users must be a list of user ids")
        users_raw = []
    users: list[str] = []
    for entry in users_raw:
        user = _telegram_user_id(entry)
        if user is None:
            problems.append(
                "transports.telegram.allowed_users must contain positive numeric "
                f"Telegram user ids (got {entry!r})"
            )
            continue
        users.append(user)
    return TelegramConfig(
        bot_token=token,
        allowed_users=tuple(users),
        notify=_parse_notify(raw, "telegram", problems),
    )


def _parse_providers(
    raw: object, problems: list[str], warnings: list[str], unknown: list[str]
) -> dict[str, ProviderConfig]:
    providers: dict[str, ProviderConfig] = {}
    if not isinstance(raw, dict) or not raw:
        problems.append("providers must be a non-empty object")
        return providers
    for name, entry in raw.items():
        if name not in PROVIDER_CLASSES:
            problems.append(
                f"providers.{name}: unknown provider (use {', '.join(PROVIDER_CLASSES)})"
            )
            continue
        if not isinstance(entry, dict):
            problems.append(f"providers.{name} must be an object")
            continue
        _unknown_keys(entry, PROVIDER_KEYS, f"providers.{name}", unknown)
        path = entry.get("path", name)
        models = _str_list(entry.get("models"))
        args = _str_list(entry.get("args", []))
        if not isinstance(path, str) or not path:
            problems.append(f"providers.{name}.path must be a string")
            path = name
        path = os.path.expanduser(path)
        if not models:
            problems.append(f"providers.{name}.models must be a non-empty list of strings")
            models = []
        if args is None:
            problems.append(f"providers.{name}.args must be a list of strings")
            args = []
        executable = shutil.which(path)
        if executable is None:
            warnings.append(f"providers.{name}.path {path!r} is not an executable on this machine")
        else:
            path = os.path.abspath(executable)
        providers[name] = ProviderConfig(path, tuple(models), tuple(args))
    return providers


def parse_agent(
    raw: object,
    where: str,
    providers: dict[str, ProviderConfig],
    problems: list[str],
    unknown: list[str] | None = None,
) -> Agent | None:
    """Validate one explicit provider/model/effort block; partial blocks are errors.

    ``unknown`` collects unrecognized member names and defaults to ``problems``; config
    parsing redirects it for a document that declares a schema version this build does
    not define. A ``JOB.md`` agent block has no version of its own and takes the default.
    """
    if not isinstance(raw, dict):
        problems.append(f"{where} must be an object with provider, model, and effort")
        return None
    _unknown_keys(raw, AGENT_KEYS, where, problems if unknown is None else unknown)
    values = {}
    for key in AGENT_KEYS:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            problems.append(f"{where}.{key} is required")
            return None
        values[key] = value
    provider = providers.get(values["provider"])
    if provider is None:
        problems.append(f"{where}.provider {values['provider']!r} is not configured")
        return None
    if values["model"] not in provider.models:
        problems.append(
            f"{where}.model {values['model']!r} is not in providers.{values['provider']}.models"
        )
        return None
    levels = PROVIDER_CLASSES[values["provider"]].effort_levels
    if values["effort"] not in levels:
        problems.append(f"{where}.effort must be one of {', '.join(levels)}")
        return None
    return Agent(**values)


def _parse_workspaces(
    raw: object,
    providers: dict[str, ProviderConfig],
    problems: list[str],
    unknown: list[str],
) -> dict[str, WorkspaceConfig]:
    workspaces: dict[str, WorkspaceConfig] = {}
    if raw is None:
        return workspaces
    if not isinstance(raw, dict):
        problems.append("workspaces must be an object")
        return workspaces
    for name, entry in raw.items():
        if not valid_workspace_name(name):
            problems.append(f"workspaces.{name}: names are lowercase kebab-case")
            continue
        if not isinstance(entry, dict):
            problems.append(f"workspaces.{name} must be an object")
            continue
        _unknown_keys(entry, WORKSPACE_KEYS, f"workspaces.{name}", unknown)
        agent = None
        if "agent" in entry:
            agent = parse_agent(
                entry["agent"], f"workspaces.{name}.agent", providers, problems, unknown
            )
        provider_args: dict[str, tuple[str, ...]] = {}
        overrides = entry.get("providers", {})
        if not isinstance(overrides, dict):
            problems.append(f"workspaces.{name}.providers must be an object")
            overrides = {}
        for provider, override in overrides.items():
            where = f"workspaces.{name}.providers.{provider}"
            if provider not in providers:
                problems.append(f"{where}: provider is not configured")
                continue
            args = None
            if isinstance(override, dict):
                _unknown_keys(override, WORKSPACE_PROVIDER_KEYS, where, unknown)
                args = _str_list(override.get("args"))
            if args is None:
                problems.append(f"{where}.args must be a list of strings")
            else:
                provider_args[provider] = tuple(args)
        restricted = entry.get("restricted", False)
        if not isinstance(restricted, bool):
            problems.append(f"workspaces.{name}.restricted must be true or false")
            restricted = False
        workspaces[name] = WorkspaceConfig(
            agent=agent, provider_args=provider_args, restricted=restricted
        )
    return workspaces


def _parse_bindings(
    raw: object,
    paths: Paths,
    configured: set[str],
    problems: list[str],
    warnings: list[str],
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    if raw is None:
        return bindings
    if not isinstance(raw, dict):
        problems.append("bindings must be an object")
        return bindings
    for key, workspace in raw.items():
        if not BINDING_KEY_RE.fullmatch(key):
            problems.append(
                f"bindings.{key}: keys look like slack:C…, slack:G…, slack:dm:U…, "
                "slack:dm:W…, or telegram:<user id>"
            )
            continue
        if not valid_workspace_name(workspace):
            problems.append(f"bindings.{key}: workspace names are lowercase kebab-case")
            continue
        if not paths.workspace(workspace).is_dir():
            problems.append(
                f"bindings.{key}: workspace directory {paths.workspace(workspace)} missing"
            )
            continue
        if key.split(":", 1)[0] not in configured:
            warnings.append(f"bindings.{key}: transport {key.split(':', 1)[0]} is not configured")
        bindings[key] = workspace
    return bindings


def parse_stages(raw: object, where: str, problems: list[str]) -> tuple[Stage, ...]:
    """A stage list: ``name`` or ``name:human`` entries, unique, no built-in, one agent stage.

    Shared with ``enso project add`` so a ``--stages`` flag and a hand-written list are
    judged by the same rule.
    """
    entries = _str_list(raw)
    if not entries:
        problems.append(f"{where} must be a non-empty list of stage names")
        return ()
    stages: list[Stage] = []
    for entry in entries:
        name, sep, suffix = entry.partition(":")
        human = sep != "" and suffix == "human"
        if sep and not human:
            problems.append(f"{where}: {entry!r} is not a stage; use NAME or NAME:human")
            continue
        if not STAGE_NAME_RE.fullmatch(name):
            problems.append(
                f"{where}: {name!r} is not a stage name (lowercase, digits, hyphens, 2-24 chars)"
            )
            continue
        if name in BUILTIN_STAGES:
            problems.append(f"{where}: {name} is a built-in stage")
            continue
        if any(stage.name == name for stage in stages):
            problems.append(f"{where}: {name} is listed twice")
            continue
        stages.append(Stage(name, human))
    if stages and all(stage.human for stage in stages):
        problems.append(f"{where} needs at least one agent stage")
    return tuple(stages)


def _inside_repo(item: str) -> bool:
    """A relative path that cannot leave the checkout it is copied from."""
    return bool(item) and not Path(item).is_absolute() and ".." not in Path(item).parts


def _parse_project(
    key: str, entry: dict, paths: Paths, problems: list[str], unknown: list[str]
) -> ProjectConfig:
    where = f"projects.{key}"
    _unknown_keys(entry, PROJECT_KEYS, where, unknown)
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{where}.name must be non-empty text")
        name = key
    workspace = entry.get("workspace")
    if not valid_workspace_name(workspace):
        problems.append(f"{where}.workspace must be a workspace name (lowercase kebab-case)")
        workspace = ""
    elif not paths.workspace(str(workspace)).is_dir():
        problems.append(f"{where}.workspace: directory {paths.workspace(str(workspace))} missing")
    repo: Path | None = None
    repo_raw = entry.get("repo")
    if repo_raw is not None:
        if not isinstance(repo_raw, str) or not repo_raw.strip():
            problems.append(f"{where}.repo must be a path")
        else:
            repo = Path(repo_raw).expanduser()
            if not repo.is_dir() or not (repo / ".git").exists():
                problems.append(f"{where}.repo {repo} is not a directory holding a Git repository")
    stages = parse_stages(entry.get("stages"), f"{where}.stages", problems)
    setup = entry.get("setup")
    if setup is not None and (not isinstance(setup, str) or not setup.strip()):
        problems.append(f"{where}.setup must be a command string")
        setup = None
    copy = _str_list(entry.get("copy", []))
    if copy is None or not all(_inside_repo(item) for item in copy):
        problems.append(f"{where}.copy must be a list of relative paths inside the repository")
        copy = []
    return ProjectConfig(
        key=key,
        name=str(name).strip(),
        workspace=str(workspace),
        repo=repo,
        stages=stages,
        setup=setup,
        copy=tuple(copy),
    )


def _parse_projects(
    raw: object, paths: Paths, problems: list[str], unknown: list[str]
) -> dict[str, ProjectConfig]:
    projects: dict[str, ProjectConfig] = {}
    if raw is None:
        return projects
    if not isinstance(raw, dict):
        problems.append("projects must be an object")
        return projects
    for key, entry in raw.items():
        if not PROJECT_KEY_RE.fullmatch(key):
            problems.append(
                f"projects.{_key_text(key)}: keys are 2-10 uppercase letters or digits, "
                "starting with a letter"
            )
            continue
        if not isinstance(entry, dict):
            problems.append(f"projects.{key} must be an object")
            continue
        projects[key] = _parse_project(key, entry, paths, problems, unknown)
    return projects


def valid_port(value: object) -> bool:
    """A TCP port: an integer from 1 through 65535 (not a bool, not a numeric string)."""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _parse_web(raw: dict, problems: list[str]) -> WebConfig:
    host = raw.get("host", DEFAULT_WEB_HOST)
    if not isinstance(host, str) or not host.strip():
        problems.append("web.host must be a non-empty string")
        host = DEFAULT_WEB_HOST
    port = raw.get("port", DEFAULT_WEB_PORT)
    if not valid_port(port):
        problems.append("web.port must be an integer from 1 through 65535")
        port = DEFAULT_WEB_PORT
    return WebConfig(host=host.strip(), port=port)


def _parse_heartbeat(raw: dict, problems: list[str]) -> HeartbeatConfig:
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append("heartbeat.enabled must be true or false")
        enabled = False
    retention = raw.get("retention_days", 30)
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
        problems.append("heartbeat.retention_days must be a positive integer")
        retention = 30
    return HeartbeatConfig(enabled=enabled, retention_days=retention)


def parse_config(raw: object, paths: Paths) -> tuple[Config | None, list[str], list[str]]:
    """Validate a raw config document; returns (config, problems, warnings)."""
    problems: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return None, ["config.json must contain a JSON object"], warnings
    version_ok = raw.get("version") == CONFIG_VERSION
    if not version_ok:
        problems.append(f"version must be {CONFIG_VERSION}")
    # A document declaring another schema version is not a version-1 document, so its member
    # names belong to a schema this build does not define. Its value and type problems still
    # stand, but unknown-key findings go to a list that is thrown away rather than reporting
    # a future schema as a pile of version-1 typos.
    unknown = problems if version_ok else []
    _unknown_keys(raw, ROOT_KEYS, "", unknown)

    transports = raw.get("transports", {})
    if not isinstance(transports, dict):
        problems.append("transports must be an object")
        transports = {}
    else:
        _unknown_keys(transports, TRANSPORT_NAMES, "transports", unknown)
    slack = _parse_slack(transports.get("slack"), problems, unknown)
    telegram = _parse_telegram(transports.get("telegram"), problems, unknown)
    configured = {name for name in TRANSPORT_NAMES if transports.get(name) is not None}
    if not configured:
        problems.append("transports must configure slack or telegram")

    providers = _parse_providers(raw.get("providers"), problems, warnings, unknown)
    defaults = parse_agent(raw.get("defaults"), "defaults", providers, problems, unknown)
    workspaces = _parse_workspaces(raw.get("workspaces"), providers, problems, unknown)
    bindings = _parse_bindings(raw.get("bindings"), paths, configured, problems, warnings)
    projects = _parse_projects(raw.get("projects"), paths, problems, unknown)

    settings: dict[str, dict] = {}
    for key, allowed in SETTINGS_KEYS.items():
        value = raw.get(key, {})
        if isinstance(value, dict):
            _unknown_keys(value, allowed, key, unknown)
        else:
            problems.append(f"{key} must be an object")
            value = {}
        settings[key] = value
    agent, logging_raw = settings["agent"], settings["logging"]
    runs_raw, web_raw = settings["runs"], settings["web"]
    timeout = _positive_int(agent, "timeout", DEFAULT_AGENT_TIMEOUT, "agent", problems)
    level = str(logging_raw.get("level", DEFAULT_LOG_LEVEL)).upper()
    if level not in _LOG_LEVELS:
        problems.append(f"logging.level must be one of {', '.join(_LOG_LEVELS)}")
        level = "INFO"
    logging_cfg = LoggingConfig(
        level=level,
        max_bytes=_positive_int(
            logging_raw, "max_bytes", DEFAULT_LOG_MAX_BYTES, "logging", problems
        ),
        backups=_positive_int(logging_raw, "backups", DEFAULT_LOG_BACKUPS, "logging", problems),
    )
    runs = RunsConfig(
        keep=_positive_int(runs_raw, "keep", DEFAULT_RUNS_KEEP, "runs", problems),
        max_age_days=_positive_int(
            runs_raw, "max_age_days", DEFAULT_RUNS_MAX_AGE_DAYS, "runs", problems
        ),
    )
    web = _parse_web(web_raw, problems)
    heartbeat = _parse_heartbeat(settings["heartbeat"], problems)

    if problems or defaults is None:
        return None, problems, warnings
    config = Config(
        paths=paths,
        raw=raw,
        slack=slack,
        telegram=telegram,
        bindings=bindings,
        defaults=defaults,
        workspaces=workspaces,
        providers=providers,
        agent_timeout=timeout,
        logging=logging_cfg,
        runs=runs,
        web=web,
        projects=projects,
        heartbeat=heartbeat,
    )
    return config, problems, warnings


# -- Files --------------------------------------------------------------------


def read_raw_config(paths: Paths) -> dict[str, Any]:
    """Read config.json as a plain dict, or raise ``ConfigError``."""
    return _config_snapshot(paths)[0]


def _config_snapshot(paths: Paths) -> tuple[dict[str, Any], str]:
    try:
        encoded = paths.config.read_bytes()
        raw = json.loads(encoded.decode("utf-8"))
    except FileNotFoundError:
        raise ConfigError([f"{paths.config} is missing; run `enso setup` first"]) from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError([f"could not read {paths.config}: {exc}"]) from exc
    except RecursionError:
        raise ConfigError([f"{paths.config} is nested too deeply"]) from None
    if not isinstance(raw, dict):
        raise ConfigError([f"{paths.config} must contain a JSON object"])
    return raw, hashlib.sha256(encoded).hexdigest()


def safe_diagnostics(raw: object, messages: list[str]) -> list[str]:
    """Redact credential values if validation embeds one through another malformed field."""
    secrets: set[str] = set()

    def collect(value: object, sensitive: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, sensitive or str(key).endswith("token"))
        elif isinstance(value, list):
            for item in value:
                collect(item, sensitive)
        elif sensitive and isinstance(value, str) and value:
            secrets.update((value, repr(value)[1:-1], json.dumps(value)[1:-1]))

    collect(raw)
    clean = list(messages)
    for secret in sorted(secrets, key=len, reverse=True):
        clean = [message.replace(secret, "<redacted>") for message in clean]
    return clean


def check_config(paths: Paths) -> tuple[Config | None, list[str], list[str]]:
    """Read and validate without raising; for ``enso config check``."""
    try:
        raw, fingerprint = _config_snapshot(paths)
    except ConfigError as exc:
        return None, list(exc.problems), []
    config, problems, warnings = parse_config(raw, paths)
    if config is not None:
        config = replace(config, source_hash=fingerprint)
    return config, safe_diagnostics(raw, problems), safe_diagnostics(raw, warnings)


def load_config(paths: Paths) -> Config:
    """Read and validate config.json; fail closed on any problem."""
    config, problems, _warnings = check_config(paths)
    if config is None:
        raise ConfigError(problems)
    return config


def config_fingerprint(paths: Paths) -> str:
    """SHA256 of the exact config bytes, or ``missing`` before the first write."""
    try:
        return hashlib.sha256(paths.config.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "missing"


# -- Live reload ---------------------------------------------------------------

type _Signature = tuple[int, int, int] | Literal["missing"]


def _signature(path: Path) -> _Signature:
    """What ``stat`` says about the file; every write changes at least one of these."""
    try:
        status = os.stat(path)
    except OSError:
        return "missing"
    return status.st_mtime_ns, status.st_size, status.st_ino


class LiveConfig:
    """The latest valid config.json, re-read only after the file on disk has changed.

    Long-running processes hold one of these and take a snapshot from ``current()`` at
    each unit of work. A revision that does not load is reported once and the last good
    configuration stays in force, so an edit in progress never stops the service or
    changes what a running turn already decided. Writers replace the file atomically,
    so a read never sees a torn document and no reader-side lock is needed.
    """

    def __init__(self, config: Config):
        self.paths = config.paths
        self._config = config
        # A config read from disk may have been edited between that read and now, so it
        # is checked on first use; one built in memory adopts the file as it is right now.
        self._signature: _Signature | None = (
            None if config.source_hash is not None else _signature(config.paths.config)
        )
        self._rejected: _Signature | None = None
        self._lock = threading.Lock()  # callers stat and read from threads and the loop

    def current(self) -> Config:
        """The configuration on disk when it is valid, else the last good one; never raises."""
        with self._lock:
            signature = _signature(self.paths.config)
            if signature in (self._signature, self._rejected):
                return self._config
            try:
                config = load_config(self.paths)
            except (ConfigError, OSError) as exc:
                self._rejected = signature
                detail = "; ".join(exc.problems) if isinstance(exc, ConfigError) else str(exc)
                log.warning("keeping the last good configuration: %s", detail)
                return self._config
            self._signature = signature
            self._rejected = None
            self._config = config
            log.info("config.json reloaded")
            return config


@contextmanager
def config_lock(paths: Paths) -> Iterator[None]:
    """Take the shared writer lock without waiting; never delete this stable lock file."""
    paths.home.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(paths.config_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigConflictError(
                ["configuration is busy; retry after the current change"]
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def write_config_locked(paths: Paths, raw: dict[str, Any]) -> None:
    """Atomically write owner-only config; the caller must hold ``config_lock``."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=paths.home, prefix=".config-", delete=False
        ) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            json.dump(raw, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, paths.config)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_config(paths: Paths, raw: dict[str, Any]) -> None:
    """Atomically write config.json with owner-only permissions and a writer lock."""
    with config_lock(paths):
        write_config_locked(paths, raw)
