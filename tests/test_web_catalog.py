"""Read-only web views for the active workspace and policy configuration."""

from __future__ import annotations

import re
from dataclasses import asdict
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jinja2")

from enso.jobs import Job
from enso.policy import PolicyCheck
from enso.web import app as web_app
from enso.web.configuration import (
    build_configuration_view,
    build_policy_check_view,
    with_policy_checks,
    with_workspace_agents,
)
from enso.web.workspace_instructions import read_agent


def _config(tmp_path) -> dict:
    return {
        "transport": "slack",
        "transports": {
            "slack": {
                "account_id": "T123",
                "bot_token": "xoxb-never-render-this",
                "app_token": "xapp-never-render-this",
                "channels": {
                    "C2": {
                        "workspace": "zeta",
                        "audit": True,
                        "mention_required": False,
                    }
                },
                "dms": {"U1": {"workspace": "alpha", "audit": False}},
            },
            "telegram": {
                "bot_token": "telegram-never-render-this",
                "allowed_users": ["12345"],
                "workspace": "alpha",
            },
        },
        "providers": {
            "claude": {"path": "claude", "models": ["opus"]},
            "codex": {"path": "codex", "models": ["gpt-test"]},
        },
        "workspaces": {
            "zeta": {
                "path": str(tmp_path / "zeta"),
                "policy": "shared",
                "concurrency": 2,
            },
            "alpha": {
                "path": str(tmp_path / "alpha"),
                "policy": "shared",
                "concurrency": 1,
            },
        },
        "policies": {
            "shared": {
                "unrestricted": True,
                "providers": ["claude", "codex"],
                "default_provider": "claude",
                "chat_commands": "*",
            }
        },
    }


def _job(*, workspace: str, name: str, errors: tuple[str, ...] = ()) -> tuple[Job, tuple[str, ...]]:
    job = Job(
        dir_name=name,
        name=name.title(),
        schedule="0 9 * * *",
        provider="claude",
        model="opus",
        workspace=workspace,
    )
    return job, errors


def test_configuration_view_is_sorted_and_associates_every_consumer(tmp_path):
    alpha_job, alpha_errors = _job(workspace="alpha", name="daily")
    zeta_job, zeta_errors = _job(
        workspace="zeta",
        name="weekly",
        errors=("Invalid schedule",),
    )
    directory = {
        "team_id": "T123",
        "users": {
            "fetched_at": 10.0,
            "items": {"U1": {"id": "U1", "display_name": "Ada"}},
        },
        "channels": {
            "fetched_at": 20.0,
            "items": {"C2": {"id": "C2", "name": "testing"}},
        },
    }

    view = build_configuration_view(
        _config(tmp_path),
        jobs=(zeta_job, alpha_job),
        job_errors={
            alpha_job.dir_name: alpha_errors,
            zeta_job.dir_name: zeta_errors,
        },
        slack_directory=directory,
    )

    assert [workspace.name for workspace in view.workspaces] == ["alpha", "zeta"]
    alpha, zeta = view.workspaces
    assert alpha.policy_name == zeta.policy_name == "shared"
    assert alpha.telegram_bound is True
    assert zeta.telegram_bound is False
    assert [route.label for route in alpha.slack_routes] == ["Ada"]
    assert [route.label for route in zeta.slack_routes] == ["#testing"]
    assert [job.dir_name for job in alpha.jobs] == ["daily"]
    assert zeta.jobs[0].problems == ("Invalid schedule",)

    assert [policy.name for policy in view.policies] == ["shared"]
    assert view.policies[0].workspace_names == ("alpha", "zeta")
    assert view.telegram.workspace_name == "alpha"
    assert view.telegram.policy_name == "shared"
    assert view.slack.account_id == "T123"
    assert [route.kind for route in view.slack.routes] == ["channel", "dm"]
    assert view.slack.channel_cache_fetched_at == 20.0
    assert view.slack.user_cache_fetched_at == 10.0
    assert view.slack.status == "configured"
    assert all(route.status == "configured" for route in view.slack.routes)
    assert view.policies[0].status == "configured"
    assert all(workspace.status == "configured" for workspace in view.workspaces)

    assert view.summary.workspaces_total == 2
    assert view.summary.policies_total == 1
    assert view.summary.slack_routes_total == 2
    assert view.summary.problems_total == 1
    assert view.summary.status == "error"


