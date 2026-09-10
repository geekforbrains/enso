---
name: enso-tables
description: Create, inspect, query, or maintain durable user data tables in ~/.enso/enso.db. Use when the user wants to track structured facts, measurements, metrics, or history that should later be filtered, joined, or aggregated.
---

# Tables

## How Enso sets it up

`~/.enso/enso.db` is one SQLite file that Enso creates and migrates. It holds Enso's own state (`runs`, `messages`, `sessions`, `job_state`, and every `_enso_*` table) and a registry of user tables, so an agent in any workspace, turn, or job can find data another one wrote. A user table is an ordinary SQLite table you create and then register with `enso table register`, which puts its name and description in `enso table list`. Never alter, drop, or register Enso's own tables.

## Discover before writing

```bash
enso table list
enso table schema <table>       # columns, constraints, indexes, CREATE SQL; --json available
sqlite3 ~/.enso/enso.db         # rows and general SQL
```

Descriptions are the index. Reuse a table when its meaning and grain match; inspect its schema before querying or changing it.

## Create a table

Prefer raw facts at a clear grain over precomputed summaries. Use lowercase `snake_case` names (at most 63 characters), a primary key, `NOT NULL`, `UNIQUE`, `CHECK`, and foreign-key constraints where the domain warrants them, ISO-8601 UTC `TEXT` timestamps, explicit units in column names (`weight_kg`), and indexes for recurring lookups.

```bash
sqlite3 ~/.enso/enso.db <<'SQL'
.bail on
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;
CREATE TABLE weight_entries (
    id INTEGER PRIMARY KEY,
    recorded_at TEXT NOT NULL UNIQUE,
    weight_kg REAL NOT NULL CHECK (weight_kg > 0),
    notes TEXT
);
COMMIT;
SQL

enso table register weight_entries --name "Weight" \
  --description "Body-weight measurements over time, one row per recorded timestamp."
```

Registration is idempotent: registering again only updates the name and description.

## Write and query safely

- Wrap related writes in a transaction; use `.bail on` in scripted `sqlite3` so an error cannot fall through to `COMMIT`.
- Turn on `PRAGMA foreign_keys = ON` per connection; SQLite leaves it off.
- Make repeated ingestion idempotent with a natural `UNIQUE` key and `INSERT … ON CONFLICT DO UPDATE`.
- Parameterize values that come from external content; never interpolate them into SQL.
- Query the affected rows after a write and report what changed.
- Keep derived metrics as queries over source facts unless materializing them is clearly needed.
- No secrets or large files in a table.

Confirm with the user before `DROP TABLE`, a bulk `DELETE`, or a destructive migration, and back up the database before a non-trivial rewrite.
