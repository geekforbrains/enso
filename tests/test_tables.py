"""The registered-table catalog and ``enso table`` JSON shapes."""

from __future__ import annotations

import json

import pytest
from conftest import write_config
from typer.testing import CliRunner

from enso import db, tables
from enso.cli import app
from enso.config import Paths


def _create(paths: Paths, sql: str) -> None:
    db.initialize(paths)
    con = db.connect(paths)
    con.executescript(sql)
    con.close()


def test_register_list_and_schema(enso_home: Paths) -> None:
    _create(
        enso_home,
        "CREATE TABLE weight_entries (id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL UNIQUE, "
        "weight_kg REAL NOT NULL, notes TEXT DEFAULT 'x');"
        "CREATE INDEX weight_when ON weight_entries (recorded_at);"
        "CREATE VIEW recent AS SELECT * FROM weight_entries;",
    )
    table = tables.register(enso_home, "weight_entries", "Body weight over time.")
    assert (table.name, table.available, len(table.columns)) == ("Weight Entries", True, 4)
    assert [c.name for c in table.columns if c.primary_key] == ["id"]
    assert table.columns[3].default == "'x'" and table.columns[1].not_null
    assert table.indexes == ("CREATE INDEX weight_when ON weight_entries (recorded_at)",)
    again = tables.register(enso_home, "weight_entries", "Kilograms.", name="Weight")
    assert (again.name, again.description) == ("Weight", "Kilograms.")
    assert [t.table_name for t in tables.list_tables(enso_home)] == ["weight_entries"]
    for bad in ("runs", "_enso_x", "Bad", "recent", "nope"):
        with pytest.raises(tables.TableError):
            tables.register(enso_home, bad, "d")
    with pytest.raises(tables.TableError):
        tables.get_table(enso_home, "nope")
    with db.transaction(enso_home) as con:
        con.execute("DROP TABLE weight_entries")
    assert tables.list_tables(enso_home)[0].available is False
    with pytest.raises(tables.TableError):
        tables.get_table(enso_home, "weight_entries")


def test_table_commands(enso_home: Paths, raw_config: dict) -> None:
    write_config(enso_home, raw_config)
    _create(enso_home, "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL);")
    runner = CliRunner()
    made = runner.invoke(app, ["table", "register", "notes", "-d", "Notes.", "--json"])
    assert made.exit_code == 0 and json.loads(made.stdout)["table_name"] == "notes"
    assert runner.invoke(app, ["table", "register", "runs", "-d", "x"]).exit_code == 1
    listed = json.loads(runner.invoke(app, ["table", "list", "--json"]).stdout)
    assert [(t["table_name"], t["name"], t["available"]) for t in listed] == [
        ("notes", "Notes", True)
    ]
    schema = json.loads(runner.invoke(app, ["table", "schema", "notes", "--json"]).stdout)
    assert [c["name"] for c in schema["columns"]] == ["id", "body"]
    text = runner.invoke(app, ["table", "schema", "notes"])
    assert text.exit_code == 0 and "CREATE TABLE notes" in text.stdout
    assert runner.invoke(app, ["table", "schema", "missing"]).exit_code == 1
