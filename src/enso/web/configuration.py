"""Pure view models for the active Enso execution configuration.

The web UI renders the configuration already held by the running service. It
never reloads or mutates ``config.json`` and never carries transport secrets or
raw native-policy content into template context.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from ..jobs import Job
from ..policy import PolicyCheck
from ..teams import ExecutionCatalog, Route, load_catalog, load_teams, load_telegram

ConfigurationStatus = Literal["configured", "ready", "warning", "error", "unused"]


def _unique(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(problem for group in groups for problem in group))


@dataclass(frozen=True, slots=True)
class JobView:
    dir_name: str
    name: str
    schedule: str
    provider: str
    model: str
    workspace_name: str
    enabled: bool
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SlackRouteView:
    route_id: str
    kind: str
    key: str
    label: str
    workspace_name: str
    policy_name: str | None
    audit: bool
    mention_required: bool
    thread_mention_required: bool
    usable: bool
    status: ConfigurationStatus
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SlackView:
    configured: bool
    account_id: str
    dispatchable: bool
    status: ConfigurationStatus
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    routes: tuple[SlackRouteView, ...]
    channel_cache_fetched_at: float | None = None
    user_cache_fetched_at: float | None = None
    cache_team_id: str = ""


@dataclass(frozen=True, slots=True)
class TelegramView:
    configured: bool
    workspace_name: str | None
    policy_name: str | None
    allowed_user_count: int
    usable: bool
    status: ConfigurationStatus
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyCheckView:
    workspace_name: str
    provider: str
    ok: bool
    problems: tuple[str, ...]
    warnings: tuple[str, ...]
    policy_path: str | None
    policy_revision: str | None
    mcp_servers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PolicyView:
    name: str
    mode: str
    policy_dir: str | None
    providers: tuple[str, ...]
    default_provider: str | None
    chat_commands: tuple[str, ...] | str
    env_passthrough: tuple[str, ...]
    workspace_names: tuple[str, ...]
    usable: bool
    status: ConfigurationStatus
    problems: tuple[str, ...] = ()
    checks: tuple[PolicyCheckView, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkspaceView:
    name: str
    path: str
    policy_name: str
    concurrency: int
    usable: bool
    status: ConfigurationStatus
    problems: tuple[str, ...]
    slack_routes: tuple[SlackRouteView, ...] = ()
    jobs: tuple[JobView, ...] = ()
    telegram_bound: bool = False
    agent_files: tuple[str, ...] = ()
    agents_truncated: bool = False
    managed: bool = False
    root_editable: bool = False


@dataclass(frozen=True, slots=True)
class ConfigurationSummary:
    workspaces_total: int
    policies_total: int
    slack_routes_total: int
    problems_total: int
    status: ConfigurationStatus


@dataclass(frozen=True, slots=True)
class ConfigurationView:
    catalog_errors: tuple[str, ...]
    workspaces: tuple[WorkspaceView, ...]
    policies: tuple[PolicyView, ...]
    slack: SlackView
    telegram: TelegramView
    unassociated_job_errors: tuple[tuple[str, tuple[str, ...]], ...]
    summary: ConfigurationSummary = field(init=False)

    def __post_init__(self) -> None:
        problems = set(self.catalog_errors)
        for workspace in self.workspaces:
            problems.update(workspace.problems)
            for job in workspace.jobs:
                problems.update(job.problems)
        for policy in self.policies:
            problems.update(policy.problems)
        problems.update(self.slack.problems)
        for route in self.slack.routes:
            problems.update(route.problems)
        problems.update(self.telegram.problems)
        for _, job_problems in self.unassociated_job_errors:
            problems.update(job_problems)
        problem_count = len(problems)
        warnings = bool(self.slack.warnings)
        status: ConfigurationStatus = (
            "error" if problem_count else "warning" if warnings else "configured"
        )
        object.__setattr__(
            self,
            "summary",
            ConfigurationSummary(
                workspaces_total=len(self.workspaces),
                policies_total=len(self.policies),
                slack_routes_total=len(self.slack.routes),
                problems_total=problem_count,
                status=status,
            ),
        )

    def workspace(self, name: str) -> WorkspaceView | None:
        return next((workspace for workspace in self.workspaces if workspace.name == name), None)

    def policy(self, name: str) -> PolicyView | None:
        return next((policy for policy in self.policies if policy.name == name), None)


def _transport_configured(config: dict, name: str) -> bool:
    transports = config.get("transports")
    return config.get("transport") == name or (isinstance(transports, dict) and name in transports)


def _cache_section(directory: dict[str, Any], name: str) -> tuple[dict[str, dict], float | None]:
    section = directory.get(name)
    if not isinstance(section, dict):
        return {}, None
    raw_items = section.get("items")
    items = (
        {
            key: value
            for key, value in raw_items.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
        if isinstance(raw_items, dict)
        else {}
    )
    raw_fetched = section.get("fetched_at")
    fetched_at = (
        float(raw_fetched)
        if isinstance(raw_fetched, (int, float)) and not isinstance(raw_fetched, bool)
        else None
    )
    return items, fetched_at


def _cached_label(route: Route, users: dict[str, dict], channels: dict[str, dict]) -> str:
    if route.kind == "channel":
        raw_name = channels.get(route.key, {}).get("name")
        return f"#{raw_name}" if isinstance(raw_name, str) and raw_name else route.key
    user = users.get(route.key, {})
    for field_name in ("display_name", "real_name", "name"):
        value = user.get(field_name)
        if isinstance(value, str) and value:
            return value
    return route.key


def _route_problems(
    route: Route,
    catalog: ExecutionCatalog,
    own: tuple[str, ...],
) -> tuple[str, ...]:
    problems = list(own)
    workspace = catalog.workspaces.get(route.workspace)
    if workspace is None:
        if route.workspace:
            problems.append(f"unknown workspace {route.workspace!r}")
        return _unique(problems)
    problems.extend(catalog.workspace_errors.get(workspace.name, ()))
    if workspace.policy in catalog.policy_errors:
        problems.extend(
            f"policy {workspace.policy}: {problem}"
            for problem in catalog.policy_errors[workspace.policy]
        )
    return _unique(problems)


def _build_slack_view(
    config: dict,
    catalog: ExecutionCatalog,
    directory: dict[str, Any],
) -> SlackView:
    if not _transport_configured(config, "slack"):
        return SlackView(False, "", False, "unused", (), (), ())

    teams = load_teams(config)
    users, users_fetched = _cache_section(directory, "users")
    channels, channels_fetched = _cache_section(directory, "channels")
    raw_cache_team = directory.get("team_id")
    cache_team_id = raw_cache_team if isinstance(raw_cache_team, str) else ""
    warnings: tuple[str, ...] = ()
    if teams.account_id and cache_team_id != teams.account_id:
        warnings = (
            ("Cached Slack directory belongs to a different account",)
            if cache_team_id
            else ("Slack directory cache is not bound to the configured account",)
        )
        users = {}
        channels = {}

    rendered_routes: list[SlackRouteView] = []
    routes = (*teams.channel_routes.values(), *teams.dm_routes.values())
    for route in routes:
        workspace = catalog.workspaces.get(route.workspace)
        policy_name = workspace.policy if workspace is not None else None
        problems = _route_problems(
            route,
            catalog,
            teams.route_errors.get(route.route_id, ()),
        )
        usable = teams.dispatchable and teams.route_usable(route)
        if not teams.dispatchable:
            problems = _unique(
                list(problems),
                ["Slack dispatch is disabled by transport configuration errors"],
            )
        rendered_routes.append(
            SlackRouteView(
                route_id=route.route_id,
                kind=route.kind,
                key=route.key,
                label=_cached_label(route, users, channels),
                workspace_name=route.workspace,
                policy_name=policy_name,
                audit=route.audit,
                mention_required=route.mention_required,
                thread_mention_required=route.thread_mention_required,
                usable=usable,
                status="configured" if usable else "error",
                problems=problems,
            )
        )
    rendered_routes.sort(key=lambda route: (route.kind, route.label.casefold(), route.key))
    status: ConfigurationStatus = (
        "error"
        if teams.errors or any(not route.usable for route in rendered_routes)
        else "warning"
        if warnings
        else "configured"
    )
    return SlackView(
        configured=True,
        account_id=teams.account_id,
        dispatchable=teams.dispatchable,
        status=status,
        problems=_unique(list(teams.errors)),
        warnings=warnings,
        routes=tuple(rendered_routes),
        channel_cache_fetched_at=channels_fetched,
        user_cache_fetched_at=users_fetched,
        cache_team_id=cache_team_id,
    )


def _configured_workspace_name(config: dict, transport_name: str) -> str | None:
    transports = config.get("transports")
    transport = transports.get(transport_name) if isinstance(transports, dict) else None
    value = transport.get("workspace") if isinstance(transport, dict) else None
    return value if isinstance(value, str) and value else None


def _build_telegram_view(config: dict, catalog: ExecutionCatalog) -> TelegramView:
    if not _transport_configured(config, "telegram"):
        return TelegramView(False, None, None, 0, False, "unused")
    telegram = load_telegram(config)
    workspace_name = _configured_workspace_name(config, "telegram")
    workspace = catalog.workspaces.get(workspace_name or "")
    policy_name = workspace.policy if workspace is not None else None
    return TelegramView(
        configured=True,
        workspace_name=workspace_name,
        policy_name=policy_name,
        allowed_user_count=len(telegram.allowed_users),
        usable=telegram.usable,
        status="configured" if telegram.usable else "error",
        problems=_unique(list(telegram.errors)),
    )


def _build_job_views(
    jobs: tuple[Job, ...] | list[Job],
    job_errors: dict[str, tuple[str, ...]],
    workspace_names: set[str],
) -> tuple[dict[str, list[JobView]], tuple[tuple[str, tuple[str, ...]], ...]]:
    by_workspace: dict[str, list[JobView]] = {}
    unassociated: list[tuple[str, tuple[str, ...]]] = []
    parsed_names: set[str] = set()
    for job in jobs:
        parsed_names.add(job.dir_name)
        view = JobView(
            dir_name=job.dir_name,
            name=job.name,
            schedule=job.schedule,
            provider=job.provider,
            model=job.model,
            workspace_name=job.workspace,
            enabled=job.enabled,
            problems=tuple(job_errors.get(job.dir_name, ())),
        )
        if job.workspace in workspace_names:
            by_workspace.setdefault(job.workspace, []).append(view)
        else:
            unassociated.append(
                (
                    job.dir_name,
                    _unique(
                        list(view.problems),
                        [f"references unknown workspace {job.workspace!r}"],
                    ),
                )
            )
    for workspace_jobs in by_workspace.values():
        workspace_jobs.sort(key=lambda job: (job.name.casefold(), job.dir_name))
    unassociated.extend(
        (name, tuple(problems)) for name, problems in job_errors.items() if name not in parsed_names
    )
    return by_workspace, tuple(sorted(unassociated))


def _workspace_problems(catalog: ExecutionCatalog, name: str, policy_name: str) -> tuple[str, ...]:
    problems = list(catalog.workspace_errors.get(name, ()))
    problems.extend(
        f"policy {policy_name}: {problem}" for problem in catalog.policy_errors.get(policy_name, ())
    )
    return _unique(problems)


def _build_workspaces(
    catalog: ExecutionCatalog,
    slack: SlackView,
    telegram: TelegramView,
    jobs_by_workspace: dict[str, list[JobView]],
) -> tuple[WorkspaceView, ...]:
    routes_by_workspace: dict[str, list[SlackRouteView]] = {}
    for route in slack.routes:
        routes_by_workspace.setdefault(route.workspace_name, []).append(route)
    result = []
    for name, workspace in sorted(catalog.workspaces.items()):
        problems = _workspace_problems(catalog, name, workspace.policy)
        usable = catalog.usable(name)
        result.append(
            WorkspaceView(
                name=name,
                path=workspace.path,
                policy_name=workspace.policy,
                concurrency=workspace.concurrency,
                usable=usable,
                status="configured" if usable else "error",
                problems=problems,
                slack_routes=tuple(routes_by_workspace.get(name, ())),
                jobs=tuple(jobs_by_workspace.get(name, ())),
                telegram_bound=telegram.configured and telegram.workspace_name == name,
            )
        )
    return tuple(result)


def _build_policies(catalog: ExecutionCatalog) -> tuple[PolicyView, ...]:
    consumers: dict[str, list[str]] = {}
    for workspace in catalog.workspaces.values():
        consumers.setdefault(workspace.policy, []).append(workspace.name)
    result = []
    for name, execution_policy in sorted(catalog.policies.items()):
        workspace_names = tuple(sorted(consumers.get(name, ())))
        problems = tuple(catalog.policy_errors.get(name, ()))
        usable = catalog.valid and not problems
        status: ConfigurationStatus = (
            "error" if not usable else "unused" if not workspace_names else "configured"
        )
        result.append(
            PolicyView(
                name=name,
                mode="unrestricted" if execution_policy.unrestricted else "policy-controlled",
                policy_dir=execution_policy.policy_dir,
                providers=execution_policy.providers,
                default_provider=execution_policy.default_provider,
                chat_commands=execution_policy.chat_commands,
                env_passthrough=execution_policy.env_passthrough,
                workspace_names=workspace_names,
                usable=usable,
                status=status,
                problems=problems,
            )
        )
    return tuple(result)


def build_policy_check_view(workspace_name: str, check: PolicyCheck) -> PolicyCheckView:
    """Convert a native-policy check without exposing policy source content."""
    return PolicyCheckView(
        workspace_name=workspace_name,
        provider=check.provider,
        ok=check.ok,
        problems=check.problems,
        warnings=check.warnings,
        policy_path=check.policy_path,
        policy_revision=check.policy_revision,
        mcp_servers=check.mcp_servers,
    )


def with_policy_checks(
    policy: PolicyView,
    checks: tuple[PolicyCheckView, ...] | list[PolicyCheckView],
) -> PolicyView:
    """Return one policy view enriched with detail-only native checks."""
    ordered = tuple(sorted(checks, key=lambda check: (check.workspace_name, check.provider)))
    status = policy.status
    expected_checks = len(policy.workspace_names) * len(policy.providers)
    if len(ordered) != expected_checks or any(not check.ok for check in ordered):
        status = "error"
    elif any(check.warnings for check in ordered) and status != "error":
        status = "warning"
    elif ordered and status == "configured":
        status = "ready"
    return replace(policy, checks=ordered, status=status)


def with_workspace_agents(
    workspace: WorkspaceView,
    *,
    agent_files: tuple[str, ...] | list[str],
    truncated: bool,
    managed: bool,
    root_editable: bool,
    problem: str | None = None,
) -> WorkspaceView:
    """Return a workspace enriched by a bounded instruction-file scan."""
    problems = workspace.problems
    status = workspace.status
    if problem:
        problems = _unique(list(problems), [problem])
        status = "error"
    elif truncated and status in {"configured", "ready"}:
        status = "warning"
    return replace(
        workspace,
        agent_files=tuple(sorted(agent_files)),
        agents_truncated=truncated,
        managed=managed,
        root_editable=root_editable,
        problems=problems,
        status=status,
    )


def build_configuration_view(
    config: dict,
    *,
    jobs: tuple[Job, ...] | list[Job],
    job_errors: dict[str, tuple[str, ...]],
    slack_directory: dict[str, Any],
) -> ConfigurationView:
    """Build a deterministic, secret-free view of the active configuration."""
    catalog = load_catalog(config)
    slack = _build_slack_view(config, catalog, slack_directory)
    telegram = _build_telegram_view(config, catalog)
    jobs_by_workspace, unassociated_job_errors = _build_job_views(
        jobs,
        job_errors,
        set(catalog.workspaces),
    )
    workspaces = _build_workspaces(catalog, slack, telegram, jobs_by_workspace)
    policies = _build_policies(catalog)
    return ConfigurationView(
        catalog_errors=catalog.errors,
        workspaces=workspaces,
        policies=policies,
        slack=slack,
        telegram=telegram,
        unassociated_job_errors=unassociated_job_errors,
    )
