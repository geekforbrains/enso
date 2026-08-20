"""Static workspace/policy catalog and exact Slack route configuration.

Slack routing deliberately has no user/group policy composition. An exact channel
route authorizes every poster in that channel. An exact DM route is keyed by
the Slack user ID it authorizes. Each route selects one filesystem workspace,
which owns exactly one reusable native-CLI policy.

Invalid security configuration fails closed. Structural errors disable routed
dispatch, while invalid workspaces, policies, and routes make every
route that references them unusable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TypeGuard

from .auth import parse_telegram_allowed_users
from .config import (
    DEFAULT_WORKSPACE_CONCURRENCY,
    ConfigError,
    managed_workspace_path,
    validate_workspace_name,
)
from .providers import PROVIDER_NAMES

AUDIT_ON_FAILURE_VALUES = ("block", "warn")
DEFAULT_AUDIT_MAX_AGE_DAYS = 365

# Canonical native-policy sources, relative to a policy's policy_dir.
POLICY_FILES = {
    "claude": os.path.join("claude", "settings.json"),
    "codex": os.path.join("codex", "config.toml"),
    "grok": os.path.join("grok", "config.toml"),
}

# Names env_passthrough may never carry: the launch owns these (policy.py's
# _KEEP_ENV allowlist plus PATH and the launch-set CODEX_HOME and GROK_HOME;
# GROK_SANDBOX and GROK_FOLDER_TRUST are never set by a launch but change
# kernel sandboxing and folder trust, so they may not ride through either),
# and ENSO_* is Enso's own namespace. Defined literally because policy.py
# imports teams.py, so importing _KEEP_ENV back would be circular.
_ENV_PASSTHROUGH_RESERVED = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "USER",
        "SHELL",
        "PATH",
        "CODEX_HOME",
        "GROK_HOME",
        "GROK_SANDBOX",
        "GROK_FOLDER_TRUST",
    }
)
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_POLICY_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_POLICY_NAME_RE = re.compile(_POLICY_NAME_PATTERN)
MANAGED_WORKSPACES_MIGRATION_URL = (
    "https://github.com/geekforbrains/enso/blob/main/"
    "docs/migrations/v2.0-managed-workspaces.md"
)
_SLACK_TRANSPORT_KEYS = {
    "account_id",
    "app_token",
    "app_token_1password",
    "bot_token",
    "bot_token_1password",
    "bot_user_id",
    "channel_context_messages",
    "channel_defaults",
    "channels",
    "dms",
    "notify_channel",
    "persistent_surfaces",
    "rich_messages",
    # Kept only so its dedicated migration error is not duplicated as an
    # unknown-key error.
    "allowed_users",
}
_TELEGRAM_TRANSPORT_KEYS = {
    "allowed_user_ids",  # dedicated migration error
    "allowed_users",
    "bot_token",
    "bot_token_1password",
    "notify_channel",
    "workspace",
}


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _canonical(path: str) -> str:
    """Return an expanded, absolute, symlink-resolved filesystem path."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _legacy_workspace_path_problem(name: str) -> str:
    """Return the one actionable diagnostic for the removed path setting."""
    return (
        f"workspaces.{name}.path is no longer supported; move this workspace to "
        f"~/.enso/workspaces/{name}, remove the path key, and follow "
        f"{MANAGED_WORKSPACES_MIGRATION_URL}"
    )


def _catalog_path(path: str, label: str, problems: list[str]) -> str:
    """Normalize a configured path without erasing lexical symlink evidence."""
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        problems.append(f"{label} must be absolute or start with ~/")
    return os.path.abspath(expanded)


def _within(child: str, parent: str) -> bool:
    """True when *child* equals or sits under *parent* (canonical paths)."""
    return child == parent or child.startswith(parent + os.sep)


def _unknown_keys(cfg: dict, allowed: set[str], label: str) -> list[str]:
    unknown = sorted(str(key) for key in set(cfg) - allowed)
    return [f"{label} has unknown keys {unknown}"] if unknown else []


@dataclass(frozen=True)
class Workspace:
    """A named filesystem root shared by one or more Slack routes."""

    name: str
    path: str
    policy: str
    concurrency: int


