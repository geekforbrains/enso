"""Static teams schema, fail-closed validation, and exact Slack routes."""

from __future__ import annotations

import os

import pytest

from enso.teams import load_teams, resolve, slack_mode


def make_config(tmp_path, **overrides) -> dict:
    """A valid config with a shared project workspace and two access profiles."""
    company = tmp_path / "workspaces" / "company"
    acme = tmp_path / "workspaces" / "clients" / "acme"
    client_policy = tmp_path / "policies" / "client-readonly"
    for directory in (company, acme, client_policy):
        directory.mkdir(parents=True, exist_ok=True)
    config = {
        "working_dir": str(tmp_path / "legacy"),
        "transports": {"slack": {"bot_token": "x", "app_token": "x"}},
        "workspaces": {
            "company": {"path": str(company)},
            "client-a": {"path": str(acme), "concurrency": 2},
        },
        "access": {
            "admin": {
                "unrestricted": True,
                "providers": ["claude", "codex", "agy"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
            "client-readonly": {
                "policy_dir": str(client_policy),
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": ["status", "clear", "stop", "help"],
            },
        },
        "routes": {
            "slack": {
                "account_id": "T0ENSO",
                "dms": {
                    "U01ADMIN": {"workspace": "company", "access": "admin"},
                },
                "channels": {
                    "C0ACME": {
                        "workspace": "client-a",
                        "access": "client-readonly",
                        "audit": True,
                    },
                },
            }
        },
    }
    config.update(overrides)
    return config


# -- slack_mode --


def test_slack_mode_teams(tmp_path):
    assert slack_mode(make_config(tmp_path)) == "teams"


def test_slack_mode_legacy(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    assert slack_mode(config) == "legacy"


def test_slack_mode_blocked_when_neither(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]
    assert slack_mode(config) == "blocked"


def test_slack_mode_conflict_when_both(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    assert slack_mode(config) == "conflict"


# -- valid schema --


def test_load_valid_config(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    assert parsed is not None
    assert parsed.dispatchable
    assert parsed.errors == ()
    assert parsed.account_id == "T0ENSO"
    assert parsed.workspaces["company"].concurrency == 1
    assert parsed.workspaces["client-a"].concurrency == 2
    assert parsed.access_profiles["admin"].unrestricted
    assert not parsed.access_profiles["client-readonly"].unrestricted
    assert parsed.dm_routes["U01ADMIN"].route_id == "slack.dm.U01ADMIN"
    assert parsed.channel_routes["C0ACME"].access == "client-readonly"


def test_load_returns_none_without_routes(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]
    assert load_teams(config) is None


def test_paths_are_canonical_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path)
    config["workspaces"]["company"]["path"] = "workspaces/../workspaces/company"
    config["access"]["client-readonly"]["policy_dir"] = "policies/../policies/client-readonly"
    parsed = load_teams(config)
    assert parsed.workspaces["company"].path == os.path.realpath(
        tmp_path / "workspaces" / "company"
    )
    assert parsed.access_profiles["client-readonly"].policy_dir == os.path.realpath(
        tmp_path / "policies" / "client-readonly"
    )


def test_policy_dir_defaults_from_access_profile_name(tmp_path, monkeypatch):
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(tmp_path))
    config = make_config(tmp_path)
    del config["access"]["client-readonly"]["policy_dir"]
    parsed = load_teams(config)
    assert parsed.access_profiles["client-readonly"].policy_dir == str(
        tmp_path / "policies" / "client-readonly"
    )


def test_routes_may_share_workspace_with_different_access(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME_INTERNAL"] = {
        "workspace": "client-a",
        "access": "admin",
    }
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert parsed.route_usable(parsed.channel_routes["C0ACME"])
    assert parsed.route_usable(parsed.channel_routes["C0ACME_INTERNAL"])


def test_audit_defaults(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    assert not parsed.dm_routes["U01ADMIN"].audit
    assert parsed.channel_routes["C0ACME"].audit
    assert parsed.audit_on_failure == "block"
    assert parsed.audit_max_age_days == 365


# -- global fail-closed errors --


def test_conflict_with_legacy_allowlist_disables_dispatch(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    assert not load_teams(config).dispatchable


def test_legacy_groups_are_explicit_migration_error(tmp_path):
    config = make_config(tmp_path)
    config["groups"] = {"staff": {"slack": ["U1"]}}
    parsed = load_teams(config)
    assert not parsed.dispatchable
    assert any("no longer supported" in problem for problem in parsed.errors)


@pytest.mark.parametrize("bad", [None, "x", 5, ["x"]])
def test_malformed_workspaces_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["workspaces"] = bad
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("bad", [None, "x", 5, ["x"]])
def test_malformed_access_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["access"] = bad
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("bad", [None, "x", 5, ["x"]])
def test_malformed_routes_block_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"] = bad
    assert not load_teams(config).dispatchable


def test_missing_account_id_disables_dispatch(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]["slack"]["account_id"]
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize(
    ("location", "unknown_key"),
    [
        (("routes", "slack"), "fallback"),
        (("audit",), "retention"),
    ],
)
def test_unknown_structural_keys_disable_dispatch(tmp_path, location, unknown_key):
    config = make_config(tmp_path)
    if location == ("audit",):
        config["audit"] = {unknown_key: True}
    else:
        config["routes"]["slack"][unknown_key] = True
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("bad", [None, "block", 1, []])
def test_malformed_audit_block_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["audit"] = bad
    assert not load_teams(config).dispatchable


def test_nested_workspace_paths_disable_dispatch(tmp_path):
    config = make_config(tmp_path)
    nested = tmp_path / "workspaces" / "company" / "clients" / "acme"
    nested.mkdir(parents=True)
    config["workspaces"]["client-a"]["path"] = str(nested)
    assert not load_teams(config).dispatchable


def test_duplicate_workspace_paths_disable_dispatch(tmp_path):
    config = make_config(tmp_path)
    config["workspaces"]["client-a"]["path"] = config["workspaces"]["company"]["path"]
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("direction", ["inside", "contains"])
def test_policy_dir_may_not_overlap_any_workspace(tmp_path, direction):
    config = make_config(tmp_path)
    workspace = tmp_path / "workspaces" / "clients" / "acme"
    policy = workspace / "policy" if direction == "inside" else tmp_path / "workspaces" / "clients"
    config["access"]["client-readonly"]["policy_dir"] = str(policy)
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("direction", ["inside", "contains"])
def test_policy_dir_may_not_overlap_legacy_working_dir(tmp_path, direction):
    config = make_config(tmp_path)
    legacy = tmp_path / "legacy"
    policy = legacy / "policy" if direction == "inside" else tmp_path
    config["access"]["client-readonly"]["policy_dir"] = str(policy)
    assert not load_teams(config).dispatchable


# -- item-scoped fail-closed errors --


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("path", None),
        ("concurrency", True),
        ("concurrency", 0),
        ("policy_dir", "/tmp/old-schema"),
    ],
)
def test_invalid_or_unknown_workspace_values_disable_its_routes(tmp_path, key, value):
    config = make_config(tmp_path)
    config["workspaces"]["client-a"][key] = value
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "client-a" in parsed.workspace_errors
    assert not parsed.route_usable(parsed.channel_routes["C0ACME"])


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("unrestricted", "yes"),
        ("providers", "claude"),
        ("providers", []),
        ("default_provider", "codex"),
        ("chat_commands", 1),
        ("skills", ["project"]),
    ],
)
def test_invalid_or_unknown_access_values_disable_its_routes(tmp_path, key, value):
    config = make_config(tmp_path)
    config["access"]["client-readonly"][key] = value
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "client-readonly" in parsed.access_errors
    assert not parsed.route_usable(parsed.channel_routes["C0ACME"])


def test_unrestricted_and_policy_dir_is_access_error(tmp_path):
    config = make_config(tmp_path)
    config["access"]["admin"]["policy_dir"] = str(tmp_path / "policies" / "admin")
    parsed = load_teams(config)
    assert "admin" in parsed.access_errors


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("workspace", None),
        ("access", None),
        ("audit", "true"),
        ("context_from", "everyone"),
        ("allow", ["staff"]),
    ],
)
def test_invalid_unknown_and_legacy_route_values_disable_route(tmp_path, key, value):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"][key] = value
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "slack.channel.C0ACME" in parsed.route_errors


