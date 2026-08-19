"""Tests for the `enso message send/attach` destination resolver."""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer

from enso.cli import (
    _ensure_default_execution_config,
    _finalize_setup_or_exit,
    _install_launchd,
    _install_systemd,
    _resolve_send_targets,
    _resolve_slack_target,
    _scaffold_setup_or_exit,
    _setup_default_workspace,
    _setup_slack,
    _setup_telegram,
    _setup_transport,
    _update_referenced_secrets_with_rollback_or_exit,
    serve,
    setup,
    web,
)
from enso.config import ConfigError
from enso.secret_refs import SecretResolutionError


def _add_default_execution_catalog(config: dict, *, incomplete: bool = False) -> dict:
    """Give isolated setup-helper tests the catalog established by setup step 2."""
    config.setdefault(
        "providers",
        {"claude": {"path": "claude", "models": ["sonnet"]}},
    )
    config.setdefault(
        "workspaces",
        {"default": {"policy": "admin", "concurrency": 1}},
    )
    config.setdefault(
        "policies",
        {
            "admin": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
        },
    )
    if incomplete:
        config["setup"] = {"completed_at": None}
    return config


def test_explicit_to_wins_and_clears_thread(monkeypatch):
    """When --to is given we never leak the origin thread (could be a
    different channel)."""
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C_origin")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "1700.1")
    channel, thread_ts = _resolve_slack_target("#other", "C_notify")
    assert channel == "#other"
    assert thread_ts == ""


def test_origin_env_wins_over_notify_channel(monkeypatch):
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "C_origin")
    monkeypatch.setenv("ENSO_ORIGIN_THREAD_TS", "1700.1")
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "C_origin"
    assert thread_ts == "1700.1"


def test_origin_without_thread(monkeypatch):
    """DM origin: channel set, thread empty."""
    monkeypatch.setenv("ENSO_ORIGIN_CHANNEL", "D_dm")
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "D_dm"
    assert thread_ts == ""


def test_falls_back_to_notify_channel(monkeypatch):
    """No --to and no origin env → notify_channel is the last resort."""
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "C_notify")
    assert channel == "C_notify"
    assert thread_ts == ""


def test_nothing_configured(monkeypatch):
    """Fully unconfigured — returns empty so caller can error cleanly."""
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    monkeypatch.delenv("ENSO_ORIGIN_THREAD_TS", raising=False)
    channel, thread_ts = _resolve_slack_target("", "")
    assert channel == ""
    assert thread_ts == ""


def test_telegram_send_target_resolves_1password_reference(monkeypatch):
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    config = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token_1password": {
                    "item": "Telegram",
                    "field": "TOKEN",
                },
                "allowed_users": ["123"],
                "notify_channel": "456",
            },
        },
    }
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-telegram-token",
    )

    transport, token, targets, thread_ts = _resolve_send_targets(config, "")

    assert (transport, token, targets, thread_ts) == (
        "telegram",
        "resolved-telegram-token",
        ["456"],
        "",
    )


def test_telegram_send_target_does_not_broadcast_to_allowed_users(monkeypatch):
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    config = {
        "transport": "telegram",
        "transports": {
            "telegram": {
                "bot_token": "token",
                "allowed_users": ["123", "456"],
            },
        },
    }

    with pytest.raises(typer.Exit):
        _resolve_send_targets(config, "")


def test_default_execution_config_assigns_admin_policy(tmp_enso):
    config = {
        "setup": {"completed_at": None},
        "providers": {
            "claude": {"path": "claude", "models": ["sonnet"]},
            "codex": {"path": "codex", "models": ["terra"]},
        },
        "workspaces": {},
        "policies": {},
    }

    workspace = _ensure_default_execution_config(config)

    assert workspace == "default"
    assert config["workspaces"]["default"] == {
        "policy": "admin",
        "concurrency": 1,
    }
    assert config["policies"]["admin"] == {
        "unrestricted": True,
        "providers": ["claude", "codex"],
        "default_provider": "claude",
        "chat_commands": "*",
    }


def test_default_execution_config_preserves_existing_default_workspace():
    config = {
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {
            "default": {
                "policy": "staff",
                "concurrency": 1,
            }
        },
        "policies": {
            "staff": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            }
        },
    }

    workspace = _ensure_default_execution_config(config)

    assert workspace == "default"
    assert config["workspaces"]["default"] == {
        "policy": "staff",
        "concurrency": 1,
    }
    assert "admin" not in config["policies"]


def test_default_execution_config_does_not_add_default_to_pre_feature_catalog():
    config = {
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {
            "company": {
                "policy": "staff",
                "concurrency": 2,
            }
        },
        "policies": {
            "staff": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            }
        },
    }

    original = copy.deepcopy(config)

    with pytest.raises(ConfigError, match=r"workspaces\.default is required"):
        _ensure_default_execution_config(config)

    assert config == original
    assert config["workspaces"]["company"]["policy"] == "staff"
    assert "admin" not in config["policies"]


def test_default_execution_config_rejects_malformed_workspace_block_without_replacing_it():
    config = {
        "setup": {"completed_at": None},
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {"default": "broken"},
        "policies": {},
    }
    original = copy.deepcopy(config)

    with pytest.raises(ConfigError, match=r"workspaces\.default must be an object"):
        _ensure_default_execution_config(config)

    assert config == original


