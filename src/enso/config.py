"""Home paths, workspace context, and configuration snapshots from JSON and workspace Markdown."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from . import frontmatter, locks
from .providers import PROVIDER_CLASSES
from .transport_registry import TRANSPORTS

log = logging.getLogger(__name__)

CONFIG_VERSION = 2
LEGACY_HOME_MESSAGE = (
    "This Enso home predates 0.2.0; automatic migration is unsupported: "
    "https://github.com/geekforbrains/enso/blob/develop/docs/migration.md"
)
WORKSPACE_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
BINDING_KEY_RE = re.compile("|".join(f"(?:{spec.binding_pattern})" for spec in TRANSPORTS.values()))
_BINDING_FORMS = [form for spec in TRANSPORTS.values() for form in spec.binding_forms]
BINDING_KEY_FORMS = ", ".join(_BINDING_FORMS[:-1]) + f", or {_BINDING_FORMS[-1]}"
_TARGET_PREFIXES = " or ".join(f"{name}:" for name in TRANSPORTS)
DEFAULT_AGENT_TIMEOUT = 3600
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUPS = 5
DEFAULT_RUNS_KEEP = 500
DEFAULT_RUNS_MAX_AGE_DAYS = 30
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8787
DEFAULT_WEB_KNOWLEDGE_RECENT_LIMIT = 5
DEFAULT_WEB_KNOWLEDGE_PAGE_SIZE = 50
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Every project has these stages besides its own; a project may not name one of them.
BUILTIN_STAGES = ("backlog", "blocked", "done", "cancelled")
PROJECT_KEY_RE = re.compile(r"[A-Z][A-Z0-9]{1,9}")
STAGE_NAME_RE = re.compile(r"[a-z][a-z0-9-]{1,23}")


class ConfigError(Exception):
    """Installation or workspace settings cannot be used; ``problems`` lists every reason."""

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
    def web_service_log(self) -> Path:
        return self.home / "launchd-web.log"

    @property
    def workspaces(self) -> Path:
        return self.home / "workspaces"

    @property
    def knowledge(self) -> Path:
        """Shared Markdown knowledge, alongside workspace-local knowledge directories."""
        return self.home / "knowledge"

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def runtime_dir(self) -> Path:
        """Managed releases, update receipts and restart coordination for this home."""
        return self.home / "runtime"

    def lock(self, *parts: str) -> Path:
        """Where a lock file lives: ``runtime/locks/<parts>.lock``, empty and never deleted.

        A lock can be the first thing a new home holds, so this creates its private directory.
        """
        path = self.runtime_dir.joinpath("locks", *parts[:-1], f"{parts[-1]}.lock")
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

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
    def agents_md(self) -> Path:
        return self.home / "AGENTS.md"

    @property
    def secrets(self) -> Path:
        """``*.env`` files loaded into the service environment at start."""
        return self.home / "secrets"

    def workspace(self, name: str) -> Path:
        if not valid_workspace_name(name):
            raise ValueError("workspace names are lowercase kebab-case, at most 64 characters")
        return self.workspaces / name

    def workspace_settings(self, name: str) -> Path:
        return self.workspace(name) / "WORKSPACE.md"

    def workspace_knowledge(self, name: str) -> Path:
        return self.workspace(name) / "knowledge"

    def workspace_memory(self, name: str) -> Path:
        return self.workspace(name) / "memory"

    def workspace_jobs(self, name: str) -> Path:
        return self.workspace(name) / "jobs"

    def job(self, reference: str) -> Path:
        workspace, name = split_job_ref(reference)
        return self.workspace_jobs(workspace) / name / "JOB.md"

    def workspace_projects(self, name: str) -> Path:
        return self.workspace(name) / "projects"

    def project(self, workspace: str, key: str) -> Path:
        if not PROJECT_KEY_RE.fullmatch(key):
            raise ValueError(
                "project keys are 2-10 uppercase letters or digits, starting with a letter"
            )
        return self.workspace_projects(workspace) / key

    def workspace_heartbeat(self, name: str) -> Path:
        return self.workspace(name) / "heartbeat"

    def workspace_skills(self, name: str) -> Path:
        return self.workspace(name) / "skills"

    def workspace_drafts(self, name: str) -> Path:
        return self.workspace(name) / "drafts"

    def workspace_uploads(self, name: str) -> Path:
        return self.workspace(name) / "uploads"


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
    """Overrides read from a workspace's optional WORKSPACE.md in this snapshot."""

    agent: Agent | None = None
    provider_args: dict[str, tuple[str, ...]] = field(default_factory=dict)


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
class WebKnowledgeConfig:
    """Bounded knowledge-list sizes for the read-only viewer."""

    recent_limit: int = DEFAULT_WEB_KNOWLEDGE_RECENT_LIMIT
    page_size: int = DEFAULT_WEB_KNOWLEDGE_PAGE_SIZE


