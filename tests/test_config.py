"""Config validation: the rules in §3.2 as one table, plus load/check round trips."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from conftest import write_config, write_workspace
from typer.testing import CliRunner

from enso.cli import app
from enso.config import (
    LEGACY_HOME_MESSAGE,
    Config,
    ConfigError,
    LiveConfig,
    Paths,
    ProjectConfig,
    Stage,
    check_config,
    load_config,
    parse_config,
)

REPO = Path(__file__).resolve().parent.parent
UNKNOWN = "is not a recognized key"


@pytest.mark.parametrize(
    ("stages", "expected"),
    [
        ((Stage("approve", human=True),), None),
        ((Stage("plan", worktree=False),), None),
        ((Stage("implement"), Stage("report", worktree=False)), "implement"),
    ],
)
def test_last_agent_stage_allows_workflows_without_a_landing_stage(stages, expected):
    project = ProjectConfig("EN", "Enso", "default", None, stages)
    assert project.last_agent_stage == expected


def test_heartbeat_defaults_and_explicit_settings(enso_home, raw_config):
    config, problems, _ = parse_config(raw_config, enso_home)
    assert not problems
    assert config.heartbeat.enabled and config.heartbeat.retention_days == 30
    raw_config["heartbeat"] = {"enabled": False, "retention_days": 7}
    config, problems, _ = parse_config(raw_config, enso_home)
    assert not problems
    assert not config.heartbeat.enabled and config.heartbeat.retention_days == 7


@pytest.mark.parametrize("retention", [True, 0, -1, "30", None])
def test_heartbeat_config_reports_independent_errors(enso_home, raw_config, retention):
    raw_config["heartbeat"] = {"enabled": "yes", "retention_days": retention, "unknown": 1}
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None
    assert set(problems) == {
        "heartbeat.enabled must be true or false",
        "heartbeat.retention_days must be a positive integer",
        "heartbeat.unknown is not a recognized key",
    }


def test_malformed_heartbeat_config_stops_subtree_validation(enso_home, raw_config):
    raw_config["heartbeat"] = False
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and problems == ["heartbeat must be an object"]


def _without(raw: dict, *path: str) -> dict:
    node = raw
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return raw


def _set(raw: dict, value: object, *path: str) -> dict:
    node = raw
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    return raw


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda r: _set(r, 3, "version"), "version must be 2"),
        (lambda r: _without(r, "defaults"), "defaults must be an object"),
        (lambda r: _without(r, "defaults", "effort"), "defaults.effort is required"),
        (
            lambda r: _set(r, "gpt", "defaults", "provider"),
            "defaults.provider 'gpt' is not configured",
        ),
        (lambda r: _set(r, "nope", "defaults", "model"), "defaults.model 'nope' is not in"),
        (lambda r: _set(r, "ultra", "defaults", "effort"), "defaults.effort must be one of"),
        (lambda r: _set(r, "nope", "bindings", "slack:C2"), "workspace directory"),
        (lambda r: _set(r, "default", "bindings", "slack:U1"), "bindings.slack:U1: keys look like"),
        # Telegram is private chats only, so a group id (negative) is not a binding.
        (lambda r: _set(r, "default", "bindings", "telegram:-5"), "bindings.telegram:-5"),
        (
            lambda r: _set(r, "default", "bindings", "discord:1"),
            "bindings.discord:1: keys look like",
        ),
        # A trailing newline used to slip past the pattern's ``$``, storing a key that could
        # never match a real conversation; ``\d`` let non-ASCII digits through the same way.
        (
            lambda r: _set(r, "default", "bindings", "slack:C123\n"),
            "bindings.slack:C123\n: keys look like",
        ),
        (
            lambda r: _set(r, "default", "bindings", "telegram:\u0661\u0662\u0663"),
            "bindings.telegram:\u0661\u0662\u0663: keys look like",
        ),
        (
            lambda r: _set(r, {"bot_token": "1:abc", "notify": "\u00b2"}, "transports", "telegram"),
            "transports.telegram.notify must be a positive numeric Telegram user id",
        ),
        (lambda r: _set(r, "Bad Name", "bindings", "slack:C2"), "lowercase kebab-case"),
        (
            lambda r: _without(r, "transports", "slack", "app_token"),
            "transports.slack.app_token is required",
        ),
        (lambda r: _set(r, {}, "transports"), "transports must configure slack or telegram"),
        (
            lambda r: _set(r, {"bot_token": ""}, "transports", "telegram"),
            "transports.telegram.bot_token",
        ),
        (
            lambda r: _set(r, "not-a-channel", "transports", "slack", "notify"),
            "transports.slack.notify must be a Slack conversation id",
        ),
        (
            lambda r: _set(r, {"bot_token": "1:abc", "notify": "@gavin"}, "transports", "telegram"),
            "transports.telegram.notify must be a positive numeric Telegram user id",
        ),
        (
            lambda r: _set(r, {"path": "gemini", "models": ["x"]}, "providers", "gemini"),
            "unknown provider",
        ),
        (
            lambda r: _set(r, "yes", "providers", "claude", "args"),
            "providers.claude.args must be a list",
        ),
        (lambda r: _set(r, -1, "agent", "timeout"), "agent.timeout must be a non-negative integer"),
        (lambda r: _set(r, "LOUD", "logging", "level"), "logging.level must be one of"),
    ],
)
def test_invalid_configs_are_rejected(enso_home: Paths, raw_config: dict, mutate, fragment) -> None:
    config, problems, _ = parse_config(mutate(raw_config), enso_home)
    assert config is None
    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda r: _set(r, 1, "versoin"), "versoin"),
        (lambda r: _set(r, {}, "transports", "discord"), "transports.discord"),
        (lambda r: _set(r, True, "transports", "slack", "mention"), "transports.slack.mention"),
        (
            lambda r: _set(r, {"bot_token": "1:abc", "chat": 5}, "transports", "telegram"),
            "transports.telegram.chat",
        ),
        (lambda r: _set(r, [], "providers", "claude", "arg"), "providers.claude.arg"),
        (lambda r: _set(r, "high", "defaults", "reasoning"), "defaults.reasoning"),
        (lambda r: _set(r, 60, "agent", "timeouts"), "agent.timeouts"),
        (lambda r: _set(r, 1, "logging", "max_byte"), "logging.max_byte"),
        (lambda r: _set(r, 1, "runs", "keeps"), "runs.keeps"),
        (lambda r: _set(r, 8787, "web", "ports"), "web.ports"),
    ],
)
def test_unknown_keys_are_rejected_at_every_closed_object(
    enso_home: Paths, raw_config: dict, mutate, expected: str
) -> None:
    config, problems, _ = parse_config(mutate(raw_config), enso_home)
    assert config is None
    assert f"{expected} {UNKNOWN}" in problems


def test_every_unknown_key_is_reported_once_in_parse_order(
    enso_home: Paths, raw_config: dict
) -> None:
    """Independent typos are collected, not returned one at a time, and value errors survive."""
    raw_config["providers"]["claude"]["arg"] = []
    raw_config["logging"] = {"level": "LOUD", "max_byte": 1}
    raw_config["web"] = {"ports": 8787}

    config, problems, _ = parse_config(raw_config, enso_home)

    assert config is None
    assert [problem for problem in problems if UNKNOWN in problem] == [
        f"providers.claude.arg {UNKNOWN}",
        f"logging.max_byte {UNKNOWN}",
        f"web.ports {UNKNOWN}",
    ]
    assert any("logging.level must be one of" in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda r: _set(r, "INFO", "logging"), "logging must be an object"),
        (lambda r: _set(r, ["slack"], "transports"), "transports must be an object"),
        (lambda r: _set(r, "claude", "providers", "claude"), "providers.claude must be an object"),
    ],
)
def test_a_malformed_subtree_reports_its_type_and_is_not_inspected(
    enso_home: Paths, raw_config: dict, mutate, expected: str
) -> None:
    config, problems, _ = parse_config(mutate(raw_config), enso_home)
    assert config is None
    assert expected in problems
    assert not any(UNKNOWN in problem for problem in problems), problems


def test_an_unsupported_version_is_not_judged_against_current_keys(
    enso_home: Paths, raw_config: dict
) -> None:
    """A future schema's member names are its own; its value problems still stand."""
    raw_config["version"] = 3
    raw_config["future_option"] = True
    raw_config["logging"] = {"level": "LOUD", "max_byte": 1}

    config, problems, _ = parse_config(raw_config, enso_home)

    assert config is None
    assert "version must be 2" in problems
    assert any("logging.level must be one of" in problem for problem in problems), problems
    assert not any(UNKNOWN in problem for problem in problems), problems