def test_setup_rejects_legacy_working_dir_before_changes(monkeypatch, capsys):
    config = {"working_dir": "/legacy/workspace"}
    monkeypatch.setattr("enso.cli.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        "enso.cli._setup_providers",
        lambda *_: pytest.fail("setup must stop before mutating legacy config"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 1
    assert config == {"working_dir": "/legacy/workspace"}
    output = " ".join(capsys.readouterr().out.split())
    assert "working_dir is no longer supported" in output
    assert "workspaces" in output


def test_setup_rejects_legacy_workspace_path_before_repository_changes(
    monkeypatch, capsys
):
    config = {
        "workspaces": {
            "default": {
                "path": "/legacy/workspace",
                "policy": "admin",
            }
        }
    }
    monkeypatch.setattr("enso.cli.load_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        "enso.cli._ensure_repository_or_exit",
        lambda: pytest.fail("setup must stop before repository changes"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 1
    output = " ".join(capsys.readouterr().out.split())
    assert "workspaces.default.path is no longer supported" in output
    assert "v1.3-managed-workspaces.md" in output


def test_setup_default_workspace_only_updates_config(monkeypatch, tmp_enso, capsys):
    monkeypatch.setattr(
        "enso.cli.os.makedirs",
        lambda *_args, **_kwargs: pytest.fail("workspace creation belongs to scaffolding"),
    )
    config = {
        "setup": {"completed_at": None},
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {},
        "policies": {},
    }

    assert _setup_default_workspace(config) == "default"

    assert config["workspaces"]["default"] == {
        "policy": "admin",
        "concurrency": 1,
    }
    output = " ".join(capsys.readouterr().out.split())
    assert "workspaces/default" in output
    assert "Policy: admin (unrestricted)" in output


def test_setup_displays_the_existing_default_policy_without_claiming_admin_authority(
    tmp_enso,
    capsys,
):
    config = {
        "setup": {"completed_at": "2026-08-18T12:00:00+00:00"},
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {
            "default": {"policy": "client-safe", "concurrency": 1},
        },
        "policies": {
            "client-safe": {
                "policy_dir": str(Path(tmp_enso, "operator-policy")),
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": [],
            },
        },
    }

    assert _setup_default_workspace(config) == "default"

    output = " ".join(capsys.readouterr().out.split())
    assert "Policy: client-safe (policy-controlled)" in output
    assert "provider-native policy controls apply" in output
    assert "admin" not in output
    assert "full user authority" not in output


def test_fresh_setup_scaffold_seeds_complete_canonical_tree(tmp_enso, monkeypatch):
    from enso.scaffolding import ScaffoldService

    workspace = Path(tmp_enso, "workspaces", "default")
    workspace.rmdir()
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    published: list[str] = []
    real_create = ScaffoldService.create_workspace

    def recording_create(self, name):
        published.append(name)
        return real_create(self, name)

    monkeypatch.setattr(ScaffoldService, "create_workspace", recording_create)

    _scaffold_setup_or_exit(config)

    assert published == ["default"]
    assert Path(tmp_enso, "skills", "policy", "SKILL.md").is_file()
    assert Path(tmp_enso, "skills", "workspace", "SKILL.md").is_file()
    assert os.readlink(Path(tmp_enso, "CLAUDE.md")) == "AGENTS.md"
    assert workspace.joinpath("AGENTS.md").is_file()
    assert workspace.joinpath("knowledge", "README.md").is_file()
    assert os.readlink(workspace / ".agents" / "skills") == "../skills"


def test_completed_setup_does_not_synthesize_a_missing_default_or_admin() -> None:
    config = {
        "setup": {"completed_at": "2026-01-01T00:00:00+00:00"},
        "providers": {"claude": {"path": "claude", "models": ["sonnet"]}},
        "workspaces": {},
        "policies": {},
    }
    original = copy.deepcopy(config)

    with pytest.raises(ConfigError, match=r"workspaces\.default is required"):
        _ensure_default_execution_config(config)

    assert config == original


@pytest.mark.parametrize(
    "setup_block",
    [
        pytest.param({}, id="pre-feature"),
        pytest.param(
            {"setup": {"completed_at": "2026-08-18T12:00:00+00:00"}},
            id="complete",
        ),
    ],
)
def test_nonfresh_setup_repairs_structure_without_reseeding_content(
    setup_block, tmp_enso
):
    from enso.scaffolding import ScaffoldService

    service = ScaffoldService()
    service.seed_fresh_global()
    workspace = Path(tmp_enso, "workspaces", "default")
    workspace.rmdir()
    service.create_workspace("default")
    workspace.joinpath("AGENTS.md").unlink()
    workspace.joinpath("CLAUDE.md").unlink()
    config = {
        **setup_block,
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }

    with pytest.raises(typer.Exit):
        _scaffold_setup_or_exit(config)

    assert not workspace.joinpath("AGENTS.md").exists()
    assert not workspace.joinpath("CLAUDE.md").exists()


def _git_output(root: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", root, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


_INITIAL_SETUP_SUBJECT = "Initialize Enso content"


def _required_initial_paths(*workspace_names: str) -> set[str]:
    paths = {
        ".agents/skills",
        ".claude/skills",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/enso/content_model.md",
        "docs/enso/layout.md",
        "docs/operator.md",
        "skills/docs/SKILL.md",
        "skills/jobs/SKILL.md",
        "skills/policy/SKILL.md",
        "skills/slack/SKILL.md",
        "skills/tables/SKILL.md",
        "skills/workspace/SKILL.md",
    }
    for name in workspace_names:
        base = f"workspaces/{name}"
        paths.update(
            {
                f"{base}/.agents/skills",
                f"{base}/.claude/skills",
                f"{base}/AGENTS.md",
                f"{base}/CLAUDE.md",
                f"{base}/knowledge/README.md",
            }
        )
    return paths


def _initial_snapshot_scopes(*workspace_names: str) -> set[str]:
    paths = {
        ".agents/skills",
        ".claude/skills",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "docs",
        "skills",
    }
    for name in workspace_names:
        base = f"workspaces/{name}"
        paths.update(
            {
                f"{base}/.agents/skills",
                f"{base}/.claude/skills",
                f"{base}/AGENTS.md",
                f"{base}/CLAUDE.md",
                f"{base}/knowledge",
                f"{base}/skills",
            }
        )
    return paths


def test_fresh_setup_finalization_orders_null_seed_snapshot_then_timestamp(monkeypatch):
    from enso import cli as cli_module
    from enso.repository import EnsoRepository

    events: list[tuple[str, object]] = []
    snapshot_paths: tuple[str, ...] = ()
    required_paths = tuple(_required_initial_paths("alpha", "default"))
    config = {
        "setup": {"completed_at": None},
        "workspaces": {
            "alpha": {"policy": "admin", "concurrency": 1},
            "default": {"policy": "admin", "concurrency": 1},
        },
    }

    monkeypatch.setattr(
        cli_module,
        "save_config",
        lambda candidate: events.append(
            ("save", candidate["setup"]["completed_at"])
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "_scaffold_setup_or_exit",
        lambda _candidate: events.append(("scaffold", None)),
    )
    monkeypatch.setattr(cli_module.os.path, "lexists", lambda _path: True)
    monkeypatch.setattr(cli_module.os, "listdir", lambda _path: ["local-skill"])
    monkeypatch.setattr(
        EnsoRepository,
        "ensure",
        lambda _self: events.append(("repository", None)),
    )
    monkeypatch.setattr(
        EnsoRepository,
        "has_head",
        lambda _self: events.append(("head", None)) or False,
        raising=False,
    )
    monkeypatch.setattr(
        EnsoRepository,
        "ignored_paths",
        lambda _self, paths: events.append(("ignored", tuple(paths))) or (),
        raising=False,
    )

    def snapshot(_self, paths, message):
        nonlocal snapshot_paths
        snapshot_paths = tuple(paths)
        events.append(("snapshot", message))
        return True

    monkeypatch.setattr(
        EnsoRepository,
        "snapshot",
        snapshot,
    )
    monkeypatch.setattr(
        EnsoRepository,
        "tracked_paths",
        lambda _self: events.append(("tracked", None)) or required_paths,
        raising=False,
    )
    monkeypatch.setattr(
        EnsoRepository,
        "commit_subject_paths",
        lambda _self, subject: events.append(("marker-paths", subject))
        or (required_paths if snapshot_paths else None),
        raising=False,
    )

    _finalize_setup_or_exit(config)

    assert [event for event, _value in events] == [
        "save",
        "repository",
        "marker-paths",
        "head",
        "scaffold",
        "ignored",
        "snapshot",
        "tracked",
        "marker-paths",
        "save",
    ]
    assert events[0][1] is None
    assert events[2][1] == _INITIAL_SETUP_SUBJECT
    assert events[6][1] == _INITIAL_SETUP_SUBJECT
    assert set(snapshot_paths) == _initial_snapshot_scopes("alpha", "default")
    assert set(events[5][1]) == _required_initial_paths("alpha", "default")
    assert isinstance(events[-1][1], str)


def test_finalize_fresh_setup_seeds_snapshots_then_marks_complete(tmp_enso):
    from datetime import datetime

    from enso.config import SetupState, load_config, setup_state
    from enso.docs import load_docs
    from enso.repository import EnsoRepository

    Path(tmp_enso, "workspaces", "default").rmdir()
    EnsoRepository().ensure()
    custom_doc = Path(tmp_enso, "docs", "custom.md")
    custom_doc.parent.mkdir()
    custom_doc.write_text(
        "---\nname: Custom\ndescription: Existing operator content.\n---\n\nKeep me.\n",
        encoding="utf-8",
    )
    custom_skill = Path(tmp_enso, "skills", "custom", "SKILL.md")
    custom_skill.parent.mkdir(parents=True)
    custom_skill.write_text(
        "---\nname: custom\ndescription: Existing operator workflow.\n---\n\n# Custom\n",
        encoding="utf-8",
    )
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }

    _finalize_setup_or_exit(config)

    persisted = load_config()
    assert setup_state(persisted) is SetupState.COMPLETE
    completed_at = datetime.fromisoformat(persisted["setup"]["completed_at"])
    assert completed_at.utcoffset() is not None
    assert _git_output(tmp_enso, "rev-list", "--count", "HEAD") == "1"
    tracked = set(_git_output(tmp_enso, "ls-files").splitlines())
    assert _required_initial_paths("default") <= tracked
    assert {"docs/custom.md", "skills/custom/SKILL.md"} <= tracked
    assert "config.json" not in tracked
    assert _git_output(tmp_enso, "log", "-1", "--format=%s") == _INITIAL_SETUP_SUBJECT
    assert {
        (doc.rel_path, doc.description)
        for doc in load_docs().docs
    } >= {
        (
            "enso/content_model.md",
            "Where durable Enso context belongs and which source wins; read before "
            "creating, moving, or duplicating persistent knowledge.",
        ),
        (
            "enso/layout.md",
            "The current managed Enso filesystem and local-history boundaries; read "
            "when locating, validating, or repairing installation content.",
        ),
        (
            "operator.md",
            "Confirmed operator identity, locale, communication preferences, and "
            "standing personal context; read when a task depends on those facts.",
        ),
    }

    operator_doc = Path(tmp_enso, "docs", "operator.md")
    operator_doc.write_text("operator-owned content\n", encoding="utf-8")

    _finalize_setup_or_exit(persisted)

    assert _git_output(tmp_enso, "rev-list", "--count", "HEAD") == "1"
    assert operator_doc.read_text(encoding="utf-8") == "operator-owned content\n"


def test_fresh_setup_recovers_a_committed_snapshot_killed_before_index_realign(
    tmp_path,
):
    from datetime import datetime

    home = tmp_path / "home"
    home.mkdir()
    root = home / ".enso"
    custom_content = "operator content survives the crash\n"
    environment = os.environ.copy()
    environment["HOME"] = str(home)

    crash_script = "\n".join(
        (
            "import os",
            "import signal",
            "from pathlib import Path",
            "from enso.cli import _finalize_setup_or_exit",
            "from enso.repository import EnsoRepository",
            "repository = EnsoRepository()",
            "repository.ensure()",
            "root = Path(repository.root)",
            "custom = root / 'docs' / 'custom.md'",
            "custom.parent.mkdir()",
            f"custom.write_text({custom_content!r}, encoding='utf-8')",
            "config = {'setup': {'completed_at': None}, "
            "'workspaces': {'default': {'policy': 'admin', 'concurrency': 1}}}",
            "real_run_git = EnsoRepository._run_git",
            "def crash_after_ref_update(self, args, **kwargs):",
            "    result = real_run_git(self, args, **kwargs)",
            "    if 'update-ref' in args:",
            "        os.kill(os.getpid(), signal.SIGKILL)",
            "    return result",
            "EnsoRepository._run_git = crash_after_ref_update",
            "_finalize_setup_or_exit(config)",
        )
    )
    crashed = subprocess.run(
        [sys.executable, "-c", crash_script],
        check=False,
        capture_output=True,
        env=environment,
        timeout=20,
    )

    assert crashed.returncode == -signal.SIGKILL
    transaction = root / ".snapshot.transaction.json"
    temporary_indexes = tuple((root / ".git").glob(".snapshot-index-*"))
    assert transaction.is_file()
    assert len(temporary_indexes) == 1
    assert json.loads((root / "config.json").read_text(encoding="utf-8"))["setup"] == {
        "completed_at": None
    }
    committed_head = _git_output(str(root), "rev-parse", "HEAD")
    assert _git_output(str(root), "log", "-1", "--format=%s") == _INITIAL_SETUP_SUBJECT
    assert (
        subprocess.run(
            ["git", "-C", str(root), "diff", "--cached", "--quiet", "--exit-code"],
            check=False,
            capture_output=True,
            timeout=10,
        ).returncode
        == 1
    )

    retry_script = "\n".join(
        (
            "import json",
            "from pathlib import Path",
            "from enso.cli import _finalize_setup_or_exit",
            "from enso.config import load_config",
            "from enso.repository import EnsoRepository",
            "root = Path.home() / '.enso'",
            "events = []",
            "real_ensure = EnsoRepository.ensure",
            "real_subject_paths = EnsoRepository.commit_subject_paths",
            "def observed_ensure(self):",
            "    result = real_ensure(self)",
            "    assert not (root / '.snapshot.transaction.json').exists()",
            "    assert not tuple((root / '.git').glob('.snapshot-index-*'))",
            "    clean = self._run_git(",
            "        ['diff', '--cached', '--quiet', '--exit-code'],",
            "        check=False, read_only=True, description='verify recovered setup index',",
            "    )",
            "    assert clean.returncode == 0",
            "    events.append('ensure-recovered')",
            "    return result",
            "def observed_subject_paths(self, subject):",
            "    assert events == ['ensure-recovered']",
            "    assert not (root / '.snapshot.transaction.json').exists()",
            "    assert not tuple((root / '.git').glob('.snapshot-index-*'))",
            "    result = real_subject_paths(self, subject)",
            "    events.append('marker-read')",
            "    return result",
            "EnsoRepository.ensure = observed_ensure",
            "EnsoRepository.commit_subject_paths = observed_subject_paths",
            "_finalize_setup_or_exit(load_config())",
            "(Path.home() / 'retry-events.json').write_text(",
            "    json.dumps(events), encoding='utf-8'",
            ")",
        )
    )
    retried = subprocess.run(
        [sys.executable, "-c", retry_script],
        check=False,
        capture_output=True,
        env=environment,
        timeout=20,
    )

    assert retried.returncode == 0, retried.stderr.decode(errors="replace")
    assert json.loads((home / "retry-events.json").read_text(encoding="utf-8")) == [
        "ensure-recovered",
        "marker-read",
    ]
    persisted = json.loads((root / "config.json").read_text(encoding="utf-8"))
    completed_at = datetime.fromisoformat(persisted["setup"]["completed_at"])
    assert completed_at.utcoffset() is not None
    assert _git_output(str(root), "rev-parse", "HEAD") == committed_head
    assert _git_output(str(root), "rev-list", "--count", "HEAD") == "1"
    assert _git_output(str(root), "log", "--format=%s").splitlines().count(
        _INITIAL_SETUP_SUBJECT
    ) == 1
    assert _required_initial_paths("default") | {"docs/custom.md"} <= set(
        _git_output(str(root), "ls-files").splitlines()
    )
    assert (root / "docs" / "custom.md").read_text(encoding="utf-8") == custom_content
    assert _git_output(str(root), "show", "HEAD:docs/custom.md") == custom_content.rstrip()
    assert _git_output(str(root), "status", "--porcelain") == ""
    assert not transaction.exists()
    assert tuple((root / ".git").glob(".snapshot-index-*")) == ()
    assert not (root / ".git" / "index.lock").exists()


def test_snapshot_failure_leaves_fresh_setup_incomplete(
    tmp_enso, monkeypatch, capsys
):
    from enso.config import SetupState, load_config, setup_state
    from enso.repository import EnsoRepository, RepositoryError

    Path(tmp_enso, "workspaces", "default").rmdir()
    EnsoRepository().ensure()
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    monkeypatch.setattr(
        "enso.repository.EnsoRepository.snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RepositoryError("snapshot failed")),
    )

    with pytest.raises(typer.Exit) as exc_info:
        _finalize_setup_or_exit(config)

    assert exc_info.value.exit_code == 1
    assert setup_state(load_config()) is SetupState.INCOMPLETE
    assert "snapshot failed" in capsys.readouterr().out
    assert (
        subprocess.run(
            ["git", "-C", tmp_enso, "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


@pytest.mark.parametrize("edited_path", ["AGENTS.md", "docs/operator.md"])
def test_timestamp_save_failure_then_user_edit_retries_without_snapshotting_edit(
    tmp_enso, monkeypatch, edited_path
):
    from enso import cli as cli_module
    from enso.config import SetupState, load_config, setup_state
    from enso.repository import EnsoRepository

    Path(tmp_enso, "workspaces", "default").rmdir()
    EnsoRepository().ensure()
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    real_save = cli_module.save_config
    calls = 0

    def fail_completion_save(candidate):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("completion write failed")
        real_save(candidate)

    monkeypatch.setattr(cli_module, "save_config", fail_completion_save)

    with pytest.raises(typer.Exit):
        _finalize_setup_or_exit(config)

    persisted = load_config()
    assert setup_state(persisted) is SetupState.INCOMPLETE
    assert _git_output(tmp_enso, "rev-list", "--count", "HEAD") == "1"

    edited = Path(tmp_enso, edited_path)
    edited.write_text("operator-owned change after initial snapshot\n", encoding="utf-8")
    monkeypatch.setattr(
        EnsoRepository,
        "snapshot",
        lambda *_args, **_kwargs: pytest.fail("marker retry must not call snapshot"),
    )
    _finalize_setup_or_exit(persisted)

    assert setup_state(load_config()) is SetupState.COMPLETE
    assert _git_output(tmp_enso, "rev-list", "--count", "HEAD") == "1"
    assert _git_output(tmp_enso, "status", "--short", "--", edited_path) == (
        f"M {edited_path}"
    )
    assert (
        subprocess.run(
            ["git", "-C", tmp_enso, "diff", "--cached", "--quiet", "--", edited_path],
            check=False,
        ).returncode
        == 0
    )


def test_ignored_required_docs_block_before_head_then_clean_retry_commits_once(
    tmp_enso, capsys
):
    from enso.config import SetupState, load_config, setup_state
    from enso.repository import EnsoRepository

    Path(tmp_enso, "workspaces", "default").rmdir()
    repository = EnsoRepository()
    repository.ensure()
    gitignore = Path(tmp_enso, ".gitignore")
    gitignore.write_text(
        f"/docs/\n\n{gitignore.read_text(encoding='utf-8')}",
        encoding="utf-8",
    )
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }

    with pytest.raises(typer.Exit) as exc_info:
        _finalize_setup_or_exit(config)

    assert exc_info.value.exit_code == 1
    assert setup_state(load_config()) is SetupState.INCOMPLETE
    output = capsys.readouterr().out
    assert "ignored" in output.lower()
    assert "docs/operator.md" in output
    assert (
        subprocess.run(
            ["git", "-C", tmp_enso, "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )

    gitignore.write_text(
        gitignore.read_text(encoding="utf-8").removeprefix("/docs/\n\n"),
        encoding="utf-8",
    )
    _finalize_setup_or_exit(load_config())

    assert setup_state(load_config()) is SetupState.COMPLETE
    assert _git_output(tmp_enso, "rev-list", "--count", "HEAD") == "1"
    assert _git_output(tmp_enso, "log", "-1", "--format=%s") == _INITIAL_SETUP_SUBJECT


def test_missing_required_baseline_stops_before_snapshot_or_commit(
    tmp_enso, monkeypatch, capsys
):
    from enso import cli as cli_module
    from enso.config import SetupState, load_config, setup_state
    from enso.repository import EnsoRepository

    Path(tmp_enso, "workspaces", "default").rmdir()
    EnsoRepository().ensure()
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    real_scaffold = cli_module._scaffold_setup_or_exit

    def omit_required_doc(candidate):
        real_scaffold(candidate)
        Path(tmp_enso, "docs", "operator.md").unlink()

    snapshot = Mock()
    monkeypatch.setattr(cli_module, "_scaffold_setup_or_exit", omit_required_doc)
    monkeypatch.setattr(EnsoRepository, "snapshot", snapshot)

    with pytest.raises(typer.Exit) as exc_info:
        _finalize_setup_or_exit(config)

    assert exc_info.value.exit_code == 1
    assert snapshot.call_count == 0
    assert setup_state(load_config()) is SetupState.INCOMPLETE
    assert "docs/operator.md" in capsys.readouterr().out
    assert (
        subprocess.run(
            ["git", "-C", tmp_enso, "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_fresh_setup_refuses_and_preserves_preexisting_staging(tmp_enso, capsys):
    from enso.config import SetupState, load_config, setup_state
    from enso.repository import EnsoRepository

    Path(tmp_enso, "workspaces", "default").rmdir()
    repository = EnsoRepository()
    repository.ensure()
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    _scaffold_setup_or_exit(config)
    subprocess.run(
        ["git", "-C", tmp_enso, "add", "--", "docs/operator.md"],
        check=True,
    )
    assert _git_output(tmp_enso, "diff", "--cached", "--name-only") == (
        "docs/operator.md"
    )
    staged_content = Path(tmp_enso, "docs", "operator.md").read_text(encoding="utf-8")

    with pytest.raises(typer.Exit) as exc_info:
        _finalize_setup_or_exit(config)

    assert exc_info.value.exit_code == 1
    assert setup_state(load_config()) is SetupState.INCOMPLETE
    assert "staging area is not clean" in " ".join(capsys.readouterr().out.split())
    assert _git_output(tmp_enso, "diff", "--cached", "--name-only") == (
        "docs/operator.md"
    )
    assert Path(tmp_enso, "docs", "operator.md").read_text(encoding="utf-8") == staged_content
    assert (
        subprocess.run(
            ["git", "-C", tmp_enso, "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )
    assert not Path(tmp_enso, ".snapshot.transaction.json").exists()
    assert tuple((Path(tmp_enso) / ".git").glob(".snapshot-index-*")) == ()


def test_similar_unrelated_commit_subject_blocks_fresh_snapshot(tmp_enso, capsys):
    from enso.config import SetupState, load_config, setup_state
    from enso.repository import EnsoRepository

    Path(tmp_enso, "workspaces", "default").rmdir()
    EnsoRepository().ensure()
    subprocess.run(
        ["git", "-C", tmp_enso, "add", "--", "AGENTS.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", tmp_enso, "commit", "--quiet", "-m", f"{_INITIAL_SETUP_SUBJECT} manually"],
        check=True,
    )
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }

    with pytest.raises(typer.Exit) as exc_info:
        _finalize_setup_or_exit(config)

    assert exc_info.value.exit_code == 1
    assert setup_state(load_config()) is SetupState.INCOMPLETE
    assert _git_output(tmp_enso, "rev-list", "--count", "HEAD") == "1"
    assert not Path(tmp_enso, "docs").exists()
    output = " ".join(capsys.readouterr().out.split()).lower()
    assert "history" in output
    assert "initial" in output


def test_initial_marker_below_later_user_commit_skips_snapshot_and_current_tree_check(
    tmp_enso, monkeypatch
):
    from enso import cli as cli_module
    from enso.config import SetupState, load_config, setup_state
    from enso.repository import EnsoRepository

    Path(tmp_enso, "workspaces", "default").rmdir()
    EnsoRepository().ensure()
    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    real_save = cli_module.save_config
    save_calls = 0

    def fail_first_completion(candidate):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("completion write failed")
        real_save(candidate)

    monkeypatch.setattr(cli_module, "save_config", fail_first_completion)
    with pytest.raises(typer.Exit):
        _finalize_setup_or_exit(config)

    operator_doc = Path(tmp_enso, "docs", "operator.md")
    operator_doc.unlink()
    subprocess.run(
        ["git", "-C", tmp_enso, "add", "--update", "--", "docs/operator.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", tmp_enso, "commit", "--quiet", "-m", "Remove starter I do not need"],
        check=True,
    )
    persisted = load_config()
    assert setup_state(persisted) is SetupState.INCOMPLETE
    assert _git_output(tmp_enso, "rev-list", "--count", "HEAD") == "2"
    monkeypatch.setattr(
        EnsoRepository,
        "snapshot",
        lambda *_args, **_kwargs: pytest.fail("historical marker must skip snapshot"),
    )

    _finalize_setup_or_exit(persisted)

    assert setup_state(load_config()) is SetupState.COMPLETE
    assert _git_output(tmp_enso, "rev-list", "--count", "HEAD") == "2"
    assert not operator_doc.exists()
    assert _git_output(tmp_enso, "log", "--format=%s").splitlines().count(
        _INITIAL_SETUP_SUBJECT
    ) == 1


def test_new_snapshot_must_track_every_required_exact_path_before_timestamp(
    monkeypatch, capsys
):
    from enso import cli as cli_module
    from enso.repository import EnsoRepository

    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    required = _required_initial_paths("default")
    saves: list[object] = []
    snapshots = 0
    monkeypatch.setattr(
        cli_module,
        "save_config",
        lambda candidate: saves.append(candidate["setup"]["completed_at"]),
    )
    monkeypatch.setattr(cli_module, "_scaffold_setup_or_exit", lambda _config: None)
    monkeypatch.setattr(cli_module.os.path, "lexists", lambda _path: True)
    monkeypatch.setattr(cli_module.os, "listdir", lambda _path: [])
    monkeypatch.setattr(EnsoRepository, "ensure", lambda _self: None)
    monkeypatch.setattr(EnsoRepository, "has_head", lambda _self: False, raising=False)
    monkeypatch.setattr(
        EnsoRepository, "ignored_paths", lambda _self, _paths: (), raising=False
    )

    def snapshot(_self, _paths, _message):
        nonlocal snapshots
        snapshots += 1
        return True

    monkeypatch.setattr(EnsoRepository, "snapshot", snapshot)
    monkeypatch.setattr(
        EnsoRepository,
        "tracked_paths",
        lambda _self: tuple(required - {"docs/operator.md"}),
        raising=False,
    )
    monkeypatch.setattr(
        EnsoRepository,
        "commit_subject_paths",
        lambda _self, _subject: tuple(required) if snapshots else None,
        raising=False,
    )

    with pytest.raises(typer.Exit) as exc_info:
        _finalize_setup_or_exit(config)

    assert exc_info.value.exit_code == 1
    assert snapshots == 1
    assert saves == [None]
    assert config["setup"]["completed_at"] is None
    assert "docs/operator.md" in capsys.readouterr().out


def test_historical_initial_marker_must_contain_the_complete_required_tree(
    monkeypatch, capsys
):
    from enso import cli as cli_module
    from enso.repository import EnsoRepository

    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    required = _required_initial_paths("default")
    saves: list[object] = []
    monkeypatch.setattr(
        cli_module,
        "save_config",
        lambda candidate: saves.append(candidate["setup"]["completed_at"]),
    )
    monkeypatch.setattr(EnsoRepository, "ensure", lambda _self: None)
    monkeypatch.setattr(
        EnsoRepository,
        "commit_subject_paths",
        lambda _self, _subject: tuple(required - {"skills/docs/SKILL.md"}),
        raising=False,
    )
    monkeypatch.setattr(
        EnsoRepository,
        "snapshot",
        lambda *_args, **_kwargs: pytest.fail("an existing marker must never snapshot"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        _finalize_setup_or_exit(config)

    assert exc_info.value.exit_code == 1
    assert saves == [None]
    assert config["setup"]["completed_at"] is None
    assert "skills/docs/SKILL.md" in capsys.readouterr().out


def test_historical_marker_retry_repairs_structure_without_fresh_seeding(monkeypatch):
    from enso import cli as cli_module
    from enso.repository import EnsoRepository

    config = {
        "setup": {"completed_at": None},
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }
    required = _required_initial_paths("default")
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        cli_module,
        "save_config",
        lambda candidate: events.append(("save", candidate["setup"]["completed_at"])),
    )
    monkeypatch.setattr(
        EnsoRepository,
        "ensure",
        lambda _self: events.append(("repository", None)),
    )
    monkeypatch.setattr(
        EnsoRepository,
        "commit_subject_paths",
        lambda _self, subject: events.append(("marker-paths", subject)) or tuple(required),
        raising=False,
    )

    def scaffold(_config, *, seed_fresh=None):
        events.append(("scaffold", seed_fresh))

    monkeypatch.setattr(cli_module, "_scaffold_setup_or_exit", scaffold)
    monkeypatch.setattr(
        EnsoRepository,
        "snapshot",
        lambda *_args, **_kwargs: pytest.fail("historical marker must skip snapshot"),
    )
    monkeypatch.setattr(
        EnsoRepository,
        "tracked_paths",
        lambda _self: pytest.fail("historical marker must skip current-index checks"),
        raising=False,
    )

    _finalize_setup_or_exit(config)

    assert [event for event, _value in events] == [
        "save",
        "repository",
        "marker-paths",
        "scaffold",
        "save",
    ]
    assert events[3] == ("scaffold", False)


@pytest.mark.parametrize(
    ("setup_block", "expected_state"),
    [
        pytest.param({}, "pre-feature", id="pre-feature"),
        pytest.param(
            {"setup": {"completed_at": "2026-08-18T12:00:00+00:00"}},
            "complete",
            id="complete",
        ),
    ],
)
def test_nonfresh_setup_never_seeds_starter_docs_or_creates_a_snapshot(
    tmp_enso, setup_block, expected_state
):
    from enso.config import load_config, setup_state
    from enso.repository import EnsoRepository
    from enso.scaffolding import ScaffoldService

    workspace = Path(tmp_enso, "workspaces", "default")
    workspace.rmdir()
    service = ScaffoldService()
    service.seed_fresh_global()
    service.create_workspace("default")
    EnsoRepository().ensure()
    config = {
        **setup_block,
        "workspaces": {"default": {"policy": "admin", "concurrency": 1}},
    }

    _finalize_setup_or_exit(config)

    assert setup_state(load_config()).value == expected_state
    assert list(Path(tmp_enso, "docs").iterdir()) == []
    assert (
        subprocess.run(
            ["git", "-C", tmp_enso, "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
        ).returncode
        != 0
    )


def test_setup_rejects_malformed_config_before_scaffolding(monkeypatch, tmp_enso, capsys):
    config_file = Path(tmp_enso, "config.json")
    config_file.write_text("{malformed")
    original = config_file.read_bytes()
    monkeypatch.setattr(
        "enso.cli._setup_providers",
        lambda *_: pytest.fail("setup must stop before mutating the installation"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 1
    assert "Could not read" in capsys.readouterr().out
    assert config_file.read_bytes() == original
    assert not Path(f"{config_file}.lock").exists()


def test_setup_rejects_symlinked_config_root_before_writing(monkeypatch, tmp_path, capsys):
    target = tmp_path / "outside-enso"
    target.mkdir()
    config_root = tmp_path / "enso"
    config_root.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr("enso.config.CONFIG_DIR", str(config_root))
    monkeypatch.setattr("enso.config.CONFIG_FILE", str(config_root / "config.json"))
    monkeypatch.setattr(
        "enso.cli._setup_providers",
        lambda *_: pytest.fail("setup must stop before provider configuration"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 1
    assert "physical directory" in capsys.readouterr().out
    assert list(target.iterdir()) == []


def test_setup_ensures_repository_before_provider_configuration(
    monkeypatch, tmp_enso
):
    events = []
    monkeypatch.setattr(
        "enso.cli._ensure_repository_or_exit",
        lambda: events.append("repository"),
        raising=False,
    )

    def stop_after_repository(_config):
        events.append("providers")
        raise typer.Exit(7)

    monkeypatch.setattr("enso.cli._setup_providers", stop_after_repository)

    with pytest.raises(typer.Exit) as exc_info:
        setup()

    assert exc_info.value.exit_code == 7
    assert events == ["repository", "providers"]


@pytest.mark.parametrize("command", [serve, web])
def test_operational_commands_require_existing_config(
    command, monkeypatch, tmp_enso, capsys
):
    monkeypatch.setattr(
        "enso.core.Runtime",
        lambda *_: pytest.fail("a runtime must not be created without config.json"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        command()

    assert exc_info.value.exit_code == 1
    assert "config.json" in capsys.readouterr().out
    assert not Path(tmp_enso, "config.json").exists()


@pytest.mark.parametrize("command", [serve, web])
def test_operational_startup_validates_before_runtime(command, monkeypatch):
    config = {"transport": "slack"}
    events = []
    monkeypatch.setattr("enso.cli.load_config", lambda: config)

    def stop_at_validation(candidate):
        events.append(candidate)
        raise typer.Exit(9)

    monkeypatch.setattr("enso.cli._validate_installation_or_exit", stop_at_validation)
    monkeypatch.setattr(
        "enso.core.Runtime",
        lambda *_: pytest.fail("runtime construction must follow installation validation"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        command()

    assert exc_info.value.exit_code == 9
    assert events == [config]


@pytest.mark.parametrize("configured_transport", ["", "email", None])
def test_setup_transport_requires_supported_choice(monkeypatch, configured_transport):
    config = {"transport": configured_transport}
    responses = iter(["", "matrix", "slack"])
    entered = []

    def get_input(*_args, **_kwargs):
        response = next(responses)
        entered.append(response)
        return response

    monkeypatch.setattr("enso.cli.Prompt.get_input", get_input)
    monkeypatch.setattr("enso.cli._setup_slack", lambda _: None)
    monkeypatch.setattr(
        "enso.cli._setup_telegram",
        lambda _: pytest.fail("Telegram setup must not run"),
    )

    _setup_transport(config)

    assert entered == ["", "matrix", "slack"]
    assert config["transport"] == "slack"


def test_setup_transport_keeps_supported_existing_choice_as_default(monkeypatch):
    config = {"transport": "telegram"}
    monkeypatch.setattr("enso.cli.Prompt.get_input", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("enso.cli._setup_telegram", lambda _: 123)
    monkeypatch.setattr(
        "enso.cli._setup_slack",
        lambda _: pytest.fail("Slack setup must not run"),
    )

    assert _setup_transport(config) == 123
    assert config["transport"] == "telegram"


def test_launchd_service_has_no_process_working_directory(monkeypatch, tmp_path):
    plist = tmp_path / "enso.plist"
    monkeypatch.setattr("enso.cli._LAUNCHD_PLIST", str(plist))
    monkeypatch.setattr(
        "enso.cli.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert _install_launchd("/venv/bin/enso")

    content = plist.read_text()
    assert "WorkingDirectory" not in content
    assert "<string>/venv/bin/enso</string>" in content


def test_systemd_service_has_no_process_working_directory(monkeypatch, tmp_path):
    service_dir = tmp_path / "systemd"
    original_expanduser = os.path.expanduser
    monkeypatch.setattr(
        "enso.cli.os.path.expanduser",
        lambda path: str(service_dir)
        if path == "~/.config/systemd/user"
        else original_expanduser(path),
    )
    monkeypatch.setattr(
        "enso.cli.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert _install_systemd("/venv/bin/enso")

    content = (service_dir / "enso.service").read_text()
    assert "WorkingDirectory=" not in content
    assert "ExecStart=/venv/bin/enso serve" in content


def test_slack_send_target_resolves_1password_reference(monkeypatch):
    monkeypatch.delenv("ENSO_ORIGIN_CHANNEL", raising=False)
    config = {
        "transport": "slack",
        "transports": {
            "slack": {
                "bot_token_1password": {
                    "item": "Slack",
                    "field": "BOT_TOKEN",
                },
                "notify_channel": "C123",
            },
        },
    }
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-slack-token",
    )

    transport, token, targets, thread_ts = _resolve_send_targets(config, "")

    assert (transport, token, targets, thread_ts) == (
        "slack",
        "resolved-slack-token",
        ["C123"],
        "",
    )


def test_telegram_setup_validates_existing_token_and_binds_default_workspace(
    monkeypatch, tmp_enso
):
    config = {
        "transports": {
            "telegram": {
                "bot_token_1password": {
                    "item": "Telegram",
                    "field": "TOKEN",
                },
                "allowed_users": ["123"],
            },
        },
    }
    _add_default_execution_catalog(config)
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-telegram-token",
    )
    monkeypatch.setattr(
        "enso.cli._tg_validate_token",
        lambda token: {"username": "enso_test"} if token == "resolved-telegram-token" else None,
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: False)

    assert _setup_telegram(config) is None
    telegram = config["transports"]["telegram"]
    assert "bot_token" not in telegram
    assert telegram["workspace"] == "default"
    assert "path" not in config["workspaces"]["default"]
    assert config["workspaces"]["default"]["policy"] == "admin"


def test_telegram_setup_does_not_synthesize_default_for_pre_feature_catalog(monkeypatch):
    config = {
        "transports": {
            "telegram": {
                "bot_token": "token",
                "allowed_users": ["123"],
            },
        },
        "workspaces": {
            "company": {
                "policy": "staff",
                "concurrency": 1,
            },
        },
        "policies": {
            "staff": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
        },
    }
    monkeypatch.setattr("enso.cli.resolve_config_secret", lambda cfg, key: "token")
    monkeypatch.setattr(
        "enso.cli._tg_validate_token", lambda token: {"username": "enso_test"}
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: False)
    original = copy.deepcopy(config)

    with pytest.raises(ConfigError, match=r"workspaces\.default is required"):
        _setup_telegram(config)

    assert config == original
    assert "admin" not in config["policies"]


def test_slack_setup_validates_resolved_existing_token(monkeypatch):
    config = {
        "transports": {
            "slack": {
                "bot_token_1password": {
                    "item": "Slack",
                    "field": "BOT_TOKEN",
                },
                "account_id": "T123",
                "dms": {"U123": {"workspace": "default"}},
                "channels": {},
                "channel_defaults": {"mention_required": False},
            },
        },
    }
    _add_default_execution_catalog(config)
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "resolved-slack-token",
    )
    monkeypatch.setattr(
        "enso.cli._slack_validate_token",
        lambda token: {"user": "enso", "team_id": "T123"}
        if token == "resolved-slack-token"
        else None,
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: False)
    write_manifest = Mock(return_value="/tmp/slack-manifest.yaml")
    monkeypatch.setattr("enso.cli._write_slack_manifest_copy", write_manifest)

    assert _setup_slack(config) is None
    assert "bot_token" not in config["transports"]["slack"]
    assert config == original
    write_manifest.assert_called_once_with()


def test_slack_setup_rejects_legacy_routes_before_writing(monkeypatch, capsys):
    config = {
        "transports": {
            "slack": {
                "bot_token": "old-bot",
                "app_token": "old-app",
            },
        },
        "routes": {
            "slack": {
                "account_id": "T1",
                "dms": {"UOLD": {"workspace": "default"}},
                "channels": {},
            },
        },
    }
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: pytest.fail("setup must not write before legacy config is migrated"),
    )

    with pytest.raises(typer.Exit) as exc_info:
        _setup_slack(config)

    assert exc_info.value.exit_code == 1
    assert config == original
    output = " ".join(capsys.readouterr().out.split())
    assert "move routes.slack fields into transports.slack" in output


def test_telegram_setup_reconfiguration_updates_reference_without_plaintext(
    monkeypatch,
):
    reference = {"item": "Telegram", "field": "TOKEN"}
    config = {
        "transports": {
            "telegram": {
                "bot_token_1password": reference,
                "bot_token": "stale-literal",
                "allowed_users": ["123"],
                "allowed_user_ids": [999],
            },
        },
    }
    _add_default_execution_catalog(config)
    updates = []
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "old-token",
    )
    monkeypatch.setattr(
        "enso.cli._tg_validate_token",
        lambda token: {
            "username": "old_bot" if token == "old-token" else "new_bot",
        },
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "enso.cli.Prompt.ask",
        lambda *args, **kwargs: "new-token",
    )
    monkeypatch.setattr(
        "enso.cli.update_config_secret_reference",
        lambda cfg, key, value: updates.append((cfg, key, value)) or True,
    )
    monkeypatch.setattr(
        "enso.cli._tg_wait_for_message",
        lambda token, timeout: {
            "user_id": 456,
            "first_name": "Tester",
            "chat_id": 456,
        },
    )

    assert _setup_telegram(config) == 456
    telegram = config["transports"]["telegram"]
    assert telegram["bot_token_1password"] is reference
    assert "bot_token" not in telegram
    assert "allowed_user_ids" not in telegram
    assert telegram["allowed_users"] == ["456"]
    assert telegram["notify_channel"] == "456"
    assert telegram["workspace"] == "default"
    assert updates == [
        (
            {
                "bot_token_1password": reference,
                "bot_token": "stale-literal",
                "allowed_users": ["123"],
                "allowed_user_ids": [999],
            },
            "bot_token",
            "new-token",
        ),
    ]


def test_slack_setup_reconfiguration_updates_references_without_plaintext(
    monkeypatch,
):
    bot_reference = {"item": "Slack", "field": "BOT_TOKEN"}
    app_reference = {"item": "Slack", "field": "APP_TOKEN"}
    config = {
        "transports": {
            "slack": {
                "bot_token_1password": bot_reference,
                "app_token_1password": app_reference,
                "bot_token": "stale-bot-literal",
                "app_token": "stale-app-literal",
                "notify_channel": "COLD",
                "channel_context_messages": 12,
                "rich_messages": False,
                "persistent_surfaces": False,
                "account_id": "T1",
                "channel_defaults": {"mention_required": False},
                "dms": {"UOLD": {"workspace": "company"}},
                "channels": {"CSTAFF": {"workspace": "company"}},
            },
        },
        "workspaces": {
            "company": {"policy": "admin", "concurrency": 1},
        },
        "policies": {
            "admin": {
                "unrestricted": True,
                "providers": ["claude"],
                "default_provider": "claude",
                "chat_commands": "*",
            },
        },
    }
    updates = []
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "old-bot-token",
    )

    def validate(token):
        if token == "old-bot-token":
            return {"user": "old-bot", "user_id": "UOLD", "team_id": "T1"}
        return {"user": "new-bot", "user_id": "UNEWBOT", "team_id": "T1"}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot-token"
        if "App Token" in label:
            return "new-app-token"
        if "Notify channel" in label:
            return "CNEW"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli._slack_validate_token", validate)
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )
    monkeypatch.setattr(
        "enso.cli.update_config_secret_reference",
        lambda cfg, key, value: updates.append((key, value)) or True,
    )

    assert _setup_slack(config) is None
    slack = config["transports"]["slack"]
    assert slack["bot_token_1password"] is bot_reference
    assert slack["app_token_1password"] is app_reference
    assert "bot_token" not in slack
    assert "app_token" not in slack
    assert slack["bot_user_id"] == "UNEWBOT"
    assert "allowed_users" not in slack
    assert slack["notify_channel"] == "CNEW"
    assert slack["channel_context_messages"] == 12
    assert slack["rich_messages"] is False
    assert slack["persistent_surfaces"] is False
    assert slack["account_id"] == "T1"
    assert slack["channel_defaults"] == {"mention_required": False}
    assert slack["dms"] == {"UOLD": {"workspace": "company"}}
    assert slack["channels"] == {"CSTAFF": {"workspace": "company"}}
    assert "routes" not in config
    assert updates == [
        ("bot_token", "new-bot-token"),
        ("app_token", "new-app-token"),
    ]


@pytest.mark.parametrize(
    ("route_key", "route_value"),
    [
        ("dms", {"UOLD": {"workspace": "company"}}),
        ("channels", {"CSTAFF": {"workspace": "company"}}),
    ],
)
def test_slack_setup_preserves_routes_when_other_map_is_omitted(
    monkeypatch,
    route_key,
    route_value,
):
    config = {
        "transports": {
            "slack": {
                "bot_token": "old-bot",
                "app_token": "old-app",
                "account_id": "T1",
                route_key: route_value,
            },
        },
    }

    def validate(token):
        return {"user": "enso", "user_id": "UBOT", "team_id": "T1"}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot"
        if "App Token" in label:
            return "new-app"
        if "Notify channel" in label:
            return ""
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli._slack_validate_token", validate)
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )

    _setup_slack(config)

    slack = config["transports"]["slack"]
    assert slack[route_key] == route_value
    assert slack["dms"] == (route_value if route_key == "dms" else {})
    assert slack["channels"] == (route_value if route_key == "channels" else {})


def test_slack_setup_replaces_only_routing_for_a_different_account(monkeypatch):
    config = {
        "transports": {
            "slack": {
                "bot_token": "old-bot",
                "app_token": "old-app",
                "channel_context_messages": 7,
                "rich_messages": False,
                "account_id": "T1",
                "channel_defaults": {"mention_required": False},
                "dms": {"UOLD": {"workspace": "default"}},
                "channels": {"COLD": {"workspace": "default"}},
            }
        },
    }
    _add_default_execution_catalog(config)
    confirmations = iter([True, True])

    def validate(token):
        team = "T1" if token == "old-bot" else "T2"
        return {"user": "enso", "user_id": "UBOT2", "team_id": team}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot"
        if "App Token" in label:
            return "new-app"
        if "Owner Slack user ID" in label:
            return "UNEW"
        if "Notify channel" in label:
            return "CNEW"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli._slack_validate_token", validate)
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: next(confirmations))
    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )

    _setup_slack(config)

    slack = config["transports"]["slack"]
    assert slack["account_id"] == "T2"
    assert slack["dms"] == {"UNEW": {"workspace": "default"}}
    assert slack["channels"] == {}
    assert "channel_defaults" not in slack
    assert slack["channel_context_messages"] == 7
    assert slack["rich_messages"] is False


def test_slack_setup_account_change_cancel_preserves_config(monkeypatch):
    config = {
        "transports": {
            "slack": {
                "bot_token": "old-bot",
                "app_token": "old-app",
                "account_id": "T1",
                "dms": {"UOLD": {"workspace": "default"}},
                "channels": {},
            }
        }
    }
    original = copy.deepcopy(config)
    confirmations = iter([True, False])

    def validate(token):
        team = "T1" if token == "old-bot" else "T2"
        return {"user": "enso", "user_id": "UBOT2", "team_id": team}

    def prompt(label, **kwargs):
        if "Bot Token" in label:
            return "new-bot"
        if "App Token" in label:
            return "new-app"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli._slack_validate_token", validate)
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: next(confirmations))
    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )
    monkeypatch.setattr(
        "enso.cli._update_referenced_secrets_with_rollback_or_exit",
        lambda *args, **kwargs: pytest.fail("credential writes must not run after cancel"),
    )

    _setup_slack(config)

    assert config == original


def test_reconfiguration_write_failure_keeps_config_and_exits_clearly(
    monkeypatch, capsys,
):
    config = {
        "transports": {
            "telegram": {
                "bot_token_1password": {
                    "item": "Telegram",
                    "field": "TOKEN",
                },
                "allowed_users": ["123"],
            },
        },
    }
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda cfg, key: "old-token",
    )
    monkeypatch.setattr(
        "enso.cli._tg_validate_token",
        lambda token: {"username": "enso_bot"},
    )
    monkeypatch.setattr("enso.cli.Confirm.ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "enso.cli.Prompt.ask",
        lambda *args, **kwargs: "new-token",
    )

    def fail(*args, **kwargs):
        raise SecretResolutionError("helper exit 9")

    monkeypatch.setattr("enso.cli.update_config_secret_reference", fail)

    with pytest.raises(typer.Exit):
        _setup_telegram(config)

    assert config == original
    assert "Could not save Telegram bot token" in capsys.readouterr().out


def test_slack_reference_updates_prevalidate_every_old_value(monkeypatch, capsys):
    config = {
        "bot_token_1password": {"item": "Slack", "field": "BOT"},
        "app_token_1password": {"item": "Slack", "field": "APP"},
    }
    writes = []

    def resolve(_config, key):
        if key == "app_token":
            raise SecretResolutionError("sensitive helper output")
        return "old-bot-secret"

    monkeypatch.setattr("enso.cli.resolve_config_secret", resolve)
    monkeypatch.setattr(
        "enso.cli.update_config_secret_reference",
        lambda *args: writes.append(args) or True,
    )

    with pytest.raises(typer.Exit):
        _update_referenced_secrets_with_rollback_or_exit(
            config,
            [
                ("bot_token", "new-bot-secret", "Slack bot token"),
                ("app_token", "new-app-secret", "Slack app token"),
            ],
        )

    output = " ".join(capsys.readouterr().out.split())
    assert writes == []
    assert "existing Slack app token could not be loaded" in output
    assert "sensitive helper output" not in output
    assert "old-bot-secret" not in output


def test_slack_reference_update_rolls_back_earlier_write(monkeypatch, capsys):
    config = {
        "bot_token_1password": {"item": "Slack", "field": "BOT"},
        "app_token_1password": {"item": "Slack", "field": "APP"},
    }
    old_values = {
        "bot_token": "old-bot-secret",
        "app_token": "old-app-secret",
    }
    writes = []

    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda _config, key: old_values[key],
    )

    def update(_config, key, value):
        writes.append((key, value))
        if key == "app_token":
            raise SecretResolutionError("new-app-secret must not leak")
        return True

    monkeypatch.setattr("enso.cli.update_config_secret_reference", update)

    with pytest.raises(typer.Exit):
        _update_referenced_secrets_with_rollback_or_exit(
            config,
            [
                ("bot_token", "new-bot-secret", "Slack bot token"),
                ("app_token", "new-app-secret", "Slack app token"),
            ],
        )

    output = " ".join(capsys.readouterr().out.split())
    assert writes == [
        ("bot_token", "new-bot-secret"),
        ("app_token", "new-app-secret"),
        ("bot_token", "old-bot-secret"),
    ]
    assert "Earlier referenced credential updates were restored" in output
    assert "new-app-secret" not in output
    assert "old-bot-secret" not in output


def test_slack_reference_update_reports_rollback_failure_without_secrets(
    monkeypatch, capsys,
):
    config = {
        "bot_token_1password": {"item": "Slack", "field": "BOT"},
        "app_token_1password": {"item": "Slack", "field": "APP"},
    }
    old_values = {
        "bot_token": "old-bot-secret",
        "app_token": "old-app-secret",
    }
    writes = []

    monkeypatch.setattr(
        "enso.cli.resolve_config_secret",
        lambda _config, key: old_values[key],
    )

    def update(_config, key, value):
        writes.append((key, value))
        if key == "app_token":
            raise SecretResolutionError("new-app-secret must not leak")
        if value == "old-bot-secret":
            raise SecretResolutionError("old-bot-secret must not leak")
        return True

    monkeypatch.setattr("enso.cli.update_config_secret_reference", update)

    with pytest.raises(typer.Exit):
        _update_referenced_secrets_with_rollback_or_exit(
            config,
            [
                ("bot_token", "new-bot-secret", "Slack bot token"),
                ("app_token", "new-app-secret", "Slack app token"),
            ],
        )

    output = " ".join(capsys.readouterr().out.split())
    assert writes[-1] == ("bot_token", "old-bot-secret")
    assert "Rollback also failed for: Slack bot token" in output
    assert "Referenced credentials may be inconsistent" in output
    for secret in (*old_values.values(), "new-bot-secret", "new-app-secret"):
        assert secret not in output


def test_slack_setup_reprompts_until_app_token_provided(monkeypatch, capsys):
    """A blank app token silently breaks Socket Mode later (or aborts a
    referenced update with a misleading 1Password error), so setup must
    insist on one just like it does for the bot token."""
    config: dict = _add_default_execution_catalog({})
    app_prompts = 0

    def prompt(label, **kwargs):
        nonlocal app_prompts
        if "Bot Token" in label:
            return "xoxb-new"
        if "App Token" in label:
            app_prompts += 1
            return "" if app_prompts == 1 else "xapp-new"
        if "Owner Slack user ID" in label:
            return "UOWNER"
        if "Notify channel" in label:
            return "C123"
        raise AssertionError(f"Unexpected prompt: {label}")

    monkeypatch.setattr("enso.cli.Prompt.ask", prompt)
    monkeypatch.setattr(
        "enso.cli._slack_validate_token",
        lambda token: {"user": "enso", "user_id": "UBOT", "team_id": "T1"},
    )
    monkeypatch.setattr(
        "enso.cli._write_slack_manifest_copy",
        lambda: "/tmp/slack-manifest.yaml",
    )

    _setup_slack(config)

    slack = config["transports"]["slack"]
    assert app_prompts == 2
    assert slack["app_token"] == "xapp-new"
    assert "allowed_users" not in slack
    assert slack["account_id"] == "T1"
    assert slack["dms"] == {"UOWNER": {"workspace": "default"}}
    assert slack["channels"] == {}
    assert "routes" not in config
    assert "Token is required" in capsys.readouterr().out


def test_serve_reports_secret_resolution_failure_cleanly(
    monkeypatch, tmp_path, capsys,
):
    """`enso serve` must exit with a one-line credential error like every
    other command instead of surfacing a raw traceback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "enso.cli.load_config",
        lambda: {"transport": "slack"},
    )
    monkeypatch.setattr("enso.cli.configure_logging", lambda *a, **k: {})
    monkeypatch.setattr("enso.cli._load_secret_env", lambda: [])
    monkeypatch.setattr("enso.cli._validate_installation_or_exit", lambda _config: None)

    class FakeRuntime:
        def __init__(self, config):
            pass

        def install_system_prompts(self):
            pytest.fail("serve must not install or upgrade user-owned content")

        def install_workspaces(self):
            pytest.fail("serve must not create or repair workspace content")

        def load_state(self):
            pass

    monkeypatch.setattr("enso.core.Runtime", FakeRuntime)

    def fail(name, runtime):
        raise SecretResolutionError(
            "Could not resolve bot_token from 1Password (helper exit 1)"
        )

    monkeypatch.setattr("enso.cli._load_transport", fail)

    with pytest.raises(typer.Exit) as excinfo:
        serve(transport=None)

    assert excinfo.value.exit_code == 1
    out = capsys.readouterr().out
    assert "Could not load transport credentials" in out
    assert "helper exit 1" in out


# ---------------------------------------------------------------------------
# Slack helper payloads include thread_ts when set
# ---------------------------------------------------------------------------


class _FakeResp:
    status = 200

    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return b'{"ok": true}'


def test_slack_send_message_includes_thread_ts(monkeypatch):
    """_slack_send_message adds thread_ts to chat.postMessage payload."""
    import json

    from enso import cli as cli_mod

    captured: dict = {}

    def _fake_urlopen(req, timeout=10):
        captured["data"] = json.loads(req.data)
        captured["url"] = req.full_url
        return _FakeResp()

    monkeypatch.setattr(cli_mod.urllib.request, "urlopen", _fake_urlopen)

    ok = cli_mod._slack_send_message(
        "xoxb-fake", "C012345", "hi", thread_ts="1700000000.123",
    )
    assert ok is True
    assert captured["data"] == {
        "channel": "C012345",
        "text": "hi",
        "thread_ts": "1700000000.123",
    }


def test_slack_send_message_no_thread(monkeypatch):
    """Without thread_ts the payload stays clean."""
    import json

    from enso import cli as cli_mod

    captured: dict = {}

    def _fake_urlopen(req, timeout=10):
        captured["data"] = json.loads(req.data)
        return _FakeResp()

    monkeypatch.setattr(cli_mod.urllib.request, "urlopen", _fake_urlopen)

    cli_mod._slack_send_message("xoxb-fake", "C012345", "hi")
    assert "thread_ts" not in captured["data"]


# ---------------------------------------------------------------------------
# Service control
# ---------------------------------------------------------------------------


def test_service_restart_unknown_platform_returns_false(monkeypatch):
    """On a platform with no service manager (and no os.getuid), restart
    returns False instead of raising."""
    from enso import cli as cli_mod

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delattr(cli_mod.os, "getuid", raising=False)
    assert cli_mod._service_restart() is False
