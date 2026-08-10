"""Static Slack route, workspace, and access-profile configuration.

Teams mode deliberately has no user/group policy composition. An exact channel
route authorizes every poster in that channel. An exact DM route is keyed by
the Slack user ID it authorizes. Each route selects one filesystem workspace
and one complete native-CLI access profile.

Invalid security configuration fails closed. Structural errors disable teams
dispatch, while invalid workspaces, access profiles, and routes make every
route that references them unusable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .providers import PROVIDER_NAMES

AUDIT_ON_FAILURE_VALUES = ("block", "warn")
DEFAULT_AUDIT_MAX_AGE_DAYS = 365

# Canonical native-policy sources, relative to an access profile's policy_dir.
POLICY_FILES = {
    "claude": os.path.join("claude", "settings.json"),
    "codex": os.path.join("codex", "config.toml"),
}


def _default_policy_dir(access_name: str) -> str:
    from . import config as config_mod

    return os.path.join(config_mod.CONFIG_DIR, "policies", access_name)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _canonical(path: str) -> str:
    """Return an expanded, absolute, symlink-resolved filesystem path."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


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
    concurrency: int


@dataclass(frozen=True)
class AccessProfile:
    """One complete native-CLI policy and Enso capability selection."""

    name: str
    policy_dir: str | None
    unrestricted: bool
    providers: tuple[str, ...]
    default_provider: str | None
    chat_commands: tuple[str, ...] | str

    def allows_provider(self, name: str) -> bool:
        return name in self.providers

    def allows_command(self, name: str) -> bool:
        return self.chat_commands == "*" or name in self.chat_commands


@dataclass(frozen=True)
class Route:
    """One exact Slack DM-user or channel route."""

    route_id: str  # "slack.dm.<USER_ID>" | "slack.channel.<CHANNEL_ID>"
    kind: str  # "dm" | "channel"
    key: str  # exact Slack user ID for DMs; exact channel ID otherwise
    workspace: str
    access: str
    audit: bool


@dataclass(frozen=True)
class Decision:
    """Outcome of resolving one inbound Slack event against static config."""

    status: str  # "authorized" | "unconfigured" | "error"
    reason: str
    route: Route | None = None


@dataclass(frozen=True)
class TeamsConfig:
    """Parsed teams blocks plus every validation problem found."""

    account_id: str
    workspaces: dict[str, Workspace]
    access_profiles: dict[str, AccessProfile]
    dm_routes: dict[str, Route]
    channel_routes: dict[str, Route]
    audit_on_failure: str
    audit_max_age_days: int
    errors: tuple[str, ...] = ()
    workspace_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    access_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    route_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def dispatchable(self) -> bool:
        """Whether Slack teams dispatch is enabled at all."""
        return not self.errors

    def route_usable(self, route: Route) -> bool:
        """Whether a configured route has usable workspace and access bindings."""
        if route.route_id in self.route_errors:
            return False
        if route.workspace not in self.workspaces:
            return False
        if route.access not in self.access_profiles:
            return False
        return (
            route.workspace not in self.workspace_errors and route.access not in self.access_errors
        )


def slack_mode(config: dict) -> str:
    """Classify Slack handling: 'teams' | 'legacy' | 'conflict' | 'blocked'."""
    routes = config.get("routes")
    has_teams = isinstance(routes, dict) and "slack" in routes
    transports = config.get("transports", {})
    slack_cfg = transports.get("slack", {}) if isinstance(transports, dict) else {}
    has_legacy = isinstance(slack_cfg, dict) and "allowed_users" in slack_cfg
    if has_teams and has_legacy:
        return "conflict"
    if has_teams:
        return "teams"
    if has_legacy:
        return "legacy"
    return "blocked"


