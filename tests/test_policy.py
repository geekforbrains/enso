"""Tests for native policy selection, validation, and launch construction."""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

import pytest

from enso import policy
from enso.providers.claude import ClaudeProvider
from enso.providers.codex import CodexProvider
from enso.teams import Policy, Workspace, load_catalog

CLAUDE_SETTINGS = {
    "permissions": {"deny": ["Bash(enso *)"]},
    "sandbox": {"enabled": True},
    "disableAllHooks": True,
}

CODEX_CONFIG = 'default_permissions = "enso"\n\n[permissions.enso.network]\nenabled = false\n'

GROK_CONFIG = (
    "[permission]\n"
    'allow = ["run_terminal_command(echo *)"]\n'
    'deny = ["run_terminal_command(enso *)"]\n'
)

# The stanza the Grok CLI appends to config.toml after a run; staging
# pre-seeds it so the published snapshot bytes stay stable across runs.
GROK_MARKETPLACE = (
    "[marketplace]\n"
    "default_skills_installs_purged = true\n"
    "official_marketplace_auto_installed = true\n"
    "\n"
    "[[marketplace.sources]]\n"
    'name = "xAI Official"\n'
    'git = "https://github.com/xai-org/plugin-marketplace.git"\n'
)

CLAUDE_MCP = {
    "mcpServers": {
        "metrics": {
            "type": "http",
            "url": "https://metrics.internal.example/mcp",
            "headers": {"Authorization": "${METRICS_API_TOKEN}"},
        }
    }
}


def make_workspace(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(exist_ok=True)
    return Workspace(
        name="acme",
        path=str(ws_dir),
        policy="standard",
        concurrency=1,
    )


def make_policy(
    tmp_path, *, unrestricted=False, providers=("claude", "codex"), env_passthrough=()
):
    policy_dir = tmp_path / "policies"
    return Policy(
        name="standard",
        policy_dir=None if unrestricted else str(policy_dir),
        unrestricted=unrestricted,
        providers=tuple(providers),
        default_provider=providers[0] if providers else None,
        chat_commands=(),
        env_passthrough=tuple(env_passthrough),
    )


def write_claude_policy(tmp_path, content=None) -> str:
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(CLAUDE_SETTINGS if content is None else content))
    path.chmod(0o600)
    return str(path)


def write_codex_policy(tmp_path, content=CODEX_CONFIG) -> str:
    path = tmp_path / "policies" / "codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)
    return str(path)


def write_grok_policy(tmp_path, content=GROK_CONFIG) -> str:
    path = tmp_path / "policies" / "grok" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)
    return str(path)


def write_claude_mcp(tmp_path, content=None) -> str:
    path = tmp_path / "policies" / "claude" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = CLAUDE_MCP if content is None else content
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    path.chmod(0o600)
    return str(path)


# -- check_provider --


def test_unrestricted_workspace_passes_all_providers(tmp_path):
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path, unrestricted=True, providers=("claude", "codex", "agy"))
    for provider in access.providers:
        check = policy.check_provider(workspace, access, provider)
        assert check.ok, check.problems
        assert check.policy_revision == policy.UNRESTRICTED_REVISION


def test_missing_policy_file_fails(tmp_path):
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("settings.json" in p for p in check.problems)


def test_agy_requires_unrestricted(tmp_path):
    check = policy.check_provider(
        make_workspace(tmp_path),
        make_policy(tmp_path, providers=("claude", "agy")),
        "agy",
    )
    assert not check.ok
    assert any("unrestricted" in p for p in check.problems)


def test_valid_claude_policy_passes(tmp_path):
    write_claude_policy(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.policy_revision
    assert len(check.policy_revision) == 64


def test_invalid_claude_json_fails(tmp_path):
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    path.chmod(0o600)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok


def test_group_readable_policy_fails(tmp_path):
    path = write_claude_policy(tmp_path)
    os.chmod(path, 0o644)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("owner-only" in p for p in check.problems)


def test_symlinked_policy_fails(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text("{}")
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(real)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok


def test_hard_linked_policy_fails(tmp_path):
    workspace = make_workspace(tmp_path)
    path = write_claude_policy(tmp_path)
    os.link(path, tmp_path / "ws" / "writable-policy-link.json")
    check = policy.check_provider(workspace, make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("hard links" in problem for problem in check.problems)


def test_policy_resolving_into_workspace_fails(tmp_path):
    workspace = make_workspace(tmp_path)
    inside = tmp_path / "ws" / "settings.json"
    inside.write_text("{}")
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(inside)
    check = policy.check_provider(workspace, make_policy(tmp_path), "claude")
    assert not check.ok


def test_claude_without_sandbox_warns(tmp_path):
    write_claude_policy(tmp_path, {"permissions": {"deny": []}, "disableAllHooks": True})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok
    assert any("sandbox" in w for w in check.warnings)


def test_valid_codex_policy_passes(tmp_path):
    write_codex_policy(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "codex")
    assert check.ok, check.problems


def test_codex_mixed_sandbox_and_permissions_fails(tmp_path):
    write_codex_policy(tmp_path, 'sandbox_mode = "workspace-write"\n' + CODEX_CONFIG)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "codex")
    assert not check.ok
    assert any("legacy sandbox" in p for p in check.problems)


def test_codex_pure_legacy_sandbox_is_allowed(tmp_path):
    """The operator's file is authoritative; a pure-legacy sandbox config is theirs."""
    write_codex_policy(tmp_path, 'sandbox_mode = "workspace-write"\n')
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "codex")
    assert check.ok, check.problems


def test_codex_invalid_toml_fails(tmp_path):
    write_codex_policy(tmp_path, "= not toml")
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "codex")
    assert not check.ok


