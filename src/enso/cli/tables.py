"""``enso table``: the registered-table catalog."""

from __future__ import annotations

import typer

from .. import tables
from ..config import Paths
from .common import JSON_FLAG, columns, echo_json, fail, load

table_app = typer.Typer(no_args_is_help=True, help="Registered data tables in $ENSO_HOME/enso.db.")


@table_app.command("list")
def table_list(as_json: bool = JSON_FLAG) -> None:
    """List registered tables with their descriptions."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)
    found = tables.list_tables(paths)
    if as_json:
        echo_json([table.as_dict() for table in found])
        return
    if not found:
        typer.echo("no registered tables; run `enso table register NAME --description ...`")
        return
    rows = [["TABLE", "NAME", "COLUMNS", "DESCRIPTION"]]
    for table in found:
        count = str(len(table.columns)) if table.available else "missing"
        rows.append([table.table_name, table.name, count, table.description])
    typer.echo(columns(rows))


@table_app.command("register")
def table_register(
    table_name: str = typer.Argument(..., help="An existing table in enso.db."),
    description: str = typer.Option(..., "--description", "-d", help="What it contains."),
    name: str = typer.Option("", "--name", help="Display name; default: from the table name."),
    as_json: bool = JSON_FLAG,
) -> None:
    """Register an existing table so agents can discover it; re-registering updates the text."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)
    try:
        table = tables.register(paths, table_name, description, name)
    except tables.TableError as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(table.as_dict())
    else:
        typer.echo(f"registered {table.table_name} ({table.name})")


@table_app.command("schema")
def table_schema(
    table_name: str = typer.Argument(..., help="A registered table."), as_json: bool = JSON_FLAG
) -> None:
    """Show a registered table's columns, indexes, and CREATE statement."""
    paths = Paths.from_env()
    load(paths, as_json=as_json)
    try:
        table = tables.get_table(paths, table_name)
    except tables.TableError as exc:
        fail([str(exc)], as_json=as_json)
    if as_json:
        echo_json(table.as_dict())
        return
    typer.echo(f"{table.name} ({table.table_name})\n{table.description}\n")
    rows = [["COLUMN", "TYPE", "CONSTRAINTS"]]
    for column in table.columns:
        constraints = []
        if column.primary_key:
            constraints.append("PRIMARY KEY")
        if column.not_null:
            constraints.append("NOT NULL")
        if column.default is not None:
            constraints.append(f"DEFAULT {column.default}")
        rows.append([column.name, column.type or "any", ", ".join(constraints)])
    typer.echo(columns(rows))
    typer.echo(f"\n{table.sql}")
    for index in table.indexes:
        typer.echo(index)