def test_configuration_view_explains_invalid_policy_and_route_without_secrets(tmp_path):
    config = _config(tmp_path)
    config["workspaces"]["alpha"]["policy"] = "broken"
    config["transports"]["slack"]["channels"]["C9"] = {
        "workspace": "missing",
        "audit": False,
    }
    config["policies"]["shared"]["default_provider"] = "not-allowed"

    view = build_configuration_view(config, jobs=(), job_errors={}, slack_directory={})

    alpha = view.workspace("alpha")
    shared = view.policy("shared")
    missing_route = next(route for route in view.slack.routes if route.key == "C9")
    assert alpha is not None and alpha.status == "error"
    assert any("unknown policy" in problem for problem in alpha.problems)
    assert shared is not None and shared.status == "error"
    assert any("default_provider" in problem for problem in shared.problems)
    assert missing_route.status == "error"
    assert any("unknown workspace" in problem for problem in missing_route.problems)
    assert missing_route.label == "C9"

    rendered_data = repr(asdict(view))
    assert "xoxb-never-render-this" not in rendered_data
    assert "xapp-never-render-this" not in rendered_data
    assert "telegram-never-render-this" not in rendered_data


def test_global_slack_errors_disable_every_rendered_route(tmp_path):
    config = _config(tmp_path)
    del config["transports"]["slack"]["account_id"]

    view = build_configuration_view(config, jobs=(), job_errors={}, slack_directory={})

    assert view.slack.dispatchable is False
    assert view.slack.status == "error"
    assert view.slack.routes
    assert all(route.status == "error" for route in view.slack.routes)
    assert all(
        any("Slack dispatch is disabled" in problem for problem in route.problems)
        for route in view.slack.routes
    )


def test_slack_cache_from_another_account_never_supplies_route_labels(tmp_path):
    directory = {
        "team_id": "T-DIFFERENT",
        "users": {"items": {"U1": {"display_name": "Wrong person"}}},
        "channels": {"items": {"C2": {"name": "wrong-channel"}}},
    }

    view = build_configuration_view(
        _config(tmp_path), jobs=(), job_errors={}, slack_directory=directory
    )

    assert [route.label for route in view.slack.routes] == ["C2", "U1"]
    assert view.slack.warnings == ("Cached Slack directory belongs to a different account",)


def test_unbound_slack_cache_never_supplies_route_labels(tmp_path):
    directory = {
        "team_id": "",
        "users": {"items": {"U1": {"display_name": "Unbound person"}}},
        "channels": {"items": {"C2": {"name": "unbound-channel"}}},
    }

    view = build_configuration_view(
        _config(tmp_path), jobs=(), job_errors={}, slack_directory=directory
    )

    assert [route.label for route in view.slack.routes] == ["C2", "U1"]
    assert view.slack.warnings == (
        "Slack directory cache is not bound to the configured account",
    )


def test_configuration_view_uses_attempted_telegram_binding_for_diagnostics(tmp_path):
    config = _config(tmp_path)
    config["workspaces"]["alpha"]["policy"] = "missing"

    view = build_configuration_view(config, jobs=(), job_errors={}, slack_directory={})

    alpha = view.workspace("alpha")
    assert alpha is not None and alpha.telegram_bound is True
    assert view.telegram.configured is True
    assert view.telegram.usable is False
    assert view.telegram.workspace_name == "alpha"
    assert any("usable policy" in problem for problem in view.telegram.problems)


def test_configuration_view_preserves_jobs_with_unknown_workspaces(tmp_path):
    orphan, _ = _job(workspace="missing", name="orphan")

    view = build_configuration_view(
        _config(tmp_path),
        jobs=(orphan,),
        job_errors={},
        slack_directory={},
    )

    assert view.unassociated_job_errors == (
        ("orphan", ("references unknown workspace 'missing'",)),
    )
    assert view.summary.status == "error"


def test_configuration_view_does_not_invent_unconfigured_transports(tmp_path):
    config = _config(tmp_path)
    config["transport"] = ""
    config["transports"] = {}

    view = build_configuration_view(config, jobs=(), job_errors={}, slack_directory={})

    assert view.slack.configured is False
    assert view.slack.routes == ()
    assert view.slack.problems == ()
    assert view.telegram.configured is False
    assert view.telegram.workspace_name is None