def test_dynamic_names_are_data_rather_than_schema_keys(enso_home: Paths, raw_config: dict) -> None:
    """Binding keys, workspace names, and provider names are the user's to choose."""
    (enso_home.workspaces / "meteor").mkdir()
    raw_config["bindings"]["slack:dm:W1"] = "meteor"
    write_workspace(
        enso_home,
        "meteor",
        {
            "agent": {"provider": "codex", "model": "sol", "effort": "xhigh"},
            "providers": {"claude": {"args": ["--settings", "x.json"]}},
        },
    )

    config, problems, _ = parse_config(raw_config, enso_home)

    assert config is not None, problems
    assert problems == []


# A member name is arbitrary text: a newline would forge a problem line of its own and a
# right-to-left override would reorder one, so anything but ordinary text is JSON-escaped.
@pytest.mark.parametrize("key", ["max\nbytes", "max bytes", "level\u202e", "\u0661\u0662"])
def test_strange_key_names_are_escaped_and_their_values_stay_out(
    enso_home: Paths, raw_config: dict, key: str
) -> None:
    _set(raw_config, "xoxb-secret", "logging", key)

    config, problems, _ = parse_config(raw_config, enso_home)

    assert config is None
    assert problems == [f"logging.{json.dumps(key)} {UNKNOWN}"]
    assert "xoxb-secret" not in problems[0]
    assert not any(character in problems[0] for character in "\n\u202e")


