"""Tests for native policy selection, validation, and launch construction."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from enso import policy
from enso.providers.claude import ClaudeProvider
from enso.providers.codex import CodexProvider
from enso.teams import AccessProfile, Workspace

CLAUDE_SETTINGS = {
    "permissions": {"deny": ["Bash(enso *)"]},
    "sandbox": {"enabled": True},
    "disableAllHooks": True,
}

CODEX_CONFIG = 'default_permissions = "enso"\n\n[permissions.enso.network]\nenabled = false\n'


def make_workspace(tmp_path):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(exist_ok=True)
    return Workspace(
        name="acme",
        path=str(ws_dir),
        concurrency=1,
    )


def make_access(tmp_path, *, unrestricted=False, providers=("claude", "codex")):
    policy_dir = tmp_path / "policies"
    return AccessProfile(
        name="standard",
        policy_dir=None if unrestricted else str(policy_dir),
        unrestricted=unrestricted,
        providers=tuple(providers),
        default_provider=providers[0] if providers else None,
        chat_commands=(),
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


# -- check_provider --


def test_unrestricted_workspace_passes_all_providers(tmp_path):
    workspace = make_workspace(tmp_path)
    access = make_access(tmp_path, unrestricted=True, providers=("claude", "codex", "agy"))
    for provider in access.providers:
        check = policy.check_provider(workspace, access, provider)
        assert check.ok, check.problems
        assert check.policy_revision == policy.UNRESTRICTED_REVISION


def test_missing_policy_file_fails(tmp_path):
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert not check.ok
    assert any("settings.json" in p for p in check.problems)


def test_agy_requires_unrestricted(tmp_path):
    check = policy.check_provider(
        make_workspace(tmp_path),
        make_access(tmp_path, providers=("claude", "agy")),
        "agy",
    )
    assert not check.ok
    assert any("unrestricted" in p for p in check.problems)


def test_valid_claude_policy_passes(tmp_path):
    write_claude_policy(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.policy_revision
    assert len(check.policy_revision) == 64


def test_invalid_claude_json_fails(tmp_path):
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    path.chmod(0o600)
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert not check.ok


def test_group_readable_policy_fails(tmp_path):
    path = write_claude_policy(tmp_path)
    os.chmod(path, 0o644)
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert not check.ok
    assert any("owner-only" in p for p in check.problems)


def test_symlinked_policy_fails(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text("{}")
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(real)
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert not check.ok


def test_hard_linked_policy_fails(tmp_path):
    workspace = make_workspace(tmp_path)
    path = write_claude_policy(tmp_path)
    os.link(path, tmp_path / "ws" / "writable-policy-link.json")
    check = policy.check_provider(workspace, make_access(tmp_path), "claude")
    assert not check.ok
    assert any("hard links" in problem for problem in check.problems)


def test_policy_resolving_into_workspace_fails(tmp_path):
    workspace = make_workspace(tmp_path)
    inside = tmp_path / "ws" / "settings.json"
    inside.write_text("{}")
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(inside)
    check = policy.check_provider(workspace, make_access(tmp_path), "claude")
    assert not check.ok


def test_claude_without_sandbox_warns(tmp_path):
    write_claude_policy(tmp_path, {"permissions": {"deny": []}, "disableAllHooks": True})
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert check.ok
    assert any("sandbox" in w for w in check.warnings)


def test_valid_codex_policy_passes(tmp_path):
    write_codex_policy(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "codex")
    assert check.ok, check.problems


def test_codex_mixed_sandbox_and_permissions_fails(tmp_path):
    write_codex_policy(tmp_path, 'sandbox_mode = "workspace-write"\n' + CODEX_CONFIG)
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "codex")
    assert not check.ok
    assert any("legacy sandbox" in p for p in check.problems)


def test_codex_pure_legacy_sandbox_is_allowed(tmp_path):
    """The operator's file is authoritative; a pure-legacy sandbox config is theirs."""
    write_codex_policy(tmp_path, 'sandbox_mode = "workspace-write"\n')
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "codex")
    assert check.ok, check.problems


def test_codex_invalid_toml_fails(tmp_path):
    write_codex_policy(tmp_path, "= not toml")
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "codex")
    assert not check.ok


def test_unknown_provider_fails(tmp_path):
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "mystery")
    assert not check.ok


# -- policy_revision --


def test_revision_changes_with_policy_bytes(tmp_path):
    write_claude_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_access(tmp_path)
    first = policy.check_provider(workspace, access, "claude").policy_revision
    write_claude_policy(tmp_path, {"permissions": {"deny": ["WebFetch"]}, "disableAllHooks": True})
    second = policy.check_provider(workspace, access, "claude").policy_revision
    assert first != second


