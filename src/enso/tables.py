"""Registered user data tables stored in ``~/.enso/enso.db``.

Enso's database also contains internal run history.  User tables therefore
have an explicit catalog: only ordinary SQLite tables registered in
``_enso_tables`` are discoverable through the CLI and dashboard.  The catalog
is metadata, not a second schema source; columns and indexes are always read
from SQLite itself.
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config
from .sqlite_store import database_exists, read_connection, write_connection

MAX_TABLES = 500
MAX_COLUMNS = 50
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50
MAX_OFFSET = 100_000
MAX_CELL_CHARS = 240

_TABLE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS _enso_tables (
    table_name  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


class TableNameError(ValueError):
    """Raised when a user table name is unsafe or reserved by Enso/SQLite."""


class TableNotFoundError(LookupError):
    """Raised when a table is unregistered, missing, or not an ordinary table."""


@dataclass(frozen=True)
class TableColumn:
    """One column reported by SQLite's ``table_xinfo`` pragma."""

    cid: int
    name: str
    declared_type: str
    not_null: bool
    default_value: object | None
    primary_key: int
    hidden: int = 0


@dataclass(frozen=True)
class TableIndex:
    """One explicitly created index for a registered table."""

    name: str
    sql: str


@dataclass
class DataTable:
    """Catalog metadata plus SQLite-derived schema details."""

    table_name: str
    name: str
    description: str
    available: bool
    column_count: int = 0
    sql: str = ""
    columns: list[TableColumn] = field(default_factory=list)
    indexes: list[TableIndex] = field(default_factory=list)


@dataclass
class TableListing:
    """Bounded registered-table listing and its truncation state."""

    tables: list[DataTable] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class TableCell:
    """A display-safe, bounded representation of one SQLite value."""

    text: str
    kind: str
    truncated: bool = False


@dataclass
class TablePage:
    """One bounded page of rows from a registered table."""

    table: DataTable
    columns: list[TableColumn]
    rows: list[list[TableCell]]
    offset: int
    limit: int
    has_previous: bool
    has_next: bool
    columns_truncated: bool = False


def db_path() -> str:
    """Return the shared Enso SQLite database path."""
    return os.path.join(config.CONFIG_DIR, "enso.db")


def validate_table_name(table_name: str) -> str:
    """Return a normalized safe table name or raise ``TableNameError``."""
    candidate = table_name.strip() if isinstance(table_name, str) else ""
    if not _TABLE_NAME_RE.fullmatch(candidate):
        raise TableNameError(
            "Table names must start with a lowercase letter, contain only "
            "lowercase letters, numbers, or underscores, and be at most 63 characters"
        )
    if candidate == "runs" or candidate.startswith(("_enso_", "sqlite_")):
        raise TableNameError(f"Table name is reserved: {candidate!r}")
    return candidate


def title_from_identifier(table_name: str) -> str:
    """Turn a snake-case SQLite identifier into a display title."""
    return " ".join(part[:1].upper() + part[1:] for part in table_name.split("_") if part)