def test_config_check_reports_an_unknown_key_without_a_traceback(
    enso_home: Paths, raw_config: dict
) -> None:
    raw_config["logging"] = {"max_byte": 1}
    write_config(enso_home, raw_config)

    result = CliRunner().invoke(app, ["config", "check"])

    assert result.exit_code == 1
    assert result.stderr == f"error: logging.max_byte {UNKNOWN}\n"


def test_the_documented_example_uses_only_recognized_keys(enso_home: Paths) -> None:
    """The `config.json` block in docs/configuration.md is the schema users copy from."""
    body = (REPO / "docs" / "configuration.md").read_text()
    block = body.split("```jsonc\n", 1)[1].split("```", 1)[0]
    # The block is annotated JSON with comments; none of its strings contain "//".
    example = json.loads("\n".join(line.split("//")[0] for line in block.splitlines()))

    _, problems, _ = parse_config(example, enso_home)

    assert not any(UNKNOWN in problem for problem in problems), problems


def test_valid_config_parses_with_defaults(enso_home: Paths, raw_config: dict) -> None:
    (enso_home.workspaces / "meteor").mkdir()
    write_workspace(
        enso_home,
        "meteor",
        {
            "agent": {"provider": "codex", "model": "sol", "effort": "ultra"},
            "providers": {"claude": {"args": ["--settings", "x.json"]}},
        },
    )
    raw_config["bindings"]["slack:C2"] = "meteor"
    raw_config["transports"]["telegram"] = {
        "bot_token": "1:abc",
        "allowed_users": [12, "34"],
        "notify": 12,
    }
    raw_config["bindings"]["telegram:12"] = "default"
    config, problems, warnings = parse_config(raw_config, enso_home)
    assert config is not None, problems
    assert warnings == []
    assert config.defaults.effort == "xhigh"
    assert config.workspaces["meteor"].agent is not None
    assert config.workspaces["meteor"].agent.provider == "codex"
    assert config.provider_args("meteor", "claude") == ("--settings", "x.json")
    assert config.provider_args("default", "claude") == ("--skip",)
    assert config.telegram is not None
    assert config.telegram.allowed_users == ("12", "34") and config.telegram.notify == "12"
    assert config.slack is not None and config.slack.notify == "C1"
    assert (config.agent_timeout, config.logging.level, config.runs.keep) == (30, "INFO", 500)