@dataclass(frozen=True)
class WebConfig:
    """Where ``enso web start`` listens unless its flags say otherwise."""

    host: str = DEFAULT_WEB_HOST
    port: int = DEFAULT_WEB_PORT
    knowledge: WebKnowledgeConfig = field(default_factory=WebKnowledgeConfig)


@dataclass(frozen=True)
class Check:
    """An engine-executed acceptance command and its trusted inputs."""

    name: str
    command: str
    timeout: int = 600
    protect: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stage:
    """One step of a project's pipeline: served by a stage job, or waiting on a person."""

    name: str
    human: bool = False
    command: str | None = None
    worktree: bool | None = None
    checks: tuple[Check, ...] = ()
    max_repairs: int = 2
    max_returns: int = 2
    return_to: str | None = None
    integrate: bool = False


@dataclass(frozen=True)
class ProjectConfig:
    """One workspace PROJECT.md and its derived ownership."""

    key: str
    name: str
    workspace: str
    repo: Path | None
    stages: tuple[Stage, ...]
    setup: str | None = None  # command beside PROJECT.md, preparing ENSO_TASK_DIR
    copy: tuple[str, ...] = ()  # relative paths copied from the main checkout into a worktree
    worktree_root: str | None = None
    base: str | None = None
    max_concurrency: int = 1
    hooks: dict[str, str] = field(default_factory=dict)
    script_timeout: int = 600

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
    def last_agent_stage(self) -> str | None:
        """The last non-human stage that can hold a worktree, if the flow has one."""
        return next(
            (
                stage.name
                for stage in reversed(self.stages)
                if not stage.human and stage.worktree is not False
            ),
            None,
        )

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
            "stages": [stage_dict(stage) for stage in self.stages],
            "setup": self.setup,
            "copy": list(self.copy),
            "worktree_root": self.worktree_root,
            "base": self.base,
            "max_concurrency": self.max_concurrency,
            "hooks": self.hooks,
            "script_timeout": self.script_timeout,
        }


@dataclass(frozen=True)
class Config:
    """Validated installation and workspace settings for one unit of work."""

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
                raise ValueError(f"{value!r} is ambiguous; prefix it with {_TARGET_PREFIXES}")
            transport, target = next(iter(self.transports)), value
        elif transport not in TRANSPORTS:
            raise ValueError(f"{value!r}: unknown transport {transport!r}; use {_TARGET_PREFIXES}")
        elif transport not in self.transports:
            raise ValueError(f"transport {transport} is not configured")
        if TRANSPORTS[transport].canonical_target(target) is None:
            raise ValueError(f"{target!r} is not {TRANSPORTS[transport].target_form}")
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


def split_job_ref(reference: str) -> tuple[str, str]:
    """Require an explicit workspace and a local job slug, including for retained history."""
    workspace, separator, name = reference.partition(":")
    if (
        not separator
        or not valid_workspace_name(workspace)
        or not WORKSPACE_NAME_RE.fullmatch(name)
    ):
        raise ValueError("use a job reference <workspace>:<job>, such as team:digest")
    return workspace, name


