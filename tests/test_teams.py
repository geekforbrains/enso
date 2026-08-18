"""Static teams schema, fail-closed validation, and exact Slack routes."""

from __future__ import annotations

import os

import pytest

from enso.teams import load_catalog, load_teams, load_telegram, resolve


@pytest.fixture(autouse=True)
def managed_config_root(tmp_path, monkeypatch):
    """Keep name-derived workspace roots isolated from the developer's home."""
    root = tmp_path / "enso"
    root.mkdir()
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(root))
    return root


def make_config(tmp_path, **overrides) -> dict:
    """A valid config with two workspaces and reusable policies."""
    client_policy = tmp_path / "policies" / "client-readonly"
    client_policy.mkdir(parents=True, exist_ok=True)
    config = {
        "transports": {
            "slack": {
                "bot_token": "x",
                "app_token": "x",
                "account_id": "T0ENSO",
                "dms": {
                    "U01ADMIN": {"workspace": "company"},
                },
                "channels": {
                    "C0ACME": {
                        "workspace": "client-a",
                        "audit": True,
                    },
                },
            }
        },
        "workspaces": {
            "company": {"policy": "admin"},
            "client-a": {
                "policy": "client-readonly",
                "concurrency": 2,
            },
        },
        "policies": {
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
    }
    config.update(overrides)
    return config


def make_workspace_policy_config(tmp_path) -> dict:
    """Return the v2 ownership model: workspace -> policy, route -> workspace."""
    return make_config(tmp_path)


# -- transport-independent execution catalog --


def test_workspace_selects_one_reusable_policy(tmp_path):
    config = make_workspace_policy_config(tmp_path)
    config["workspaces"]["client-b"] = {
        "policy": "client-readonly",
    }

    catalog = load_catalog(config)

    assert catalog.errors == ()
    assert catalog.workspaces["client-a"].policy == "client-readonly"
    assert catalog.workspaces["client-b"].policy == "client-readonly"
    assert catalog.policies["client-readonly"].policy_dir is not None
    assert catalog.usable("client-a")
    assert catalog.usable("client-b")


def test_workspace_requires_a_known_policy(tmp_path):
    config = make_workspace_policy_config(tmp_path)
    del config["workspaces"]["company"]["policy"]
    config["workspaces"]["client-a"]["policy"] = "missing"

    catalog = load_catalog(config)

    assert not catalog.usable("company")
    assert not catalog.usable("client-a")
    assert "policy is required and must be a string" in catalog.workspace_errors["company"]
    assert "unknown policy 'missing'" in catalog.workspace_errors["client-a"]


@pytest.mark.parametrize(
    "name",
    ["Client", "client_ops", "client.ops", "-client", "client-", "client--ops", "a" * 65],
)
def test_workspace_names_are_lowercase_kebab_case(tmp_path, name):
    config = make_workspace_policy_config(tmp_path)
    config["workspaces"][name] = {"policy": "admin"}

    catalog = load_catalog(config)

    assert any("workspace names must be lowercase kebab-case" in error for error in catalog.errors)
    assert name not in catalog.workspaces


@pytest.mark.parametrize("name", ["Client.Read_Only", "policy.v2", "OPS_admin"])
def test_policy_names_keep_their_separate_portable_identifier_rules(tmp_path, name):
    config = make_workspace_policy_config(tmp_path)
    config["policies"][name] = {
        "unrestricted": True,
        "providers": ["claude"],
        "default_provider": "claude",
    }

    catalog = load_catalog(config)

    assert name in catalog.policies


def test_route_cannot_override_workspace_policy(tmp_path):
    config = make_workspace_policy_config(tmp_path)
    config["transports"]["slack"]["channels"]["C0ACME"]["policy"] = "admin"

    parsed = load_teams(config)

    route = parsed.channel_routes["C0ACME"]
    assert parsed.catalog.policy_for(route.workspace).name == "client-readonly"
    assert route.route_id in parsed.route_errors
    assert any("unknown keys ['policy']" in error for error in parsed.route_errors[route.route_id])


def test_slack_routes_coexist_with_transport_settings(tmp_path):
    config = make_workspace_policy_config(tmp_path)
    slack = config["transports"]["slack"]
    slack.pop("bot_token")
    slack.pop("app_token")
    slack.update(
        {
            "bot_token_1password": {"item": "Slack", "field": "BOT_TOKEN"},
            "app_token_1password": {"item": "Slack", "field": "APP_TOKEN"},
            "bot_user_id": "UBOT",
            "notify_channel": "C0ACME",
            "channel_context_messages": 12,
            "rich_messages": False,
            "persistent_surfaces": False,
        }
    )

    parsed = load_teams(config)

    assert parsed.dispatchable
    assert parsed.account_id == "T0ENSO"
    assert resolve(parsed, user_id="U01ADMIN", channel_id=None).status == "authorized"
    assert resolve(parsed, user_id="UANY", channel_id="C0ACME").status == "authorized"


def test_legacy_top_level_routes_are_rejected(tmp_path):
    config = make_workspace_policy_config(tmp_path)
    config["routes"] = {"slack": {"account_id": "T0LEGACY"}}

    parsed = load_teams(config)

    assert not parsed.dispatchable
    assert any(
        "routes is no longer supported" in error
        and "transports.slack" in error
        for error in parsed.errors
    )


def test_catalog_loads_without_slack_routes(tmp_path):
    config = make_config(tmp_path)
    del config["transports"]["slack"]

    catalog = load_catalog(config)

    assert catalog.errors == ()
    assert catalog.workspaces["company"].path == str(
        tmp_path / "enso" / "workspaces" / "company"
    )
    assert catalog.policies["admin"].unrestricted
    assert catalog.usable("company")


def test_catalog_rejects_legacy_working_dir(tmp_path):
    config = make_config(tmp_path)
    config["working_dir"] = str(tmp_path / "legacy")

    catalog = load_catalog(config)

    assert any(
        "working_dir is no longer supported" in error and "workspaces" in error
        for error in catalog.errors
    )


def test_telegram_binds_one_workspace_and_derived_policy(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["telegram"] = {
        "bot_token": "token",
        "allowed_users": ["123"],
        "notify_channel": "123",
        "workspace": "client-a",
    }

    parsed = load_telegram(config)

    assert parsed.errors == ()
    assert parsed.allowed_users == ("123",)
    assert parsed.workspace.name == "client-a"
    assert parsed.policy.name == "client-readonly"


@pytest.mark.parametrize("workspace", [None, "missing"])
def test_telegram_requires_a_usable_workspace(tmp_path, workspace):
    config = make_config(tmp_path)
    telegram = {
        "bot_token": "token",
        "allowed_users": ["123"],
    }
    if workspace is not None:
        telegram["workspace"] = workspace
    config["transports"]["telegram"] = telegram

    parsed = load_telegram(config)

    assert parsed.workspace is None
    assert parsed.policy is None
    assert any("transports.telegram.workspace" in error for error in parsed.errors)


def test_telegram_rejects_unknown_transport_keys(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["telegram"] = {
        "bot_token": "token",
        "allowed_users": ["123"],
        "workspace": "company",
        "fallback_workspace": "client-a",
    }

    parsed = load_telegram(config)

    assert any("unknown keys ['fallback_workspace']" in error for error in parsed.errors)


def test_catalog_rejects_unknown_or_invalid_bindings(tmp_path):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["providers"] = []
    catalog = load_catalog(config)

    assert not catalog.usable("client-a")
    assert not catalog.usable("missing")


@pytest.mark.parametrize("legacy_path", [None, "relative", "/tmp/external"])
def test_legacy_workspace_path_fails_closed_with_one_migration_diagnostic(
    tmp_path, legacy_path
):
    config = make_config(tmp_path)
    del config["transports"]["slack"]
    config["workspaces"]["client-a"]["path"] = legacy_path

    catalog = load_catalog(config)

    problems = catalog.workspace_errors["client-a"]
    assert len([problem for problem in problems if "path" in problem]) == 1
    assert "path is no longer supported" in problems[0]
    assert "docs/migrations/v1.3-managed-workspaces.md" in problems[0]
    assert not catalog.usable("client-a")


# -- valid schema --


def test_load_valid_config(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    assert parsed is not None
    assert parsed.dispatchable
    assert parsed.errors == ()
    assert parsed.account_id == "T0ENSO"
    assert parsed.workspaces["company"].concurrency == 1
    assert parsed.workspaces["client-a"].concurrency == 2
    assert parsed.policies["admin"].unrestricted
    assert not parsed.policies["client-readonly"].unrestricted
    assert parsed.dm_routes["U01ADMIN"].route_id == "slack.dm.U01ADMIN"
    assert parsed.catalog.policy_for(parsed.channel_routes["C0ACME"].workspace).name == (
        "client-readonly"
    )


def test_missing_slack_account_id_is_actionable_invalid_config(tmp_path):
    config = make_config(tmp_path)
    del config["transports"]["slack"]["account_id"]
    parsed = load_teams(config)

    assert not parsed.dispatchable
    assert any(
        "transports.slack.account_id is required" in problem for problem in parsed.errors
    )


def test_workspace_path_is_name_derived_and_policy_path_is_canonical(tmp_path):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["policy_dir"] = (
        str(tmp_path / "policies" / ".." / "policies" / "client-readonly")
    )
    parsed = load_teams(config)
    assert parsed.workspaces["company"].path == str(
        tmp_path / "enso" / "workspaces" / "company"
    )
    assert parsed.policies["client-readonly"].policy_dir == os.path.realpath(
        tmp_path / "policies" / "client-readonly"
    )


def test_workspace_path_uses_the_current_managed_root(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    moved_root = tmp_path / "moved-enso"
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(moved_root))

    catalog = load_catalog(config)

    assert catalog.workspaces["client-a"].path == str(
        moved_root / "workspaces" / "client-a"
    )


def test_catalog_rejects_cwd_dependent_relative_policy_paths(tmp_path):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["policy_dir"] = "relative/path"

    catalog = load_catalog(config)

    problems = catalog.policy_errors["client-readonly"]
    assert any("must be absolute" in problem for problem in problems)
    assert not catalog.usable("client-a")


def test_policy_dir_defaults_from_policy_name(tmp_path, monkeypatch):
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(tmp_path))
    config = make_config(tmp_path)
    del config["policies"]["client-readonly"]["policy_dir"]
    parsed = load_teams(config)
    assert parsed.policies["client-readonly"].policy_dir == str(
        tmp_path / "policies" / "client-readonly"
    )


def test_routes_share_the_workspace_policy(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channels"]["C0ACME_INTERNAL"] = {
        "workspace": "client-a",
    }
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert parsed.route_usable(parsed.channel_routes["C0ACME"])
    assert parsed.route_usable(parsed.channel_routes["C0ACME_INTERNAL"])
    assert parsed.catalog.policy_for("client-a").name == "client-readonly"


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
        and "transports.slack.dms" in problem
        for problem in parsed.errors
    )


def test_removed_slack_allowlist_cannot_enable_slack_without_routes(tmp_path):
    config = make_config(tmp_path)
    for key in ("account_id", "dms", "channels"):
        config["transports"]["slack"].pop(key)
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    parsed = load_teams(config)

    assert not parsed.dispatchable
    assert any("transports.slack.account_id is required" in problem for problem in parsed.errors)
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
def test_malformed_policies_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["policies"] = bad
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("bad", [None, "x", 5, ["x"]])
def test_malformed_routes_block_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channels"] = bad
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("bad", [None, "x", 5, ["x"]])
def test_malformed_slack_transport_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["transports"]["slack"] = bad

    parsed = load_teams(config)

    assert not parsed.dispatchable
    assert "transports.slack must be an object" in parsed.errors


def test_missing_account_id_disables_dispatch(tmp_path):
    config = make_config(tmp_path)
    del config["transports"]["slack"]["account_id"]
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize(
    ("location", "unknown_key"),
    [
        (("transports", "slack"), "fallback"),
        (("audit",), "retention"),
    ],
)
def test_unknown_structural_keys_disable_dispatch(tmp_path, location, unknown_key):
    config = make_config(tmp_path)
    if location == ("audit",):
        config["audit"] = {unknown_key: True}
    else:
        config["transports"]["slack"][unknown_key] = True
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("bad", [None, "block", 1, []])
def test_malformed_audit_block_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["audit"] = bad
    assert not load_teams(config).dispatchable


@pytest.mark.parametrize("direction", ["inside", "contains"])
def test_policy_dir_may_not_overlap_any_workspace(tmp_path, direction):
    config = make_config(tmp_path)
    workspace = tmp_path / "enso" / "workspaces" / "client-a"
    policy = workspace / "policy" if direction == "inside" else workspace.parent
    config["policies"]["client-readonly"]["policy_dir"] = str(policy)
    assert not load_teams(config).dispatchable


# -- item-scoped fail-closed errors --


@pytest.mark.parametrize(
    ("key", "value"),
    [
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
def test_invalid_or_unknown_policy_values_disable_its_routes(tmp_path, key, value):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"][key] = value
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "client-readonly" in parsed.policy_errors
    assert not parsed.route_usable(parsed.channel_routes["C0ACME"])


def test_unrestricted_and_policy_dir_is_policy_error(tmp_path):
    config = make_config(tmp_path)
    config["policies"]["admin"]["policy_dir"] = str(tmp_path / "policies" / "admin")
    parsed = load_teams(config)
    assert "admin" in parsed.policy_errors


# -- env_passthrough --


def test_env_passthrough_valid_list_is_stored_as_tuple(tmp_path):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["env_passthrough"] = [
        "METRICS_API_TOKEN",
        "TICKETS_API_TOKEN",
    ]
    parsed = load_teams(config)
    assert "client-readonly" not in parsed.policy_errors
    assert parsed.policies["client-readonly"].env_passthrough == (
        "METRICS_API_TOKEN",
        "TICKETS_API_TOKEN",
    )


def test_env_passthrough_defaults_to_empty(tmp_path):
    parsed = load_teams(make_config(tmp_path))
    assert parsed.policies["client-readonly"].env_passthrough == ()


@pytest.mark.parametrize("bad", ["METRICS_API_TOKEN", 5, [1], ["OK_NAME", None], {"A": "B"}])
def test_env_passthrough_must_be_a_string_list(tmp_path, bad):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["env_passthrough"] = bad
    parsed = load_teams(config)
    problems = parsed.policy_errors["client-readonly"]
    assert any("env_passthrough must be a list of strings" in p for p in problems)
    assert parsed.policies["client-readonly"].env_passthrough == ()


def test_env_passthrough_rejects_duplicates(tmp_path):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["env_passthrough"] = ["A_TOKEN", "A_TOKEN"]
    parsed = load_teams(config)
    problems = parsed.policy_errors["client-readonly"]
    assert any("duplicate" in p for p in problems)


@pytest.mark.parametrize("name", ["metrics_token", "FOO=BAR", "1FOO", ""])
def test_env_passthrough_rejects_malformed_names(tmp_path, name):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["env_passthrough"] = [name]
    parsed = load_teams(config)
    problems = parsed.policy_errors["client-readonly"]
    assert any("must match" in p for p in problems)


@pytest.mark.parametrize("name", ["HOME", "PATH", "CODEX_HOME", "ENSO_ANYTHING"])
def test_env_passthrough_rejects_reserved_names(tmp_path, name):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["env_passthrough"] = [name]
    parsed = load_teams(config)
    problems = parsed.policy_errors["client-readonly"]
    assert any("reserved" in p for p in problems)


def test_env_passthrough_is_rejected_on_unrestricted_policy(tmp_path):
    config = make_config(tmp_path)
    config["policies"]["admin"]["env_passthrough"] = ["METRICS_API_TOKEN"]
    parsed = load_teams(config)
    problems = parsed.policy_errors["admin"]
    assert any("unrestricted: true is invalid alongside env_passthrough" in p for p in problems)


def test_env_passthrough_key_typo_is_unknown_key_error(tmp_path):
    config = make_config(tmp_path)
    config["policies"]["client-readonly"]["env_passthru"] = ["METRICS_API_TOKEN"]
    parsed = load_teams(config)
    problems = parsed.policy_errors["client-readonly"]
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
    config["transports"]["slack"]["channels"]["C0ACME"][key] = value
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "slack.channel.C0ACME" in parsed.route_errors


def test_unknown_workspace_disables_route(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channels"]["C0ACME"]["workspace"] = "missing"
    parsed = load_teams(config)
    assert "slack.channel.C0ACME" in parsed.route_errors


def test_legacy_route_access_override_disables_route(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channels"]["C0ACME"]["access"] = "missing"
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
    config["transports"]["slack"]["channels"]["C0ACME"]["audit"] = "yes"
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
    config["transports"]["slack"]["channels"]["C0ACME"].update(
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
    config["transports"]["slack"]["channel_defaults"] = {
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
    config["transports"]["slack"]["channel_defaults"] = {
        "mention_required": False,
        "thread_mention_required": False,
    }
    config["transports"]["slack"]["channels"]["C0ACME"]["mention_required"] = True
    parsed = load_teams(config)
    route = parsed.channel_routes["C0ACME"]
    assert route.mention_required is True
    assert route.thread_mention_required is False


def test_channel_defaults_do_not_affect_dm_routes(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channel_defaults"] = {"mention_required": False}
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "slack.dm.U01ADMIN" not in parsed.route_errors


@pytest.mark.parametrize("bad", ["yes", 1, None, []])
def test_channel_defaults_values_must_be_boolean(tmp_path, bad):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channel_defaults"] = {"mention_required": bad}
    parsed = load_teams(config)
    assert not parsed.dispatchable


@pytest.mark.parametrize("bad", ["mention", ["mention_required"], 5])
def test_channel_defaults_must_be_an_object(tmp_path, bad):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channel_defaults"] = bad
    parsed = load_teams(config)
    assert not parsed.dispatchable


def test_channel_defaults_unknown_keys_disable_dispatch(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channel_defaults"] = {"mentions_required": True}
    parsed = load_teams(config)
    assert not parsed.dispatchable


@pytest.mark.parametrize("key", ["mention_required", "thread_mention_required"])
@pytest.mark.parametrize("bad", ["true", 0, None])
def test_route_mention_settings_must_be_boolean(tmp_path, key, bad):
    config = make_config(tmp_path)
    config["transports"]["slack"]["channels"]["C0ACME"][key] = bad
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "slack.channel.C0ACME" in parsed.route_errors


@pytest.mark.parametrize("key", ["mention_required", "thread_mention_required"])
def test_mention_settings_are_rejected_on_dm_routes(tmp_path, key):
    """DM behavior is fixed; accepting the key would misrepresent the config."""
    config = make_config(tmp_path)
    config["transports"]["slack"]["dms"]["U01ADMIN"][key] = False
    parsed = load_teams(config)
    assert parsed.dispatchable
    assert "slack.dm.U01ADMIN" in parsed.route_errors
