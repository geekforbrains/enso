"""Tests for registered user data tables in ``~/.enso/enso.db``."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from enso import runs, tables


def db_path(tmp_enso: str) -> Path:
    return Path(tmp_enso) / "enso.db"


def create_table(tmp_enso: str, ddl: str) -> None:
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute(ddl)


def test_empty_listing_does_not_create_database(tmp_enso):
    listing = tables.list_tables()

    assert listing.tables == []
    assert listing.truncated is False
    assert not db_path(tmp_enso).exists()


def test_reading_tables_hardens_existing_database_and_sidecars(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE habits (id INTEGER PRIMARY KEY)")
    tables.register_table("habits", description="Daily habits.")
    database = db_path(tmp_enso)
    keeper = sqlite3.connect(database)
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("INSERT INTO habits DEFAULT VALUES")
        keeper.commit()
        sqlite_files = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
        assert all(path.is_file() for path in sqlite_files)
        for path in sqlite_files:
            path.chmod(0o644)

        tables.list_tables()

        assert all(stat.S_IMODE(os.stat(path).st_mode) == 0o600 for path in sqlite_files)
    finally:
        keeper.close()


def test_listing_never_exposes_runs_or_unregistered_tables(tmp_enso):
    runs.create("job", "daily")
    create_table(tmp_enso, "CREATE TABLE private_notes (id INTEGER PRIMARY KEY, body TEXT)")

    assert tables.list_tables().tables == []


def test_register_and_describe_table(tmp_enso):
    create_table(
        tmp_enso,
        """
        CREATE TABLE weight_entries (
            id INTEGER PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            weight_kg REAL NOT NULL,
            notes TEXT
        )
        """,
    )
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute(
            "CREATE UNIQUE INDEX idx_weight_recorded_at ON weight_entries (recorded_at)"
        )

    registered = tables.register_table(
        "weight_entries",
        name="Weight",
        description="Body-weight measurements recorded over time.",
    )
    listing = tables.list_tables()
    described = tables.get_table("weight_entries")

    assert registered.table_name == "weight_entries"
    assert registered.name == "Weight"
    assert registered.available is True
    assert [item.table_name for item in listing.tables] == ["weight_entries"]
    assert listing.tables[0].column_count == 4
    assert described.description == "Body-weight measurements recorded over time."
    assert [column.name for column in described.columns] == [
        "id",
        "recorded_at",
        "weight_kg",
        "notes",
    ]
    assert described.columns[0].primary_key == 1
    assert described.columns[1].not_null is True
    assert "CREATE TABLE weight_entries" in described.sql
    assert [index.name for index in described.indexes] == ["idx_weight_recorded_at"]


def test_register_defaults_display_name_from_identifier(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE blood_pressure (id INTEGER PRIMARY KEY)")

    table = tables.register_table(
        "blood_pressure",
        description="Blood-pressure readings.",
    )

    assert table.name == "Blood Pressure"


def test_register_updates_metadata_without_duplicate_catalog_row(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE habits (id INTEGER PRIMARY KEY)")
    tables.register_table("habits", name="Habits", description="Initial description.")

    updated = tables.register_table(
        "habits",
        name="Daily Habits",
        description="Daily habit completion records.",
    )

    assert updated.name == "Daily Habits"
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        rows = conn.execute(
            "SELECT table_name, name, description FROM _enso_tables"
        ).fetchall()
    assert rows == [("habits", "Daily Habits", "Daily habit completion records.")]


@pytest.mark.parametrize(
    "name",
    [
        "runs",
        "_enso_tables",
        "_enso_hidden",
        "sqlite_stat1",
        "HasCaps",
        "has-dash",
        "has space",
        "1starts_with_number",
        "semi;colon",
        "a" * 64,
    ],
)
def test_register_rejects_reserved_or_unsafe_names(tmp_enso, name):
    with pytest.raises(tables.TableNameError):
        tables.register_table(name, description="Should not register.")

    assert not db_path(tmp_enso).exists()


def test_register_requires_a_real_existing_table(tmp_enso):
    with pytest.raises(tables.TableNotFoundError, match="not found"):
        tables.register_table("missing_table", description="Missing.")


def test_register_rejects_views(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE source_data (value TEXT)")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute("CREATE VIEW source_view AS SELECT value FROM source_data")

    with pytest.raises(tables.TableNotFoundError):
        tables.register_table("source_view", description="A view, not a table.")


def test_register_rejects_virtual_tables(tmp_enso):
    try:
        create_table(tmp_enso, "CREATE VIRTUAL TABLE search_data USING fts5(body)")
    except sqlite3.OperationalError:
        pytest.skip("SQLite build does not include FTS5")

    with pytest.raises(tables.TableNotFoundError):
        tables.register_table("search_data", description="Search data.")


def test_register_rejects_virtual_table_shadow_tables(tmp_enso):
    try:
        create_table(tmp_enso, "CREATE VIRTUAL TABLE search_data USING fts5(body)")
    except sqlite3.OperationalError:
        pytest.skip("SQLite build does not include FTS5")

    with pytest.raises(tables.TableNotFoundError):
        tables.register_table(
            "search_data_data",
            description="An FTS implementation table, not user data.",
        )


@pytest.mark.parametrize("description", ["", "   "])
def test_register_requires_description(tmp_enso, description):
    create_table(tmp_enso, "CREATE TABLE habits (id INTEGER PRIMARY KEY)")

    with pytest.raises(ValueError, match="description"):
        tables.register_table("habits", description=description)


def test_stale_catalog_entry_remains_visible_as_unavailable(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE habits (id INTEGER PRIMARY KEY)")
    tables.register_table("habits", description="Daily habits.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute("DROP TABLE habits")

    (stale,) = tables.list_tables().tables

    assert stale.table_name == "habits"
    assert stale.available is False
    assert stale.column_count == 0
    with pytest.raises(tables.TableNotFoundError):
        tables.get_table("habits")


def test_hostile_catalog_name_is_skipped_without_hiding_valid_tables(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE habits (id INTEGER PRIMARY KEY)")
    tables.register_table("habits", description="Daily habits.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute(
            "INSERT INTO _enso_tables "
            "(table_name, name, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("runs; DROP TABLE runs", "Hostile", "Invalid catalog row.", "now", "now"),
        )

    listing = tables.list_tables()

    assert [item.table_name for item in listing.tables] == ["habits"]


def test_preview_is_bounded_paginated_and_normalizes_cells(tmp_enso):
    create_table(
        tmp_enso,
        "CREATE TABLE samples (id INTEGER PRIMARY KEY, note TEXT, payload BLOB, score REAL)",
    )
    tables.register_table("samples", description="Preview rendering samples.")
    long_note = "x" * (tables.MAX_CELL_CHARS + 25)
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.executemany(
            "INSERT INTO samples (note, payload, score) VALUES (?, ?, ?)",
            [
                (None, b"\x00\x01\x02", 1.5),
                (long_note, b"", 2),
                ("last", None, 3),
            ],
        )

    first = tables.preview_table("samples", offset=0, limit=2)
    second = tables.preview_table("samples", offset=2, limit=2)

    assert [column.name for column in first.columns] == ["id", "note", "payload", "score"]
    assert len(first.rows) == 2
    assert first.has_previous is False
    assert first.has_next is True
    assert first.rows[0][1].kind == "null"
    assert first.rows[0][2].kind == "blob"
    assert first.rows[0][2].text == "3 bytes"
    assert first.rows[1][1].kind == "text"
    assert first.rows[1][1].truncated is True
    assert len(first.rows[1][1].text) == tables.MAX_CELL_CHARS
    assert first.rows[1][2].text == "0 bytes"
    assert first.rows[1][3].text == "2.0"
    assert len(second.rows) == 1
    assert second.has_previous is True
    assert second.has_next is False
    assert second.rows[0][1].text == "last"


def test_preview_exact_page_has_no_false_next_page(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE samples (id INTEGER PRIMARY KEY)")
    tables.register_table("samples", description="Samples.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.executemany("INSERT INTO samples (id) VALUES (?)", [(1,), (2,)])

    page = tables.preview_table("samples", limit=2)

    assert len(page.rows) == 2
    assert page.has_next is False


def test_preview_empty_table_keeps_schema(tmp_enso):
    create_table(
        tmp_enso,
        "CREATE TABLE empty_samples (id INTEGER PRIMARY KEY, recorded_at TEXT)",
    )
    tables.register_table("empty_samples", description="Empty samples.")

    page = tables.preview_table("empty_samples")

    assert [column.name for column in page.columns] == ["id", "recorded_at"]
    assert page.rows == []


def test_preview_orders_by_primary_key_for_stable_pagination(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE samples (id TEXT PRIMARY KEY, value TEXT)")
    tables.register_table("samples", description="Samples.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.executemany(
            "INSERT INTO samples (id, value) VALUES (?, ?)",
            [("c", "third"), ("a", "first"), ("b", "second")],
        )

    first = tables.preview_table("samples", limit=2)
    second = tables.preview_table("samples", offset=2, limit=2)

    assert [row[0].text for row in first.rows] == ["a", "b"]
    assert [row[0].text for row in second.rows] == ["c"]


def test_preview_quotes_unusual_column_identifiers(tmp_enso):
    create_table(
        tmp_enso,
        'CREATE TABLE quoted_columns (id INTEGER PRIMARY KEY, "odd""name" TEXT)',
    )
    tables.register_table("quoted_columns", description="Quoted columns.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute('INSERT INTO quoted_columns ("odd""name") VALUES (?)', ("works",))

    page = tables.preview_table("quoted_columns")

    assert page.columns[1].name == 'odd"name'
    assert page.rows[0][1].text == "works"


def test_preview_includes_generated_columns(tmp_enso):
    create_table(
        tmp_enso,
        """
        CREATE TABLE computed_values (
            source INTEGER NOT NULL,
            doubled INTEGER GENERATED ALWAYS AS (source * 2) STORED
        )
        """,
    )
    tables.register_table("computed_values", description="Computed values.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute("INSERT INTO computed_values (source) VALUES (4)")

    page = tables.preview_table("computed_values")

    assert [column.name for column in page.columns] == ["source", "doubled"]
    assert page.columns[1].hidden == 3
    assert [cell.text for cell in page.rows[0]] == ["4", "8"]


def test_preview_orders_without_rowid_table_by_composite_primary_key(tmp_enso):
    create_table(
        tmp_enso,
        """
        CREATE TABLE daily_metrics (
            recorded_on TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (recorded_on, metric)
        ) WITHOUT ROWID
        """,
    )
    tables.register_table("daily_metrics", description="Daily metrics.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.executemany(
            "INSERT INTO daily_metrics VALUES (?, ?, ?)",
            [
                ("2026-07-30", "weight", 72),
                ("2026-07-29", "steps", 1000),
                ("2026-07-29", "sleep", 8),
            ],
        )

    page = tables.preview_table("daily_metrics")

    assert [(row[0].text, row[1].text) for row in page.rows] == [
        ("2026-07-29", "sleep"),
        ("2026-07-29", "steps"),
        ("2026-07-30", "weight"),
    ]


def test_preview_removes_unsafe_control_characters(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    tables.register_table("notes", description="Notes.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute("INSERT INTO notes (body) VALUES (?)", ("safe\x01text\nnext",))

    cell = tables.preview_table("notes").rows[0][1]

    assert cell.text == "safetext\nnext"


def test_preview_reads_committed_rows_while_wal_writer_is_active(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE samples (id INTEGER PRIMARY KEY)")
    tables.register_table("samples", description="Samples.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute("INSERT INTO samples DEFAULT VALUES")

    writer = sqlite3.connect(db_path(tmp_enso))
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO samples DEFAULT VALUES")

        page = tables.preview_table("samples")
    finally:
        writer.rollback()
        writer.close()

    assert [row[0].text for row in page.rows] == ["1"]


def test_preview_rejects_unregistered_table_even_when_it_exists(tmp_enso):
    create_table(tmp_enso, "CREATE TABLE secrets (value TEXT)")

    with pytest.raises(tables.TableNotFoundError):
        tables.preview_table("secrets")


@pytest.mark.parametrize(
    ("offset", "limit"),
    [(-1, 10), (tables.MAX_OFFSET + 1, 10), (0, 0), (0, tables.MAX_PAGE_SIZE + 1)],
)
def test_preview_rejects_unbounded_pagination(tmp_enso, offset, limit):
    create_table(tmp_enso, "CREATE TABLE samples (id INTEGER PRIMARY KEY)")
    tables.register_table("samples", description="Samples.")

    with pytest.raises(ValueError):
        tables.preview_table("samples", offset=offset, limit=limit)


def test_preview_caps_visible_columns(tmp_enso):
    definitions = ", ".join(f"column_{i} TEXT" for i in range(tables.MAX_COLUMNS + 2))
    create_table(tmp_enso, f"CREATE TABLE wide_table ({definitions})")
    tables.register_table("wide_table", description="A deliberately wide table.")

    page = tables.preview_table("wide_table")

    assert len(page.columns) == tables.MAX_COLUMNS
    assert page.columns_truncated is True


def test_table_operations_coexist_with_run_history(tmp_enso):
    run_id = runs.create("job", "daily")
    create_table(tmp_enso, "CREATE TABLE metrics (id INTEGER PRIMARY KEY, value REAL)")
    tables.register_table("metrics", description="Tracked metrics.")
    with sqlite3.connect(db_path(tmp_enso)) as conn:
        conn.execute("INSERT INTO metrics (value) VALUES (1.25)")

    assert tables.preview_table("metrics").rows[0][1].text == "1.25"
    runs.finish(run_id, exit_code=0, status="ok")
    assert runs.get(run_id)["status"] == "ok"


def test_listing_is_sorted_and_bounded(tmp_enso, monkeypatch):
    monkeypatch.setattr(tables, "MAX_TABLES", 2)
    for name in ("charlie", "alpha", "bravo"):
        create_table(tmp_enso, f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)")
        tables.register_table(name, description=f"{name} data.")

    listing = tables.list_tables()

    assert [item.table_name for item in listing.tables] == ["alpha", "bravo"]
    assert listing.truncated is True