def test_codex_policy_cannot_override_shared_developer_instructions(tmp_path):
    write_codex_policy(
        tmp_path,
        'developer_instructions = "policy-local override"\n' + CODEX_CONFIG,
    )

    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "codex")

    assert not check.ok
    assert any(
        "developer_instructions" in problem and "reserved" in problem
        for problem in check.problems
    )


def test_valid_grok_policy_passes(tmp_path):
    write_grok_policy(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "grok")
    assert check.ok, check.problems
    assert check.policy_revision
    assert len(check.policy_revision) == 64


def test_grok_invalid_toml_fails(tmp_path):
    write_grok_policy(tmp_path, "= not toml")
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "grok")
    assert not check.ok


def test_grok_config_requires_a_permission_table(tmp_path):
    """grok silently loads zero rules from a config without [permission]."""
    write_grok_policy(tmp_path, 'model = "grok-4.6"\n')
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "grok")
    assert not check.ok
    assert any("[permission]" in p for p in check.problems)


@pytest.mark.parametrize(
    "content",
    [
        # Table-name typo: [permissions] loads 0 rules with no error.
        '[permissions]\nallow = ["run_terminal_command(echo *)"]\n',
        # Key misspell: allowed= is silently ignored.
        '[permission]\nallowed = ["run_terminal_command(echo *)"]\n',
        # Wrong shape: rules must be an array, not a string.
        '[permission]\nrules = "run_terminal_command(echo *)"\n',
        # Unknown permission-table keys are dropped without a diagnostic.
        '[permission]\nallow = ["run_terminal_command(echo *)"]\nfrobnicate = ["x"]\n',
        # Empty arrays are well-shaped but load zero rules: wide open under
        # dontAsk, the exact state the gate exists to catch.
        '[permission]\nallow = []\n',
        '[permission]\nallow = []\ndeny = []\n',
        '[permission]\nrules = []\n',
    ],
)
def test_grok_fail_open_config_shapes_are_static_problems(tmp_path, content):
    """Shapes the CLI accepts while loading zero rules must fail the check."""
    write_grok_policy(tmp_path, content)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "grok")
    assert not check.ok
    assert any("permission" in p for p in check.problems)


def test_grok_config_with_own_marketplace_stanza_warns(tmp_path):
    """The CLI's post-run write-back would mutate an operator-authored
    [marketplace] stanza and fail the next snapshot verification."""
    write_grok_policy(tmp_path, GROK_CONFIG + "\n" + GROK_MARKETPLACE)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "grok")
    assert check.ok, check.problems
    assert any("marketplace" in w for w in check.warnings)


def test_unknown_provider_fails(tmp_path):
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "mystery")
    assert not check.ok


# -- claude mcp.json --