def load_teams(config: dict) -> TeamsConfig | None:
    """Parse static teams configuration without raising on invalid input."""
    routes_block = config.get("routes")
    if not (isinstance(routes_block, dict) and "slack" in routes_block):
        return None

    errors: list[str] = []
    if slack_mode(config) == "conflict":
        errors.append(
            "routes.slack and transports.slack.allowed_users are both set; "
            "remove one — teams mode and the legacy allowlist are mutually exclusive"
        )
    if "groups" in config:
        errors.append(
            "groups is no longer supported in teams mode; channel membership and "
            "exact DM user routes define authorization"
        )

    slack_routes = routes_block["slack"]
    if not isinstance(slack_routes, dict):
        errors.append("routes.slack must be an object")
        slack_routes = {}
    errors.extend(_unknown_keys(slack_routes, {"account_id", "dms", "channels"}, "routes.slack"))

    account_id = slack_routes.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        errors.append("routes.slack.account_id is required and must be a string")
        account_id = ""

    workspaces, workspace_errors = _load_workspaces(config.get("workspaces", {}), errors)
    access_profiles, access_errors = _load_access(config.get("access", {}), errors)
    dm_routes, dm_schema_errors = _load_routes(slack_routes.get("dms", {}), "dm", errors)
    channel_routes, channel_schema_errors = _load_routes(
        slack_routes.get("channels", {}), "channel", errors
    )

    route_errors: dict[str, tuple[str, ...]] = {}
    for route in (*dm_routes.values(), *channel_routes.values()):
        schema_errors = dm_schema_errors if route.kind == "dm" else channel_schema_errors
        problems = [*schema_errors.get(route.route_id, ())]
        problems.extend(_route_problems(route, workspaces, access_profiles))
        if problems:
            route_errors[route.route_id] = tuple(problems)

    _check_topology(
        workspaces,
        access_profiles,
        config.get("working_dir"),
        errors,
    )

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
        workspaces=workspaces,
        access_profiles=access_profiles,
        dm_routes=dm_routes,
        channel_routes=channel_routes,
        audit_on_failure=on_failure,
        audit_max_age_days=max_age,
        errors=tuple(errors),
        workspace_errors=workspace_errors,
        access_errors=access_errors,
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
        if not isinstance(cfg, dict):
            errors.append(f"workspaces.{name} must be an object")
            continue
        problems = _unknown_keys(cfg, {"path", "concurrency"}, f"workspaces.{name}")
        path = cfg.get("path")
        if not isinstance(path, str) or not path:
            problems.append("path is required and must be a string")
            path = ""
        concurrency = cfg.get("concurrency", 1)
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            problems.append("concurrency must be a positive integer")
            concurrency = 1
        workspaces[name] = Workspace(
            name=name,
            path=_canonical(path) if path else "",
            concurrency=concurrency,
        )
        if problems:
            workspace_errors[name] = tuple(problems)

    return workspaces, workspace_errors


def _load_access(
    block: object, errors: list[str]
) -> tuple[dict[str, AccessProfile], dict[str, tuple[str, ...]]]:
    profiles: dict[str, AccessProfile] = {}
    profile_errors: dict[str, tuple[str, ...]] = {}
    if not isinstance(block, dict):
        errors.append("access must be an object")
        return profiles, profile_errors

    allowed = {
        "policy_dir",
        "unrestricted",
        "providers",
        "default_provider",
        "chat_commands",
    }
    for raw_name, cfg in block.items():
        if not isinstance(raw_name, str) or not raw_name:
            errors.append("access profile names must be non-empty strings")
            continue
        name = raw_name
        if not isinstance(cfg, dict):
            errors.append(f"access.{name} must be an object")
            continue
        problems = _unknown_keys(cfg, allowed, f"access.{name}")

        unrestricted_raw = cfg.get("unrestricted", False)
        if not isinstance(unrestricted_raw, bool):
            problems.append("unrestricted must be a boolean")
            unrestricted = False
        else:
            unrestricted = unrestricted_raw

        explicit_policy_dir = cfg.get("policy_dir")
        if explicit_policy_dir is not None and (
            not isinstance(explicit_policy_dir, str) or not explicit_policy_dir
        ):
            problems.append("policy_dir must be a non-empty string path")
            explicit_policy_dir = None
        if unrestricted and explicit_policy_dir is not None:
            problems.append("unrestricted: true is invalid alongside policy_dir")
        policy_dir = (
            None if unrestricted else _canonical(explicit_policy_dir or _default_policy_dir(name))
        )

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
        profiles[name] = AccessProfile(
            name=name,
            policy_dir=policy_dir,
            unrestricted=unrestricted,
            providers=tuple(providers_raw),
            default_provider=default_provider,
            chat_commands=commands,
        )
        if problems:
            profile_errors[name] = tuple(problems)

    return profiles, profile_errors


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