@dataclass(frozen=True)
class Policy:
    """One reusable native-CLI policy and Enso capability selection."""

    name: str
    policy_dir: str | None
    unrestricted: bool
    providers: tuple[str, ...]
    default_provider: str | None
    chat_commands: tuple[str, ...] | str
    env_passthrough: tuple[str, ...] = ()

    def allows_provider(self, name: str) -> bool:
        return name in self.providers

    def allows_command(self, name: str) -> bool:
        return self.chat_commands == "*" or name in self.chat_commands


@dataclass(frozen=True)
class Route:
    """One exact Slack DM-user or channel route.

    ``mention_required`` and ``thread_mention_required`` are channel response
    triggers resolved at load time (route override, then
    ``transports.slack.channel_defaults``, then the built-in ``True``). DM routes
    always carry the defaults; DM behavior is not configurable.
    """

    route_id: str  # "slack.dm.<USER_ID>" | "slack.channel.<CHANNEL_ID>"
    kind: str  # "dm" | "channel"
    key: str  # exact Slack user ID for DMs; exact channel ID otherwise
    workspace: str
    audit: bool
    mention_required: bool = True
    thread_mention_required: bool = True


@dataclass(frozen=True)
class Decision:
    """Outcome of resolving one inbound Slack event against static config."""

    status: str  # "authorized" | "unconfigured" | "error"
    reason: str
    route: Route | None = None


@dataclass(frozen=True)
class ExecutionCatalog:
    """Transport-independent workspace and policy configuration."""

    workspaces: dict[str, Workspace]
    policies: dict[str, Policy]
    errors: tuple[str, ...] = ()
    workspace_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    policy_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        """Whether catalog-wide structure and path topology are valid."""
        return not self.errors

    def usable(self, workspace: str) -> bool:
        """Whether a workspace and its configured policy form a valid binding."""
        item = self.workspaces.get(workspace)
        return (
            self.valid
            and item is not None
            and workspace not in self.workspace_errors
            and item.policy in self.policies
            and item.policy not in self.policy_errors
        )

    def policy_for(self, workspace: str | Workspace) -> Policy:
        """Return the policy owned by a known workspace.

        Callers that process untrusted configuration must check ``usable`` first.
        """
        item = workspace if isinstance(workspace, Workspace) else self.workspaces[workspace]
        return self.policies[item.policy]


@dataclass(frozen=True)
class TeamsConfig:
    """Parsed Slack routes over a transport-independent execution catalog."""

    account_id: str
    catalog: ExecutionCatalog
    dm_routes: dict[str, Route]
    channel_routes: dict[str, Route]
    audit_on_failure: str
    audit_max_age_days: int
    errors: tuple[str, ...] = ()
    route_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def workspaces(self) -> dict[str, Workspace]:
        return self.catalog.workspaces

    @property
    def policies(self) -> dict[str, Policy]:
        return self.catalog.policies

    @property
    def workspace_errors(self) -> dict[str, tuple[str, ...]]:
        return self.catalog.workspace_errors

    @property
    def policy_errors(self) -> dict[str, tuple[str, ...]]:
        return self.catalog.policy_errors

    @property
    def dispatchable(self) -> bool:
        """Whether Slack teams dispatch is enabled at all."""
        return not self.errors

    def route_usable(self, route: Route) -> bool:
        """Whether a configured route has a usable workspace-policy binding."""
        if route.route_id in self.route_errors:
            return False
        return self.catalog.usable(route.workspace)


@dataclass(frozen=True)
class TelegramConfig:
    """Parsed private Telegram authorization and workspace binding."""

    catalog: ExecutionCatalog
    allowed_users: tuple[str, ...]
    workspace: Workspace | None
    policy: Policy | None
    errors: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.errors and self.workspace is not None and self.policy is not None


def load_catalog(config: dict) -> ExecutionCatalog:
    """Parse reusable workspace/policy configuration without a transport."""
    errors: list[str] = []
    workspaces, workspace_errors = _load_workspaces(config.get("workspaces", {}), errors)
    policies, policy_errors = _load_policies(config.get("policies", {}), errors)
    if "access" in config:
        errors.append(
            "access is no longer supported; rename it to policies and assign one policy "
            "to every workspace"
        )
    if "routes" in config:
        errors.append(
            "routes is no longer supported; move routes.slack fields into transports.slack"
        )
    if "working_dir" in config:
        errors.append(
            "working_dir is no longer supported; define named workspaces and bind each "
            "transport to one"
        )
    for name, workspace in workspaces.items():
        if workspace.policy and workspace.policy not in policies:
            workspace_errors[name] = (
                *workspace_errors.get(name, ()),
                f"unknown policy {workspace.policy!r}",
            )
    _check_topology(workspaces, policies, errors)
    return ExecutionCatalog(
        workspaces=workspaces,
        policies=policies,
        errors=tuple(errors),
        workspace_errors=workspace_errors,
        policy_errors=policy_errors,
    )


