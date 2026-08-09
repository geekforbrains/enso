"""Teams config schema, fail-closed validation, and Slack route resolution."""

from __future__ import annotations

import pytest

from enso import teams
from enso.teams import binding_revision, load_teams, resolve, slack_mode


def make_config(tmp_path, **overrides) -> dict:
    """A valid teams-mode config with two groups, two workspaces, two routes."""
    ops = tmp_path / "workspaces" / "ops"
    acme = tmp_path / "workspaces" / "acme"
    policies = tmp_path / "policies" / "acme"
    for d in (ops, acme, policies):
        d.mkdir(parents=True, exist_ok=True)
    config = {
        "working_dir": str(tmp_path / "workspace"),
        "transports": {"slack": {"bot_token": "x", "app_token": "x"}},
        "groups": {
            "admin": {"slack": ["U01ADMIN"]},
            "team": {"slack": ["U02DEV", "U03PM"]},
        },
        "workspaces": {
            "ops": {
                "path": str(ops),
                "unrestricted": True,
                "providers": ["claude", "codex", "agy"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
            "acme": {
                "path": str(acme),
                "policy_dir": str(policies),
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": ["status", "clear", "stop", "help"],
            },
        },
        "routes": {
            "slack": {
                "account_id": "T0ENSO",
                "dms": {
                    "owner": {"allow": ["admin"], "workspace": "ops"},
                },
                "channels": {
                    "C0ACME": {
                        "allow": ["team", "admin"],
                        "workspace": "acme",
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


# -- load_teams: valid config --


def test_load_valid_config(tmp_path):
    t = load_teams(make_config(tmp_path))
    assert t is not None
    assert t.dispatchable
    assert t.errors == ()
    assert t.account_id == "T0ENSO"
    assert t.groups["team"] == frozenset({"U02DEV", "U03PM"})
    assert t.workspaces["ops"].unrestricted
    assert not t.workspaces["acme"].unrestricted
    assert t.dm_routes["owner"].route_id == "slack.dm.owner"
    assert t.channel_routes["C0ACME"].route_id == "slack.channel.C0ACME"


def test_load_returns_none_without_routes(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]
    assert load_teams(config) is None


def test_defaults_applied(tmp_path):
    t = load_teams(make_config(tmp_path))
    dm = t.dm_routes["owner"]
    assert dm.audit is False
    assert dm.context_from == "allowed"
    ch = t.channel_routes["C0ACME"]
    assert ch.audit is True
    assert t.audit_on_failure == "block"
    assert t.audit_max_age_days == 365
    assert t.workspaces["ops"].concurrency == 1




def test_policy_dir_defaults_for_policy_controlled(tmp_path, monkeypatch):
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(tmp_path))
    config = make_config(tmp_path)
    del config["workspaces"]["acme"]["policy_dir"]
    t = load_teams(config)
    assert t.workspaces["acme"].policy_dir == str(tmp_path / "policies" / "acme")


# -- load_teams: global fail-closed errors --


def test_conflict_with_legacy_allowlist_disables_dispatch(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    t = load_teams(config)
    assert not t.dispatchable


def test_missing_account_id_disables_dispatch(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]["slack"]["account_id"]
    assert not load_teams(config).dispatchable


def test_malformed_groups_disable_dispatch(tmp_path):
    config = make_config(tmp_path)
    config["groups"] = {"admin": ["U01ADMIN"]}  # not a platform map
    assert not load_teams(config).dispatchable


def test_telegram_group_platform_is_invalid(tmp_path):
    config = make_config(tmp_path)
    config["groups"]["admin"]["telegram"] = ["123"]
    assert not load_teams(config).dispatchable


def test_duplicate_group_member_is_invalid(tmp_path):
    config = make_config(tmp_path)
    config["groups"]["admin"]["slack"] = ["U01ADMIN", "U01ADMIN"]
    assert not load_teams(config).dispatchable


def test_ambiguous_dm_routes_disable_dispatch(tmp_path):
    config = make_config(tmp_path)
    # U01ADMIN matches both "owner" (admin) and a second DM route.
    config["routes"]["slack"]["dms"]["second"] = {
        "allow": ["admin"],
        "workspace": "acme",
    }
    t = load_teams(config)
    assert not t.dispatchable
    assert any("ambiguous" in e.lower() for e in t.errors)


def test_disjoint_dm_routes_are_not_ambiguous(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["dms"]["project-team"] = {
        "allow": ["team"],
        "workspace": "acme",
    }
    assert load_teams(config).dispatchable


def test_malformed_routes_block_disables_dispatch(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"] = ["C0ACME"]
    assert not load_teams(config).dispatchable


def test_nested_workspace_paths_disable_dispatch(tmp_path):
    config = make_config(tmp_path)
    nested = tmp_path / "workspaces" / "ops" / "inner"
    nested.mkdir(parents=True)
    config["workspaces"]["acme"]["path"] = str(nested)
    assert not load_teams(config).dispatchable


def test_policy_dir_inside_workspace_disables_dispatch(tmp_path):
    config = make_config(tmp_path)
    inside = tmp_path / "workspaces" / "acme" / "policies"
    inside.mkdir(parents=True)
    config["workspaces"]["acme"]["policy_dir"] = str(inside)
    assert not load_teams(config).dispatchable


# -- load_teams: workspace/route scoped errors --


def test_unrestricted_with_policy_dir_is_workspace_error(tmp_path):
    config = make_config(tmp_path)
    config["workspaces"]["ops"]["policy_dir"] = str(tmp_path / "policies" / "ops")
    t = load_teams(config)
    assert t.dispatchable
    assert "ops" in t.workspace_errors


def test_unrestricted_with_discovered_policy_is_workspace_error(tmp_path, monkeypatch):
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(tmp_path))
    discovered = tmp_path / "policies" / "ops" / "claude"
    discovered.mkdir(parents=True)
    (discovered / "settings.json").write_text("{}")
    t = load_teams(make_config(tmp_path))
    assert "ops" in t.workspace_errors


def test_policy_controlled_workspace_may_not_overlap_working_dir(tmp_path):
    config = make_config(tmp_path)
    config["workspaces"]["acme"]["path"] = config["working_dir"]
    t = load_teams(config)
    assert "acme" in t.workspace_errors


def test_unrestricted_workspace_may_reuse_working_dir(tmp_path):
    config = make_config(tmp_path)
    config["workspaces"]["ops"]["path"] = config["working_dir"]
    t = load_teams(config)
    assert "ops" not in t.workspace_errors


def test_missing_default_provider_is_workspace_error(tmp_path):
    config = make_config(tmp_path)
    del config["workspaces"]["acme"]["default_provider"]
    t = load_teams(config)
    assert "acme" in t.workspace_errors


def test_default_provider_outside_list_is_workspace_error(tmp_path):
    config = make_config(tmp_path)
    config["workspaces"]["acme"]["default_provider"] = "codex"
    t = load_teams(config)
    assert "acme" in t.workspace_errors


def test_route_with_unknown_group_is_disabled(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"]["allow"] = ["team", "ghosts"]
    t = load_teams(config)
    assert t.dispatchable
    assert "slack.channel.C0ACME" in t.route_errors


def test_route_with_unknown_workspace_is_disabled(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["dms"]["owner"]["workspace"] = "missing"
    t = load_teams(config)
    assert "slack.dm.owner" in t.route_errors


def test_route_missing_allow_is_reported(tmp_path):
    config = make_config(tmp_path)
    del config["routes"]["slack"]["channels"]["C0ACME"]["allow"]
    t = load_teams(config)
    assert "slack.channel.C0ACME" in t.route_errors


def test_bad_context_from_is_route_error(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"]["context_from"] = "anyone"
    t = load_teams(config)
    assert "slack.channel.C0ACME" in t.route_errors


# -- resolve --


def test_resolve_authorized_channel(tmp_path):
    t = load_teams(make_config(tmp_path))
    d = resolve(t, user_id="U02DEV", channel_id="C0ACME")
    assert d.status == "authorized"
    assert d.route.route_id == "slack.channel.C0ACME"
    assert d.groups == ("team",)
    assert d.authorized_groups == ("team",)


def test_resolve_authorized_dm(tmp_path):
    t = load_teams(make_config(tmp_path))
    d = resolve(t, user_id="U01ADMIN", channel_id=None)
    assert d.status == "authorized"
    assert d.route.route_id == "slack.dm.owner"


def test_resolve_unknown_user_is_silent(tmp_path):
    t = load_teams(make_config(tmp_path))
    d = resolve(t, user_id="UEVIL", channel_id="C0ACME")
    assert d.status == "silent"
    assert d.reason == "unknown_user"


def test_resolve_unrouted_channel_is_silent(tmp_path):
    t = load_teams(make_config(tmp_path))
    d = resolve(t, user_id="U01ADMIN", channel_id="CPRIVATE")
    assert d.status == "silent"
    assert d.reason == "no_route"


def test_resolve_known_user_not_in_allow_is_silent(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["channels"]["C0ACME"]["allow"] = ["admin"]
    t = load_teams(config)
    d = resolve(t, user_id="U02DEV", channel_id="C0ACME")
    assert d.status == "silent"
    assert d.reason == "not_allowed"


def test_resolve_dm_without_matching_route_is_silent(tmp_path):
    t = load_teams(make_config(tmp_path))
    d = resolve(t, user_id="U02DEV", channel_id=None)  # team has no DM route
    assert d.status == "silent"
    assert d.reason == "no_route"


def test_resolve_disabled_route_errors_for_authorized_user(tmp_path):
    config = make_config(tmp_path)
    config["routes"]["slack"]["dms"]["owner"]["workspace"] = "missing"
    t = load_teams(config)
    d = resolve(t, user_id="U01ADMIN", channel_id=None)
    assert d.status == "error"
    assert d.reason == "route_unusable"


def test_resolve_workspace_error_makes_route_unusable(tmp_path):
    config = make_config(tmp_path)
    del config["workspaces"]["acme"]["default_provider"]
    t = load_teams(config)
    d = resolve(t, user_id="U02DEV", channel_id="C0ACME")
    assert d.status == "error"
    assert d.reason == "route_unusable"


def test_resolve_global_error_is_silent_for_unknown(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    t = load_teams(config)
    d = resolve(t, user_id="UEVIL", channel_id="C0ACME")
    assert d.status == "silent"


def test_resolve_global_error_reports_to_authorized(tmp_path):
    config = make_config(tmp_path)
    config["transports"]["slack"]["allowed_users"] = ["U1"]
    t = load_teams(config)
    d = resolve(t, user_id="U02DEV", channel_id="C0ACME")
    assert d.status == "error"
    assert d.reason == "teams_config_invalid"


# -- binding_revision --


def test_binding_revision_is_stable(tmp_path):
    config = make_config(tmp_path)
    t1 = load_teams(config)
    t2 = load_teams(make_config(tmp_path))
    route = t1.channel_routes["C0ACME"]
    assert binding_revision(t1, route) == binding_revision(t2, t2.channel_routes["C0ACME"])
    assert len(binding_revision(t1, route)) == 64


def test_binding_revision_changes_with_allow(tmp_path):
    config = make_config(tmp_path)
    t1 = load_teams(config)
    config["routes"]["slack"]["channels"]["C0ACME"]["allow"] = ["admin"]
    t2 = load_teams(config)
    assert binding_revision(t1, t1.channel_routes["C0ACME"]) != binding_revision(
        t2, t2.channel_routes["C0ACME"]
    )


def test_binding_revision_changes_with_group_membership(tmp_path):
    config = make_config(tmp_path)
    t1 = load_teams(config)
    config["groups"]["team"]["slack"].append("U9NEW")
    t2 = load_teams(config)
    assert binding_revision(t1, t1.channel_routes["C0ACME"]) != binding_revision(
        t2, t2.channel_routes["C0ACME"]
    )


def test_binding_revision_ignores_unrelated_route(tmp_path):
    config = make_config(tmp_path)
    t1 = load_teams(config)
    config["routes"]["slack"]["dms"]["owner"]["audit"] = True
    t2 = load_teams(config)
    assert binding_revision(t1, t1.channel_routes["C0ACME"]) == binding_revision(
        t2, t2.channel_routes["C0ACME"]
    )


# -- memberships --


def test_memberships_collects_all_groups(tmp_path):
    config = make_config(tmp_path)
    config["groups"]["team"]["slack"].append("U01ADMIN")
    config["routes"]["slack"]["dms"]["owner"]["allow"] = ["admin"]
    t = load_teams(config)
    assert teams.memberships(t, "U01ADMIN") == ("admin", "team")


def test_group_declaration_order_has_no_effect(tmp_path):
    """Reversing group declaration order changes nothing about authorization."""
    config = make_config(tmp_path)
    config["groups"] = dict(reversed(list(config["groups"].items())))
    t = load_teams(config)
    d = resolve(t, user_id="U02DEV", channel_id="C0ACME")
    assert d.status == "authorized"
    assert d.authorized_groups == ("team",)


@pytest.mark.parametrize("bad", [None, "x", 5, ["U1"]])
def test_malformed_workspaces_block_disables_dispatch(tmp_path, bad):
    config = make_config(tmp_path)
    config["workspaces"] = bad
    assert not load_teams(config).dispatchable
