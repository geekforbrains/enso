"""Static teams schema, fail-closed validation, and exact Slack routes."""

from __future__ import annotations

import os

import pytest

from enso.teams import load_catalog, load_teams, resolve


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


# -- transport-independent execution catalog --


def test_catalog_loads_without_slack_routes(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]

    catalog = load_catalog(config)

    assert catalog.errors == ()
    assert catalog.workspaces["company"].path.endswith("workspaces/company")
    assert catalog.access_profiles["admin"].unrestricted
    assert catalog.usable("company", "admin")


def test_catalog_rejects_unknown_or_invalid_bindings(tmp_path):
    config = make_config(tmp_path)
    config["access"]["client-readonly"]["providers"] = []
    catalog = load_catalog(config)

    assert not catalog.usable("client-a", "client-readonly")
    assert not catalog.usable("missing", "admin")
    assert not catalog.usable("company", "missing")


def test_catalog_retains_topology_validation_without_slack_routes(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]
    config["workspaces"]["client-a"]["path"] = config["workspaces"]["company"]["path"]

    catalog = load_catalog(config)

    assert catalog.errors
    assert not catalog.usable("company", "admin")


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


def test_missing_slack_routes_is_actionable_invalid_config(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]
    parsed = load_teams(config)

    assert not parsed.dispatchable
    assert any("routes.slack is required" in problem for problem in parsed.errors)


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