def load_telegram(config: dict) -> TelegramConfig:
    """Parse Telegram's exact users and single workspace-owned policy."""
    catalog = load_catalog(config)
    errors = list(catalog.errors)
    transports = config.get("transports", {})
    if not isinstance(transports, dict):
        errors.append("transports must be an object")
        telegram_cfg: dict = {}
    else:
        raw_telegram = transports.get("telegram", {})
        if not isinstance(raw_telegram, dict):
            errors.append("transports.telegram must be an object")
            telegram_cfg = {}
        else:
            telegram_cfg = raw_telegram

    errors.extend(
        _unknown_keys(telegram_cfg, _TELEGRAM_TRANSPORT_KEYS, "transports.telegram")
    )
    if "allowed_user_ids" in telegram_cfg:
        errors.append(
            "transports.telegram.allowed_user_ids is no longer supported; use "
            "allowed_users with exact numeric string IDs"
        )
    allowed_users = parse_telegram_allowed_users(telegram_cfg)
    if not allowed_users:
        errors.append(
            "transports.telegram.allowed_users must be a non-empty list of unique "
            "positive numeric strings"
        )

    workspace: Workspace | None = None
    execution_policy: Policy | None = None
    workspace_name = telegram_cfg.get("workspace")
    if not isinstance(workspace_name, str) or not workspace_name:
        errors.append("transports.telegram.workspace is required and must be a string")
    elif workspace_name not in catalog.workspaces:
        errors.append(
            f"transports.telegram.workspace references unknown workspace {workspace_name!r}"
        )
    elif not catalog.usable(workspace_name):
        errors.append(
            f"transports.telegram.workspace {workspace_name!r} does not have a usable policy"
        )
    else:
        workspace = catalog.workspaces[workspace_name]
        execution_policy = catalog.policy_for(workspace)

    return TelegramConfig(
        catalog=catalog,
        allowed_users=tuple(allowed_users),
        workspace=workspace,
        policy=execution_policy,
        errors=tuple(errors),
    )


def load_teams(config: dict) -> TeamsConfig:
    """Parse static teams configuration without raising on invalid input."""
    catalog = load_catalog(config)
    errors = list(catalog.errors)

    transports = config.get("transports", {})
    if not isinstance(transports, dict):
        errors.append("transports must be an object")
        slack_cfg: dict = {}
    else:
        raw_slack = transports.get("slack", {})
        if not isinstance(raw_slack, dict):
            errors.append("transports.slack must be an object")
            slack_cfg = {}
        else:
            slack_cfg = raw_slack
    if "allowed_users" in slack_cfg:
        errors.append(
            "transports.slack.allowed_users is no longer supported; migrate authorized "
            "DM users to transports.slack.dms and channels to transports.slack.channels"
        )
    errors.extend(_unknown_keys(slack_cfg, _SLACK_TRANSPORT_KEYS, "transports.slack"))
    if "groups" in config:
        errors.append(
            "groups is no longer supported; channel membership and "
            "exact DM user routes define authorization"
        )

    account_id = slack_cfg.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        errors.append("transports.slack.account_id is required and must be a string")
        account_id = ""

    channel_defaults = _load_channel_defaults(slack_cfg.get("channel_defaults"), errors)
    dm_routes, dm_schema_errors = _load_routes(slack_cfg.get("dms", {}), "dm", errors)
    channel_routes, channel_schema_errors = _load_routes(
        slack_cfg.get("channels", {}),
        "channel",
        errors,
        channel_defaults=channel_defaults,
    )

    route_errors: dict[str, tuple[str, ...]] = {}
    for route in (*dm_routes.values(), *channel_routes.values()):
        schema_errors = dm_schema_errors if route.kind == "dm" else channel_schema_errors
        problems = [*schema_errors.get(route.route_id, ())]
        problems.extend(_route_problems(route, catalog))
        if problems:
            route_errors[route.route_id] = tuple(problems)

    audit_cfg = config.get("audit", {})
    if not isinstance(audit_cfg, dict):
        errors.append("audit must be an object")
        audit_cfg = {}
    errors.extend(_unknown_keys(audit_cfg, {"on_failure", "max_age_days"}, "audit"))
    on_failure = audit_cfg.get("on_failure", "block")
    if on_failure not in AUDIT_ON_FAILURE_VALUES:
        errors.append(f"audit.on_failure must be one of {AUDIT_ON_FAILURE_VALUES}")
        on_failure = "block"
    max_age = audit_cfg.get("max_age_days", DEFAULT_AUDIT_MAX_AGE_DAYS)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 0:
        errors.append("audit.max_age_days must be a non-negative integer")
        max_age = DEFAULT_AUDIT_MAX_AGE_DAYS

    return TeamsConfig(
        account_id=account_id,
        catalog=catalog,
        dm_routes=dm_routes,
        channel_routes=channel_routes,
        audit_on_failure=on_failure,
        audit_max_age_days=max_age,
        errors=tuple(errors),
        route_errors=route_errors,
    )


