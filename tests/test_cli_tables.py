"""CLI tests for registered SQLite data tables."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from enso import cli as cli_mod

runner = CliRunner()


def create_table(tmp_enso: str, ddl: str) -> None:
    with sqlite3.connect(Path(tmp_enso) / "enso.db") as conn:
        conn.execute(ddl)


def test_table_list_reports_empty_catalog(tmp_enso):
    result = runner.invoke(cli_mod.app, ["table", "list"])

    assert result.exit_code == 0
    assert "No registered tables" in result.output
    assert "enso table register" in result.output


def test_table_register_list_and_schema(tmp_enso):
    create_table(
        tmp_enso,
        "CREATE TABLE weight_entries "
        "(id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, weight_kg REAL)",
    )

    registered = runner.invoke(
        cli_mod.app,
        [
            "table",
            "register",
            "weight_entries",
            "--name",
            "Weight",
            "--description",
            "Body-weight measurements over time.",
        ],
    )
    listed = runner.invoke(cli_mod.app, ["table", "list"])
    schema = runner.invoke(cli_mod.app, ["table", "schema", "weight_entries"])

    assert registered.exit_code == 0
    assert "Table registered" in registered.output
    assert "weight_entries" in listed.output
    assert "Weight" in listed.output
    assert "Body-weight measurements over time." in listed.output
    assert schema.exit_code == 0
    assert "recorded_at" in schema.output
    assert "TEXT" in schema.output
    assert "NOT NULL" in schema.output
    assert "CREATE TABLE weight_entries" in schema.output


def test_table_register_renders_operator_text_literally(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE samples (id INTEGER PRIMARY KEY)")

    result = runner.invoke(
        cli_mod.app,
        [
            "table",
            "register",
            "samples",
            "--name",
            "Samples [beta]",
            "--description",
            "Values containing [/] markup-like text.",
        ],
    )
    listed = runner.invoke(cli_mod.app, ["table", "list"])

    assert result.exit_code == 0
    assert "Samples [beta]" in listed.output
    assert "Values containing [/] markup-like" in listed.output
    assert "text." in listed.output


def test_table_register_rejects_missing_or_reserved_table(tmp_enso):
    missing = runner.invoke(
        cli_mod.app,
        ["table", "register", "missing", "--description", "Missing table."],
    )
    reserved = runner.invoke(
        cli_mod.app,
        ["table", "register", "runs", "--description", "Internal table."],
    )

    assert missing.exit_code == 1
    assert "Could not register table" in missing.output
    assert reserved.exit_code == 1
    assert "Could not register table" in reserved.output


def test_table_schema_rejects_unregistered_table(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE hidden (id INTEGER PRIMARY KEY)")

    result = runner.invoke(cli_mod.app, ["table", "schema", "hidden"])

    assert result.exit_code == 1
    assert "Table not found" in result.output
