"""Tests for the read-only registered Tables dashboard."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("starlette")
pytest.importorskip("jinja2")

from enso import tables as tables_mod
from enso.web import app as web_app


def _tables_web_app(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    config_dir = tmp_path / "enso"
    working_dir = tmp_path / "workspace"
    working_dir.mkdir()
    config_dir.mkdir()
    monkeypatch.setattr(tables_mod.config, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(web_app, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(web_app, "load_jobs", lambda: [])
    monkeypatch.setattr(web_app.runs, "list_runs", lambda **_kwargs: [])
    monkeypatch.setattr(web_app.docs, "load_docs", lambda: SimpleNamespace(docs=[]))
    monkeypatch.setattr(web_app, "_skill_inventory", lambda _request: ([], []))
    runtime = SimpleNamespace(working_dir=str(working_dir), config={"web": {}})
    client = TestClient(web_app.create_app(runtime), base_url="http://127.0.0.1")
    return config_dir, client


def _execute(config_dir: Path, sql: str, params=()) -> None:
    with sqlite3.connect(config_dir / "enso.db") as conn:
        conn.execute(sql, params)


def _register(
    table_name: str,
    *,
    name: str = "",
    description: str = "Registered test data.",
):
    return tables_mod.register_table(
        table_name,
        name=name,
        description=description,
    )


def test_tables_list_only_shows_registered_user_tables(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(config_dir, "CREATE TABLE runs (id TEXT PRIMARY KEY)")
    _execute(config_dir, "CREATE TABLE hidden_data (id INTEGER PRIMARY KEY)")
    _execute(
        config_dir,
        "CREATE TABLE weight_entries (id INTEGER PRIMARY KEY, weight_kg REAL)",
    )
    _register(
        "weight_entries",
        name="Weight",
        description="Body-weight measurements over time.",
    )

    response = client.get("/tables")

    assert response.status_code == 200
    assert "<h1" in response.text
    assert "Tables" in response.text
    assert 'href="/tables/weight_entries"' in response.text
    assert "Weight" in response.text
    assert "Body-weight measurements over time." in response.text
    assert "2 columns" in response.text
    assert "hidden_data" not in response.text
    assert "_enso_tables" not in response.text
    assert 'href="/tables/runs"' not in response.text


def test_tables_list_keeps_stale_registration_visible_but_disabled(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(config_dir, "CREATE TABLE habits (id INTEGER PRIMARY KEY)")
    _register("habits", description="Daily habits.")
    _execute(config_dir, "DROP TABLE habits")

    response = client.get("/tables")

    assert response.status_code == 200
    assert "Habits" in response.text
    assert "unavailable" in response.text
    assert 'href="/tables/habits"' not in response.text


def test_tables_list_empty_state_points_to_registration_cli(tmp_path, monkeypatch):
    _, client = _tables_web_app(tmp_path, monkeypatch)

    response = client.get("/tables")

    assert response.status_code == 200
    assert "No tables yet" in response.text
    assert "enso table register" in response.text


def test_table_detail_shows_schema_and_empty_state(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(
        config_dir,
        """
        CREATE TABLE weight_entries (
            id INTEGER PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            weight_kg REAL DEFAULT 0
        )
        """,
    )
    _register("weight_entries", name="Weight", description="Weight history.")

    response = client.get("/tables/weight_entries")

    assert response.status_code == 200
    assert "Weight" in response.text
    assert "Weight history." in response.text
    assert "weight_entries" in response.text
    assert "Read-only" in response.text
    assert "recorded_at" in response.text
    assert "TEXT" in response.text
    assert "NOT NULL" in response.text
    assert "DEFAULT 0" in response.text
    assert "No rows yet" in response.text
    assert "<form" not in response.text


def test_table_detail_renders_bounded_cell_kinds_and_escapes_html(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(
        config_dir,
        "CREATE TABLE samples (id INTEGER PRIMARY KEY, note TEXT, payload BLOB)",
    )
    _register(
        "samples",
        name='<img src=x onerror="window.__ensoXss=1">',
        description="<script>window.__ensoXss=2</script>",
    )
    long_text = "x" * (tables_mod.MAX_CELL_CHARS + 20)
    with sqlite3.connect(config_dir / "enso.db") as conn:
        conn.executemany(
            "INSERT INTO samples (note, payload) VALUES (?, ?)",
            [
                (None, b"\x00\x01\x02"),
                ("", b""),
                (long_text, None),
                ('<img src=x onerror="window.__ensoXss=3">', None),
            ],
        )

    response = client.get("/tables/samples")

    assert response.status_code == 200
    assert '<img src=x onerror="window.__ensoXss=1">' not in response.text
    assert "&lt;img src=x onerror=&#34;window.__ensoXss=1&#34;&gt;" in response.text
    assert "<script>window.__ensoXss=2</script>" not in response.text
    assert 'data-cell-kind="null"' in response.text
    assert ">NULL<" in response.text
    assert 'data-cell-kind="blob"' in response.text
    assert "BLOB · 3 bytes" in response.text
    assert "BLOB · 0 bytes" in response.text
    assert 'data-cell-truncated="true"' in response.text
    assert 'data-table-row' in response.text


def test_table_detail_paginates_without_counting_all_rows(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(config_dir, "CREATE TABLE samples (id INTEGER PRIMARY KEY, value TEXT)")
    _register("samples", description="Pagination samples.")
    with sqlite3.connect(config_dir / "enso.db") as conn:
        conn.executemany(
            "INSERT INTO samples (id, value) VALUES (?, ?)",
            [(index, f"row-{index}") for index in range(1, 56)],
        )

    first = client.get("/tables/samples")
    second = client.get("/tables/samples?page=2")

    assert first.status_code == 200
    assert first.text.count("data-table-row") == tables_mod.DEFAULT_PAGE_SIZE
    assert "row-50" in first.text
    assert "row-51" not in first.text
    assert 'href="/tables/samples?page=2"' in first.text
    assert second.status_code == 200
    assert second.text.count("data-table-row") == 5
    assert "row-51" in second.text
    assert "row-55" in second.text
    assert 'href="/tables/samples?page=1"' in second.text
    assert 'href="/tables/samples?page=3"' not in second.text

    fragment = client.get(
        "/tables/samples?page=2",
        headers={"HX-Request": "true", "HX-Target": "table-data"},
    )
    assert fragment.status_code == 200
    assert fragment.text.lstrip().startswith('<section id="table-data"')
    assert 'hx-target="#table-data"' in fragment.text
    assert 'hx-select="#table-data"' in fragment.text
    assert 'hx-push-url="true"' in fragment.text
    assert "<!doctype html>" not in fragment.text
    assert '<main id="main-content"' not in fragment.text
    assert "Table schema" not in fragment.text
    assert fragment.text.count("data-table-row") == 5

    history_restore = client.get(
        "/tables/samples?page=2",
        headers={
            "HX-Request": "true",
            "HX-Target": "table-data",
            "HX-History-Restore-Request": "true",
        },
    )
    assert "<!doctype html>" in history_restore.text
    assert "Table schema" in history_restore.text


def test_table_detail_suppresses_next_link_at_maximum_page(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(config_dir, "CREATE TABLE samples (id INTEGER PRIMARY KEY)")
    _register("samples", description="Pagination samples.")
    with sqlite3.connect(config_dir / "enso.db") as conn:
        conn.executemany(
            "INSERT INTO samples (id) VALUES (?)",
            [(index,) for index in range(1, tables_mod.DEFAULT_PAGE_SIZE + 2)],
        )
    monkeypatch.setattr(web_app, "_MAX_TABLE_PAGE", 1)

    response = client.get("/tables/samples?page=1")

    assert response.status_code == 200
    assert 'href="/tables/samples?page=2"' not in response.text
    assert "Preview limit reached" in response.text


@pytest.mark.parametrize("page", ["0", "-2", "not-a-number", "999999999"])
def test_table_detail_bounds_invalid_page_numbers(tmp_path, monkeypatch, page):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(config_dir, "CREATE TABLE samples (id INTEGER PRIMARY KEY)")
    _register("samples", description="Samples.")

    response = client.get(f"/tables/samples?page={page}")

    assert response.status_code == 200


@pytest.mark.parametrize("name", ["hidden", "runs", "_enso_tables", "bad%3Bname"])
def test_table_detail_404s_unregistered_reserved_and_hostile_names(
    tmp_path, monkeypatch, name
):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(config_dir, "CREATE TABLE hidden (id INTEGER PRIMARY KEY)")

    response = client.get(f"/tables/{name}")

    assert response.status_code == 404
    assert "Table not found" in response.text


def test_table_detail_maps_sqlite_failures_to_503(tmp_path, monkeypatch):
    _, client = _tables_web_app(tmp_path, monkeypatch)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("sentinel database path")

    monkeypatch.setattr(web_app.tables, "preview_table", fail)
    response = client.get("/tables/samples")

    assert response.status_code == 503
    assert response.text == "Table unavailable"
    assert "sentinel" not in response.text


def test_dashboard_and_navigation_surface_table_count(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    _execute(config_dir, "CREATE TABLE visible (id INTEGER PRIMARY KEY)")
    _register("visible", description="Visible data.")
    _execute(config_dir, "CREATE TABLE stale (id INTEGER PRIMARY KEY)")
    _register("stale", description="Stale data.")
    _execute(config_dir, "DROP TABLE stale")

    dashboard = client.get("/")
    listing = client.get("/tables")

    assert dashboard.status_code == 200
    assert 'data-tables-total="1"' in dashboard.text
    assert 'href="/tables"' in dashboard.text
    assert "Tables" in dashboard.text
    assert listing.status_code == 200
    assert 'aria-current="page"' in listing.text


def test_table_detail_reports_hidden_columns_cap(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    definitions = ", ".join(
        f"column_{index} TEXT" for index in range(tables_mod.MAX_COLUMNS + 1)
    )
    _execute(config_dir, f"CREATE TABLE wide_table ({definitions})")
    _register("wide_table", description="Wide data.")

    response = client.get("/tables/wide_table")

    assert response.status_code == 200
    assert f"Showing the first {tables_mod.MAX_COLUMNS}" in response.text
    assert 'data-table-grid' in response.text
    assert response.text.count("data-schema-column") == tables_mod.MAX_COLUMNS


def test_table_detail_bounds_schema_sql_and_indexes(tmp_path, monkeypatch):
    config_dir, client = _tables_web_app(tmp_path, monkeypatch)
    ddl_tail = "ddl-tail-sentinel"
    long_check = "x" * (web_app._MAX_TABLE_SCHEMA_SQL_CHARS + 100) + ddl_tail
    _execute(
        config_dir,
        "CREATE TABLE bounded_schema (id INTEGER PRIMARY KEY, value TEXT "
        f"CHECK (value != '{long_check}'))",
    )
    index_tail = "index-tail-sentinel"
    long_comment = "y" * (web_app._MAX_TABLE_INDEX_SQL_CHARS + 100) + index_tail
    _execute(
        config_dir,
        f"CREATE INDEX idx_00_long ON bounded_schema (value) /* {long_comment} */",
    )
    for index in range(1, web_app._MAX_TABLE_INDEXES + 1):
        _execute(
            config_dir,
            f"CREATE INDEX idx_{index:02d} ON bounded_schema (value)",
        )
    _register("bounded_schema", description="Bounded schema metadata.")

    response = client.get("/tables/bounded_schema")

    assert response.status_code == 200
    assert ddl_tail not in response.text
    assert index_tail not in response.text
    assert "CREATE SQL truncated" in response.text
    assert "index SQL truncated" in response.text
    assert "additional index not shown" in response.text