def _load_workspaces(
    block: object, errors: list[str]
) -> tuple[dict[str, Workspace], dict[str, tuple[str, ...]]]:
    workspaces: dict[str, Workspace] = {}
    workspace_errors: dict[str, tuple[str, ...]] = {}
    if not isinstance(block, dict):
        errors.append("workspaces must be an object")
        return workspaces, workspace_errors

    for raw_name, cfg in block.items():
        if not isinstance(raw_name, str) or not raw_name:
            errors.append("workspace names must be non-empty strings")
            continue
        name = raw_name
        try:
            validate_workspace_name(name)
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(cfg, dict):
            errors.append(f"workspaces.{name} must be an object")
            continue
        # ``path`` is recognized only to emit one focused breaking-migration
        # diagnostic instead of duplicating it as an unknown-key error.
        problems = _unknown_keys(cfg, {"path", "policy", "concurrency"}, f"workspaces.{name}")
        if "path" in cfg:
            problems.append(_legacy_workspace_path_problem(name))
        policy = cfg.get("policy")
        if not isinstance(policy, str) or not policy:
            problems.append("policy is required and must be a string")
            policy = ""
        concurrency = cfg.get("concurrency", DEFAULT_WORKSPACE_CONCURRENCY)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            problems.append("concurrency must be a positive integer")
            concurrency = DEFAULT_WORKSPACE_CONCURRENCY
        workspaces[name] = Workspace(
            name=name,
            path=managed_workspace_path(name),
            policy=policy,
            concurrency=concurrency,
        )
        if problems:
            workspace_errors[name] = tuple(problems)

    return workspaces, workspace_errors


def _load_policies(
    block: object, errors: list[str]
) -> tuple[dict[str, Policy], dict[str, tuple[str, ...]]]:
    policies: dict[str, Policy] = {}
    policy_errors: dict[str, tuple[str, ...]] = {}
    if not isinstance(block, dict):
        errors.append("policies must be an object")
        return policies, policy_errors

    allowed = {
        "policy_dir",
        "unrestricted",
        "providers",
        "default_provider",
        "chat_commands",
        "env_passthrough",
    }
    for raw_name, cfg in block.items():
        if not isinstance(raw_name, str) or not raw_name:
            errors.append("policy names must be non-empty strings")
            continue
        name = raw_name
        if not _POLICY_NAME_RE.fullmatch(name):
            errors.append(f"policy names must match {_POLICY_NAME_PATTERN}")
            continue
        if not isinstance(cfg, dict):
            errors.append(f"policies.{name} must be an object")
            continue
        problems = _unknown_keys(cfg, allowed, f"policies.{name}")

        unrestricted_raw = cfg.get("unrestricted", False)
        if not isinstance(unrestricted_raw, bool):
            problems.append("unrestricted must be a boolean")
            unrestricted = False
        else:
            unrestricted = unrestricted_raw

        policy_dir = _load_policy_dir(cfg, unrestricted, problems)

        providers_raw = cfg.get("providers")
        if not _is_str_list(providers_raw) or not providers_raw:
            problems.append("providers is required and must be a non-empty string list")
            providers_raw = []
        elif len(providers_raw) != len(set(providers_raw)):
            problems.append("providers contains duplicate names")
        unknown_providers = [p for p in providers_raw if p not in PROVIDER_NAMES]
        if unknown_providers:
            problems.append(f"unknown providers {unknown_providers}")

        default_provider = cfg.get("default_provider")
        if not isinstance(default_provider, str) or default_provider not in providers_raw:
            problems.append("default_provider is required and must be one of providers")
            default_provider = None

        commands = _load_capability(cfg.get("chat_commands"), "chat_commands", problems)
        passthrough = _load_env_passthrough(cfg.get("env_passthrough"), unrestricted, problems)
        policies[name] = Policy(
            name=name,
            policy_dir=policy_dir,
            unrestricted=unrestricted,
            providers=tuple(providers_raw),
            default_provider=default_provider,
            chat_commands=commands,
            env_passthrough=passthrough,
        )
        if problems:
            policy_errors[name] = tuple(problems)

    return policies, policy_errors