def test_absent_mcp_file_means_no_servers(tmp_path):
    write_claude_policy(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.mcp_servers == ()


def test_valid_mcp_file_resolves_sorted_server_names(tmp_path):
    write_claude_policy(tmp_path)
    write_claude_mcp(
        tmp_path,
        {"mcpServers": {"tickets": {"type": "http"}, "metrics": {"type": "http"}}},
    )
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.mcp_servers == ("metrics", "tickets")


def test_symlinked_mcp_file_fails(tmp_path):
    write_claude_policy(tmp_path)
    real = tmp_path / "elsewhere-mcp.json"
    real.write_text(json.dumps(CLAUDE_MCP))
    path = tmp_path / "policies" / "claude" / "mcp.json"
    path.symlink_to(real)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("mcp.json must be a regular file, not a symlink" in p for p in check.problems)


def test_group_readable_mcp_file_fails(tmp_path):
    write_claude_policy(tmp_path)
    path = write_claude_mcp(tmp_path)
    os.chmod(path, 0o644)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("mcp.json must be owner-only" in p for p in check.problems)


def test_unparseable_mcp_file_fails(tmp_path):
    write_claude_policy(tmp_path)
    write_claude_mcp(tmp_path, "{not json")
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("mcp.json does not parse" in p for p in check.problems)


def test_non_object_mcp_file_fails(tmp_path):
    write_claude_policy(tmp_path)
    write_claude_mcp(tmp_path, ["mcpServers"])
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("mcp.json must be a JSON object" in p for p in check.problems)


def test_mcp_file_without_mcp_servers_fails(tmp_path):
    write_claude_policy(tmp_path)
    write_claude_mcp(tmp_path, {})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any('must define "mcpServers" as an object' in p for p in check.problems)


def test_empty_mcp_servers_fails_with_delete_guidance(tmp_path):
    write_claude_policy(tmp_path)
    write_claude_mcp(tmp_path, {"mcpServers": {}})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("delete the file to disable MCP" in p for p in check.problems)


# -- mcp permission-rule and secret-literal warnings --


def test_mcp_rule_without_mcp_file_warns_inert_rule(tmp_path):
    write_claude_policy(
        tmp_path, {**CLAUDE_SETTINGS, "permissions": {"allow": ["mcp__ghost__tool"]}}
    )
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert any(
        'permission rule "mcp__ghost__tool" matches no MCP server' in w for w in check.warnings
    )


def test_mcp_rule_matching_a_defined_server_does_not_warn(tmp_path):
    write_claude_policy(
        tmp_path, {**CLAUDE_SETTINGS, "permissions": {"allow": ["mcp__metrics__query"]}}
    )
    write_claude_mcp(tmp_path, {"mcpServers": {"metrics": {"type": "http"}}})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.warnings == ()


def test_mcp_rule_matches_server_names_containing_underscores(tmp_path):
    # A double underscore inside the server name defeats any split-based
    # matching; only whole-known-name matching keeps this warning-free.
    write_claude_policy(
        tmp_path, {**CLAUDE_SETTINGS, "permissions": {"allow": ["mcp__my__server__tool"]}}
    )
    write_claude_mcp(tmp_path, {"mcpServers": {"my__server": {"type": "http"}}})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.warnings == ()


def test_unreferenced_mcp_server_warns(tmp_path):
    write_claude_policy(tmp_path)
    write_claude_mcp(tmp_path, {"mcpServers": {"metrics": {"type": "http"}}})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert any(
        'no allow rule references MCP server "metrics"' in w for w in check.warnings
    )


def test_deny_only_mcp_server_reference_still_warns(tmp_path):
    # A deny (or ask) rule cannot admit a tool under dontAsk, so a server
    # referenced only there is as unusable as one never referenced at all.
    write_claude_policy(
        tmp_path, {**CLAUDE_SETTINGS, "permissions": {"deny": ["mcp__metrics"]}}
    )
    write_claude_mcp(tmp_path, {"mcpServers": {"metrics": {"type": "http"}}})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert any(
        'no allow rule references MCP server "metrics"' in w for w in check.warnings
    )


def test_secret_shaped_literal_in_mcp_headers_warns(tmp_path):
    write_claude_policy(
        tmp_path, {**CLAUDE_SETTINGS, "permissions": {"allow": ["mcp__tickets__list"]}}
    )
    write_claude_mcp(
        tmp_path,
        {
            "mcpServers": {
                "tickets": {
                    "type": "http",
                    "headers": {
                        "Authorization": "Bearer abc123",
                        "Content-Type": "application/json",
                    },
                }
            }
        },
    )
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.warnings == (
        'mcp.json server "tickets" has a secret-shaped literal in '
        "headers.Authorization; use a ${VAR} reference",
    )


def test_env_reference_in_mcp_headers_does_not_warn(tmp_path):
    write_claude_policy(
        tmp_path, {**CLAUDE_SETTINGS, "permissions": {"allow": ["mcp__metrics__query"]}}
    )
    write_claude_mcp(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.warnings == ()


def test_secret_shaped_literal_in_mcp_env_warns(tmp_path):
    write_claude_policy(
        tmp_path, {**CLAUDE_SETTINGS, "permissions": {"allow": ["mcp__tickets__list"]}}
    )
    write_claude_mcp(
        tmp_path,
        {
            "mcpServers": {
                "tickets": {
                    "type": "stdio",
                    "command": "tickets-mcp",
                    "env": {"API_KEY": "literal-secret"},
                }
            }
        },
    )
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems
    assert any("env.API_KEY" in w and "${VAR}" in w for w in check.warnings)


# -- policy_revision --


def test_revision_changes_with_policy_bytes(tmp_path):
    write_claude_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    first = policy.check_provider(workspace, access, "claude").policy_revision
    write_claude_policy(tmp_path, {"permissions": {"deny": ["WebFetch"]}, "disableAllHooks": True})
    second = policy.check_provider(workspace, access, "claude").policy_revision
    assert first != second


def test_codex_revision_covers_rules_files(tmp_path):
    write_codex_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    first = policy.check_provider(workspace, access, "codex").policy_revision
    rules = tmp_path / "policies" / "codex" / "rules"
    rules.mkdir()
    rules_file = rules / "enso.rules"
    rules_file.write_text('prefix_rule(pattern=["enso"], decision="forbidden")\n')
    rules_file.chmod(0o600)
    second = policy.check_provider(workspace, access, "codex").policy_revision
    assert first != second


def test_codex_revision_covers_relative_profile_files(tmp_path):
    write_codex_policy(tmp_path)
    agent_config = tmp_path / "policies" / "codex" / "agents" / "reviewer.toml"
    agent_config.parent.mkdir()
    agent_config.write_text('model = "gpt-5.6-terra"\n')
    agent_config.chmod(0o600)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    first = policy.check_provider(workspace, access, "codex").policy_revision
    agent_config.write_text('model = "gpt-5.6-sol"\n')
    second = policy.check_provider(workspace, access, "codex").policy_revision
    assert first != second


def test_revision_rotates_on_mcp_file_add_edit_and_remove(tmp_path):
    write_claude_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    base = policy.check_provider(workspace, access, "claude").policy_revision

    mcp_path = write_claude_mcp(tmp_path, {"mcpServers": {"metrics": {"type": "http"}}})
    added = policy.check_provider(workspace, access, "claude").policy_revision
    assert added != base

    write_claude_mcp(tmp_path, {"mcpServers": {"tickets": {"type": "http"}}})
    edited = policy.check_provider(workspace, access, "claude").policy_revision
    assert edited != added
    assert edited != base

    os.remove(mcp_path)
    removed = policy.check_provider(workspace, access, "claude").policy_revision
    assert removed == base


def test_launch_contract_is_version_five():
    assert policy.LAUNCH_CONTRACT_VERSION == "5"
    assert policy.UNRESTRICTED_REVISION == "unrestricted:v5"


def test_hard_linked_codex_rule_fails(tmp_path):
    workspace = make_workspace(tmp_path)
    write_codex_policy(tmp_path)
    rules = tmp_path / "policies" / "codex" / "rules"
    rules.mkdir()
    rules_file = rules / "enso.rules"
    rules_file.write_text('prefix_rule(pattern=["enso"], decision="forbidden")\n')
    rules_file.chmod(0o600)
    os.link(rules_file, tmp_path / "ws" / "writable-rule-link.rules")
    check = policy.check_provider(workspace, make_policy(tmp_path), "codex")
    assert not check.ok
    assert any("hard links" in problem for problem in check.problems)


# -- prepare_launch --


def test_prepare_launch_unrestricted(tmp_path):
    launch = policy.prepare_launch(
        make_workspace(tmp_path),
        make_policy(tmp_path, unrestricted=True, providers=("claude",)),
        "claude",
    )
    assert launch.mode == "unrestricted"
    assert launch.env is None


def test_prepare_launch_fails_closed(tmp_path):
    with pytest.raises(policy.PolicyError):
        policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "claude")


def test_claude_launch_has_policy_and_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "sekret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "sekret2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthro")
    path = write_claude_policy(tmp_path)
    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert launch.mode == "policy"
    assert launch.policy_path == path
    assert launch.env is not None
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in launch.env
    assert "SLACK_BOT_TOKEN" not in launch.env
    assert launch.env.get("ANTHROPIC_API_KEY") == "anthro"
    assert "HOME" in launch.env and "PATH" in launch.env


def test_enso_bin_dir_is_stripped_from_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "enso").write_text("#!/bin/sh\n")
    (bin_dir / "enso").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    write_claude_policy(tmp_path)
    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert str(bin_dir) not in launch.env["PATH"].split(":")
    assert "/usr/bin" in launch.env["PATH"].split(":")


