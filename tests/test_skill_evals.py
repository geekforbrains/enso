"""Offline evaluation contracts: raw measurements, fixture checks and CLI orchestration."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from enso.skill_evals import cli, reports, runner
from enso.skill_evals.events import measure
from enso.skill_evals.scenarios import check_results, load_scenario, prepare, safe_path

REPO = Path(__file__).resolve().parents[1]
SCENARIO = REPO / "evals/skills/tables-import.json"


@pytest.fixture(autouse=True)
def isolated_evaluation_user(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "CODEX_HOME",
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def measurements(tmp_path, provider, events):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    return measure(path, provider).as_dict()


def codex_tool(kind, ident, **kwargs):
    return {"type": kind, "item": {"id": ident, "type": "command_execution", **kwargs}}


def codex_usage(**kwargs):
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 1000,
            "cached_input_tokens": 800,
            "output_tokens": 40,
            **kwargs,
        },
    }


def test_codex_counts_calls_once_and_keeps_cache_inside_input(tmp_path):
    result = measurements(
        tmp_path,
        "codex",
        [
            codex_tool("item.started", "1"),
            codex_tool("item.updated", "1"),
            codex_tool("item.completed", "1", exit_code=0),
            codex_tool("item.completed", "1", exit_code=0),
            codex_tool("item.completed", "2", exit_code=1),
            {
                "type": "item.completed",
                "item": {
                    "id": "3",
                    "type": "mcp_tool_call",
                    "result": {"isError": True},
                },
            },
            {
                "type": "item.completed",
                "item": {"id": "4", "type": "agent_message", "text": "Done"},
            },
            codex_usage(),
        ],
    )
    assert (result["input_tokens"], result["cached_input_tokens"], result["output_tokens"]) == (
        1000,
        800,
        40,
    )
    assert (result["tool_calls"], result["tool_failures"]) == (3, 2)
    assert result["measurement_complete"] and result["output"] == "Done"


@pytest.mark.parametrize(
    "events",
    [
        [codex_tool("item.started", "1"), codex_usage()],
        [codex_usage(input_tokens=-1)],
        [codex_usage(input_tokens=True)],
        [codex_usage(output_tokens=None)],
        [{"type": "item.completed", "item": {"type": "new_tool", "id": "1"}}, codex_usage()],
        [],
    ],
)
def test_missing_or_unknown_measurements_never_certify_a_run(tmp_path, events):
    assert not measurements(tmp_path, "codex", events)["measurement_complete"]


def test_malformed_lines_are_retained_as_measurement_warnings(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("not JSON\n" + json.dumps(codex_usage()) + "\n")
    result = measure(path, "codex").as_dict()
    assert not result["measurement_complete"]
    assert "Event 1" in result["warnings"][0]


def test_claude_counts_tool_ids_and_terminal_model_usage_without_double_counting(tmp_path):
    tool = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "a", "name": "Bash"}]},
    }
    result = measurements(
        tmp_path,
        "claude",
        [
            tool,
            tool,
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "a", "is_error": True}]
                },
            },
            {"type": "assistant", "message": {"usage": {"input_tokens": 999999}, "content": []}},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Finished",
                "usage": {"input_tokens": 9000, "output_tokens": 9000},
                "modelUsage": {
                    "primary": {
                        "inputTokens": 100,
                        "cacheReadInputTokens": 200,
                        "cacheCreationInputTokens": 50,
                        "outputTokens": 10,
                    },
                    "helper": {
                        "inputTokens": 30,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "outputTokens": 5,
                    },
                },
            },
        ],
    )
    assert result["input_tokens"] == 380
    assert result["cached_input_tokens"] == 200
    assert result["cache_creation_tokens"] == 50
    assert result["output_tokens"] == 15
    assert (result["tool_calls"], result["tool_failures"]) == (1, 1)
    assert result["measurement_complete"]
    assert result["models"] == ["helper", "primary"]


def test_claude_missing_complete_usage_and_permission_denials_are_explicit(tmp_path):
    result = measurements(
        tmp_path,
        "claude",
        [
            {
                "type": "result",
                "subtype": "success",
                "usage": {
                    "input_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                    "output_tokens": 10,
                },
                "permission_denials": [{"tool_name": "Bash", "tool_use_id": "denied-1"}],
            }
        ],
    )
    assert result["input_tokens"] == 90
    assert not result["measurement_complete"]
    assert "conversation" in result["usage_source"]
    assert result["tool_failures"] == 1 and result["permission_denials"]
    assert not result["errors"]  # A recovered failed call does not fail the entire task.


def test_scenario_checks_final_database_and_protects_internal_state(tmp_path):
    scenario = load_scenario(SCENARIO)
    home = tmp_path / "enso"
    workspace = home / "workspaces/eval"
    original = prepare(home, workspace, scenario)
    before = check_results(home, workspace, scenario, original)
    assert not all(c["passed"] for c in before)
    with sqlite3.connect(home / "enso.db") as con:
        con.execute(
            "INSERT INTO weight_entries(recorded_at, weight_kg) "
            "VALUES ('2026-09-02T08:00:00Z', 79.5)"
        )
        con.execute(
            "INSERT INTO weight_entries(recorded_at, weight_kg) "
            "VALUES ('2026-09-03T08:00:00Z', 79.0)"
        )
    (workspace / "drafts").mkdir()
    (workspace / "drafts/weight-summary.json").write_text('{"count":3,"average_kg":79.5}')
    assert all(c["passed"] for c in check_results(home, workspace, scenario, original))
    with sqlite3.connect(home / "enso.db") as con:
        con.execute("DROP TABLE sessions")
    assert not check_results(home, workspace, scenario, original)[-1]["passed"]


def test_scenario_rejects_escaping_paths_and_empty_checks(tmp_path):
    scenario = json.loads(SCENARIO.read_text())
    scenario["files"]["../escape"] = "bad"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(scenario))
    with pytest.raises(ValueError, match="relative path"):
        load_scenario(path)
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        safe_path(tmp_path, "link/escape")
    scenario["files"] = {}
    scenario["checks"] = []
    path.write_text(json.dumps(scenario))
    with pytest.raises(ValueError, match="expected-result"):
        load_scenario(path)


def test_checks_refuse_provider_created_database_symlinks(tmp_path):
    scenario = load_scenario(SCENARIO)
    home = tmp_path / "enso"
    workspace = home / "workspaces/eval"
    original = prepare(home, workspace, scenario)
    database = home / "enso.db"
    database.unlink()
    database.symlink_to(tmp_path / "outside.db")
    results = check_results(home, workspace, scenario, original)
    assert all(not result["passed"] for result in results)
    assert "symlink" in results[0]["error"]
    assert not (tmp_path / "outside.db").exists()


def test_scenarios_can_assert_intended_internal_changes(tmp_path):
    scenario = load_scenario(SCENARIO)
    scenario["protect_internal_state"] = False
    scenario["checks"] = [
        {
            "name": "sessions table removed",
            "kind": "sql",
            "query": "SELECT name FROM sqlite_master WHERE name = 'sessions'",
            "expected": [],
        }
    ]
    home = tmp_path / "enso"
    workspace = home / "workspaces/eval"
    original = prepare(home, workspace, scenario)
    assert original is None
    with sqlite3.connect(home / "enso.db") as con:
        con.execute("DROP TABLE sessions")
    assert check_results(home, workspace, scenario, original)[0]["passed"]


def test_environment_drops_live_enso_context_credentials_and_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("ENSO_HOME", "/real/enso")
    monkeypatch.setenv("ENSO_TASK", "EN-999")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("BASH_ENV", "/real/hook")
    monkeypatch.setenv("PYTHONPATH", "/real/python")
    env = runner.environment(tmp_path, tmp_path / "fixture", "codex")
    assert env["ENSO_HOME"] == str(tmp_path / "fixture")
    assert env["HOME"] == str(tmp_path / "user")
    assert not {"ENSO_TASK", "AWS_SECRET_ACCESS_KEY", "BASH_ENV", "PYTHONPATH"} & env.keys()


def test_launches_only_local_clis_with_requested_model_effort_and_scoped_settings(tmp_path):
    for provider in ("codex", "claude"):
        args = runner.command(
            provider, provider, "test-model", "high", tmp_path / "home", tmp_path / "ws", tmp_path
        )
        assert args[0] == provider and args[args.index("--model") + 1] == "test-model"
        assert not any("dangerously" in arg for arg in args)
        if provider == "codex":
            assert 'model_reasoning_effort="high"' in args
            assert args[args.index("--sandbox") + 1] == "workspace-write"
        else:
            assert args[args.index("--effort") + 1] == "high"
            assert args[args.index("--setting-sources") + 1] == "project"
            assert "--strict-mcp-config" in args


def test_process_timeout_retains_partial_events(tmp_path):
    code, status, _ = runner.execute(
        [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(10)"],
        "x" * 100_000,
        tmp_path,
        {"PATH": os.defpath},
        tmp_path,
        0.2,
    )
    assert status == "timeout" and code != 0
    assert (tmp_path / "events.jsonl").read_text() == "partial\n"


def test_process_output_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAX_OUTPUT", 100)
    code, status, _ = runner.execute(
        [sys.executable, "-c", "import time; print('x'*200, flush=True); time.sleep(10)"],
        "",
        tmp_path,
        {"PATH": os.defpath},
        tmp_path,
        2,
    )
    assert status == "output_limit" and code != 0


def test_real_cli_entrypoint_offline_with_fake_provider(tmp_path, monkeypatch):
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json,os,sqlite3,sys
from pathlib import Path
if '--version' in sys.argv:
 print('fake-codex 1.0'); raise SystemExit
assert 'ENSO_TASK' not in os.environ
home=Path(os.environ['ENSO_HOME'])
with sqlite3.connect(home/'enso.db') as con:
 con.execute("INSERT INTO weight_entries(recorded_at,weight_kg) "
             "VALUES ('2026-09-02T08:00:00Z',79.5)")
 con.execute("INSERT INTO weight_entries(recorded_at,weight_kg) "
             "VALUES ('2026-09-03T08:00:00Z',79.0)")
Path('drafts').mkdir()
Path('drafts/weight-summary.json').write_text(' {"count":3,"average_kg":79.5}')
print(json.dumps({'type':'item.started','item':{'id':'one','type':'command_execution'}}))
print(json.dumps({'type':'item.completed','item':{'id':'one','type':'command_execution','exit_code':0}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':120,'cached_input_tokens':100,'output_tokens':12}}))
"""
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.defpath)
    output = tmp_path / "report"
    args = cli.parser().parse_args(
        [
            "run",
            "--skill",
            "enso-tables",
            "--provider",
            "codex",
            "--model",
            "test-model",
            "--effort",
            "low",
            "--trials",
            "2",
            "--output",
            str(output),
        ]
    )
    assert cli.compare(args, REPO) == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert [r["variant"] for r in manifest["runs"]] == [
        "baseline",
        "candidate",
        "candidate",
        "baseline",
    ]
    assert manifest["packages"]["baseline"] == manifest["packages"]["candidate"]
    report = (output / "report.md").read_text()
    assert "human review pending" in report and "120" in report
    assert "Identical packages" in report
    assert "0 absolute (baseline zero)" in report
    for entry in manifest["runs"]:
        result = json.loads((output / entry["id"] / "result.json").read_text())
        assert result["checks_passed"] and result["measurements"]["measurement_complete"]
        assert result["review"] == "pending"
        assert (output / entry["id"] / "state/enso.db").is_file()
    with pytest.raises(FileExistsError):
        cli.compare(args, REPO)