def test_codex_revision_covers_rules_files(tmp_path):
    write_codex_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_access(tmp_path)
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
    access = make_access(tmp_path)
    first = policy.check_provider(workspace, access, "codex").policy_revision
    agent_config.write_text('model = "gpt-5.6-sol"\n')
    second = policy.check_provider(workspace, access, "codex").policy_revision
    assert first != second


def test_hard_linked_codex_rule_fails(tmp_path):
    workspace = make_workspace(tmp_path)
    write_codex_policy(tmp_path)
    rules = tmp_path / "policies" / "codex" / "rules"
    rules.mkdir()
    rules_file = rules / "enso.rules"
    rules_file.write_text('prefix_rule(pattern=["enso"], decision="forbidden")\n')
    rules_file.chmod(0o600)
    os.link(rules_file, tmp_path / "ws" / "writable-rule-link.rules")
    check = policy.check_provider(workspace, make_access(tmp_path), "codex")
    assert not check.ok
    assert any("hard links" in problem for problem in check.problems)


# -- prepare_launch --


def test_prepare_launch_unrestricted(tmp_path):
    launch = policy.prepare_launch(
        make_workspace(tmp_path),
        make_access(tmp_path, unrestricted=True, providers=("claude",)),
        "claude",
    )
    assert launch.mode == "unrestricted"
    assert launch.env is None


def test_prepare_launch_fails_closed(tmp_path):
    with pytest.raises(policy.PolicyError):
        policy.prepare_launch(make_workspace(tmp_path), make_access(tmp_path), "claude")


def test_claude_launch_has_policy_and_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "sekret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "sekret2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthro")
    path = write_claude_policy(tmp_path)
    launch = policy.prepare_launch(make_workspace(tmp_path), make_access(tmp_path), "claude")
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
    launch = policy.prepare_launch(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert str(bin_dir) not in launch.env["PATH"].split(":")
    assert "/usr/bin" in launch.env["PATH"].split(":")


def test_codex_launch_stages_isolated_home(tmp_path, monkeypatch):
    src = write_codex_policy(tmp_path)
    fake_codex_home = tmp_path / "codex-user-home"
    fake_codex_home.mkdir()
    (fake_codex_home / "auth.json").write_text('{"tokens": "x"}')
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(fake_codex_home))
    launch = policy.prepare_launch(make_workspace(tmp_path), make_access(tmp_path), "codex")
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
    launch = policy.prepare_launch(make_workspace(tmp_path), make_access(tmp_path), "codex")
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

    launch = policy.prepare_launch(make_workspace(tmp_path), make_access(tmp_path), "codex")

    assert launch.home is not None
    staged_agent = os.path.join(launch.home, "agents", "reviewer.toml")
    with open(staged_agent) as file:
        assert file.read() == 'model = "gpt-5.6-terra"\n'


def test_codex_policy_snapshots_are_revision_keyed_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(tmp_path / "nope"))
    source = write_codex_policy(tmp_path)
    workspace = make_workspace(tmp_path)
    access = make_access(tmp_path)
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
    access = make_access(tmp_path)
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
    access = make_access(tmp_path)
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
    access = make_access(tmp_path)
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


def test_claude_unrestricted_command_is_unchanged():
    provider = ClaudeProvider("claude")
    assert "--dangerously-skip-permissions" in provider.build_command("hi", "opus")


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
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert not check.ok
    assert any("disableAllHooks" in p for p in check.problems)


def test_claude_policy_rejects_disable_all_hooks_false(tmp_path):
    write_claude_policy(tmp_path, {"sandbox": {"enabled": True}, "disableAllHooks": False})
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert not check.ok


def test_claude_policy_with_disable_all_hooks_passes(tmp_path):
    write_claude_policy(tmp_path, {"sandbox": {"enabled": True}, "disableAllHooks": True})
    check = policy.check_provider(make_workspace(tmp_path), make_access(tmp_path), "claude")
    assert check.ok, check.problems


def test_unrestricted_workspace_skips_hook_requirement(tmp_path):
    assert policy.check_provider(
        make_workspace(tmp_path),
        make_access(tmp_path, unrestricted=True, providers=("claude",)),
        "claude",
    ).ok


def test_agy_refusal_cites_missing_contract_not_missing_model(tmp_path):
    """agy does have a permission model; the honest reason is no verified contract."""
    check = policy.check_provider(
        make_workspace(tmp_path), make_access(tmp_path, providers=("agy",)), "agy"
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
    access = make_access(tmp_path, providers=("codex",))

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(policy.os, "makedirs", boom)
    with pytest.raises(policy.PolicyError):
        policy.prepare_launch(workspace, access, "codex")