def test_env_passthrough_name_is_copied_into_launch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("METRICS_API_TOKEN", "tok-value")
    write_claude_policy(tmp_path)
    access = make_policy(tmp_path, env_passthrough=("METRICS_API_TOKEN",))
    launch = policy.prepare_launch(make_workspace(tmp_path), access, "claude")
    assert launch.env["METRICS_API_TOKEN"] == "tok-value"


def test_absent_env_passthrough_name_is_skipped_and_warned(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("ABSENT_METRICS_TOKEN", raising=False)
    write_claude_policy(tmp_path)
    access = make_policy(tmp_path, env_passthrough=("ABSENT_METRICS_TOKEN",))
    with caplog.at_level(logging.WARNING, logger="enso.policy"):
        launch = policy.prepare_launch(make_workspace(tmp_path), access, "claude")
    assert "ABSENT_METRICS_TOKEN" not in launch.env
    assert (
        "env_passthrough names not set in the service environment: ABSENT_METRICS_TOKEN"
        in caplog.text
    )


def test_passthrough_naming_path_cannot_displace_the_filtered_path(tmp_path, monkeypatch):
    """Validation rejects PATH; a bypassed validator still must not win over the launch."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "enso").write_text("#!/bin/sh\n")
    (bin_dir / "enso").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    write_claude_policy(tmp_path)
    access = make_policy(tmp_path, env_passthrough=("PATH",))
    launch = policy.prepare_launch(make_workspace(tmp_path), access, "claude")
    assert launch.env["PATH"] != os.environ["PATH"]
    assert str(bin_dir) not in launch.env["PATH"].split(":")


def test_claude_launch_without_mcp_file_has_no_mcp_config(tmp_path):
    write_claude_policy(tmp_path)
    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert launch.mcp_config is None
    cmd = ClaudeProvider("claude").build_command("hi", "opus", launch=launch)
    assert "--mcp-config" not in cmd
    assert "--strict-mcp-config" in cmd


def test_claude_launch_with_mcp_file_passes_it_as_mcp_config(tmp_path):
    write_claude_policy(tmp_path)
    mcp_path = write_claude_mcp(tmp_path)
    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert launch.mcp_config == mcp_path
    cmd = ClaudeProvider("claude").build_command("hi", "opus", launch=launch)
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--mcp-config") + 1] == mcp_path


def test_prepare_launch_logs_capability_set_without_values(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("METRICS_API_TOKEN", "sekret-value")
    monkeypatch.delenv("ABSENT_METRICS_TOKEN", raising=False)
    write_claude_policy(tmp_path)
    write_claude_mcp(tmp_path)
    access = make_policy(
        tmp_path, env_passthrough=("METRICS_API_TOKEN", "ABSENT_METRICS_TOKEN")
    )
    with caplog.at_level(logging.INFO, logger="enso.policy"):
        policy.prepare_launch(make_workspace(tmp_path), access, "claude")
    assert (
        "Policy launch for claude: MCP servers [metrics], passthrough [METRICS_API_TOKEN]"
        in caplog.text
    )
    assert "sekret-value" not in caplog.text


def test_codex_launch_stages_isolated_home(tmp_path, monkeypatch):
    src = write_codex_policy(tmp_path)
    fake_codex_home = tmp_path / "codex-user-home"
    fake_codex_home.mkdir()
    (fake_codex_home / "auth.json").write_text('{"tokens": "x"}')
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(fake_codex_home))
    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "codex")
    assert launch.home is not None
    staged = os.path.join(launch.home, "config.toml")
    with open(staged) as f_staged, open(src) as f_src:
        assert f_staged.read() == f_src.read()
    assert os.path.exists(os.path.join(launch.home, "auth.json"))
    assert launch.env["CODEX_HOME"] == launch.home
    assert launch.ignore_rules is True


def test_codex_launch_stages_rules(tmp_path, monkeypatch):
    write_codex_policy(tmp_path)
    rules = tmp_path / "policies" / "codex" / "rules"
    rules.mkdir()
    rules_file = rules / "enso.rules"
    rules_file.write_text("x = 1\n")
    rules_file.chmod(0o600)
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(tmp_path / "nope"))
    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "codex")
    assert launch.ignore_rules is False
    assert os.path.exists(os.path.join(launch.home, "rules", "enso.rules"))


def test_codex_launch_copies_relative_profile_files(tmp_path, monkeypatch):
    write_codex_policy(
        tmp_path,
        CODEX_CONFIG + '\n[agents.reviewer]\nconfig_file = "agents/reviewer.toml"\n',
    )
    agent_config = tmp_path / "policies" / "codex" / "agents" / "reviewer.toml"
    agent_config.parent.mkdir()
    agent_config.write_text('model = "gpt-5.6-terra"\n')
    agent_config.chmod(0o600)
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(tmp_path / "nope"))

    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "codex")

    assert launch.home is not None
    staged_agent = os.path.join(launch.home, "agents", "reviewer.toml")
    with open(staged_agent) as file:
        assert file.read() == 'model = "gpt-5.6-terra"\n'


def test_codex_policy_snapshots_are_revision_keyed_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(tmp_path / "nope"))
    source = write_codex_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    first = policy.prepare_launch(workspace, access, "codex")
    with open(os.path.join(first.home, "config.toml")) as file:
        first_bytes = file.read()

    write_codex_policy(
        tmp_path,
        CODEX_CONFIG.replace("enabled = false", "enabled = true"),
    )
    second = policy.prepare_launch(workspace, access, "codex")

    assert first.home != second.home
    assert os.path.basename(first.home) == first.policy_revision
    assert os.path.basename(second.home) == second.policy_revision
    with open(os.path.join(first.home, "config.toml")) as file:
        assert file.read() == first_bytes
    with open(source) as file:
        assert file.read() != first_bytes


def test_codex_rejects_a_mutated_published_snapshot(tmp_path, monkeypatch):
    write_codex_policy(tmp_path)
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(tmp_path / "nope"))
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    launch = policy.prepare_launch(workspace, access, "codex")
    staged_config = os.path.join(launch.home, "config.toml")
    os.chmod(staged_config, 0o600)
    with open(staged_config, "w") as file:
        file.write(CODEX_CONFIG.replace("enabled = false", "enabled = true"))

    with pytest.raises(policy.PolicyError, match="digest does not match"):
        policy.prepare_launch(workspace, access, "codex")


def test_codex_auth_refresh_is_atomic_within_revision_home(tmp_path, monkeypatch):
    write_codex_policy(tmp_path)
    user_home = tmp_path / "codex-user-home"
    user_home.mkdir()
    auth = user_home / "auth.json"
    auth.write_text('{"token":"first"}')
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(user_home))
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    first = policy.prepare_launch(workspace, access, "codex")

    auth.write_text('{"token":"second"}')
    second = policy.prepare_launch(workspace, access, "codex")

    assert second.home == first.home
    with open(os.path.join(second.home, "auth.json")) as file:
        assert file.read() == '{"token":"second"}'
    assert not [name for name in os.listdir(second.home) if name.startswith(".auth-")]


def test_concurrent_codex_snapshot_publish_has_one_complete_winner(tmp_path, monkeypatch):
    write_codex_policy(tmp_path)
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(tmp_path / "nope"))
    original_copy = policy._copy_codex_tree
    copies_ready = threading.Barrier(2)

    def synchronized_copy(source, destination):
        original_copy(source, destination)
        copies_ready.wait(timeout=5)

    monkeypatch.setattr(policy, "_copy_codex_tree", synchronized_copy)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        launches = list(
            executor.map(
                lambda _index: policy.prepare_launch(workspace, access, "codex"),
                range(2),
            )
        )

    assert launches[0].home == launches[1].home
    assert os.path.exists(os.path.join(launches[0].home, "config.toml"))
    snapshot_parent = os.path.dirname(launches[0].home)
    assert sorted(os.listdir(snapshot_parent)) == [launches[0].policy_revision]


# -- grok staged GROK_HOME --


def test_grok_launch_stages_isolated_home(tmp_path, monkeypatch):
    src = write_grok_policy(tmp_path)
    fake_grok_home = tmp_path / "grok-user-home"
    fake_grok_home.mkdir()
    (fake_grok_home / "auth.json").write_text('{"session": "x"}')
    monkeypatch.setattr(policy, "_user_grok_home", lambda: str(fake_grok_home))
    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "grok")
    assert launch.home is not None
    assert os.path.basename(os.path.dirname(launch.home)) == "grok-home"
    staged = os.path.join(launch.home, "config.toml")
    with open(staged) as f_staged, open(src) as f_src:
        assert f_staged.read().startswith(f_src.read())
    assert os.path.exists(os.path.join(launch.home, "auth.json"))
    assert launch.env["GROK_HOME"] == launch.home
    assert launch.ignore_rules is True


def test_grok_staging_preseeds_the_marketplace_stanza(tmp_path, monkeypatch):
    """The CLI appends [marketplace] to config.toml after a run; pre-seeding
    it at staging time keeps the published bytes stable, and the manifest
    hashes those effective bytes so the next launch still verifies."""
    write_grok_policy(tmp_path)
    monkeypatch.setattr(policy, "_user_grok_home", lambda: str(tmp_path / "nope"))
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    first = policy.prepare_launch(workspace, access, "grok")

    with open(os.path.join(first.home, "config.toml"), "rb") as file:
        staged = tomllib.load(file)
    assert staged["permission"] == {
        "allow": ["run_terminal_command(echo *)"],
        "deny": ["run_terminal_command(enso *)"],
    }
    assert staged["marketplace"]["default_skills_installs_purged"] is True
    assert staged["marketplace"]["official_marketplace_auto_installed"] is True
    assert staged["marketplace"]["sources"] == [
        {"name": "xAI Official", "git": "https://github.com/xai-org/plugin-marketplace.git"}
    ]
    # Byte-exact: the CLI's write-back is only defeated when the staged bytes
    # already match what it would produce, so pin them, not TOML semantics.
    with open(os.path.join(first.home, "config.toml"), "rb") as file:
        staged_bytes = file.read()
    assert staged_bytes == GROK_CONFIG.encode() + b"\n" + GROK_MARKETPLACE.encode()

    second = policy.prepare_launch(workspace, access, "grok")
    assert second.home == first.home


def test_grok_config_with_own_marketplace_stanza_stages_verbatim(tmp_path, monkeypatch):
    """An operator-authored [marketplace] is theirs; staging must not touch it."""
    src = write_grok_policy(tmp_path, GROK_CONFIG + "\n" + GROK_MARKETPLACE)
    monkeypatch.setattr(policy, "_user_grok_home", lambda: str(tmp_path / "nope"))
    launch = policy.prepare_launch(make_workspace(tmp_path), make_policy(tmp_path), "grok")
    staged = os.path.join(launch.home, "config.toml")
    with open(staged) as f_staged, open(src) as f_src:
        assert f_staged.read() == f_src.read()


def test_grok_policy_snapshots_are_revision_keyed_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_user_grok_home", lambda: str(tmp_path / "nope"))
    source = write_grok_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    first = policy.prepare_launch(workspace, access, "grok")
    with open(os.path.join(first.home, "config.toml")) as file:
        first_bytes = file.read()

    write_grok_policy(tmp_path, GROK_CONFIG.replace("echo *", "date *"))
    second = policy.prepare_launch(workspace, access, "grok")

    assert first.home != second.home
    assert os.path.basename(first.home) == first.policy_revision
    assert os.path.basename(second.home) == second.policy_revision
    with open(os.path.join(first.home, "config.toml")) as file:
        assert file.read() == first_bytes
    with open(source) as file:
        assert file.read() != first_bytes


def test_grok_rejects_a_mutated_published_snapshot(tmp_path, monkeypatch):
    write_grok_policy(tmp_path)
    monkeypatch.setattr(policy, "_user_grok_home", lambda: str(tmp_path / "nope"))
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    launch = policy.prepare_launch(workspace, access, "grok")
    staged_config = os.path.join(launch.home, "config.toml")
    os.chmod(staged_config, 0o600)
    # Mimic the CLI's write-back: append to the staged config in place.
    with open(staged_config, "a") as file:
        file.write('\n[[marketplace.sources]]\nname = "rogue"\ngit = "https://x.example/r.git"\n')

    with pytest.raises(policy.PolicyError, match="digest does not match"):
        policy.prepare_launch(workspace, access, "grok")


def test_grok_auth_refresh_is_atomic_within_revision_home(tmp_path, monkeypatch):
    write_grok_policy(tmp_path)
    user_home = tmp_path / "grok-user-home"
    user_home.mkdir()
    auth = user_home / "auth.json"
    auth.write_text('{"token":"first"}')
    monkeypatch.setattr(policy, "_user_grok_home", lambda: str(user_home))
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    first = policy.prepare_launch(workspace, access, "grok")

    auth.write_text('{"token":"second"}')
    second = policy.prepare_launch(workspace, access, "grok")

    assert second.home == first.home
    with open(os.path.join(second.home, "auth.json")) as file:
        assert file.read() == '{"token":"second"}'
    assert not [name for name in os.listdir(second.home) if name.startswith(".auth-")]


def test_concurrent_grok_snapshot_publish_has_one_complete_winner(tmp_path, monkeypatch):
    # Staging shares the codex copy helper; synchronizing it drives both
    # threads into publishing the same grok revision at once.
    write_grok_policy(tmp_path)
    monkeypatch.setattr(policy, "_user_grok_home", lambda: str(tmp_path / "nope"))
    original_copy = policy._copy_codex_tree
    copies_ready = threading.Barrier(2)

    def synchronized_copy(source, destination):
        original_copy(source, destination)
        copies_ready.wait(timeout=5)

    monkeypatch.setattr(policy, "_copy_codex_tree", synchronized_copy)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        launches = list(
            executor.map(
                lambda _index: policy.prepare_launch(workspace, access, "grok"),
                range(2),
            )
        )

    assert launches[0].home == launches[1].home
    assert os.path.exists(os.path.join(launches[0].home, "config.toml"))
    snapshot_parent = os.path.dirname(launches[0].home)
    assert sorted(os.listdir(snapshot_parent)) == [launches[0].policy_revision]


def test_grok_staging_failure_raises_policy_error(tmp_path, monkeypatch):
    """A read-only policy dir must refuse the turn, not escape as PermissionError."""
    write_grok_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path, providers=("grok",))

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(policy.os, "makedirs", boom)
    with pytest.raises(policy.PolicyError):
        policy.prepare_launch(workspace, access, "grok")


@pytest.mark.parametrize("name", ["GROK_HOME", "GROK_SANDBOX", "GROK_FOLDER_TRUST"])
def test_env_passthrough_reserves_grok_launch_env(tmp_path, name):
    """GROK_SANDBOX and GROK_FOLDER_TRUST change kernel sandboxing and folder
    trust; all three are launch-owned and may never ride env_passthrough."""
    config = {
        "workspaces": {"acme": {"path": str(tmp_path / "ws"), "policy": "standard"}},
        "policies": {
            "standard": {
                "policy_dir": str(tmp_path / "policies"),
                "providers": ["grok"],
                "default_provider": "grok",
                "env_passthrough": [name],
            },
        },
    }
    catalog = load_catalog(config)
    assert any("reserved" in p for p in catalog.policy_errors["standard"])


# -- provider command construction --


def _policy_launch(**overrides):
    fields = {
        "mode": "policy",
        "provider": "claude",
        "policy_path": "/protected/claude/settings.json",
        "home": None,
        "policy_revision": "r" * 64,
        "env": {"PATH": "/usr/bin"},
        "ignore_rules": True,
    }
    fields.update(overrides)
    return policy.Launch(**fields)


def test_claude_policy_command_drops_bypass():
    provider = ClaudeProvider("claude")
    cmd = provider.build_command("hi", "opus", launch=_policy_launch())
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--settings") + 1] == "/protected/claude/settings.json"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in cmd


def test_claude_policy_command_appends_mcp_config_after_strict(tmp_path):
    provider = ClaudeProvider("claude")
    launch = _policy_launch(mcp_config="/protected/claude/mcp.json")
    cmd = provider.build_command("hi", "opus", launch=launch)
    strict = cmd.index("--strict-mcp-config")
    assert cmd[strict + 1] == "--mcp-config"
    assert cmd[strict + 2] == "/protected/claude/mcp.json"


def test_claude_policy_command_without_mcp_config_stays_strict():
    provider = ClaudeProvider("claude")
    cmd = provider.build_command("hi", "opus", launch=_policy_launch())
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" not in cmd


def test_claude_unrestricted_command_is_unchanged():
    cmd = ClaudeProvider("claude").build_command("hi", "opus")
    assert "--dangerously-skip-permissions" in cmd
    assert "--mcp-config" not in cmd


def test_claude_batch_policy_command_drops_bypass():
    provider = ClaudeProvider("claude")
    cmd = provider.build_batch_command("hi", "opus", launch=_policy_launch())
    assert "--dangerously-skip-permissions" not in cmd
    assert "--permission-mode" in cmd


def test_codex_policy_command_drops_bypass():
    provider = CodexProvider("codex")
    launch = _policy_launch(provider="codex", home="/staged", ignore_rules=True)
    cmd = provider.build_command("hi", "sol", launch=launch)
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--strict-config" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--ignore-rules" in cmd


def test_codex_policy_command_loads_staged_rules():
    provider = CodexProvider("codex")
    launch = _policy_launch(provider="codex", home="/staged", ignore_rules=False)
    cmd = provider.build_command("hi", "sol", launch=launch)
    assert "--ignore-rules" not in cmd


# -- Launch contract hardening --


def test_claude_policy_requires_disable_all_hooks(tmp_path):
    """Without it, an agent can write workspace hooks that run outside the sandbox."""
    write_claude_policy(tmp_path, {"sandbox": {"enabled": True}})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok
    assert any("disableAllHooks" in p for p in check.problems)


def test_claude_policy_rejects_disable_all_hooks_false(tmp_path):
    write_claude_policy(tmp_path, {"sandbox": {"enabled": True}, "disableAllHooks": False})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert not check.ok


def test_claude_policy_with_disable_all_hooks_passes(tmp_path):
    write_claude_policy(tmp_path, {"sandbox": {"enabled": True}, "disableAllHooks": True})
    check = policy.check_provider(make_workspace(tmp_path), make_policy(tmp_path), "claude")
    assert check.ok, check.problems


def test_unrestricted_workspace_skips_hook_requirement(tmp_path):
    assert policy.check_provider(
        make_workspace(tmp_path),
        make_policy(tmp_path, unrestricted=True, providers=("claude",)),
        "claude",
    ).ok


def test_agy_refusal_cites_missing_contract_not_missing_model(tmp_path):
    """agy does have a permission model; the honest reason is no verified contract."""
    check = policy.check_provider(
        make_workspace(tmp_path), make_policy(tmp_path, providers=("agy",)), "agy"
    )
    assert not check.ok
    joined = " ".join(check.problems)
    assert "no permission model" not in joined
    assert "unrestricted" in joined


def test_claude_launch_uses_project_setting_source():
    provider = ClaudeProvider("claude")
    cmd = provider.build_command("hi", "opus", launch=_policy_launch())
    assert cmd[cmd.index("--setting-sources") + 1] == "project"


def test_codex_staging_failure_raises_policy_error(tmp_path, monkeypatch):
    """A read-only policy dir must refuse the turn, not escape as PermissionError."""
    write_codex_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_policy(tmp_path, providers=("codex",))

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(policy.os, "makedirs", boom)
    with pytest.raises(policy.PolicyError):
        policy.prepare_launch(workspace, access, "codex")