def test_failures_cannot_look_like_improvements():
    good = {
        "variant": "baseline",
        "checks_passed": True,
        "review": "pass",
        "elapsed_seconds": 5,
        "measurements": {
            "measurement_complete": True,
            "input_tokens": 1000,
            "output_tokens": 50,
            "tool_calls": 5,
            "tool_failures": 0,
        },
    }
    bad = {
        **good,
        "variant": "candidate",
        "checks_passed": False,
        "measurements": {**good["measurements"], "input_tokens": 1},
    }
    summary = {"baseline": reports.summarize([good]), "candidate": reports.summarize([bad])}
    assert summary["candidate"]["samples"] == 0
    assert summary["candidate"]["medians"]["input_tokens"] is None
    assert "regression" in reports.conclusion([good, bad], summary)


def test_package_snapshot_tracks_support_files_and_rejects_symlinks(tmp_path):
    root = tmp_path / "src/enso/bundled/skills/test"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("skill")
    (root / "references").mkdir()
    reference = root / "references/info.md"
    reference.write_text("one")
    before = runner.package(tmp_path, "test", "working-tree")
    assert runner.package(tmp_path, "test", str(root)) == before
    reference.write_text("two")
    after = runner.package(tmp_path, "test", "working-tree")
    assert runner.package_hash(before) != runner.package_hash(after)
    reference.chmod(0o755)
    executable = runner.package(tmp_path, "test", "working-tree")
    assert runner.package_hash(executable) != runner.package_hash(after)
    runner.write_package(tmp_path / "copy", executable)
    assert (tmp_path / "copy/references/info.md").stat().st_mode & 0o111
    assert not (tmp_path / "copy/SKILL.md").stat().st_mode & 0o111
    (root / "escape").symlink_to(tmp_path.parent)
    with pytest.raises(ValueError, match="symlinks"):
        runner.package(tmp_path, "test", "working-tree")


