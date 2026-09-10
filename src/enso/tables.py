"""Registered user tables: the ``_enso_tables`` catalog over ordinary tables in ``enso.db``.

The catalog is metadata only; columns, indexes, and the CREATE statement are always read
back from SQLite itself.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass

from . import db
from .config import Paths

TABLE_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
RESERVED = ("runs", "messages", "sessions", "job_state")


class TableError(ValueError):
    """The name is unsafe or reserved, or the table is not registered or missing."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    not_null: bool
    default: object | None
    primary_key: int  # position in the key, 0 when not part of it


@dataclass(frozen=True)
class DataTable:
    table_name: str
    name: str
    description: str
    sql: str  # "" when the catalog names a table that no longer exists
    columns: tuple[Column, ...] = ()
    indexes: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.sql)

    def as_dict(self) -> dict:
        return {**asdict(self), "available": self.available}


def valid_name(table_name: object) -> str:
    """The name when it is a safe, unreserved SQLite identifier; else ``TableError``."""
    candidate = table_name.strip() if isinstance(table_name, str) else ""
    if not TABLE_NAME_RE.fullmatch(candidate):
        raise TableError(
            "table names are lowercase letters, digits, and underscores, start with a "
            "letter, and have at most 63 characters"
        )
    if candidate in RESERVED or candidate.startswith(("_enso_", "sqlite_")):
        raise TableError(f"{candidate} is reserved")
    return candidate


def title(table_name: str) -> str:
    """``weight_entries`` → ``Weight Entries``."""
    return " ".join(part.capitalize() for part in table_name.split("_") if part)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _create_sql(con: sqlite3.Connection, table_name: str) -> str:
    """The CREATE statement of an ordinary table; "" for views, virtual tables, or nothing."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    sql = row["sql"] if row and isinstance(row["sql"], str) else ""
    return sql if sql.lstrip().upper().startswith("CREATE TABLE") else ""


def _describe(con: sqlite3.Connection, row: sqlite3.Row) -> DataTable:
    table_name = row["table_name"]
    sql = _create_sql(con, table_name)
    columns: tuple[Column, ...] = ()
    indexes: tuple[str, ...] = ()
    if sql:
        columns = tuple(
            Column(
                name=info["name"],
                type=info["type"] or "",
                not_null=bool(info["notnull"]),
                default=info["dflt_value"],
                primary_key=info["pk"],
            )
            for info in con.execute(f"PRAGMA table_xinfo({_quote(table_name)})")
        )
        indexes = tuple(
            index["sql"]
            for index in con.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                "AND sql IS NOT NULL ORDER BY name",
                (table_name,),
            )
        )
    return DataTable(table_name, row["name"], row["description"], sql, columns, indexes)


def register(paths: Paths, table_name: str, description: str, name: str = "") -> DataTable:
    """Register an existing ordinary table (or update its metadata); idempotent."""
    table_name = valid_name(table_name)
    description = description.strip()
    if not description:
        raise TableError("the description must not be empty")
    stamp = db.now()
    with db.transaction(paths) as con:
        if not _create_sql(con, table_name):
            raise TableError(f"table {table_name} does not exist in {paths.db}")
        con.execute(
            """INSERT INTO _enso_tables (table_name, name, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (table_name) DO UPDATE SET
                 name = excluded.name, description = excluded.description,
                 updated_at = excluded.updated_at""",
            (table_name, name.strip() or title(table_name), description, stamp, stamp),
        )
        row = con.execute(
            "SELECT * FROM _enso_tables WHERE table_name = ?", (table_name,)
        ).fetchone()
        return _describe(con, row)


def list_tables(paths: Paths) -> list[DataTable]:
    """Every registered table, including catalog rows whose table is gone."""
    with db.transaction(paths) as con:
        rows = con.execute("SELECT * FROM _enso_tables ORDER BY table_name").fetchall()
        # The catalog may have been edited outside Enso: never interpolate an unchecked name.
        return [_describe(con, row) for row in rows if TABLE_NAME_RE.fullmatch(row["table_name"])]


def get_table(paths: Paths, table_name: str) -> DataTable:
    table_name = valid_name(table_name)
    with db.transaction(paths) as con:
        row = con.execute(
            "SELECT * FROM _enso_tables WHERE table_name = ?", (table_name,)
        ).fetchone()
        if row is None:
            raise TableError(f"table {table_name} is not registered")
        table = _describe(con, row)
    if not table.available:
        raise TableError(f"table {table_name} is registered but no longer exists")
    return table
