---
name: tables
description: Use this skill to create, inspect, query, or maintain durable user data tables in ~/.enso/enso.db when the user wants to track structured facts, measurements, metrics, history, or other persistent data that should later be filtered, joined, or aggregated.
---

# Tables

Enso Tables are ordinary SQLite tables in `~/.enso/enso.db`, explicitly
registered so agents and the dashboard can discover them. The database also
contains Enso's internal run history, so keep the boundary below exact.

`~/.enso/enso.db` and its sidecars are protected runtime state, not versionable Enso content; the managed `.gitignore` excludes them, and they must never be committed to Enso's local content history. History is for human-authored files, not database rows.

## Discover before writing

Start every table task with:

```bash
enso table list
enso table schema <matching_table>
```

Descriptions are the discovery index. Reuse a table when its meaning and grain
match the request; inspect its schema before querying or changing it. Use the
standard client for rows and general SQL:

```bash
sqlite3 ~/.enso/enso.db
```

Never alter or register `runs`, any name beginning `_enso_`, or any name
beginning `sqlite_`. Those are reserved internal tables. Do not put secrets or
large files in a data table.

## Create a table

Prefer raw facts at a clear grain over precomputed summaries. Use:

- lowercase `snake_case` names, at most 63 characters
- a primary key and `NOT NULL`, `UNIQUE`, `CHECK`, and foreign-key constraints
  where the domain warrants them
- ISO-8601 UTC `TEXT` timestamps unless the domain requires another convention
- explicit units in the column name (`weight_kg`) or a dedicated unit column
- indexes for recurring time, entity, and lookup predicates

Create the physical table transactionally, then register it:

```bash
(
  umask 077
  sqlite3 ~/.enso/enso.db <<'SQL'
.bail on
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE weight_entries (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL,
    weight_kg REAL NOT NULL CHECK (weight_kg > 0),
    notes TEXT,
    UNIQUE (recorded_at)
);
COMMIT;
SQL
)

enso table register weight_entries \
  --name "Weight" \
  --description "Body-weight measurements over time, one row per recorded timestamp."
```

Registration is idempotent: running it again updates display metadata without
changing the table or its rows. If physical creation succeeds but registration
does not, fix the error and register the existing table; do not recreate it.

## Write and query safely

- Enable foreign-key enforcement on every write connection before starting a
  transaction (`PRAGMA foreign_keys = ON;`); SQLite leaves it off by default.
- Use `.bail on` for scripted `sqlite3` writes so an error cannot fall through
  to a later `COMMIT`, and use `umask 077` when creating the database directly.
- Wrap related writes in a transaction.
- Make repeated ingestion idempotent with a natural `UNIQUE` key and
  `INSERT ... ON CONFLICT DO UPDATE` when appropriate.
- Parameterize values in Python or another client when values come from
  external content. Never interpolate them into SQL.
- Query the affected rows after every write and report what changed.
- Keep derived metrics as queries over source facts unless materializing them
  has a demonstrated need.

## Change or remove data carefully

Inspect the current schema and dependent indexes before a migration. Use a
transaction when SQLite supports the intended change, and make a SQLite backup
before a non-trivial rewrite. Confirm with the user before `DROP TABLE`, bulk
`DELETE`, destructive migration, or replacing previously tracked values.

Use docs for prose/reference knowledge, skills for procedures, and jobs for
scheduled work. Use Tables when the value comes from querying structured rows.