def test_help_and_catalog_do_not_launch_a_provider():
    result = subprocess.run(
        [str(REPO / "scripts/eval-skills"), "list"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and "tables-import" in result.stdout


def test_recovered_permission_denial_is_effort_not_failed_execution(tmp_path):
    result = measurements(
        tmp_path,
        "claude",
        [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "modelUsage": {
                    "primary": {
                        "inputTokens": 10,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "outputTokens": 5,
                    }
                },
                "permission_denials": [{"tool_name": "Bash", "tool_use_id": "denied"}],
            }
        ],
    )
    assert result["measurement_complete"] and not result["errors"]
    assert (result["tool_calls"], result["tool_failures"]) == (1, 1)


def test_human_review_records_verdict_without_changing_raw_measurements(tmp_path, monkeypatch):
    output = tmp_path / "review"
    output.mkdir()
    manifest = {
        "skill": "enso-tables",
        "provider": "codex",
        "model": "test",
        "effort": "low",
        "cli_version": "fake",
        "runtime_commit": "abc",
        "baseline": "old",
        "candidate": "new",
        "packages": {"baseline": "a", "candidate": "b"},
        "scenarios": [{"id": "scenario", "prompt": "test"}],
        "runs": [{"id": "run1", "scenario": "scenario", "variant": "candidate"}],
    }
    runner.save_json(output / "manifest.json", manifest)
    (output / "run1").mkdir()
    result = {
        "status": "completed",
        "checks_passed": True,
        "checks": [],
        "review": "pending",
        "measurements": {"input_tokens": 300},
    }
    runner.save_json(output / "run1/result.json", result)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval-skills",
            "review",
            str(output),
            "--run",
            "run1",
            "--verdict",
            "fail",
            "--note",
            "Unsafe write in trace",
        ],
    )
    assert cli.main(REPO) == 0
    reviewed = json.loads((output / "run1/result.json").read_text())
    assert reviewed["review"] == "fail" and reviewed["measurements"] == result["measurements"]
    assert "Unsafe write in trace" in (output / "report.md").read_text()
