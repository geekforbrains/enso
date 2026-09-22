---
name: enso-tables
description: Create, inspect, query, or maintain durable user data tables for structured facts, measurements, metrics, and history in Enso.
---

# Tables

```bash
enso table list
enso table schema TABLE
enso_home="${ENSO_HOME:-$HOME/.enso}"
sqlite3 "$enso_home/enso.db"
```

Tables are installation-wide. Inspect descriptions and schemas before writing; reuse a
table when its meaning and grain match. Never alter, drop, or register Enso's internal
tables, including `runs`, `messages`, `sessions`, `job_state`, and `_enso_*`; `sqlite_*`
is also reserved.

Create ordinary SQLite tables, then make them discoverable:

```bash
enso table register TABLE --name "Display name" --description "What one row represents."
```

Re-registration updates catalog text, not the schema. Prefer raw facts at a clear grain,
`snake_case` names (at most 63 characters), explicit units, ISO-8601 UTC timestamps, and
constraints and indexes suited to the data. Keep derived metrics as queries unless storage
is necessary. Store neither secrets nor large files here.

For writes:

- Parameterize external values; never interpolate them into SQL.
- Enable `PRAGMA foreign_keys = ON` per connection. Wrap related writes in a transaction;
  scripted `sqlite3` needs `.bail on` so failure cannot fall through to `COMMIT`.
- Make repeated ingestion idempotent with a natural unique key and an upsert.
- Query affected rows afterward and report the verified change.

Destructive migrations, bulk deletion, and dropping tables need user authorization. Back
up the database before a non-trivial rewrite.
