"""Tests for native policy selection, validation, and launch construction."""

from __future__ import annotations

import json
import os

import pytest

from enso import policy
from enso.providers.claude import ClaudeProvider
from enso.providers.codex import CodexProvider
from enso.teams import Workspace

CLAUDE_SETTINGS = {
    "permissions": {"deny": ["Bash(enso *)"]},
    "sandbox": {"enabled": True},
}

CODEX_CONFIG = 'default_permissions = "enso"\n\n[permissions.enso]\nnetwork = false\n'


def make_workspace(tmp_path, *, unrestricted=False, providers=("claude", "codex")):
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(exist_ok=True)
    policy_dir = tmp_path / "policies"
    return Workspace(
        name="acme",
        path=str(ws_dir),
        policy_dir=None if unrestricted else str(policy_dir),
        unrestricted=unrestricted,
        providers=tuple(providers),
        default_provider=providers[0] if providers else None,
        skills=(),
        chat_commands=(),
        concurrency=1,
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
    ws = make_workspace(tmp_path, unrestricted=True, providers=("claude", "codex", "agy"))
    for provider in ws.providers:
        check = policy.check_provider(ws, provider)
        assert check.ok, check.problems
        assert check.policy_revision == policy.UNRESTRICTED_REVISION


def test_missing_policy_file_fails(tmp_path):
    ws = make_workspace(tmp_path)
    check = policy.check_provider(ws, "claude")
    assert not check.ok
    assert any("settings.json" in p for p in check.problems)


def test_agy_requires_unrestricted(tmp_path):
    ws = make_workspace(tmp_path, providers=("claude", "agy"))
    check = policy.check_provider(ws, "agy")
    assert not check.ok
    assert any("unrestricted" in p for p in check.problems)


def test_valid_claude_policy_passes(tmp_path):
    write_claude_policy(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), "claude")
    assert check.ok, check.problems
    assert check.policy_revision
    assert len(check.policy_revision) == 64


def test_invalid_claude_json_fails(tmp_path):
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    path.chmod(0o600)
    check = policy.check_provider(make_workspace(tmp_path), "claude")
    assert not check.ok


def test_group_readable_policy_fails(tmp_path):
    path = write_claude_policy(tmp_path)
    os.chmod(path, 0o644)
    check = policy.check_provider(make_workspace(tmp_path), "claude")
    assert not check.ok
    assert any("owner-only" in p for p in check.problems)


def test_symlinked_policy_fails(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text("{}")
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(real)
    check = policy.check_provider(make_workspace(tmp_path), "claude")
    assert not check.ok


def test_policy_resolving_into_workspace_fails(tmp_path):
    ws = make_workspace(tmp_path)
    inside = tmp_path / "ws" / "settings.json"
    inside.write_text("{}")
    path = tmp_path / "policies" / "claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(inside)
    check = policy.check_provider(ws, "claude")
    assert not check.ok


def test_claude_without_sandbox_warns(tmp_path):
    write_claude_policy(tmp_path, {"permissions": {"deny": []}})
    check = policy.check_provider(make_workspace(tmp_path), "claude")
    assert check.ok
    assert any("sandbox" in w for w in check.warnings)


def test_valid_codex_policy_passes(tmp_path):
    write_codex_policy(tmp_path)
    check = policy.check_provider(make_workspace(tmp_path), "codex")
    assert check.ok, check.problems


def test_codex_mixed_sandbox_and_permissions_fails(tmp_path):
    write_codex_policy(
        tmp_path, 'sandbox_mode = "workspace-write"\n' + CODEX_CONFIG
    )
    check = policy.check_provider(make_workspace(tmp_path), "codex")
    assert not check.ok
    assert any("legacy sandbox" in p for p in check.problems)


def test_codex_pure_legacy_sandbox_is_allowed(tmp_path):
    """The operator's file is authoritative; a pure-legacy sandbox config is theirs."""
    write_codex_policy(tmp_path, 'sandbox_mode = "workspace-write"\n')
    check = policy.check_provider(make_workspace(tmp_path), "codex")
    assert check.ok, check.problems


def test_codex_invalid_toml_fails(tmp_path):
    write_codex_policy(tmp_path, "= not toml")
    check = policy.check_provider(make_workspace(tmp_path), "codex")
    assert not check.ok


def test_unknown_provider_fails(tmp_path):
    check = policy.check_provider(make_workspace(tmp_path), "mystery")
    assert not check.ok


# -- policy_revision --


def test_revision_changes_with_policy_bytes(tmp_path):
    write_claude_policy(tmp_path)
    ws = make_workspace(tmp_path)
    first = policy.check_provider(ws, "claude").policy_revision
    write_claude_policy(tmp_path, {"permissions": {"deny": ["WebFetch"]}})
    second = policy.check_provider(ws, "claude").policy_revision
    assert first != second


def test_codex_revision_covers_rules_files(tmp_path):
    write_codex_policy(tmp_path)
    ws = make_workspace(tmp_path)
    first = policy.check_provider(ws, "codex").policy_revision
    rules = tmp_path / "policies" / "codex" / "rules"
    rules.mkdir()
    rules_file = rules / "enso.rules"
    rules_file.write_text('prefix_rule(pattern=["enso"], decision="forbidden")\n')
    rules_file.chmod(0o600)
    second = policy.check_provider(ws, "codex").policy_revision
    assert first != second


# -- prepare_launch --


def test_prepare_launch_unrestricted(tmp_path):
    ws = make_workspace(tmp_path, unrestricted=True, providers=("claude",))
    launch = policy.prepare_launch(ws, "claude")
    assert launch.mode == "unrestricted"
    assert launch.env is None


def test_prepare_launch_fails_closed(tmp_path):
    ws = make_workspace(tmp_path)
    with pytest.raises(policy.PolicyError):
        policy.prepare_launch(ws, "claude")


def test_claude_launch_has_policy_and_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "sekret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "sekret2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthro")
    path = write_claude_policy(tmp_path)
    launch = policy.prepare_launch(make_workspace(tmp_path), "claude")
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
    launch = policy.prepare_launch(make_workspace(tmp_path), "claude")
    assert str(bin_dir) not in launch.env["PATH"].split(":")
    assert "/usr/bin" in launch.env["PATH"].split(":")


def test_codex_launch_stages_isolated_home(tmp_path, monkeypatch):
    src = write_codex_policy(tmp_path)
    fake_codex_home = tmp_path / "codex-user-home"
    fake_codex_home.mkdir()
    (fake_codex_home / "auth.json").write_text('{"tokens": "x"}')
    monkeypatch.setattr(policy, "_user_codex_home", lambda: str(fake_codex_home))
    launch = policy.prepare_launch(make_workspace(tmp_path), "codex")
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
    launch = policy.prepare_launch(make_workspace(tmp_path), "codex")
    assert launch.ignore_rules is False
    assert os.path.exists(os.path.join(launch.home, "rules", "enso.rules"))


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
    assert cmd[cmd.index("--setting-sources") + 1] == ""
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
