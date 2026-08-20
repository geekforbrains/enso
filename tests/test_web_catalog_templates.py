"""Rendering contract for the configuration catalog pages."""

from __future__ import annotations

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jinja2")

from enso.web import app as web_app


def _render(template: str, **context: object) -> str:
    defaults: dict[str, object] = {
        "current_path": "/",
        "flash": None,
        "csrf_token": "test-csrf-token",
    }
    defaults.update(context)
    return web_app.templates.env.get_template(template).render(**defaults)


def test_primary_navigation_groups_configuration_operations_and_resources():
    html = _render(
        "agents.html",
        current_path="/agents",
        path="/tmp/enso/AGENTS.md",
        content="# Shared",
        revision="abc123",
        editable=True,
        problem=None,
    )

    assert html.count('<nav aria-label="Primary"') == 2
    assert html.count("Configuration") == 2
    assert html.count("Operations") == 2
    assert html.count("Resources") == 2
    for href, label in (
        ("/", "Overview"),
        ("/workspaces", "Workspaces"),
        ("/policies", "Policies"),
        ("/slack", "Slack routes"),
        ("/agents", "Shared instructions"),
        ("/jobs", "Jobs"),
        ("/runs", "Runs"),
        ("/tables", "Tables"),
        ("/skills", "Skills"),
        ("/docs", "Docs"),
    ):
        assert html.count(f'href="{href}"') >= 2
        assert html.count(label) >= 2
    assert 'href="/agents"' in html
    assert 'aria-current="page"' in html
    assert 'name="revision" value="abc123"' in html
    assert html.count("overflow-y-auto") >= 2


def test_shared_instruction_problem_is_read_only():
    html = _render(
        "agents.html",
        current_path="/agents",
        path="/tmp/enso/AGENTS.md",
        content="# Unsafe source",
        revision="",
        editable=False,
        problem="AGENTS.md must not be a symlink",
    )

    assert "AGENTS.md must not be a symlink" in html
    assert "Read-only" in html
    assert "<form" not in html


def test_workspaces_list_renders_bindings_and_visible_status_text():
    html = _render(
        "workspaces.html",
        current_path="/workspaces",
        catalog_errors=(),
        workspaces=[
            {
                "name": "company",
                "path": "/tmp/enso/workspaces/company",
                "policy_name": "admin",
                "concurrency": 2,
                "usable": True,
                "status": "ready",
                "problems": (),
                "slack_routes": ({}, {}, {}),
                "jobs": ({},),
                "telegram_bound": False,
                "agent_files": ("AGENTS.md", "services/api/AGENTS.md"),
                "agents_truncated": False,
                "root_editable": True,
            },
            {
                "name": "external",
                "path": "/tmp/project",
                "policy_name": "review",
                "concurrency": 1,
                "usable": False,
                "status": "warning",
                "problems": ("Workspace directory is missing",),
                "slack_routes": (),
                "jobs": (),
                "telegram_bound": False,
                "agent_files": (),
                "agents_truncated": True,
                "root_editable": False,
            },
        ],
    )

    assert "company" in html
    assert "/tmp/enso/workspaces/company" in html
    assert 'href="/policies/admin"' in html
    assert "2 concurrent turns per process" in html
    assert "3 Slack routes" in html
    assert "1 job" in html
    assert "2 AGENTS.md files" in html
    assert "partial scan" in html
    assert "External workspace" not in html
    assert "Workspace directory is missing" in html


def test_workspaces_list_explains_transport_and_orphan_job_errors():
    html = _render(
        "workspaces.html",
        current_path="/workspaces",
        catalog_errors=(),
        configuration={
            "telegram": {
                "problems": ("Telegram references unknown workspace 'missing'",),
            },
            "unassociated_job_errors": (("daily", ("references unknown workspace 'missing'",)),),
        },
        workspaces=(),
    )

    assert "Telegram binding errors" in html
    assert "Telegram references unknown workspace" in html
    assert "Job binding errors" in html
    assert "daily" in html
    assert "references unknown workspace" in html


def test_workspace_detail_edits_only_root_agents_file():
    html = _render(
        "workspace_detail.html",
        current_path="/workspaces/company",
        workspace={
            "name": "company",
            "path": "/tmp/enso/workspaces/company",
            "policy_name": "admin",
            "concurrency": 1,
            "usable": True,
            "status": "ready",
            "problems": (),
            "telegram_bound": False,
            "slack_routes": (
                {"label": "#general", "key": "C1", "kind": "channel", "status": "ready"},
            ),
            "jobs": (
                {
                    "name": "Daily report",
                    "dir_name": "daily-report",
                    "provider": "claude",
                    "model": "opus",
                    "enabled": True,
                    "problems": (),
                },
            ),
            "agent_files": ("AGENTS.md", "services/api/AGENTS.md"),
            "agents_truncated": False,
            "root_editable": True,
        },
        root_document={
            "rel_path": "AGENTS.md",
            "content": "# Company\n",
            "revision": "company-revision",
            "mode": 0o644,
        },
        root_revision="company-revision",
        root_problem=None,
        agent_listing={
            "files": ({"rel_path": "services/api/AGENTS.md"},),
            "truncated": False,
            "errors": (),
        },
        catalog_errors=(),
    )

    assert 'action="/workspaces/company/agents/edit"' in html
    assert 'name="_csrf" value="test-csrf-token"' in html
    assert "# Company" in html
    assert "services/api/AGENTS.md" in html
    assert 'href="/workspaces/company/agents/services/api/AGENTS.md"' in html
    assert "shown read-only" in html
    assert "#general" in html and "C1" in html
    assert 'href="/jobs/daily-report"' in html