def _load_policy_dir(cfg: dict, unrestricted: bool, problems: list[str]) -> str | None:
    """Load an explicit lexical policy root without resolving away symlinks."""
    if "policy_dir" not in cfg:
        if not unrestricted:
            problems.append("policy_dir is required for restricted policies")
        return None
    configured = cfg["policy_dir"]
    if not isinstance(configured, str) or not configured:
        problems.append("policy_dir must be a non-empty string path")
        configured = None
    if unrestricted:
        problems.append("unrestricted: true is invalid alongside policy_dir")
        return None
    if configured is None:
        return None
    return _catalog_path(configured, "policy_dir", problems)


def _load_capability(value: object, key: str, problems: list[str]) -> tuple[str, ...] | str:
    """Load an allowlist expressed as the exact string ``*`` or a string list."""
    if value is None:
        return ()
    if value == "*":
        return "*"
    if _is_str_list(value):
        if len(value) != len(set(value)):
            problems.append(f"{key} contains duplicate names")
        return tuple(value)
    problems.append(f'{key} must be "*" or a list of names')
    return ()


def _load_env_passthrough(
    value: object, unrestricted: bool, problems: list[str]
) -> tuple[str, ...]:
    """Load environment variable names admitted into policy-controlled launches."""
    if value is None:
        return ()
    if not _is_str_list(value):
        problems.append("env_passthrough must be a list of strings")
        return ()
    if unrestricted:
        # An unrestricted policy inherits the full environment; accepting the
        # key would let the operator believe they scoped something.
        problems.append("unrestricted: true is invalid alongside env_passthrough")
    if len(value) != len(set(value)):
        problems.append("env_passthrough contains duplicate names")
    valid: list[str] = []
    for name in value:
        if not _ENV_NAME_RE.fullmatch(name):
            problems.append(f"env_passthrough name {name!r} must match [A-Z][A-Z0-9_]*")
        elif name in _ENV_PASSTHROUGH_RESERVED or name.startswith("ENSO_"):
            problems.append(
                f"env_passthrough may not name {name}; launch-controlled and "
                "Enso-owned names are reserved"
            )
        elif name not in valid:
            valid.append(name)
    return tuple(valid)


def _check_topology(
    workspaces: dict[str, Workspace],
    policies: dict[str, Policy],
    errors: list[str],
) -> None:
    """Validate that mutable workspaces cannot overlap policy locations."""
    paths = {name: ws.path for name, ws in workspaces.items()}
    for policy_name, policy in policies.items():
        if policy.policy_dir is None:
            continue
        policy_lexical = os.path.abspath(os.path.expanduser(policy.policy_dir))
        policy_path = _canonical(policy.policy_dir)
        for workspace_name, root in paths.items():
            workspace_lexical = os.path.abspath(os.path.expanduser(root))
            if _within(policy_lexical, workspace_lexical) or _within(
                workspace_lexical, policy_lexical
            ):
                errors.append(
                    f"policy_dir of policy {policy_name} lexically overlaps {workspace_name}"
                )
                continue
            workspace_path = _canonical(root)
            if _within(policy_path, workspace_path) or _within(workspace_path, policy_path):
                errors.append(f"policy_dir of policy {policy_name} overlaps {workspace_name}")


# Channel response triggers: config key -> built-in default.
_MENTION_SETTING_DEFAULTS = {
    "mention_required": True,
    "thread_mention_required": True,
}