def test_unknown_workspace_disables_route(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"]["workspace"] = "missing"
    parsed = load_teams(config)
    assert "slack.channel.C0ACME" in parsed.route_errors


def test_unknown_access_profile_disables_route(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"]["access"] = "missing"
    parsed = load_teams(config)
    assert "slack.channel.C0ACME" in parsed.route_errors


# -- exact route resolution --


@pytest.mark.parametrize("user_id", ["U02STAFF", "U03CLIENT", "USOMEONE"])
def test_configured_channel_authorizes_every_poster(tmp_path, user_id):
    parsed = load_teams(make_config(tmp_path))
    decision = resolve(parsed, user_id=user_id, channel_id="C0ACME")
    assert decision.status == "authorized"
    assert decision.route.route_id == "slack.channel.C0ACME"


def test_unconfigured_channel_is_silent(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    decision = resolve(parsed, user_id="U01ADMIN", channel_id="CPRIVATE")
    assert decision.status == "silent"
    assert decision.reason == "no_route"


def test_dm_route_key_is_exact_slack_user_id(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    allowed = resolve(parsed, user_id="U01ADMIN", channel_id=None)
    denied = resolve(parsed, user_id="U02STAFF", channel_id=None)
    assert allowed.status == "authorized"
    assert allowed.route.route_id == "slack.dm.U01ADMIN"
    assert denied.status == "silent"
    assert denied.reason == "no_route"


def test_route_scoped_error_is_reported_to_that_route(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"]["access"] = "missing"
    parsed = load_teams(config)
    decision = resolve(parsed, user_id="UANY", channel_id="C0ACME")
    assert decision.status == "error"
    assert decision.reason == "route_unusable"


def test_global_error_is_silent_without_exact_route(tmp_path):
    config = make_config(tmp_path)
    config["groups"] = {}
    parsed = load_teams(config)
    decision = resolve(parsed, user_id="UUNKNOWN", channel_id=None)
    assert decision.status == "silent"


def test_global_error_is_reported_on_exact_route(tmp_path):
    config = make_config(tmp_path)
    config["groups"] = {}
    parsed = load_teams(config)
    decision = resolve(parsed, user_id="UANY", channel_id="C0ACME")
    assert decision.status == "error"
    assert decision.reason == "teams_config_invalid"
