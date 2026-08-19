"""CLI behavior for safe, scoped local content snapshots."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from enso import cli as cli_mod
from enso import repository as repository_mod

runner = CliRunner()


def test_snapshot_help_exposes_only_scoped_creation() -> None:
    result = runner.invoke(cli_mod.app, ["snapshot", "--help"])

    assert result.exit_code == 0
    assert "create" in result.output
    for deferred_command in ("restore", "reset", "delete"):
        assert deferred_command not in result.output


@pytest.mark.parametrize("command", ["restore", "reset", "delete"])
def test_snapshot_history_mutation_commands_are_not_exposed(command: str) -> None:
    result = runner.invoke(cli_mod.app, ["snapshot", command])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_snapshot_create_requires_an_explicit_path(monkeypatch) -> None:
    constructed = False

    class UnexpectedRepository:
        def __init__(self) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(repository_mod, "EnsoRepository", UnexpectedRepository)

    result = runner.invoke(
        cli_mod.app,
        ["snapshot", "create", "--message", "Document release"],
    )

    assert result.exit_code != 0
    assert "Missing argument" in result.output
    assert constructed is False


def test_snapshot_create_requires_an_explicit_message(monkeypatch) -> None:
    constructed = False

    class UnexpectedRepository:
        def __init__(self) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(repository_mod, "EnsoRepository", UnexpectedRepository)

    result = runner.invoke(cli_mod.app, ["snapshot", "create", "--", "docs"])

    assert result.exit_code != 0
    assert "--message" in result.output
    assert constructed is False


def test_snapshot_create_passes_raw_relative_and_absolute_paths_with_caller_cwd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    caller_cwd = tmp_path / "workspaces" / "default"
    caller_cwd.mkdir(parents=True)
    absolute_path = tmp_path / "enso" / "docs" / "absolute.md"
    calls: list[tuple[list[str], str, str | None]] = []

    class RecordingRepository:
        def snapshot(
            self,
            paths: list[str],
            message: str,
            *,
            caller_cwd: str | None = None,
        ) -> bool:
            calls.append((paths, message, caller_cwd))
            return True

    monkeypatch.setattr(repository_mod, "EnsoRepository", RecordingRepository)
    monkeypatch.chdir(caller_cwd)

    result = runner.invoke(
        cli_mod.app,
        [
            "snapshot",
            "create",
            "--message",
            "Document release",
            "--",
            "knowledge/note with spaces.md",
            os.fspath(absolute_path),
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            ["knowledge/note with spaces.md", os.fspath(absolute_path)],
            "Document release",
            os.fspath(caller_cwd),
        )
    ]
    assert "Snapshot created" in result.output
    assert "Document release" in result.output


def test_snapshot_create_resolves_relative_paths_from_caller_and_accepts_absolute_paths(
    tmp_enso: str,
    monkeypatch,
) -> None:
    root = Path(tmp_enso)
    repository_mod.EnsoRepository().ensure()
    caller_cwd = root / "workspaces" / "default"
    relative_note = caller_cwd / "knowledge" / "relative note.md"
    absolute_doc = root / "docs" / "absolute note.md"
    relative_note.parent.mkdir()
    absolute_doc.parent.mkdir()
    relative_note.write_text("relative\n", encoding="utf-8")
    absolute_doc.write_text("absolute\n", encoding="utf-8")
    monkeypatch.chdir(caller_cwd)

    result = runner.invoke(
        cli_mod.app,
        [
            "snapshot",
            "create",
            "--message",
            "Capture exact notes",
            "--",
            "knowledge/relative note.md",
            os.fspath(absolute_doc),
        ],
    )

    assert result.exit_code == 0, result.output
    assert repository_mod.EnsoRepository().commit_subject_paths("Capture exact notes") == (
        "docs/absolute note.md",
        "workspaces/default/knowledge/relative note.md",
    )
    assert (root / "AGENTS.md").is_file()
    assert "AGENTS.md" not in repository_mod.EnsoRepository().tracked_paths()


def test_snapshot_create_reports_a_successful_noop(monkeypatch) -> None:
    class CleanRepository:
        def snapshot(
            self,
            paths: list[str],
            message: str,
            *,
            caller_cwd: str | None = None,
        ) -> bool:
            return False

    monkeypatch.setattr(repository_mod, "EnsoRepository", CleanRepository)

    result = runner.invoke(
        cli_mod.app,
        ["snapshot", "create", "--message", "Nothing changed", "--", "docs"],
    )

    assert result.exit_code == 0
    assert "No changes to snapshot" in result.output


def test_snapshot_create_reports_an_absent_repository_without_initializing(
    tmp_enso: str,
) -> None:
    root = Path(tmp_enso)
    note = root / "docs" / "note.md"
    note.parent.mkdir()
    note.write_text("note\n", encoding="utf-8")

    result = runner.invoke(
        cli_mod.app,
        ["snapshot", "create", "--message", "Save note", "--", os.fspath(note)],
    )

    assert result.exit_code == 1
    assert "Could not create Enso snapshot" in result.output
    assert "missing" in result.output
    assert ".git" in result.output
    assert not (root / ".git").exists()


@pytest.mark.parametrize(
    "problem",
    [
        "snapshot path '../outside' must remain beneath /tmp/enso",
        "the Enso snapshot lock is already held",
        "the native Git index lock already exists",
        "repository is corrupt [repair it]",
    ],
)
def test_snapshot_create_reports_repository_failures_actionably(problem: str, monkeypatch) -> None:
    class FailingRepository:
        def snapshot(
            self,
            paths: list[str],
            message: str,
            *,
            caller_cwd: str | None = None,
        ) -> bool:
            raise repository_mod.RepositoryError(problem)

    monkeypatch.setattr(repository_mod, "EnsoRepository", FailingRepository)

    result = runner.invoke(
        cli_mod.app,
        ["snapshot", "create", "--message", "Save docs", "--", "docs"],
    )

    assert result.exit_code == 1
    assert "Could not create Enso snapshot" in result.output
    assert " ".join(problem.split()) in " ".join(result.output.split())