def _load_channel_defaults(value: object, errors: list[str]) -> dict[str, bool]:
    """Parse ``transports.slack.channel_defaults`` into effective trigger defaults."""
    defaults = dict(_MENTION_SETTING_DEFAULTS)
    if value is None:
        return defaults
    if not isinstance(value, dict):
        errors.append("transports.slack.channel_defaults must be an object")
        return defaults
    errors.extend(
        _unknown_keys(value, set(_MENTION_SETTING_DEFAULTS), "transports.slack.channel_defaults")
    )
    for key in _MENTION_SETTING_DEFAULTS:
        if key not in value:
            continue
        if isinstance(value[key], bool):
            defaults[key] = value[key]
        else:
            errors.append(f"transports.slack.channel_defaults.{key} must be a boolean")
    return defaults


def _build_route(
    cfg: dict,
    *,
    kind: str,
    key: str,
    route_id: str,
    allowed: set[str],
    defaults: dict[str, bool],
) -> tuple[Route, list[str]]:
    """Build one route from its config block, collecting its own problems.

    Problems are per-route rather than global so one bad route disables only
    itself; the returned Route still exists so dispatch can report why.
    """
    problems = _unknown_keys(cfg, allowed, route_id)
    if "allow" in cfg:
        problems.append(
            "allow is no longer supported; channel membership and exact DM user "
            "routes define authorization"
        )
    if kind == "dm":
        for setting in _MENTION_SETTING_DEFAULTS:
            if setting in cfg:
                problems.append(
                    f"{setting} is not valid on a DM route; DM behavior is fixed"
                )
    workspace = cfg.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        problems.append("workspace is required and must be a string")
        workspace = ""
    audit_raw = cfg.get("audit", False)
    if not isinstance(audit_raw, bool):
        problems.append("audit must be a boolean")
        audit_value = False
    else:
        audit_value = audit_raw
    triggers = dict(defaults)
    if kind == "channel":
        for setting in _MENTION_SETTING_DEFAULTS:
            if setting not in cfg:
                continue
            if isinstance(cfg[setting], bool):
                triggers[setting] = cfg[setting]
            else:
                problems.append(f"{setting} must be a boolean")
    route = Route(
        route_id=route_id,
        kind=kind,
        key=key,
        workspace=workspace,
        audit=audit_value,
        mention_required=triggers["mention_required"],
        thread_mention_required=triggers["thread_mention_required"],
    )
    return route, problems


def _load_routes(
    block: object,
    kind: str,
    errors: list[str],
    *,
    channel_defaults: dict[str, bool] | None = None,
) -> tuple[dict[str, Route], dict[str, tuple[str, ...]]]:
    routes: dict[str, Route] = {}
    route_errors: dict[str, tuple[str, ...]] = {}
    label = "dms" if kind == "dm" else "channels"
    allowed = {"workspace", "audit"}
    if kind == "channel":
        allowed |= set(_MENTION_SETTING_DEFAULTS)
    defaults = channel_defaults or dict(_MENTION_SETTING_DEFAULTS)
    if not isinstance(block, dict):
        errors.append(f"transports.slack.{label} must be an object")
        return routes, route_errors
    for raw_key, cfg in block.items():
        if not isinstance(raw_key, str) or not raw_key:
            errors.append(f"transports.slack.{label} keys must be non-empty Slack IDs")
            continue
        key = raw_key
        route_id = f"slack.{kind}.{key}"
        if not isinstance(cfg, dict):
            errors.append(f"{route_id} must be an object")
            continue
        route, problems = _build_route(
            cfg,
            kind=kind,
            key=key,
            route_id=route_id,
            allowed=allowed,
            defaults=defaults,
        )
        routes[key] = route
        if problems:
            route_errors[route_id] = tuple(problems)
    return routes, route_errors


def _route_problems(
    route: Route,
    catalog: ExecutionCatalog,
) -> list[str]:
    problems: list[str] = []
    if route.workspace and route.workspace not in catalog.workspaces:
        problems.append(f"unknown workspace {route.workspace!r}")
    elif route.workspace:
        problems.extend(catalog.workspace_errors.get(route.workspace, ()))
    return problems


# -- Resolution --


def resolve(teams: TeamsConfig, *, user_id: str, channel_id: str | None) -> Decision:
    """Resolve one Slack event using exact static channel or DM-user routes."""
    route = (
        teams.dm_routes.get(user_id) if channel_id is None else teams.channel_routes.get(channel_id)
    )
    if route is None:
        return Decision(status="unconfigured", reason="no_route")
    if not teams.dispatchable:
        return Decision(status="error", reason="teams_config_invalid", route=route)
    if not teams.route_usable(route):
        return Decision(status="error", reason="route_unusable", route=route)
    return Decision(status="authorized", reason="ok", route=route)