def _client(tmp_path, monkeypatch, config: dict, *, jobs: tuple[Job, ...] = ()):
    from starlette.testclient import TestClient

    runtime = SimpleNamespace(config=config)
    monkeypatch.setattr(web_app, "load_jobs_with_errors", lambda _config: (list(jobs), {}))
    monkeypatch.setattr(
        web_app.slack_cache,
        "load",
        lambda: {
            "team_id": "T123",
            "users": {
                "fetched_at": 10.0,
                "items": {"U1": {"display_name": "Ada"}},
            },
            "channels": {
                "fetched_at": 20.0,
                "items": {"C2": {"name": "testing"}},
            },
        },
    )
    client = TestClient(web_app.create_app(runtime), base_url="http://127.0.0.1")
    return runtime, client


def test_workspace_policy_and_slack_lists_render_active_configuration(tmp_path, monkeypatch):
    runtime, client = _client(tmp_path, monkeypatch, _config(tmp_path))

    workspaces = client.get("/workspaces")
    policies = client.get("/policies")
    slack = client.get("/slack")

    assert workspaces.status_code == policies.status_code == slack.status_code == 200
    assert workspaces.text.index("alpha") < workspaces.text.index("zeta")
    assert 'href="/policies/shared"' in workspaces.text
    assert "2 workspace consumers" in policies.text
    assert "#testing" in slack.text and "C2" in slack.text
    assert "Ada" in slack.text and "U1" in slack.text
    for secret in (
        "xoxb-never-render-this",
        "xapp-never-render-this",
        "telegram-never-render-this",
    ):
        assert secret not in workspaces.text + policies.text + slack.text

    changed = _config(tmp_path)
    changed["workspaces"] = {
        "replacement": {
            "path": str(tmp_path / "replacement"),
            "policy": "shared",
            "concurrency": 1,
        }
    }
    runtime.config = changed
    refreshed = client.get("/workspaces")
    assert "replacement" in refreshed.text
    assert 'href="/workspaces/alpha"' not in refreshed.text


def test_policy_checks_run_only_on_detail_and_unknown_names_404(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch, _config(tmp_path))
    calls: list[tuple[str, str]] = []

    def check(workspace, _policy, provider):
        calls.append((workspace.name, provider))
        return PolicyCheck(
            provider=provider,
            ok=True,
            policy_revision=f"revision-{workspace.name}-{provider}",
            mcp_servers=("github",) if provider == "claude" else (),
        )

    monkeypatch.setattr(web_app, "check_provider", check)

    assert client.get("/policies").status_code == 200
    assert calls == []
    detail = client.get("/policies/shared")

    assert detail.status_code == 200
    assert calls == [
        ("alpha", "claude"),
        ("alpha", "codex"),
        ("zeta", "claude"),
        ("zeta", "codex"),
    ]
    assert "revision-alpha-claude" in detail.text
    assert "github" in detail.text
    assert client.get("/policies/missing").status_code == 404