def _check_topology(
    workspaces: dict[str, Workspace],
    profiles: dict[str, AccessProfile],
    working_dir: object,
    errors: list[str],
) -> None:
    """Validate that mutable workspaces cannot overlap policy locations."""
    paths = {name: ws.path for name, ws in workspaces.items() if ws.path}
    names = sorted(paths)
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            if _within(paths[first], paths[second]) or _within(paths[second], paths[first]):
                errors.append(f"workspaces {first} and {second} have overlapping or nested paths")

    protected_roots = dict(paths)
    if isinstance(working_dir, str) and working_dir:
        protected_roots["legacy working_dir"] = _canonical(working_dir)
    for profile_name, profile in profiles.items():
        if profile.policy_dir is None:
            continue
        for root_name, root in protected_roots.items():
            if _within(profile.policy_dir, root) or _within(root, profile.policy_dir):
                errors.append(f"policy_dir of access profile {profile_name} overlaps {root_name}")


def _load_routes(
    block: object, kind: str, errors: list[str]
) -> tuple[dict[str, Route], dict[str, tuple[str, ...]]]:
    routes: dict[str, Route] = {}
    route_errors: dict[str, tuple[str, ...]] = {}
    label = "dms" if kind == "dm" else "channels"
    if not isinstance(block, dict):
        errors.append(f"routes.slack.{label} must be an object")
        return routes, route_errors
    for raw_key, cfg in block.items():
        if not isinstance(raw_key, str) or not raw_key:
            errors.append(f"routes.slack.{label} keys must be non-empty Slack IDs")
            continue
        key = raw_key
        route_id = f"slack.{kind}.{key}"
        if not isinstance(cfg, dict):
            errors.append(f"{route_id} must be an object")
            continue
        problems = _unknown_keys(cfg, {"workspace", "access", "audit"}, route_id)
        if "allow" in cfg:
            problems.append(
                "allow is no longer supported; channel membership and exact DM user "
                "routes define authorization"
            )
        workspace = cfg.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            problems.append("workspace is required and must be a string")
            workspace = ""
        access = cfg.get("access")
        if not isinstance(access, str) or not access:
            problems.append("access is required and must be a string")
            access = ""
        audit_raw = cfg.get("audit", False)
        if not isinstance(audit_raw, bool):
            problems.append("audit must be a boolean")
            audit_value = False
        else:
            audit_value = audit_raw
        route = Route(
            route_id=route_id,
            kind=kind,
            key=key,
            workspace=workspace,
            access=access,
            audit=audit_value,
        )
        routes[key] = route
        if problems:
            route_errors[route_id] = tuple(problems)
    return routes, route_errors


def _route_problems(
    route: Route,
    workspaces: dict[str, Workspace],
    profiles: dict[str, AccessProfile],
) -> list[str]:
    problems: list[str] = []
    if route.workspace and route.workspace not in workspaces:
        problems.append(f"unknown workspace {route.workspace!r}")
    if route.access and route.access not in profiles:
        problems.append(f"unknown access profile {route.access!r}")
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