def register_table(
    table_name: str,
    *,
    description: str,
    name: str = "",
) -> DataTable:
    """Register an existing ordinary SQLite table for agent/UI discovery."""
    table_name = validate_table_name(table_name)
    description = description.strip() if isinstance(description, str) else ""
    if not description:
        raise ValueError("Table description must not be empty")
    display_name = name.strip() if isinstance(name, str) else ""
    display_name = display_name or title_from_identifier(table_name)
    if not display_name:
        raise ValueError("Table display name must not be empty")

    with _write_connection() as conn:
        _ensure_catalog(conn)
        if _ordinary_table_sql(conn, table_name) is None:
            raise TableNotFoundError(f"Table '{table_name}' not found")
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO _enso_tables (table_name, name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(table_name) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                updated_at = excluded.updated_at
            """,
            (table_name, display_name, description, now, now),
        )
        return _get_table(conn, table_name)


def list_tables() -> TableListing:
    """List registered tables, including stale catalog entries as unavailable."""
    if not database_exists(db_path()):
        return TableListing()
    with _read_connection() as conn:
        if not _catalog_exists(conn):
            return TableListing()
        rows = conn.execute(
            "SELECT table_name, name, description FROM _enso_tables "
            "ORDER BY table_name LIMIT ?",
            (MAX_TABLES + 1,),
        ).fetchall()
        listing = TableListing(truncated=len(rows) > MAX_TABLES)
        for row in rows[:MAX_TABLES]:
            # Catalog contents may have been written outside Enso. Validate
            # again before any value is interpolated as an SQL identifier.
            try:
                table_name = validate_table_name(row["table_name"])
            except TableNameError:
                continue
            sql = _ordinary_table_sql(conn, table_name)
            columns = _columns(conn, table_name) if sql is not None else []
            listing.tables.append(
                DataTable(
                    table_name=table_name,
                    name=row["name"],
                    description=row["description"],
                    available=sql is not None,
                    column_count=len(columns),
                )
            )
        return listing


def get_table(table_name: str) -> DataTable:
    """Return catalog and schema information for one registered table."""
    table_name = validate_table_name(table_name)
    if not database_exists(db_path()):
        raise TableNotFoundError(f"Table '{table_name}' not found")
    with _read_connection() as conn:
        _require_catalog(conn, table_name)
        return _get_table(conn, table_name)


def preview_table(
    table_name: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
) -> TablePage:
    """Return a bounded, display-safe page from a registered table.

    Values are converted inside SQLite so large BLOBs never enter Python and
    long text is sliced before it reaches the renderer. Rows are ordered by the
    declared primary key when present, otherwise by an accessible rowid alias.
    """
    table_name = validate_table_name(table_name)
    if not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"Offset must be between 0 and {MAX_OFFSET}")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"Limit must be between 1 and {MAX_PAGE_SIZE}")

    if not database_exists(db_path()):
        raise TableNotFoundError(f"Table '{table_name}' not found")
    with _read_connection() as conn:
        _require_catalog(conn, table_name)
        table = _get_table(conn, table_name)
        visible_columns = table.columns[:MAX_COLUMNS]
        columns_truncated = len(table.columns) > MAX_COLUMNS
        if not visible_columns:
            return TablePage(
                table=table,
                columns=[],
                rows=[],
                offset=offset,
                limit=limit,
                has_previous=offset > 0,
                has_next=False,
                columns_truncated=columns_truncated,
            )

        expressions: list[str] = []
        params: list[object] = []
        for column in visible_columns:
            identifier = _quote_identifier(column.name)
            expressions.extend(
                [
                    f"typeof({identifier})",
                    (
                        f"CASE WHEN typeof({identifier}) IN ('text', 'blob') "
                        f"THEN length({identifier}) END"
                    ),
                    (
                        "CASE "
                        f"WHEN typeof({identifier}) IN ('null', 'blob') THEN '' "
                        f"WHEN typeof({identifier}) = 'text' THEN substr({identifier}, 1, ?) "
                        f"ELSE CAST({identifier} AS TEXT) END"
                    ),
                ]
            )
            params.append(MAX_CELL_CHARS)

        params.extend([limit + 1, offset])
        order_by = _order_by(table.columns)
        sql = f"SELECT {', '.join(expressions)} FROM main.{_quote_identifier(table_name)}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        sql += " LIMIT ? OFFSET ?"
        raw_rows = conn.execute(sql, params).fetchall()
        has_next = len(raw_rows) > limit
        rows = [
            _display_row(row, len(visible_columns))
            for row in raw_rows[:limit]
        ]
        return TablePage(
            table=table,
            columns=visible_columns,
            rows=rows,
            offset=offset,
            limit=limit,
            has_previous=offset > 0,
            has_next=has_next,
            columns_truncated=columns_truncated,
        )


def _write_connection():
    """Return a short-lived transaction context for a catalog write."""
    return write_connection(db_path())


def _read_connection():
    """Return a short-lived read-only context for a catalog operation."""
    return read_connection(db_path())


def _ensure_catalog(conn: sqlite3.Connection) -> None:
    conn.execute(_CATALOG_SCHEMA)


def _catalog_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = '_enso_tables'"
    ).fetchone() is not None


def _require_catalog(conn: sqlite3.Connection, table_name: str) -> None:
    if not _catalog_exists(conn):
        raise TableNotFoundError(f"Table '{table_name}' not found")


def _get_table(conn: sqlite3.Connection, table_name: str) -> DataTable:
    row = conn.execute(
        "SELECT table_name, name, description FROM _enso_tables WHERE table_name = ?",
        (table_name,),
    ).fetchone()
    if row is None:
        raise TableNotFoundError(f"Table '{table_name}' not found")
    sql = _ordinary_table_sql(conn, table_name)
    if sql is None:
        raise TableNotFoundError(f"Table '{table_name}' not found")
    columns = _columns(conn, table_name)
    indexes = [
        TableIndex(name=index["name"], sql=index["sql"])
        for index in conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL ORDER BY name",
            (table_name,),
        ).fetchall()
    ]
    return DataTable(
        table_name=table_name,
        name=row["name"],
        description=row["description"],
        available=True,
        column_count=len(columns),
        sql=sql,
        columns=columns,
        indexes=indexes,
    )


def _ordinary_table_sql(conn: sqlite3.Connection, table_name: str) -> str | None:
    table_list_supported = True
    try:
        kind = conn.execute(
            "SELECT type FROM pragma_table_list "
            "WHERE schema = 'main' AND name = ?",
            (table_name,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # ``PRAGMA table_list`` arrived in SQLite 3.37. Retain the schema-SQL
        # fallback for older Python builds while using the stronger type check
        # wherever it is available.
        if "pragma_table_list" not in str(exc):
            raise
        table_list_supported = False
    else:
        if kind is None or kind["type"] != "table":
            return None

    row = conn.execute(
        "SELECT sql FROM main.sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if row is None or not isinstance(row["sql"], str):
        return None
    sql = row["sql"].lstrip()
    if not table_list_supported and sql.upper().startswith("CREATE VIRTUAL TABLE"):
        return None
    return row["sql"]


def _columns(conn: sqlite3.Connection, table_name: str) -> list[TableColumn]:
    rows = conn.execute(
        f"PRAGMA main.table_xinfo({_quote_identifier(table_name)})"
    ).fetchall()
    return [
        TableColumn(
            cid=row["cid"],
            name=row["name"],
            declared_type=row["type"] or "",
            not_null=bool(row["notnull"]),
            default_value=row["dflt_value"],
            primary_key=row["pk"],
            hidden=row["hidden"],
        )
        for row in rows
    ]


def _order_by(columns: list[TableColumn]) -> str:
    primary_key = sorted(
        (column for column in columns if column.primary_key),
        key=lambda column: column.primary_key,
    )
    if primary_key:
        return ", ".join(_quote_identifier(column.name) for column in primary_key)

    names = {column.name.casefold() for column in columns}
    for rowid_alias in ("rowid", "_rowid_", "oid"):
        if rowid_alias not in names:
            return rowid_alias
    return ""


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _display_row(row: sqlite3.Row, column_count: int) -> list[TableCell]:
    cells: list[TableCell] = []
    for index in range(column_count):
        value_type = row[index * 3]
        value_length = row[index * 3 + 1]
        value = row[index * 3 + 2]
        if value_type == "null":
            cells.append(TableCell(text="", kind="null"))
        elif value_type == "blob":
            cells.append(TableCell(text=f"{value_length or 0} bytes", kind="blob"))
        else:
            text = _clean_display_text("" if value is None else str(value))
            cells.append(
                TableCell(
                    text=text,
                    kind=value_type,
                    truncated=value_type == "text" and (value_length or 0) > MAX_CELL_CHARS,
                )
            )
    return cells


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_display_text(value: str) -> str:
    """Remove control characters that are unsafe or meaningless in HTML."""
    return "".join(
        character
        for character in value
        if character in {"\n", "\t"} or unicodedata.category(character) != "Cc"
    )