def test_policy_detail_skips_checks_when_catalog_topology_is_invalid(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config["workspaces"]["zeta"]["path"] = config["workspaces"]["alpha"]["path"]
    _, client = _client(tmp_path, monkeypatch, config)
    calls: list[tuple[str, str]] = []

    def check(workspace, _policy, provider):
        calls.append((workspace.name, provider))
        raise AssertionError("globally unusable bindings must not be checked")

    monkeypatch.setattr(web_app, "check_provider", check)

    response = client.get("/policies/shared")

    assert response.status_code == 200
    assert calls == []
    assert "overlapping or nested paths" in response.text
    assert "Cannot launch" in response.text
    assert "overlapping or nested paths" in response.text
    assert "unused policy" not in response.text


def test_policy_detail_marks_skipped_consumers_as_failed_checks(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config["workspaces"]["zeta"]["concurrency"] = 0
    _, client = _client(tmp_path, monkeypatch, config)
    calls: list[tuple[str, str]] = []

    def check(workspace, _policy, provider):
        calls.append((workspace.name, provider))
        return PolicyCheck(provider=provider, ok=True, policy_revision="healthy")

    monkeypatch.setattr(web_app, "check_provider", check)

    response = client.get("/policies/shared")

    assert response.status_code == 200
    assert calls == [("alpha", "claude"), ("alpha", "codex")]
    assert response.text.count("Cannot launch") == 2
    assert "concurrency must be a positive integer" in response.text
    assert re.search(r">\s*error\s*</span>", response.text)


def test_slack_page_never_uses_network_lookup(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch, _config(tmp_path))

    def fail(*_args, **_kwargs):
        raise AssertionError("web rendering must not call Slack")

    monkeypatch.setattr(web_app.slack_cache, "api_get", fail)
    monkeypatch.setattr(web_app.slack_cache, "api_post", fail)

    response = client.get("/slack")

    assert response.status_code == 200
    assert "#testing" in response.text


def test_policy_detail_status_aggregates_native_check_results(tmp_path):
    view = build_configuration_view(
        _config(tmp_path),
        jobs=(),
        job_errors={},
        slack_directory={},
    )
    policy = view.policy("shared")
    assert policy is not None

    warning = build_policy_check_view(
        "alpha",
        PolicyCheck(provider="claude", ok=True, warnings=("review this",)),
    )
    broken = build_policy_check_view(
        "zeta",
        PolicyCheck(provider="codex", ok=False, problems=("cannot launch",)),
    )

    healthy = build_policy_check_view(
        "alpha",
        PolicyCheck(provider="claude", ok=True, policy_revision="healthy"),
    )

    assert with_policy_checks(policy, [healthy]).status == "error"
    assert with_policy_checks(policy, [warning]).status == "error"
    assert with_policy_checks(policy, [warning, broken]).status == "error"

    all_healthy = [
        build_policy_check_view(
            workspace,
            PolicyCheck(provider=provider, ok=True, policy_revision="healthy"),
        )
        for workspace in ("alpha", "zeta")
        for provider in ("claude", "codex")
    ]
    assert with_policy_checks(policy, all_healthy).status == "ready"
    all_healthy[0] = warning
    assert with_policy_checks(policy, all_healthy).status == "warning"


def test_truncated_instruction_inventory_is_a_warning(tmp_path):
    view = build_configuration_view(
        _config(tmp_path), jobs=(), job_errors={}, slack_directory={}
    )
    workspace = view.workspace("alpha")
    assert workspace is not None and workspace.status == "configured"

    enriched = with_workspace_agents(
        workspace,
        agent_files=("AGENTS.md",),
        truncated=True,
        managed=True,
        root_editable=True,
    )

    assert enriched.status == "warning"


def _managed_catalog_client(tmp_path, monkeypatch):
    config_dir = tmp_path / "enso"
    alpha = config_dir / "workspaces" / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "AGENTS.md").write_text("# Alpha\n", encoding="utf-8")
    child = alpha / "service"
    child.mkdir()
    (child / "AGENTS.md").write_text("# Service\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "AGENTS.md").write_text("# External\n", encoding="utf-8")

    config = _config(tmp_path)
    config["workspaces"]["alpha"]["path"] = str(alpha)
    config["workspaces"]["zeta"]["path"] = str(external)
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))
    _, client = _client(tmp_path, monkeypatch, config)
    return alpha, external, client