def test_workspace_with_failed_integrity_check_is_unavailable():
    html = _render(
        "workspace_detail.html",
        current_path="/workspaces/external",
        workspace={
            "name": "external",
            "path": "/tmp/project",
            "policy_name": "review",
            "concurrency": 1,
            "usable": True,
            "status": "ready",
            "problems": (),
            "telegram_bound": False,
            "slack_routes": (),
            "jobs": (),
            "agent_files": ("AGENTS.md",),
            "agents_truncated": False,
            "root_editable": False,
        },
        root_document=None,
        root_revision="",
        root_problem="Workspace instruction root failed its integrity check",
        agent_listing={"files": (), "truncated": False, "errors": ()},
        catalog_errors=(),
    )

    assert "External workspace" not in html
    assert "Read-only" not in html
    assert "failed its integrity check" in html
    assert 'action="/workspaces/external/agents/edit"' not in html


def test_workspace_can_create_a_missing_root_agents_file():
    html = _render(
        "workspace_detail.html",
        current_path="/workspaces/default",
        workspace={
            "name": "default",
            "path": "/tmp/enso/workspaces/default",
            "policy_name": "admin",
            "concurrency": 1,
            "usable": True,
            "status": "ready",
            "problems": (),
            "telegram_bound": False,
            "slack_routes": (),
            "jobs": (),
            "agent_files": (),
            "agents_truncated": False,
            "root_editable": True,
        },
        root_document=None,
        root_revision="",
        root_problem=None,
        agent_listing={"files": (), "truncated": False, "errors": ()},
        catalog_errors=(),
    )

    assert 'action="/workspaces/default/agents/edit"' in html
    assert 'name="revision" value=""' in html
    assert "Save workspace AGENTS.md" in html
    assert "Create the workspace instruction file on disk" not in html


def test_workspace_discovery_errors_render_as_paths_and_reasons():
    html = _render(
        "workspace_detail.html",
        current_path="/workspaces/default",
        workspace={
            "name": "default",
            "path": "/tmp/enso/workspaces/default",
            "policy_name": "admin",
            "concurrency": 1,
            "usable": True,
            "status": "ready",
            "problems": (),
            "telegram_bound": False,
            "slack_routes": (),
            "jobs": (),
            "agent_files": (),
            "agents_truncated": True,
            "root_editable": True,
        },
        root_document=None,
        root_revision="",
        root_problem=None,
        agent_listing={
            "files": (),
            "truncated": True,
            "errors": (
                {
                    "rel_path": "services/api/AGENTS.md",
                    "reason": "instruction file must not be a symlink",
                },
            ),
        },
        catalog_errors=(),
    )

    assert "services/api/AGENTS.md" in html
    assert "instruction file must not be a symlink" in html
    assert "bounded scan reached its display limit" in html


def test_child_agents_page_is_read_only_and_links_back_to_workspace():
    html = _render(
        "workspace_agents.html",
        current_path="/workspaces/company/agents/services/api/AGENTS.md",
        workspace={"name": "company", "path": "/tmp/enso/workspaces/company"},
        agent_document={
            "rel_path": "services/api/AGENTS.md",
            "content": "# API rules\n",
            "revision": "api-revision",
            "mode": 0o644,
        },
    )

    assert 'href="/workspaces/company"' in html
    assert "services/api/AGENTS.md" in html
    assert "/tmp/enso/workspaces/company/services/api/AGENTS.md" in html
    assert "# API rules" in html
    assert "Read-only" in html
    assert "<form" not in html


