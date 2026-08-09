"""Slack teams mode — groups, workspaces, exact routes, and resolution.

Owns the fail-closed reading of the ``groups``, ``workspaces``, ``routes``,
and ``audit`` config blocks defined in docs/specs/data-model.md, and the
route resolution defined in docs/specs/teams.md.

The governing rule is that invalid security config never degrades to
permissive defaults: a schema error in any teams block disables Slack teams
dispatch entirely, a workspace error makes every route bound to it unusable,
and a route error disables that route. Silence and explicit errors are
distinct controls — an unknown or disallowed sender learns nothing, while an
authorized sender with an unusable route gets a diagnostic.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

from .providers import PROVIDER_NAMES

CONTEXT_FROM_VALUES = ("allowed", "everyone")
AUDIT_ON_FAILURE_VALUES = ("block", "warn")
DEFAULT_AUDIT_MAX_AGE_DAYS = 365

# Canonical native-policy sources, relative to a workspace's policy_dir.
POLICY_FILES = {
    "claude": os.path.join("claude", "settings.json"),
    "codex": os.path.join("codex", "config.toml"),
}


def _default_policy_dir(workspace_name: str) -> str:
    from . import config as config_mod

    return os.path.join(config_mod.CONFIG_DIR, "policies", workspace_name)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _within(child: str, parent: str) -> bool:
    """True when *child* equals or sits under *parent* (both realpaths)."""
    return child == parent or child.startswith(parent + os.sep)


@dataclass(frozen=True)
class Workspace:
    """A named execution root with explicit capability allowlists."""

    name: str
    path: str
    policy_dir: str | None
    unrestricted: bool
    providers: tuple[str, ...]
    default_provider: str | None
    skills: tuple[str, ...] | str  # "*" or explicit allowlist
    chat_commands: tuple[str, ...] | str
    concurrency: int

    def allows_provider(self, name: str) -> bool:
        return name in self.providers

    def allows_skill(self, name: str) -> bool:
        return self.skills == "*" or name in self.skills

    def allows_command(self, name: str) -> bool:
        return self.chat_commands == "*" or name in self.chat_commands


@dataclass(frozen=True)
class Route:
    """One exact Slack DM or channel route."""

    route_id: str  # "slack.dm.<name>" | "slack.channel.<ID>"
    kind: str  # "dm" | "channel"
    key: str  # DM route name or exact channel ID
    allow: tuple[str, ...]
    workspace: str
    audit: bool
    context_from: str


@dataclass(frozen=True)
class Decision:
    """Outcome of resolving one inbound Slack event against the config."""

    status: str  # "authorized" | "silent" | "error"
    reason: str
    route: Route | None = None
    groups: tuple[str, ...] = ()
    authorized_groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class TeamsConfig:
    """Parsed teams blocks plus every validation problem found."""

    account_id: str
    groups: dict[str, frozenset[str]]
    workspaces: dict[str, Workspace]
    dm_routes: dict[str, Route]
    channel_routes: dict[str, Route]
    audit_on_failure: str
    audit_max_age_days: int
    errors: tuple[str, ...] = ()
    workspace_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)
    route_errors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def dispatchable(self) -> bool:
        """Whether Slack teams dispatch is enabled at all."""
        return not self.errors

    def route_usable(self, route: Route) -> bool:
        """Whether an authorized sender on *route* may actually dispatch."""
        if route.route_id in self.route_errors:
            return False
        workspace = self.workspaces.get(route.workspace)
        return workspace is not None and route.workspace not in self.workspace_errors


def slack_mode(config: dict) -> str:
    """Classify Slack handling: 'teams' | 'legacy' | 'conflict' | 'blocked'."""
    routes = config.get("routes")
    has_teams = isinstance(routes, dict) and "slack" in routes
    slack_cfg = config.get("transports", {}).get("slack", {})
    has_legacy = isinstance(slack_cfg, dict) and "allowed_users" in slack_cfg
    if has_teams and has_legacy:
        return "conflict"
    if has_teams:
        return "teams"
    if has_legacy:
        return "legacy"
    return "blocked"


def load_teams(config: dict) -> TeamsConfig | None:
    """Parse the teams blocks. Returns None when teams mode is not configured.

    Never raises on bad config: every problem lands in ``errors``,
    ``workspace_errors``, or ``route_errors`` so callers fail closed with a
    diagnostic instead of falling back to permissive defaults.
    """
    routes_block = config.get("routes")
    if not (isinstance(routes_block, dict) and "slack" in routes_block):
        return None

    errors: list[str] = []
    if slack_mode(config) == "conflict":
        errors.append(
            "routes.slack and transports.slack.allowed_users are both set; "
            "remove one — teams mode and the legacy allowlist are mutually exclusive"
        )

    slack_routes = routes_block["slack"]
    if not isinstance(slack_routes, dict):
        errors.append("routes.slack must be an object")
        slack_routes = {}

    account_id = slack_routes.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        errors.append("routes.slack.account_id is required")
        account_id = ""

    groups = _load_groups(config.get("groups", {}), errors)
    workspaces, workspace_errors = _load_workspaces(
        config.get("workspaces", {}), config.get("working_dir", ""), errors
    )
    dm_routes = _load_routes(slack_routes.get("dms", {}), "dm", errors)
    channel_routes = _load_routes(slack_routes.get("channels", {}), "channel", errors)

    route_errors: dict[str, tuple[str, ...]] = {}
    for route in (*dm_routes.values(), *channel_routes.values()):
        problems = _route_problems(route, groups, workspaces)
        if problems:
            route_errors[route.route_id] = tuple(problems)

    _check_dm_ambiguity(dm_routes, groups, errors)

    audit_cfg = config.get("audit", {})
    audit_cfg = audit_cfg if isinstance(audit_cfg, dict) else {}
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
        groups=groups,
        workspaces=workspaces,
        dm_routes=dm_routes,
        channel_routes=channel_routes,
        audit_on_failure=on_failure,
        audit_max_age_days=max_age,
        errors=tuple(errors),
        workspace_errors=workspace_errors,
        route_errors=route_errors,
    )


def _load_groups(block: object, errors: list[str]) -> dict[str, frozenset[str]]:
    groups: dict[str, frozenset[str]] = {}
    if not isinstance(block, dict):
        errors.append("groups must be an object")
        return groups
    for name, platforms in block.items():
        if not isinstance(platforms, dict):
            errors.append(f"groups.{name} must map platforms to ID lists")
            continue
        unknown = set(platforms) - {"slack"}
        if unknown:
            errors.append(
                f"groups.{name} names unsupported platforms {sorted(unknown)}; "
                "only 'slack' is valid in v1"
            )
        ids = platforms.get("slack", [])
        if not _is_str_list(ids):
            errors.append(f"groups.{name}.slack must be a list of Slack user IDs")
            continue
        if len(ids) != len(set(ids)):
            errors.append(f"groups.{name}.slack contains duplicate user IDs")
        groups[name] = frozenset(ids)
    return groups


def _load_workspaces(
    block: object, working_dir: str, errors: list[str]
) -> tuple[dict[str, Workspace], dict[str, tuple[str, ...]]]:
    workspaces: dict[str, Workspace] = {}
    workspace_errors: dict[str, tuple[str, ...]] = {}
    if not isinstance(block, dict):
        errors.append("workspaces must be an object")
        return workspaces, workspace_errors

    for name, cfg in block.items():
        if not isinstance(cfg, dict):
            errors.append(f"workspaces.{name} must be an object")
            continue
        problems: list[str] = []
        workspace = _load_workspace(name, cfg, working_dir, problems)
        workspaces[name] = workspace
        if problems:
            workspace_errors[name] = tuple(problems)

    _check_workspace_layout(workspaces, errors)
    return workspaces, workspace_errors


def _load_workspace(
    name: str, cfg: dict, working_dir: str, problems: list[str]
) -> Workspace:
    path = cfg.get("path")
    if not isinstance(path, str) or not path:
        problems.append("path is required")
        path = ""

    unrestricted = cfg.get("unrestricted", False) is True

    explicit_policy_dir = cfg.get("policy_dir")
    if explicit_policy_dir is not None and not isinstance(explicit_policy_dir, str):
        problems.append("policy_dir must be a string path")
        explicit_policy_dir = None
    policy_dir = explicit_policy_dir or _default_policy_dir(name)

    if unrestricted:
        # Unrestricted and policy-controlled are mutually exclusive; Enso
        # never picks one by precedence. An explicit policy_dir or a policy
        # discovered at the canonical default location is a hard conflict.
        discovered = [
            rel
            for rel in POLICY_FILES.values()
            if os.path.isfile(os.path.join(os.path.expanduser(policy_dir), rel))
        ]
        if explicit_policy_dir is not None:
            problems.append("unrestricted: true is invalid alongside policy_dir")
        elif discovered:
            problems.append(
                f"unrestricted: true but native policy exists at {policy_dir} ({discovered[0]})"
            )
        policy_dir_out = None
    else:
        policy_dir_out = os.path.expanduser(policy_dir)
        if path and working_dir and _within(_real(path), _real(working_dir)):
            problems.append(
                "a policy-controlled workspace may not overlap the legacy working_dir"
            )

    providers_raw = cfg.get("providers", [])
    if not _is_str_list(providers_raw):
        problems.append("providers must be a list of provider names")
        providers_raw = []
    unknown_providers = [p for p in providers_raw if p not in PROVIDER_NAMES]
    if unknown_providers:
        problems.append(f"unknown providers {unknown_providers}")

    default_provider = cfg.get("default_provider")
    if providers_raw and default_provider not in providers_raw:
        problems.append("default_provider is required and must be one of providers")
        default_provider = None
    elif not providers_raw:
        default_provider = None

    concurrency = cfg.get("concurrency", 1)
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        problems.append("concurrency must be a positive integer")
        concurrency = 1

    return Workspace(
        name=name,
        path=os.path.expanduser(path),
        policy_dir=policy_dir_out,
        unrestricted=unrestricted,
        providers=tuple(providers_raw),
        default_provider=default_provider,
        skills=_load_capability(cfg.get("skills"), "skills", problems),
        chat_commands=_load_capability(cfg.get("chat_commands"), "chat_commands", problems),
        concurrency=concurrency,
    )


def _load_capability(
    value: object, key: str, problems: list[str]
) -> tuple[str, ...] | str:
    """An allowlist that is either the explicit string "*" or a string list."""
    if value is None:
        return ()
    if value == "*":
        return "*"
    if _is_str_list(value):
        return tuple(value)
    problems.append(f'{key} must be "*" or a list of names')
    return ()


def _check_workspace_layout(workspaces: dict[str, Workspace], errors: list[str]) -> None:
    """Cross-workspace path rules; violations disable dispatch entirely."""
    paths = {name: _real(ws.path) for name, ws in workspaces.items() if ws.path}
    names = sorted(paths)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if _within(paths[a], paths[b]) or _within(paths[b], paths[a]):
                errors.append(
                    f"workspaces {a} and {b} have overlapping or nested paths"
                )
    for name, ws in workspaces.items():
        if ws.policy_dir is None:
            continue
        policy_real = _real(ws.policy_dir)
        for other, path in paths.items():
            if _within(policy_real, path):
                errors.append(
                    f"policy_dir of workspace {name} is inside workspace {other}"
                )


def _load_routes(block: object, kind: str, errors: list[str]) -> dict[str, Route]:
    routes: dict[str, Route] = {}
    label = "dms" if kind == "dm" else "channels"
    if not isinstance(block, dict):
        errors.append(f"routes.slack.{label} must be an object")
        return routes
    for key, cfg in block.items():
        route_id = f"slack.{kind}.{key}"
        if not isinstance(cfg, dict):
            errors.append(f"{route_id} must be an object")
            continue
        routes[key] = Route(
            route_id=route_id,
            kind=kind,
            key=key,
            allow=tuple(cfg["allow"]) if _is_str_list(cfg.get("allow")) else (),
            workspace=cfg.get("workspace") if isinstance(cfg.get("workspace"), str) else "",
            audit=cfg.get("audit", False) is True,
            context_from=cfg.get("context_from", "allowed"),
        )
    return routes


def _route_problems(
    route: Route, groups: dict[str, frozenset[str]], workspaces: dict[str, Workspace]
) -> list[str]:
    problems = []
    if not route.allow:
        problems.append("allow is required and must name at least one group")
    unknown = [g for g in route.allow if g not in groups]
    if unknown:
        problems.append(f"unknown groups {unknown}")
    if not route.workspace:
        problems.append("workspace is required")
    elif route.workspace not in workspaces:
        problems.append(f"unknown workspace {route.workspace!r}")
    if route.context_from not in CONTEXT_FROM_VALUES:
        problems.append(f"context_from must be one of {CONTEXT_FROM_VALUES}")
    return problems


def _check_dm_ambiguity(
    dm_routes: dict[str, Route], groups: dict[str, frozenset[str]], errors: list[str]
) -> None:
    """Two DM routes one user could match is a config error, not a tiebreak."""

    def matchable_users(route: Route) -> frozenset[str]:
        return frozenset().union(*(groups.get(g, frozenset()) for g in route.allow)) \
            if route.allow else frozenset()

    names = sorted(dm_routes)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            overlap = matchable_users(dm_routes[a]) & matchable_users(dm_routes[b])
            if overlap:
                errors.append(
                    f"DM routes {a!r} and {b!r} are ambiguous: "
                    f"user(s) {sorted(overlap)} match both"
                )


# -- Resolution --


def memberships(teams: TeamsConfig, user_id: str) -> tuple[str, ...]:
    """Every configured group containing *user_id*, sorted by name."""
    return tuple(sorted(name for name, ids in teams.groups.items() if user_id in ids))


def resolve(
    teams: TeamsConfig, *, user_id: str, channel_id: str | None
) -> Decision:
    """Resolve one Slack sender/location pair to a dispatch decision.

    ``channel_id`` is None for a DM. Steps 3-5 of the resolution pipeline in
    teams.md: memberships, route lookup, authorization, then usability.
    """
    decision = _resolve_inner(teams, user_id=user_id, channel_id=channel_id)
    if teams.dispatchable:
        return decision
    # Globally invalid config: authorized senders get a diagnostic, everyone
    # else gets exactly the silence they would have received anyway.
    if decision.status == "silent":
        return decision
    return Decision(
        status="error",
        reason="teams_config_invalid",
        route=decision.route,
        groups=decision.groups,
        authorized_groups=decision.authorized_groups,
    )


def _resolve_inner(
    teams: TeamsConfig, *, user_id: str, channel_id: str | None
) -> Decision:
    groups = memberships(teams, user_id)
    if not groups:
        return Decision(status="silent", reason="unknown_user")

    if channel_id is not None:
        route = teams.channel_routes.get(channel_id)
        if route is None:
            return Decision(status="silent", reason="no_route", groups=groups)
        authorized = tuple(g for g in groups if g in route.allow)
        if not authorized:
            return Decision(status="silent", reason="not_allowed", groups=groups)
    else:
        matches = [
            r for r in teams.dm_routes.values() if any(g in r.allow for g in groups)
        ]
        if not matches:
            return Decision(status="silent", reason="no_route", groups=groups)
        if len(matches) > 1:
            # Statically prevented; kept as a defensive runtime check.
            return Decision(status="error", reason="ambiguous_dm", groups=groups)
        route = matches[0]
        authorized = tuple(g for g in groups if g in route.allow)

    if not teams.route_usable(route):
        return Decision(
            status="error",
            reason="route_unusable",
            route=route,
            groups=groups,
            authorized_groups=authorized,
        )
    return Decision(
        status="authorized",
        reason="ok",
        route=route,
        groups=groups,
        authorized_groups=authorized,
    )


def binding_revision(teams: TeamsConfig, route: Route) -> str:
    """Digest of every config input that authorizes and binds *route*.

    Covers the account, the route's own values, the definitions of the groups
    it allows, and the complete workspace binding — so any change to who may
    use the route or what it runs produces a new revision (and therefore a
    fresh execution key). Unrelated routes do not affect it.
    """
    workspace = teams.workspaces.get(route.workspace)
    payload = {
        "v": 1,
        "account_id": teams.account_id,
        "route": {
            "route_id": route.route_id,
            "allow": sorted(route.allow),
            "workspace": route.workspace,
            "audit": route.audit,
            "context_from": route.context_from,
        },
        "groups": {
            g: sorted(teams.groups.get(g, frozenset())) for g in sorted(route.allow)
        },
        "workspace_binding": None
        if workspace is None
        else {
            "path": workspace.path,
            "policy_dir": workspace.policy_dir,
            "unrestricted": workspace.unrestricted,
            "providers": list(workspace.providers),
            "default_provider": workspace.default_provider,
            "skills": workspace.skills if workspace.skills == "*" else list(workspace.skills),
            "chat_commands": workspace.chat_commands
            if workspace.chat_commands == "*"
            else list(workspace.chat_commands),
            "concurrency": workspace.concurrency,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