def test_workspace_detail_edits_managed_root_and_keeps_children_read_only(
    tmp_path,
    monkeypatch,
):
    alpha, _, client = _managed_catalog_client(tmp_path, monkeypatch)

    detail = client.get("/workspaces/alpha")

    assert detail.status_code == 200
    assert 'action="/workspaces/alpha/agents/edit"' in detail.text
    assert "# Alpha" in detail.text
    assert "service/AGENTS.md" in detail.text
    assert "2 AGENTS.md files" not in detail.text  # Count belongs to the list page.

    child = client.get("/workspaces/alpha/agents/service/AGENTS.md")
    assert child.status_code == 200
    assert "# Service" in child.text
    assert "Read-only" in child.text
    assert "<form" not in child.text
    assert client.get("/workspaces/alpha/agents/AGENTS.md").status_code == 404

    original = read_agent(str(alpha), "AGENTS.md", 128 * 1024)
    response = client.post(
        "/workspaces/alpha/agents/edit",
        data={
            "content": "# Edited\r\n",
            "revision": original.revision,
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/workspaces/alpha")
    assert (alpha / "AGENTS.md").read_text(encoding="utf-8") == "# Edited\n"

    stale = client.post(
        "/workspaces/alpha/agents/edit",
        data={
            "content": "overwrite",
            "revision": original.revision,
            "_csrf": client.app.state.csrf_token,
        },
    )
    assert stale.status_code == 409
    assert (alpha / "AGENTS.md").read_text(encoding="utf-8") == "# Edited\n"


def test_workspace_list_counts_agents_and_marks_external_roots(tmp_path, monkeypatch):
    _, _, client = _managed_catalog_client(tmp_path, monkeypatch)

    response = client.get("/workspaces")

    assert response.status_code == 200
    assert "2 AGENTS.md files" in response.text
    assert "External workspace" in response.text


def test_workspace_root_integrity_failure_changes_workspace_status(tmp_path, monkeypatch):
    alpha, _, client = _managed_catalog_client(tmp_path, monkeypatch)
    (alpha / "AGENTS.md").chmod(0o666)

    listing = client.get("/workspaces")
    detail = client.get("/workspaces/alpha")

    assert listing.status_code == detail.status_code == 200
    assert "must not be group- or other-writable" in listing.text
    assert "must not be group- or other-writable" in detail.text
    assert 'action="/workspaces/alpha/agents/edit"' not in detail.text


def test_workspace_root_creation_is_managed_only(tmp_path, monkeypatch):
    alpha, external, client = _managed_catalog_client(tmp_path, monkeypatch)
    (alpha / "AGENTS.md").unlink()

    created = client.post(
        "/workspaces/alpha/agents/edit",
        data={
            "content": "# Created\n",
            "revision": "",
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )
    external_write = client.post(
        "/workspaces/zeta/agents/edit",
        data={
            "content": "# Replaced\n",
            "revision": "",
            "_csrf": client.app.state.csrf_token,
        },
    )

    assert created.status_code == 303
    assert (alpha / "AGENTS.md").read_text(encoding="utf-8") == "# Created\n"
    assert external_write.status_code == 403
    assert (external / "AGENTS.md").read_text(encoding="utf-8") == "# External\n"
    assert client.get("/workspaces/missing").status_code == 404


def test_symlinked_managed_workspace_root_is_read_only(tmp_path, monkeypatch):
    config_dir = tmp_path / "enso"
    config_dir.mkdir()
    outside = tmp_path / "outside-workspaces"
    alpha = outside / "alpha"
    alpha.mkdir(parents=True)
    agents_file = alpha / "AGENTS.md"
    agents_file.write_text("# Sentinel\n", encoding="utf-8")
    (config_dir / "workspaces").symlink_to(outside, target_is_directory=True)

    config = _config(tmp_path)
    config["workspaces"] = {
        "alpha": {
            "path": str(config_dir / "workspaces" / "alpha"),
            "policy": "shared",
            "concurrency": 1,
        }
    }
    config["transports"]["slack"]["channels"] = {}
    config["transports"]["slack"]["dms"] = {"U1": {"workspace": "alpha", "audit": False}}
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))
    _, client = _client(tmp_path, monkeypatch, config)

    detail = client.get("/workspaces/alpha")
    edited = client.post(
        "/workspaces/alpha/agents/edit",
        data={
            "content": "# Escaped\n",
            "revision": read_agent(str(alpha), "AGENTS.md", 128 * 1024).revision,
            "_csrf": client.app.state.csrf_token,
        },
    )

    assert detail.status_code == 200
    assert "External workspace" in detail.text
    assert 'action="/workspaces/alpha/agents/edit"' not in detail.text
    assert edited.status_code == 403
    assert agents_file.read_text(encoding="utf-8") == "# Sentinel\n"


def test_shared_agents_editor_requires_the_displayed_revision(tmp_path, monkeypatch):
    config_dir = tmp_path / "enso"
    config_dir.mkdir()
    shared = config_dir / "AGENTS.md"
    shared.write_text("# Shared\n", encoding="utf-8")
    config = _config(tmp_path)
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))
    _, client = _client(tmp_path, monkeypatch, config)
    original = read_agent(str(config_dir), "AGENTS.md", 20 * 1024)

    view = client.get("/agents")
    saved = client.post(
        "/agents/edit",
        data={
            "content": "# Updated\n",
            "revision": original.revision,
            "_csrf": client.app.state.csrf_token,
        },
        follow_redirects=False,
    )
    stale = client.post(
        "/agents/edit",
        data={
            "content": "# Stale\n",
            "revision": original.revision,
            "_csrf": client.app.state.csrf_token,
        },
    )

    assert view.status_code == 200
    assert original.revision in view.text
    assert saved.status_code == 303
    assert stale.status_code == 409
    assert shared.read_text(encoding="utf-8") == "# Updated\n"


def test_missing_shared_agents_is_an_editable_launch_blocker(tmp_path, monkeypatch):
    config_dir = tmp_path / "enso"
    config_dir.mkdir()
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))
    _, client = _client(tmp_path, monkeypatch, _config(tmp_path))

    response = client.get("/agents")

    assert response.status_code == 200
    assert "Shared instruction file is missing" in response.text
    assert re.search(r">\s*error\s*</span>", response.text)
    assert 'action="/agents/edit"' in response.text