def test_policy_detail_exposes_checks_without_policy_file_contents():
    html = _render(
        "policy_detail.html",
        current_path="/policies/review",
        policy={
            "name": "review",
            "mode": "native policy",
            "policy_dir": "/tmp/enso/policies/review",
            "providers": ("claude", "codex"),
            "default_provider": "claude",
            "chat_commands": ("help", "clear"),
            "env_passthrough": ("GITHUB_TOKEN",),
            "workspace_names": ("company",),
            "usable": False,
            "status": "warning",
            "problems": (),
            "checks": (
                {
                    "workspace_name": "company",
                    "provider": "claude",
                    "ok": True,
                    "policy_path": "/tmp/enso/policies/review/claude/settings.json",
                    "policy_revision": "0123456789abcdef",
                    "problems": (),
                    "warnings": ("Sandbox is disabled",),
                    "mcp_servers": ("github", "metrics"),
                },
                {
                    "workspace_name": "company",
                    "provider": "codex",
                    "ok": False,
                    "policy_path": "/tmp/enso/policies/review/codex/config.toml",
                    "policy_revision": None,
                    "problems": ("config.toml does not parse",),
                    "warnings": (),
                    "mcp_servers": (),
                },
            ),
        },
        catalog_errors=(),
    )

    assert "native policy" in html
    assert "claude" in html and "codex" in html
    assert "Default" in html
    assert "help" in html and "clear" in html
    assert "GITHUB_TOKEN" in html
    assert "0123456789abcdef" in html
    assert "Sandbox is disabled" in html
    assert "github" in html and "metrics" in html
    assert "config.toml does not parse" in html
    assert 'href="/workspaces/company"' in html
    assert "<textarea" not in html


def test_policies_list_shows_reuse_and_unused_state():
    html = _render(
        "policies.html",
        current_path="/policies",
        catalog_errors=(),
        policies=(
            {
                "name": "admin",
                "mode": "unrestricted",
                "policy_dir": None,
                "providers": ("claude", "codex"),
                "default_provider": "claude",
                "chat_commands": "*",
                "env_passthrough": (),
                "workspace_names": ("company", "testing"),
                "usable": True,
                "status": "ready",
                "problems": (),
                "checks": (),
            },
            {
                "name": "unused",
                "mode": "native policy",
                "policy_dir": "/tmp/policies/unused",
                "providers": ("claude",),
                "default_provider": "claude",
                "chat_commands": (),
                "env_passthrough": (),
                "workspace_names": (),
                "usable": True,
                "status": "unused",
                "problems": (),
                "checks": (),
            },
        ),
    )

    assert 'href="/policies/admin"' in html
    assert "unrestricted" in html
    assert "2 workspace consumers" in html
    assert "unused" in html
    assert "0 workspace consumers" in html


def test_overview_configuration_strip_links_to_each_catalog():
    html = _render(
        "index.html",
        configuration_summary={
            "workspaces_total": 3,
            "policies_total": 2,
            "slack_routes_total": 4,
            "problems_total": 1,
            "status": "error",
        },
        jobs_enabled=0,
        jobs_total=0,
        skills_total=0,
        skills_enso=0,
        skills_system=0,
        docs_total=0,
        tables_total=0,
        tables_available=True,
        tables_error=None,
        latest_runs=(),
        runs_available=True,
        runs_error=None,
    )

    assert "Overview" in html
    assert "1 issue" in html and "needs attention" in html
    assert 'href="/workspaces"' in html and ">3<" in html
    assert 'href="/policies"' in html and ">2<" in html
    assert 'href="/slack"' in html and ">4<" in html


def test_slack_routes_use_cached_names_and_show_exact_ids_and_bindings():
    html = _render(
        "slack_routes.html",
        current_path="/slack",
        slack={
            "configured": True,
            "account_id": "T1",
            "dispatchable": True,
            "status": "ready",
            "problems": (),
            "warnings": (),
            "channel_cache_fetched_at": "2026-08-14T12:00:00+00:00",
            "user_cache_fetched_at": "2026-08-14T12:00:00+00:00",
            "cache_team_id": "T1",
        },
        catalog_errors=(),
        routes=[
            {
                "route_id": "slack.channel.C1",
                "kind": "channel",
                "key": "C1",
                "label": "#general",
                "workspace_name": "company",
                "policy_name": "admin",
                "audit": True,
                "mention_required": True,
                "thread_mention_required": False,
                "status": "ready",
                "problems": (),
            },
            {
                "route_id": "slack.dm.U1",
                "kind": "dm",
                "key": "U1",
                "label": "Gavin",
                "workspace_name": "testing",
                "policy_name": "review",
                "audit": False,
                "mention_required": True,
                "thread_mention_required": True,
                "status": "error",
                "problems": ("Workspace is unavailable",),
            },
            {
                "route_id": "slack.channel.C2",
                "kind": "channel",
                "key": "C2",
                "label": "#testing",
                "workspace_name": "testing",
                "policy_name": "review",
                "audit": False,
                "mention_required": False,
                "thread_mention_required": True,
                "status": "ready",
                "problems": (),
            },
        ],
    )

    assert "#general" in html and "C1" in html
    assert "Gavin" in html and "U1" in html
    assert 'href="/workspaces/company"' in html
    assert 'href="/policies/admin"' in html
    assert "Top-level mention required" in html
    assert "Top-level messages do not require a mention" in html
    assert "Thread replies do not require a mention" in html
    assert "Thread mention required" in html
    assert "Audit on" in html and "Audit off" in html
    assert "Workspace is unavailable" in html
    assert "<textarea" not in html