def require_workspace(paths: Paths, name: str) -> Path:
    """Require one real workspace directory; never create it or substitute another owner."""
    root = paths.workspace(name)
    if any(path.is_symlink() for path in (paths.home, paths.workspaces, root)):
        raise ValueError(f"workspace directory {root} must not be a symbolic link")
    if not root.is_dir():
        raise ValueError(f"workspace directory {root} missing")
    return root


def resolve_workspace(
    paths: Paths,
    workspace: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Explicit selection beats ENSO_WORKSPACE; neither cwd nor default supplies context.

    This resolves a context hint, not an authenticated identity. Services pass an explicit
    binding or recorded owner and keep that selection throughout the operation.
    """
    selected = (
        workspace
        if workspace is not None
        else (os.environ if environ is None else environ).get("ENSO_WORKSPACE")
    )
    if selected is None:
        raise ValueError("select a workspace with --workspace or ENSO_WORKSPACE")
    require_workspace(paths, selected)
    return selected


# -- Parsing ------------------------------------------------------------------

# Every closed object in the current schema, by the path it is reported under. Dynamic maps
# are absent on purpose: bindings, providers, and workspace provider overrides are
# named by the user and validated by their own name rules, not by this table.
ROOT_KEYS = (
    "version",
    "transports",
    "bindings",
    "defaults",
    "providers",
    "agent",
    "logging",
    "runs",
    "web",
    "heartbeat",
)
PROJECT_KEYS = (
    "name",
    "repo",
    "stages",
    "setup",
    "copy",
    "worktree_root",
    "base",
    "max_concurrency",
    "hooks",
    "script_timeout",
)
SLACK_KEYS = ("bot_token", "app_token", "notify", "mention_required", "thread_mention_required")
TELEGRAM_KEYS = ("bot_token", "notify")
PROVIDER_KEYS = ("path", "models", "args")
AGENT_KEYS = ("provider", "model", "effort")
WORKSPACE_KEYS = ("agent", "providers")
WORKSPACE_PROVIDER_KEYS = ("args",)
SETTINGS_KEYS = {
    "agent": ("timeout",),
    "logging": ("level", "max_bytes", "backups"),
    "runs": ("keep", "max_age_days"),
    "web": ("host", "port", "knowledge"),
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


def _parse_notify(raw: dict, transport: str, problems: list[str]) -> str:
    """The transport's default destination as a canonical id; absent or empty is ''."""
    value = raw.get("notify")
    if value is None or value == "":
        return ""
    spec = TRANSPORTS[transport]
    target = spec.canonical_target(value)
    if target is None:
        problems.append(f"transports.{transport}.notify must be {spec.target_form}")
        return ""
    return target


def _parse_telegram(raw: object, problems: list[str], unknown: list[str]) -> TelegramConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append("transports.telegram must be an object")
        return None
    # Diagnose the removed field once, without inspecting or accepting its value.
    _unknown_keys(raw, (*TELEGRAM_KEYS, "allowed_users"), "transports.telegram", unknown)
    if "allowed_users" in raw:
        unknown.append(
            "transports.telegram.allowed_users was removed; remove it and use "
            "explicit bindings such as telegram:123456 to grant access to an existing workspace"
        )
    token = raw.get("bot_token")
    if not isinstance(token, str) or not token:
        problems.append("transports.telegram.bot_token is required")
        token = ""
    return TelegramConfig(
        bot_token=token,
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


def _workspace_entries(paths: Paths) -> list[Path]:
    """Discover workspace locations without following a linked workspace container."""
    if paths.home.is_symlink() or paths.workspaces.is_symlink():
        raise ValueError(f"{paths.workspaces}: workspace locations must not be symbolic links")
    if not paths.workspaces.exists():
        return []
    return sorted(
        entry
        for entry in paths.workspaces.iterdir()
        if valid_workspace_name(entry.name) and (entry.is_symlink() or entry.is_dir())
    )


def _read_workspace_settings(path: Path) -> dict[str, Any]:
    """Missing workspace settings inherit defaults; malformed/unsafe files are errors."""
    try:
        return frontmatter.read(path).fields
    except FileNotFoundError:
        return {}


def _load_workspaces(
    paths: Paths,
    providers: dict[str, ProviderConfig],
    problems: list[str],
) -> dict[str, WorkspaceConfig]:
    workspaces: dict[str, WorkspaceConfig] = {}
    try:
        entries = _workspace_entries(paths)
    except (OSError, ValueError) as exc:
        problems.append(str(exc))
        return workspaces
    for root in entries:
        path = paths.workspace_settings(root.name)
        where = str(path)
        try:
            require_workspace(paths, root.name)
            entry = _read_workspace_settings(path)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            detail = (
                "settings are nested too deeply" if isinstance(exc, RecursionError) else str(exc)
            )
            problems.append(f"{where}: {detail}")
            continue
        _unknown_keys(entry, WORKSPACE_KEYS, where, problems)
        agent = None
        if "agent" in entry:
            agent = parse_agent(entry["agent"], f"{where}.agent", providers, problems, problems)
        provider_args: dict[str, tuple[str, ...]] = {}
        overrides = entry.get("providers", {})
        if not isinstance(overrides, dict):
            problems.append(f"{where}.providers must be an object")
            overrides = {}
        for provider, override in overrides.items():
            location = f"{where}.providers.{provider}"
            if provider not in providers:
                problems.append(f"{location}: provider is not configured")
                continue
            args = None
            if isinstance(override, dict):
                _unknown_keys(override, WORKSPACE_PROVIDER_KEYS, location, problems)
                args = _str_list(override.get("args"))
            if args is None:
                problems.append(f"{location}.args must be a list of strings")
            else:
                provider_args[provider] = tuple(args)
        workspaces[root.name] = WorkspaceConfig(agent=agent, provider_args=provider_args)
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
            problems.append(f"bindings.{key}: keys look like {BINDING_KEY_FORMS}")
            continue
        if not valid_workspace_name(workspace):
            problems.append(f"bindings.{key}: workspace names are lowercase kebab-case")
            continue
        try:
            require_workspace(paths, workspace)
        except ValueError as exc:
            problems.append(f"bindings.{key}: {exc}")
            continue
        if key.split(":", 1)[0] not in configured:
            warnings.append(f"bindings.{key}: transport {key.split(':', 1)[0]} is not configured")
        bindings[key] = workspace
    return bindings


def stage_dict(stage: Stage) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(stage)


def _parse_checks(raw: object, where: str, problems: list[str]) -> tuple[Check, ...]:
    if not isinstance(raw, list):
        problems.append(f"{where} must be a list")
        return ()
    result: list[Check] = []
    for value in raw:
        if not isinstance(value, dict):
            problems.append(f"{where} entries must be objects")
            continue
        _unknown_keys(value, ("name", "command", "timeout", "protect"), where, problems)
        if not all(isinstance(value.get(k), str) and value[k].strip() for k in ("name", "command")):
            problems.append(f"{where} requires name and command")
            continue
        if any(check.name == value["name"] for check in result):
            problems.append(f"{where}: duplicate check {value['name']}")
        timeout = _positive_int(value, "timeout", 600, where, problems)
        if timeout == 0:
            problems.append(f"{where}: check timeout must be positive")
        protect = _str_list(value.get("protect", []))
        if protect is None or not all(_inside_repo(x) for x in protect):
            problems.append(f"{where}: protect must be repository-relative paths or patterns")
            protect = []
        result.append(Check(value["name"], value["command"], timeout, tuple(protect)))
    return tuple(result)


def _stage_entry(entry: object, where: str, problems: list[str]) -> Stage | None:
    if isinstance(entry, str):
        name, sep, suffix = entry.partition(":")
        if sep and suffix != "human":
            problems.append(f"{where}: {entry!r} is not a stage; use NAME or NAME:human")
            return None
        entry = {"name": name, "human": bool(sep)}
    if not isinstance(entry, dict):
        problems.append(f"{where}: each stage must be a name or object")
        return None
    _unknown_keys(entry, tuple(Stage.__dataclass_fields__), where, problems)
    name = entry.get("name", "")
    if not isinstance(name, str) or not STAGE_NAME_RE.fullmatch(name):
        problems.append(
            f"{where}: {name!r} is not a stage name (lowercase, digits, hyphens, 2-24 chars)"
        )
        return None
    data = {k: v for k, v in entry.items() if k in Stage.__dataclass_fields__}
    for flag in ("human", "integrate", "worktree"):
        if (
            flag in entry
            and not isinstance(entry[flag], bool)
            and not (flag == "worktree" and entry[flag] is None)
        ):
            problems.append(f"{where}.{name}.{flag} must be true or false")
            data[flag] = False
    for key in ("max_repairs", "max_returns"):
        data[key] = _positive_int(entry, key, 2, f"{where}.{name}", problems)
    for key in ("command", "return_to"):
        if entry.get(key) is not None and (
            not isinstance(entry[key], str) or not entry[key].strip()
        ):
            problems.append(f"{where}.{name}.{key} must be non-empty text")
            data[key] = None
    data["checks"] = _parse_checks(entry.get("checks", []), f"{where}.{name}.checks", problems)
    stage = Stage(**data)
    if sum((stage.human, stage.command is not None, stage.integrate)) > 1:
        problems.append(f"{where}.{name}: choose human, command, or integrate")
    if stage.integrate and stage.worktree is False:
        problems.append(f"{where}.{name}: integration requires a worktree")
    return stage


def parse_stages(raw: object, where: str, problems: list[str]) -> tuple[Stage, ...]:
    """Strings keep the simple flow; objects opt into execution and acceptance rules."""
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where} must be a non-empty list of stage names or objects")
        return ()
    stages: list[Stage] = []
    for entry in raw:
        stage = _stage_entry(entry, where, problems)
        if stage is None:
            continue
        if stage.name in BUILTIN_STAGES:
            problems.append(f"{where}: {stage.name} is a built-in stage")
        elif any(s.name == stage.name for s in stages):
            problems.append(f"{where}: {stage.name} is listed twice")
        else:
            if stage.return_to is not None and stage.return_to not in {s.name for s in stages}:
                problems.append(f"{where}.{stage.name}.return_to must name an earlier stage")
            stages.append(stage)
    return tuple(stages)


def _inside_repo(item: str) -> bool:
    """A relative path that cannot leave the checkout it is copied from."""
    return bool(item) and not Path(item).is_absolute() and ".." not in Path(item).parts


def _project_workflow_options(
    entry: dict, stages: tuple[Stage, ...], where: str, problems: list[str]
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for option in ("worktree_root", "base"):
        value = entry.get(option)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            problems.append(f"{where}.{option} must be non-empty text")
            value = None
        extra[option] = value
    for option, default in (("max_concurrency", 1), ("script_timeout", 600)):
        value = _positive_int(entry, option, default, where, problems)
        if value == 0:
            problems.append(f"{where}.{option} must be positive")
        extra[option] = value
    hooks = entry.get("hooks", {})
    allowed_hooks = {
        "after_transition",
        "teardown",
        *(f"after:{s}" for s in (*BUILTIN_STAGES, *(s.name for s in stages))),
    }
    if not isinstance(hooks, dict):
        problems.append(f"{where}.hooks must be an object")
        hooks = {}
    for event, command in hooks.items():
        if event not in allowed_hooks or not isinstance(command, str) or not command.strip():
            problems.append(
                f"{where}.hooks.{event}: use after_transition, after:STAGE, "
                "or teardown with a command"
            )
    extra["hooks"] = hooks
    return extra


def parse_project(
    paths: Paths, workspace: str, key: str, entry: dict, problems: list[str]
) -> ProjectConfig:
    """Validate PROJECT.md fields with ownership supplied only by its location."""
    where = str(paths.project(workspace, key) / "PROJECT.md")
    _unknown_keys(entry, PROJECT_KEYS, where, problems)
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append(f"{where}.name must be non-empty text")
        name = key
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
    extra = _project_workflow_options(entry, stages, where, problems)
    if repo is None and any(s.worktree or s.integrate for s in stages):
        problems.append(f"{where}: worktree/integrate stages require repo")
    return ProjectConfig(
        **extra,
        key=key,
        name=str(name).strip(),
        workspace=str(workspace),
        repo=repo,
        stages=stages,
        setup=setup,
        copy=tuple(copy),
    )


def project_directory(paths: Paths, workspace: str, key: str) -> Path:
    """Require the recorded project location; never follow or invent another owner."""
    require_workspace(paths, workspace)
    directory = paths.project(workspace, key)
    for path in (directory.parent, directory):
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"{path}: expected a real project directory")
    return directory


def _load_projects(
    paths: Paths, workspaces: Mapping[str, WorkspaceConfig], problems: list[str]
) -> dict[str, ProjectConfig]:
    projects: dict[str, ProjectConfig] = {}
    seen: dict[str, Path] = {}
    for workspace in workspaces:
        root = paths.workspace_projects(workspace)
        try:
            if root.is_symlink() or (root.exists() and not root.is_dir()):
                raise ValueError(f"{root}: expected a real projects directory")
            entries = sorted(root.iterdir()) if root.exists() else []
        except (OSError, ValueError) as exc:
            problems.append(str(exc))
            continue
        for directory in entries:
            path = directory / "PROJECT.md"
            try:
                if directory.is_symlink():
                    raise ValueError("project directory must not be a symbolic link")
                if not directory.is_dir():
                    continue
                key = directory.name
                if not PROJECT_KEY_RE.fullmatch(key):
                    raise ValueError(
                        "keys are 2-10 uppercase letters or digits, starting with a letter"
                    )
                if key in seen:
                    raise ValueError(f"duplicate project {key}; also defined at {seen[key]}")
                seen[key] = path
                document = frontmatter.read(path)
                projects[key] = parse_project(paths, workspace, key, document.fields, problems)
            except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                problems.append(f"{path}: {exc}")
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
    knowledge_raw = raw.get("knowledge", {})
    if not isinstance(knowledge_raw, dict):
        problems.append("web.knowledge must be an object")
        knowledge_raw = {}
    else:
        _unknown_keys(knowledge_raw, ("recent_limit", "page_size"), "web.knowledge", problems)
    recent_limit = _positive_int(
        knowledge_raw,
        "recent_limit",
        DEFAULT_WEB_KNOWLEDGE_RECENT_LIMIT,
        "web.knowledge",
        problems,
    )
    page_size = knowledge_raw.get("page_size", DEFAULT_WEB_KNOWLEDGE_PAGE_SIZE)
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        problems.append("web.knowledge.page_size must be a positive integer")
        page_size = DEFAULT_WEB_KNOWLEDGE_PAGE_SIZE
    return WebConfig(
        host=host.strip(),
        port=port,
        knowledge=WebKnowledgeConfig(recent_limit=recent_limit, page_size=page_size),
    )


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
    version = raw.get("version")
    if (
        (type(version) is int and version == 1)
        or (paths.home / "jobs").exists()
        or (paths.home / "jobs").is_symlink()
    ):
        return None, [LEGACY_HOME_MESSAGE], warnings
    version_ok = type(version) is int and version == CONFIG_VERSION
    if not version_ok:
        problems.append(f"version must be {CONFIG_VERSION}")
    # A document declaring another schema version is not a current-schema document, so its member
    # names belong to a schema this build does not define. Its value and type problems still
    # stand, but unknown-key findings go to a list that is thrown away rather than reporting
    # a future schema as a pile of current-schema typos.
    unknown = problems if version_ok else []
    _unknown_keys(raw, ROOT_KEYS, "", unknown)

    transports = raw.get("transports", {})
    if not isinstance(transports, dict):
        problems.append("transports must be an object")
        transports = {}
    else:
        _unknown_keys(transports, tuple(TRANSPORTS), "transports", unknown)
    slack = _parse_slack(transports.get("slack"), problems, unknown)
    telegram = _parse_telegram(transports.get("telegram"), problems, unknown)
    configured = {name for name in TRANSPORTS if transports.get(name) is not None}
    if not configured:
        problems.append(f"transports must configure {' or '.join(TRANSPORTS)}")

    providers = _parse_providers(raw.get("providers"), problems, warnings, unknown)
    defaults = parse_agent(raw.get("defaults"), "defaults", providers, problems, unknown)
    workspaces = _load_workspaces(paths, providers, problems)
    bindings = _parse_bindings(raw.get("bindings"), paths, configured, problems, warnings)
    projects = _load_projects(paths, workspaces, problems)

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

type _Signature = tuple[int, int, int, int, int] | Literal["missing"]


def _signature(path: Path, *, follow_symlinks: bool = False) -> _Signature:
    """Track content, replacement, and permission changes without opening the file."""
    try:
        status = path.stat(follow_symlinks=follow_symlinks)
    except OSError:
        return "missing"
    return status.st_mtime_ns, status.st_ctime_ns, status.st_size, status.st_ino, status.st_mode


type _ConfigSignature = tuple[tuple[str, _Signature], ...]


def _configuration_signature(paths: Paths) -> _ConfigSignature:
    """Watch installation settings and workspace/project additions, removals, and edits."""
    watched = [paths.config, paths.workspaces]
    try:
        for root in _workspace_entries(paths):
            watched.append(root)
            if root.is_symlink():
                continue
            watched.extend((root / "WORKSPACE.md", root / "projects"))
            projects = root / "projects"
            if projects.is_dir() and not projects.is_symlink():
                for directory in sorted(projects.iterdir()):
                    watched.append(directory)
                    if directory.is_dir() and not directory.is_symlink():
                        watched.append(directory / "PROJECT.md")
    except OSError, ValueError:
        pass  # Changed container signatures still trigger validation.
    return tuple(
        (str(path), _signature(path, follow_symlinks=path == paths.config)) for path in watched
    )


class LiveConfig:
    """The latest valid installation/workspace snapshot, re-read when its files change.

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
        self._signature: _ConfigSignature | None = (
            None if config.source_hash is not None else _configuration_signature(config.paths)
        )
        self._rejected: _ConfigSignature | None = None
        self._lock = threading.Lock()  # callers stat and read from threads and the loop

    def current(self) -> Config:
        """The configuration on disk when it is valid, else the last good one; never raises."""
        with self._lock:
            signature = _configuration_signature(self.paths)
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
            log.info("configuration reloaded (installation, workspace, and project settings)")
            return config


@contextmanager
def config_lock(paths: Paths) -> Iterator[None]:
    """Take the shared configuration writer lock without waiting."""
    try:
        fd = locks.acquire(paths.lock("config"))
    except locks.LockPathError as exc:
        raise ConfigError([str(exc)]) from None
    except BlockingIOError:
        raise ConfigConflictError(
            ["configuration is busy; retry after the current change"]
        ) from None
    try:
        os.fchmod(fd, 0o600)
        yield
    finally:
        os.close(fd)


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