def test_removed_slack_allowlist_has_actionable_migration_error(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    parsed = load_teams(config)

    assert not parsed.dispatchable
    assert any(
        "transports.slack.allowed_users is no longer supported" in problem
        and "routes.slack.dms" in problem
        for problem in parsed.errors
    )


def test_removed_slack_allowlist_cannot_enable_slack_without_routes(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    parsed = load_teams(config)

    assert not parsed.dispatchable
    assert any("routes.slack is required" in problem for problem in parsed.errors)
    assert any("allowed_users is no longer supported" in problem for problem in parsed.errors)


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


# -- env_passthrough --


def test_env_passthrough_valid_list_is_stored_as_tuple(tmp_path):
    config = make_config(tmp_path)
    config["access"]["client-readonly"]["env_passthrough"] = [
        "METRICS_API_TOKEN",
        "TICKETS_API_TOKEN",
    ]
    parsed = load_teams(config)
    assert "client-readonly" not in parsed.access_errors
    assert parsed.access_profiles["client-readonly"].env_passthrough == (
        "METRICS_API_TOKEN",
        "TICKETS_API_TOKEN",
    )


def test_env_passthrough_defaults_to_empty(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    assert parsed.access_profiles["client-readonly"].env_passthrough == ()


@pytest.mark.parametrize("bad", ["METRICS_API_TOKEN", 5, [1], ["OK_NAME", None], {"A": "B"}])
def test_env_passthrough_must_be_a_string_list(tmp_path, bad):
    config = make_config(tmp_path)
    config["access"]["client-readonly"]["env_passthrough"] = bad
    parsed = load_teams(config)
    problems = parsed.access_errors["client-readonly"]
    assert any("env_passthrough must be a list of strings" in p for p in problems)
    assert parsed.access_profiles["client-readonly"].env_passthrough == ()


def test_env_passthrough_rejects_duplicates(tmp_path):
    config = make_config(tmp_path)
    config["access"]["client-readonly"]["env_passthrough"] = ["A_TOKEN", "A_TOKEN"]
    parsed = load_teams(config)
    problems = parsed.access_errors["client-readonly"]
    assert any("duplicate" in p for p in problems)


@pytest.mark.parametrize("name", ["metrics_token", "FOO=BAR", "1FOO", ""])
def test_env_passthrough_rejects_malformed_names(tmp_path, name):
    config = make_config(tmp_path)
    config["access"]["client-readonly"]["env_passthrough"] = [name]
    parsed = load_teams(config)
    problems = parsed.access_errors["client-readonly"]
    assert any("must match" in p for p in problems)


@pytest.mark.parametrize("name", ["HOME", "PATH", "CODEX_HOME", "ENSO_ANYTHING"])
def test_env_passthrough_rejects_reserved_names(tmp_path, name):
    config = make_config(tmp_path)
    config["access"]["client-readonly"]["env_passthrough"] = [name]
    parsed = load_teams(config)
    problems = parsed.access_errors["client-readonly"]
    assert any("reserved" in p for p in problems)


def test_env_passthrough_is_rejected_on_unrestricted_profile(tmp_path):
    config = make_config(tmp_path)
    config["access"]["admin"]["env_passthrough"] = ["METRICS_API_TOKEN"]
    parsed = load_teams(config)
    problems = parsed.access_errors["admin"]
    assert any("unrestricted: true is invalid alongside env_passthrough" in p for p in problems)


def test_env_passthrough_key_typo_is_unknown_key_error(tmp_path):
    config = make_config(tmp_path)
    config["access"]["client-readonly"]["env_passthru"] = ["METRICS_API_TOKEN"]
    parsed = load_teams(config)
    problems = parsed.access_errors["client-readonly"]
    assert any("unknown keys" in p and "env_passthru" in p for p in problems)


def test_reserved_names_cover_every_launch_controlled_variable():
    """teams.py defines the reserved set locally (circular import); guard drift."""
    from enso import policy, teams

    launch_controlled = set(policy._KEEP_ENV) | {"PATH", "CODEX_HOME"}
    assert launch_controlled <= teams._ENV_PASSTHROUGH_RESERVED


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


def test_unconfigured_channel_has_explicit_resolution(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    decision = resolve(parsed, user_id="U01ADMIN", channel_id="CPRIVATE")
    assert decision.status == "unconfigured"
    assert decision.reason == "no_route"


def test_dm_route_key_is_exact_slack_user_id(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    allowed = resolve(parsed, user_id="U01ADMIN", channel_id=None)
    denied = resolve(parsed, user_id="U02STAFF", channel_id=None)
    assert allowed.status == "authorized"
    assert allowed.route.route_id == "slack.dm.U01ADMIN"
    assert denied.status == "unconfigured"
    assert denied.reason == "no_route"


def test_route_scoped_error_is_reported_to_that_route(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"]["access"] = "missing"
    parsed = load_teams(config)
    decision = resolve(parsed, user_id="UANY", channel_id="C0ACME")
    assert decision.status == "error"
    assert decision.reason == "route_unusable"


def test_global_error_preserves_unconfigured_resolution_without_exact_route(tmp_path):
    config = make_config(tmp_path)
    config["groups"] = {}
    parsed = load_teams(config)
    decision = resolve(parsed, user_id="UUNKNOWN", channel_id=None)
    assert decision.status == "unconfigured"
    assert decision.reason == "no_route"


def test_global_error_is_reported_on_exact_route(tmp_path):
    config = make_config(tmp_path)
    config["groups"] = {}
    parsed = load_teams(config)
    decision = resolve(parsed, user_id="UANY", channel_id="C0ACME")
    assert decision.status == "error"
    assert decision.reason == "teams_config_invalid"


# -- response trigger settings --


def test_mention_settings_default_to_required(tmp_path):
    """Absent settings reproduce original behavior: mention-gated everywhere."""
    parsed = load_teams(make_config(tmp_path))
    route = parsed.channel_routes["C0ACME"]
    assert route.mention_required is True
    assert route.thread_mention_required is True
    assert not parsed.errors
    assert "slack.channel.C0ACME" not in parsed.route_errors


def test_route_level_mention_settings_are_stored(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"].update(
        mention_required=False,
        thread_mention_required=False,
    )
    parsed = load_teams(config)
    route = parsed.channel_routes["C0ACME"]
    assert route.mention_required is False
    assert route.thread_mention_required is False
    assert parsed.dispatchable
    assert "slack.channel.C0ACME" not in parsed.route_errors


def test_channel_defaults_apply_to_routes_without_overrides(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channel_defaults"] = {
        "mention_required": False,
        "thread_mention_required": False,
    }
    parsed = load_teams(config)
    route = parsed.channel_routes["C0ACME"]
    assert route.mention_required is False
    assert route.thread_mention_required is False
    assert parsed.dispatchable


def test_route_setting_overrides_channel_defaults(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channel_defaults"] = {
        "mention_required": False,
        "thread_mention_required": False,
    }
    config["routes"]["slack"]["channels"]["C0ACME"]["mention_required"] = True
    parsed = load_teams(config)
    route = parsed.channel_routes["C0ACME"]
    assert route.mention_required is True
    assert route.thread_mention_required is False


def test_channel_defaults_do_not_affect_dm_routes(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channel_defaults"] = {"mention_required": False}
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "slack.dm.U01ADMIN" not in parsed.route_errors


@pytest.mark.parametrize("bad", ["yes", 1, None, []])
def test_channel_defaults_values_must_be_boolean(tmp_path, bad):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channel_defaults"] = {"mention_required": bad}
    parsed = load_teams(config)
    assert not parsed.dispatchable


@pytest.mark.parametrize("bad", ["mention", ["mention_required"], 5])
def test_channel_defaults_must_be_an_object(tmp_path, bad):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channel_defaults"] = bad
    parsed = load_teams(config)
    assert not parsed.dispatchable


def test_channel_defaults_unknown_keys_disable_dispatch(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channel_defaults"] = {"mentions_required": True}
    parsed = load_teams(config)
    assert not parsed.dispatchable


@pytest.mark.parametrize("key", ["mention_required", "thread_mention_required"])
@pytest.mark.parametrize("bad", ["true", 0, None])
def test_route_mention_settings_must_be_boolean(tmp_path, key, bad):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"][key] = bad
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "slack.channel.C0ACME" in parsed.route_errors


@pytest.mark.parametrize("key", ["mention_required", "thread_mention_required"])
def test_mention_settings_are_rejected_on_dm_routes(tmp_path, key):
    """DM behavior is fixed; accepting the key would misrepresent the config."""
    config = make_config(tmp_path)
    config["routes"]["slack"]["dms"]["U01ADMIN"][key] = False
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "slack.dm.U01ADMIN" in parsed.route_errors
