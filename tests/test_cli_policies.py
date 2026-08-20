"""Explicit, read-only policy inspection and safe policy registration."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from enso import cli as cli_mod
from enso.config import save_config

runner = CliRunner()


def _base_config(tmp_enso: str, *, binding: str = "admin") -> dict:
    """Persist one complete catalog suitable for policy CLI tests."""
    config = {
        "transport": "",
        "transports": {},
        "providers": {
            "claude": {"path": "claude", "models": ["sonnet"]},
            "codex": {"path": "codex", "models": ["gpt-5.3-codex"]},
            "agy": {"path": "agy", "models": ["gemini-3.6-flash-high"]},
            "grok": {"path": "grok", "models": ["grok-4.6"]},
        },
        "workspaces": {
            "default": {"policy": binding, "concurrency": 1},
        },
        "policies": {
            "admin": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
        },
        "setup": {"completed_at": "2026-01-01T00:00:00+00:00"},
    }
    save_config(config)
    return config


def _write_claude_policy(
    root: Path,
    *,
    marker: str | None = None,
    malformed: bool = False,
    mcp: bool = False,
) -> Path:
    native = root / "claude" / "settings.json"
    native.parent.mkdir(parents=True)
    if malformed:
        native.write_text("{not-json", encoding="utf-8")
    else:
        settings: dict[str, object] = {
            "sandbox": {"enabled": True},
            "disableAllHooks": True,
        }
        if marker is not None:
            settings["operator_secret_marker"] = marker
        if mcp:
            settings["permissions"] = {"allow": ["mcp__metrics__query"]}
        native.write_text(json.dumps(settings), encoding="utf-8")
    native.chmod(0o600)
    if mcp:
        mcp_path = root / "claude" / "mcp.json"
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "metrics": {
                            "type": "http",
                            "url": "https://example.invalid/mcp",
                            "headers": {"Authorization": "${CLIENT_TOKEN}"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        mcp_path.chmod(0o600)
    return native


def _write_codex_policy(root: Path) -> Path:
    native = root / "codex" / "config.toml"
    native.parent.mkdir(parents=True)
    native.write_text(
        'default_permissions = "enso"\n\n[permissions.enso.network]\nenabled = false\n',
        encoding="utf-8",
    )
    native.chmod(0o600)
    return native


def _flat(result) -> str:
    return " ".join(result.output.split())


def _create_args(
    name: str,
    *authority: str,
    providers: tuple[str, ...] = ("claude",),
    default: str | None = "claude",
    trailing: tuple[str, ...] = (),
) -> list[str]:
    args = ["policy", "create", name, *authority]
    for provider in providers:
        args.extend(("--provider", provider))
    if default is not None:
        args.extend(("--default-provider", default))
    args.extend(trailing)
    return args


def test_policy_group_exposes_only_inspection_and_registration() -> None:
    result = runner.invoke(cli_mod.app, ["policy", "--help"])

    assert result.exit_code == 0, result.output
    for command in ("list", "show", "create"):
        assert command in result.output
    for command in ("check", "repair", "delete", "rebind", "preset"):
        assert command not in result.output

    create = runner.invoke(cli_mod.app, ["policy", "create", "--help"])
    assert create.exit_code == 0, create.output
    for option in (
        "--unrestricted",
        "--policy-dir",
        "--provider",
        "--default-provider",
        "--chat-command",
        "--all-chat-commands",
        "--env-passthrough",
    ):
        assert option in create.output
    for option in ("--workspace", "--preset", "--repair", "--bind"):
        assert option not in create.output


@pytest.mark.parametrize(
    "authority",
    [
        pytest.param((), id="missing"),
        pytest.param(("--unrestricted", "--policy-dir", "/tmp/policy"), id="both"),
    ],
)
def test_policy_create_requires_exactly_one_authority_source(
    tmp_enso: str,
    authority: tuple[str, ...],
) -> None:
    _base_config(tmp_enso)

    result = runner.invoke(cli_mod.app, _create_args("client", *authority))

    assert result.exit_code != 0
    assert "exactly one" in _flat(result).lower()
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert set(persisted["policies"]) == {"admin"}


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param("{broken", id="malformed"),
        pytest.param("[]", id="non-object"),
    ],
)
def test_policy_create_rejects_unreadable_config_before_lock_or_source_changes(
    tmp_enso: str,
    content: str | None,
) -> None:
    config_file = Path(tmp_enso, "config.json")
    if content is not None:
        config_file.write_text(content, encoding="utf-8")
        original_config = config_file.read_bytes()
    root = Path(tmp_enso, "operator-policy")
    native = _write_claude_policy(root)
    original_native = (native.read_bytes(), native.stat().st_mode, native.stat().st_ino)

    result = runner.invoke(
        cli_mod.app,
        _create_args("client", "--policy-dir", str(root)),
    )

    assert result.exit_code == 1
    assert "Configuration error" in result.output
    if content is None:
        assert not config_file.exists()
    else:
        assert config_file.read_bytes() == original_config
    assert (native.read_bytes(), native.stat().st_mode, native.stat().st_ino) == original_native
    assert not Path(f"{config_file}.lock").exists()
    assert not (root / ".runtime").exists()


@pytest.mark.parametrize(
    ("providers", "default", "expected"),
    [
        pytest.param((), "claude", "provider", id="providers-required"),
        pytest.param(("claude",), None, "default-provider", id="default-required"),
        pytest.param(("claude", "claude"), "claude", "duplicate", id="duplicate"),
        pytest.param(("unknown",), "unknown", "unknown provider", id="unknown"),
        pytest.param(("claude",), "codex", "one of providers", id="default-membership"),
    ],
)
def test_policy_create_never_infers_provider_selection(
    tmp_enso: str,
    providers: tuple[str, ...],
    default: str | None,
    expected: str,
) -> None:
    _base_config(tmp_enso)

    result = runner.invoke(
        cli_mod.app,
        _create_args(
            "client",
            "--unrestricted",
            providers=providers,
            default=default,
        ),
    )

    assert result.exit_code != 0
    assert expected in _flat(result).lower()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("admin", "already configured", id="duplicate"),
        pytest.param("two words", "policy names", id="space"),
        pytest.param("/absolute", "policy names", id="slash"),
        pytest.param("x" * 65, "policy names", id="too-long"),
    ],
)
def test_policy_create_rejects_duplicate_and_nonportable_names(
    tmp_enso: str,
    name: str,
    expected: str,
) -> None:
    _base_config(tmp_enso)

    result = runner.invoke(cli_mod.app, _create_args(name, "--unrestricted"))

    assert result.exit_code == 1
    assert expected in _flat(result).lower()


@pytest.mark.parametrize(
    ("trailing", "commands"),
    [
        pytest.param((), [], id="empty"),
        pytest.param(
            ("--chat-command", "status", "--chat-command", "usage"),
            ["status", "usage"],
            id="explicit-list",
        ),
        pytest.param(("--all-chat-commands",), "*", id="all"),
    ],
)
def test_unrestricted_policy_create_persists_only_explicit_catalog_entry(
    tmp_enso: str,
    monkeypatch,
    trailing: tuple[str, ...],
    commands: list[str] | str,
) -> None:
    original = _base_config(tmp_enso)
    monkeypatch.setattr(
        cli_mod,
        "unrestricted_policy_config",
        lambda *_args, **_kwargs: pytest.fail(
            "post-setup policy creation must not infer unrestricted defaults"
        ),
    )

    result = runner.invoke(
        cli_mod.app,
        _create_args(
            "automation",
            "--unrestricted",
            providers=("codex", "claude"),
            default="codex",
            trailing=trailing,
        ),
    )

    assert result.exit_code == 0, result.output
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert persisted["policies"]["automation"] == {
        "unrestricted": True,
        "providers": ["codex", "claude"],
        "default_provider": "codex",
        "chat_commands": commands,
    }
    assert persisted["workspaces"] == original["workspaces"]
    assert not Path(tmp_enso, "policies").exists()
    assert not Path(tmp_enso, ".runtime").exists()
    flattened = _flat(result)
    assert "Policy created: automation" in flattened
    assert "unrestricted" in flattened
    assert "restart" in flattened.lower()


def test_policy_create_rejects_conflicting_commands_and_unrestricted_env(
    tmp_enso: str,
) -> None:
    _base_config(tmp_enso)

    commands = runner.invoke(
        cli_mod.app,
        _create_args(
            "commands",
            "--unrestricted",
            trailing=("--chat-command", "status", "--all-chat-commands"),
        ),
    )
    environment = runner.invoke(
        cli_mod.app,
        _create_args(
            "environment",
            "--unrestricted",
            trailing=("--env-passthrough", "CLIENT_TOKEN"),
        ),
    )

    assert commands.exit_code == 1
    assert "may not" in _flat(commands).lower() or "cannot" in _flat(commands).lower()
    assert environment.exit_code == 1
    assert "env_passthrough" in _flat(environment)
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert set(persisted["policies"]) == {"admin"}


def test_restricted_policy_create_validates_without_mutating_native_content(
    tmp_enso: str,
) -> None:
    original = _base_config(tmp_enso)
    root = Path(tmp_enso, "operator-policy")
    native = _write_claude_policy(root)
    before = (native.read_bytes(), native.stat().st_mode, native.stat().st_ino)

    result = runner.invoke(
        cli_mod.app,
        _create_args(
            "client-readonly",
            "--policy-dir",
            str(root),
            trailing=(
                "--chat-command",
                "status",
                "--env-passthrough",
                "CLIENT_TOKEN",
                "--env-passthrough",
                "METRICS_TOKEN",
            ),
        ),
    )

    assert result.exit_code == 0, result.output
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert persisted["policies"]["client-readonly"] == {
        "policy_dir": str(root),
        "providers": ["claude"],
        "default_provider": "claude",
        "chat_commands": ["status"],
        "env_passthrough": ["CLIENT_TOKEN", "METRICS_TOKEN"],
    }
    assert persisted["workspaces"] == original["workspaces"]
    assert (native.read_bytes(), native.stat().st_mode, native.stat().st_ino) == before
    assert not Path(tmp_enso, ".runtime").exists()
    assert not (root / ".runtime").exists()


@pytest.mark.parametrize("complete", [True, False], ids=("success", "missing-codex"))
def test_restricted_multi_provider_create_requires_every_selected_native_file(
    tmp_enso: str,
    complete: bool,
) -> None:
    _base_config(tmp_enso)
    root = Path(tmp_enso, "operator-policy")
    claude = _write_claude_policy(root)
    codex = _write_codex_policy(root) if complete else root / "codex" / "config.toml"
    config_file = Path(tmp_enso, "config.json")
    original_config = config_file.read_bytes()
    before = (claude.read_bytes(), claude.stat().st_mode, claude.stat().st_ino)

    result = runner.invoke(
        cli_mod.app,
        _create_args(
            "multi-provider",
            "--policy-dir",
            str(root),
            providers=("claude", "codex"),
            default="codex",
        ),
    )

    assert (claude.read_bytes(), claude.stat().st_mode, claude.stat().st_ino) == before
    assert not (root / ".runtime").exists()
    if complete:
        assert result.exit_code == 0, result.output
        persisted = json.loads(config_file.read_text(encoding="utf-8"))
        assert persisted["policies"]["multi-provider"] == {
            "policy_dir": str(root),
            "providers": ["claude", "codex"],
            "default_provider": "codex",
            "chat_commands": [],
        }
        assert codex.is_file()
    else:
        assert result.exit_code == 1
        assert "native source validation failed" in _flat(result).lower()
        assert config_file.read_bytes() == original_config


@pytest.mark.parametrize(
    "kind",
    ["missing", "file", "symlink", "malformed", "agy"],
)
def test_restricted_policy_create_rejects_unsafe_or_incomplete_sources(
    tmp_enso: str,
    tmp_path: Path,
    kind: str,
) -> None:
    _base_config(tmp_enso)
    root = Path(tmp_enso, "candidate")
    providers = ("claude",)
    if kind == "file":
        root.write_text("not a directory", encoding="utf-8")
    elif kind == "symlink":
        target = tmp_path / "outside-policy"
        _write_claude_policy(target)
        root.symlink_to(target, target_is_directory=True)
    elif kind == "malformed":
        _write_claude_policy(root, malformed=True)
    elif kind == "agy":
        root.mkdir()
        providers = ("agy",)

    config_file = Path(tmp_enso, "config.json")
    original = config_file.read_bytes()
    result = runner.invoke(
        cli_mod.app,
        _create_args(
            "client",
            "--policy-dir",
            str(root),
            providers=providers,
            default=providers[0],
        ),
    )

    assert result.exit_code == 1
    assert "native source validation failed" in _flat(result).lower()
    assert config_file.read_bytes() == original
    assert not Path(tmp_enso, ".runtime").exists()
    assert not (root / ".runtime").exists()


def test_policy_create_atomic_save_failure_preserves_config_and_sources(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _base_config(tmp_enso)
    root = Path(tmp_enso, "operator-policy")
    native = _write_claude_policy(root)
    config_file = Path(tmp_enso, "config.json")
    before_config = config_file.read_bytes()
    before_native = (native.read_bytes(), native.stat().st_mode, native.stat().st_ino)

    def fail_save(_candidate: dict) -> None:
        raise OSError("simulated atomic save failure")

    monkeypatch.setattr(cli_mod, "save_config", fail_save)
    result = runner.invoke(
        cli_mod.app,
        _create_args("client", "--policy-dir", str(root)),
    )

    assert result.exit_code == 1
    assert "simulated atomic save failure" in _flat(result)
    assert config_file.read_bytes() == before_config
    assert (native.read_bytes(), native.stat().st_mode, native.stat().st_ino) == before_native


def test_policy_create_uses_strict_locked_candidate_validation_order(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _base_config(tmp_enso)
    from enso import policy as policy_mod
    from enso import teams as teams_mod

    events: list[str] = []
    real_load = cli_mod._load_config_or_exit
    real_lock = cli_mod._config_lock_or_exit
    real_catalog = teams_mod.load_catalog
    real_check = policy_mod.check_policy_sources
    real_save = cli_mod.save_config

    def recording_load(*args, **kwargs):
        events.append("strict-read")
        return real_load(*args, **kwargs)

    @contextmanager
    def recording_lock():
        events.append("lock")
        with real_lock():
            yield
        events.append("unlock")

    def recording_catalog(config):
        if "client" in config.get("policies", {}):
            events.append("candidate-catalog")
        return real_catalog(config)

    def recording_check(policy, workspaces):
        events.append(f"source-check:{policy.name}")
        return real_check(policy, workspaces)

    def recording_save(candidate):
        assert candidate["policies"]["client"] == {
            "unrestricted": True,
            "providers": ["claude"],
            "default_provider": "claude",
            "chat_commands": [],
        }
        events.append("save")
        return real_save(candidate)

    monkeypatch.setattr(cli_mod, "_load_config_or_exit", recording_load)
    monkeypatch.setattr(cli_mod, "_config_lock_or_exit", recording_lock)
    monkeypatch.setattr(teams_mod, "load_catalog", recording_catalog)
    monkeypatch.setattr(policy_mod, "check_policy_sources", recording_check)
    monkeypatch.setattr(cli_mod, "save_config", recording_save)

    result = runner.invoke(
        cli_mod.app,
        _create_args("client", "--unrestricted"),
    )

    assert result.exit_code == 0, result.output
    assert events[0] == "strict-read"
    assert events.index("lock") < events.index("strict-read", 1)
    assert events.index("strict-read", 1) < events.index("candidate-catalog")
    source_indexes = [
        index for index, event in enumerate(events) if event.startswith("source-check:")
    ]
    assert events.index("candidate-catalog") < min(source_indexes)
    assert max(source_indexes) < events.index("save") < events.index("unlock")


def test_policy_create_can_repair_unknown_binding_only_when_final_catalog_is_valid(
    tmp_enso: str,
) -> None:
    _base_config(tmp_enso, binding="client")

    repaired = runner.invoke(
        cli_mod.app,
        _create_args("client", "--unrestricted"),
    )

    assert repaired.exit_code == 0, repaired.output
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert persisted["workspaces"]["default"]["policy"] == "client"

    persisted["workspaces"]["broken"] = {"policy": "still-missing", "concurrency": 1}
    save_config(persisted)
    before = Path(tmp_enso, "config.json").read_bytes()
    rejected = runner.invoke(
        cli_mod.app,
        _create_args("other", "--unrestricted"),
    )

    assert rejected.exit_code == 1
    assert "still-missing" in _flat(rejected)
    assert Path(tmp_enso, "config.json").read_bytes() == before


def test_policy_list_and_show_are_read_only_and_do_not_leak_values_or_bytes(
    tmp_enso: str,
    monkeypatch,
) -> None:
    config = _base_config(tmp_enso)
    marker = "NATIVE_SECRET_SENTINEL"
    env_value = "ENV_SECRET_SENTINEL"
    root = Path(tmp_enso, "operator-policy")
    _write_claude_policy(root, marker=marker, mcp=True)
    config["policies"]["unused-client"] = {
        "policy_dir": str(root),
        "providers": ["claude"],
        "default_provider": "claude",
        "chat_commands": [],
        "env_passthrough": ["CLIENT_TOKEN"],
    }
    save_config(config)
    monkeypatch.setenv("CLIENT_TOKEN", env_value)
    config_file = Path(tmp_enso, "config.json")
    original = config_file.read_bytes()

    listing = runner.invoke(
        cli_mod.app,
        ["policy", "list"],
        terminal_width=180,
    )
    detail = runner.invoke(cli_mod.app, ["policy", "show", "unused-client"])

    assert listing.exit_code == 0, listing.output
    listed = _flat(listing)
    for column in (
        "Name",
        "Authority",
        "Providers",
        "Default",
        "Commands",
        "Env",
        "Workspaces",
        "Validation",
    ):
        assert column[:6] in listed
    # Rich folds long cells on the narrow chat terminal; the row must still be
    # identifiable without requiring a particular capture width.
    assert "unused-cli" in listed
    assert "unused" in listed.lower()
    assert "valid" in listed.lower()
    assert detail.exit_code == 0, detail.output
    shown = _flat(detail)
    assert "policy-controlled" in shown
    assert str(root) in "".join(detail.output.split())
    assert "CLIENT_TOKEN" in shown
    assert "metrics" in shown
    assert "revision" in shown.lower()
    for secret in (marker, env_value):
        assert secret not in listing.output
        assert secret not in detail.output
    assert config_file.read_bytes() == original
    assert not Path(f"{config_file}.lock").exists()
    assert not Path(tmp_enso, ".runtime").exists()
    assert not (root / ".runtime").exists()


def test_policy_inspection_does_not_echo_warning_bearing_native_fields(
    tmp_enso: str,
    monkeypatch,
) -> None:
    _base_config(tmp_enso)
    native_rule = "mcp__NATIVE_RULE_SENTINEL__query"
    native_key = "NATIVE_HEADER_SENTINEL_TOKEN"
    env_value = "ENV_VALUE_SENTINEL"
    root = Path(tmp_enso, "operator-policy")
    native = _write_claude_policy(root, mcp=True)
    native.write_text(
        json.dumps(
            {
                "sandbox": {"enabled": True},
                "disableAllHooks": True,
                "permissions": {"allow": [native_rule]},
            }
        ),
        encoding="utf-8",
    )
    mcp = root / "claude" / "mcp.json"
    mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "metrics": {
                        "type": "http",
                        "headers": {native_key: "literal-native-value"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    mcp.chmod(0o600)
    monkeypatch.setenv("CLIENT_TOKEN", env_value)
    before = (native.read_bytes(), mcp.read_bytes())

    results = (
        runner.invoke(
            cli_mod.app,
            _create_args(
                "unused-client",
                "--policy-dir",
                str(root),
                trailing=("--env-passthrough", "CLIENT_TOKEN"),
            ),
        ),
        runner.invoke(cli_mod.app, ["policy", "list"]),
        runner.invoke(cli_mod.app, ["policy", "show", "unused-client"]),
        runner.invoke(cli_mod.app, ["config", "check"]),
    )

    assert results[0].exit_code == 0, results[0].output
    assert results[1].exit_code == 0, results[1].output
    assert results[2].exit_code == 0, results[2].output
    assert "warning" in _flat(results[0]).lower()
    assert "warning" in _flat(results[2]).lower()
    assert "warning" in _flat(results[3]).lower()
    for result in results:
        for secret in (native_rule, native_key, "literal-native-value", env_value):
            assert secret not in result.output
    assert (native.read_bytes(), mcp.read_bytes()) == before
    assert not (root / ".runtime").exists()


def test_policy_inspection_does_not_echo_error_bearing_native_fields(
    tmp_enso: str,
) -> None:
    config = _base_config(tmp_enso)
    native_key = "NATIVE_GROK_KEY_SENTINEL"
    root = Path(tmp_enso, "operator-policy")
    native = root / "grok" / "config.toml"
    native.parent.mkdir(parents=True)
    native.write_text(f"[permission]\n{native_key} = []\n", encoding="utf-8")
    native.chmod(0o600)
    create_result = runner.invoke(
        cli_mod.app,
        _create_args(
            "create-failure",
            "--policy-dir",
            str(root),
            providers=("grok",),
            default="grok",
        ),
    )
    config["policies"]["unused-grok"] = {
        "policy_dir": str(root),
        "providers": ["grok"],
        "default_provider": "grok",
        "chat_commands": [],
    }
    save_config(config)
    before = native.read_bytes()

    results = (
        create_result,
        runner.invoke(cli_mod.app, ["policy", "list"]),
        runner.invoke(cli_mod.app, ["policy", "show", "unused-grok"]),
        runner.invoke(cli_mod.app, ["config", "check"]),
    )

    assert results[0].exit_code == 1
    assert results[1].exit_code == 1
    assert results[2].exit_code == 1
    assert "native source validation failed" in _flat(results[0]).lower()
    assert "Validation: invalid" in _flat(results[2])
    for result in results:
        assert native_key not in result.output
    assert native.read_bytes() == before
    assert not (root / ".runtime").exists()


def test_policy_show_reports_invalid_unused_policy_without_mutating_it(
    tmp_enso: str,
) -> None:
    config = _base_config(tmp_enso)
    root = Path(tmp_enso, "operator-policy")
    native = _write_claude_policy(root, malformed=True)
    config["policies"]["unused-client"] = {
        "policy_dir": str(root),
        "providers": ["claude"],
        "default_provider": "claude",
        "chat_commands": [],
    }
    save_config(config)
    config_file = Path(tmp_enso, "config.json")
    before = (config_file.read_bytes(), native.read_bytes(), native.stat().st_ino)

    result = runner.invoke(cli_mod.app, ["policy", "show", "unused-client"])

    assert result.exit_code == 1
    assert "invalid" in _flat(result).lower()
    assert "native source validation failed" in _flat(result).lower()
    assert (config_file.read_bytes(), native.read_bytes(), native.stat().st_ino) == before
    assert not Path(f"{config_file}.lock").exists()


def test_policy_show_keeps_requested_policy_status_separate_from_catalog_errors(
    tmp_enso: str,
) -> None:
    config = _base_config(tmp_enso)
    config["workspaces"]["broken"] = {
        "policy": "missing-policy",
        "concurrency": 1,
    }
    save_config(config)
    config_file = Path(tmp_enso, "config.json")
    original = config_file.read_bytes()

    result = runner.invoke(cli_mod.app, ["policy", "show", "admin"])

    assert result.exit_code == 1
    flattened = _flat(result)
    assert "Validation: valid" in flattened
    assert "Catalog errors:" in flattened
    assert "missing-policy" in flattened
    assert config_file.read_bytes() == original
    assert not Path(f"{config_file}.lock").exists()


def test_config_check_statically_validates_an_unused_policy(tmp_enso: str) -> None:
    config = _base_config(tmp_enso)
    root = Path(tmp_enso, "operator-policy")
    _write_claude_policy(root, malformed=True)
    config["policies"]["unused-client"] = {
        "policy_dir": str(root),
        "providers": ["claude"],
        "default_provider": "claude",
        "chat_commands": [],
    }
    save_config(config)

    result = runner.invoke(cli_mod.app, ["config", "check"])

    assert result.exit_code == 1
    flattened = _flat(result).lower()
    assert "unused-client" in flattened
    assert "native source validation failed" in flattened


@pytest.mark.parametrize(
    ("names", "expected_exits", "expected_names"),
    [
        pytest.param(
            ("alpha", "beta"),
            [0, 0],
            {"admin", "alpha", "beta"},
            id="different-names",
        ),
        pytest.param(
            ("same", "same"),
            [0, 1],
            {"admin", "same"},
            id="same-name-one-winner",
        ),
    ],
)
def test_concurrent_policy_creates_serialize_without_lost_updates(
    tmp_enso: str,
    monkeypatch,
    names: tuple[str, str],
    expected_exits: list[int],
    expected_names: set[str],
) -> None:
    _base_config(tmp_enso)
    real_load = cli_mod._load_config_or_exit
    barrier = threading.Barrier(2)
    local = threading.local()

    def synchronized_load(*args, **kwargs):
        config = real_load(*args, **kwargs)
        if not getattr(local, "passed_preflight", False):
            local.passed_preflight = True
            barrier.wait(timeout=5)
        return config

    monkeypatch.setattr(cli_mod, "_load_config_or_exit", synchronized_load)

    def create(name: str) -> int:
        try:
            cli_mod.policy_create(
                name,
                ["claude"],
                "claude",
                unrestricted=True,
                policy_dir=None,
                chat_commands=None,
                all_chat_commands=False,
                env_passthrough=None,
            )
        except typer.Exit as exc:
            return exc.exit_code
        return 0

    with ThreadPoolExecutor(max_workers=2) as executor:
        exits = list(executor.map(create, names))

    assert sorted(exits) == expected_exits
    persisted = json.loads(Path(tmp_enso, "config.json").read_text(encoding="utf-8"))
    assert expected_names == set(persisted["policies"])
    assert not Path(tmp_enso, ".runtime").exists()