# ``"\u00b2"`` and ``"\u2460"`` are digits to ``str.isdigit`` but not to ``int``; Arabic-Indic
# digits parse but are not the id's own text, so none of them is a usable user id.
@pytest.mark.parametrize(
    "entry", ["@gavin", True, -5, 0, "012", 12.5, "\u00b2", "\u2460", "\u0661\u0662\u0663"]
)
def test_telegram_allowed_users_must_be_numeric_ids(
    enso_home: Paths, raw_config: dict, entry: object
) -> None:
    raw_config["transports"]["telegram"] = {"bot_token": "1:abc", "allowed_users": [entry, 12]}
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None
    assert any("allowed_users must contain positive numeric" in p for p in problems), problems


def test_missing_executable_and_unconfigured_transport_are_warnings(
    enso_home: Paths, raw_config: dict
) -> None:
    raw_config["providers"]["grok"]["path"] = "/nonexistent/grok"
    raw_config["bindings"]["telegram:1"] = "default"
    config, problems, warnings = parse_config(raw_config, enso_home)
    assert config is not None, problems
    assert any("providers.grok.path" in w for w in warnings)
    assert any("transport telegram is not configured" in w for w in warnings)


def test_provider_path_expands_current_home(
    enso_home: Paths,
    raw_config: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    executable = home / "tools" / "claude"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    raw_config["providers"]["claude"]["path"] = "~/tools/claude"

    config, problems, warnings = parse_config(raw_config, enso_home)

    assert config is not None, problems
    assert not any("providers.claude.path" in warning for warning in warnings)
    assert config.providers["claude"].path == str(executable)


def test_relative_provider_path_is_stored_absolute(
    enso_home: Paths,
    raw_config: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_dir = tmp_path / "launch"
    executable = launch_dir / "tools" / "claude"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.chdir(launch_dir)
    raw_config["providers"]["claude"]["path"] = "tools/claude"

    config, problems, warnings = parse_config(raw_config, enso_home)

    assert config is not None, problems
    assert not any("providers.claude.path" in warning for warning in warnings)
    monkeypatch.chdir(enso_home.workspace("default"))
    assert config.providers["claude"].path == str(executable)
    assert Path(config.providers["claude"].path).is_file()


def test_slack_dm_binding_accepts_enterprise_user_id(enso_home: Paths, raw_config: dict) -> None:
    raw_config["bindings"]["slack:dm:W1"] = "default"
    config, problems, warnings = parse_config(raw_config, enso_home)
    assert config is not None, problems
    assert warnings == []
    assert config.bindings["slack:dm:W1"] == "default"


def test_load_config_reads_env_home(enso_home: Paths, raw_config: dict) -> None:
    write_config(enso_home, raw_config)
    assert Paths.from_env() == enso_home
    assert load_config(enso_home).bindings["slack:C1"] == "default"


def test_load_config_fails_closed(enso_home: Paths) -> None:
    with pytest.raises(ConfigError, match="missing"):
        load_config(enso_home)
    enso_home.config.write_text("{not json")
    assert check_config(enso_home)[0] is None
    enso_home.config.write_text(json.dumps([]))
    with pytest.raises(ConfigError, match="JSON object"):
        load_config(enso_home)


@pytest.mark.parametrize(
    ("value", "both", "expect"),
    [
        ("slack:C1", False, ("slack", "C1")),
        ("C1", False, ("slack", "C1")),
        ("C1", True, "ambiguous"),
        ("telegram:123", False, "not configured"),
        ("telegram:123", True, ("telegram", "123")),
        ("slack:", False, "is not a Slack conversation id"),
        ("", False, "empty"),
        ("slack:D1", False, ("slack", "D1")),
        # DMs are bindings, not send targets; posting to one needs its D… id.
        ("slack:dm:U1", False, "is not a Slack conversation id"),
        ("slack:dm:W1", False, "is not a Slack conversation id"),
        ("slack:not-a-channel", False, "is not a Slack conversation id"),
        ("not-a-channel", False, "is not a Slack conversation id"),
        ("telegram:not-a-user-id", True, "is not a positive numeric Telegram user id"),
        ("telegram:\u00b2", True, "is not a positive numeric Telegram user id"),
        ("discord:1", False, "unknown transport"),
    ],
)
def test_resolve_target(
    config: Config, config_both: Config, value: str, both: bool, expect
) -> None:
    chosen = config_both if both else config
    if isinstance(expect, tuple):
        assert chosen.resolve_target(value) == expect
    else:
        with pytest.raises(ValueError, match=expect):
            chosen.resolve_target(value)


def test_default_notify(enso_home: Paths, raw_config_both: dict) -> None:
    config, _, _ = parse_config(raw_config_both, enso_home)
    assert config is not None and config.default_notify() == ("slack", "C1")
    raw_config_both["transports"]["slack"]["notify"] = ""
    config, _, _ = parse_config(raw_config_both, enso_home)
    assert config is not None and config.default_notify() == ("telegram", "123")
    raw_config_both["transports"]["telegram"]["notify"] = ""
    config, _, _ = parse_config(raw_config_both, enso_home)
    assert config is not None and config.default_notify() is None


@pytest.mark.parametrize("value", [{}, {"default": {"restricted": False}}, None])
def test_removed_workspaces_block_is_rejected(enso_home, raw_config, value):
    raw_config["workspaces"] = value
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None
    assert len(problems) == 1
    assert "workspaces is not a recognized key" in problems[0]


@pytest.mark.parametrize(
    "raw",
    [
        {"version": 1},
        {"version": 1, "workspaces": {"default": {"restricted": True}}, "transports": "invalid"},
    ],
)
def test_version_one_is_refused_once_without_parsing_legacy_fields(enso_home, raw):
    config, problems, warnings = parse_config(raw, enso_home)
    assert config is None and problems == [LEGACY_HOME_MESSAGE] and warnings == []
    write_config(enso_home, raw)
    before = enso_home.config.read_bytes()
    with pytest.raises(ConfigError) as refused:
        load_config(enso_home)
    assert refused.value.problems == [LEGACY_HOME_MESSAGE]
    result = CliRunner().invoke(app, ["config", "check", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["problems"] == [LEGACY_HOME_MESSAGE]
    assert enso_home.config.read_bytes() == before


@pytest.mark.parametrize("version", [True, False, 1.0, 2.0, "2", None])
def test_config_version_requires_an_integer(enso_home, raw_config, version):
    raw_config["version"] = version
    config, problems, _ = parse_config(raw_config, enso_home)
    assert config is None and problems == ["version must be 2"]


def test_a_deeply_nested_document_is_rejected_once_like_any_other_bad_revision(
    enso_home: Paths, raw_config: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """A document too deep for the JSON parser is a problem, not a ``RecursionError``."""
    write_config(enso_home, raw_config)
    good = load_config(enso_home)
    live = LiveConfig(good)
    enso_home.config.write_text("[" * 200_000)

    assert check_config(enso_home)[1] == [f"{enso_home.config} is nested too deeply"]
    with pytest.raises(ConfigError, match="nested too deeply"):
        load_config(enso_home)
    with caplog.at_level(logging.WARNING, logger="enso.config"):
        assert live.current() is good
        assert live.current() is good
    kept = [r.getMessage() for r in caplog.records if "last good configuration" in r.getMessage()]
    assert len(kept) == 1  # once per bad revision, not once per read
