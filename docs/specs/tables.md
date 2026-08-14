# Data tables

Queryable, operator-owned records that agents maintain in Enso's SQLite database:
measurements, metrics, inventories, observations, and other structured facts that are a
poor fit for Markdown. Registered tables are discoverable through the CLI and visible in
a bounded, read-only dashboard.

**Status: implemented.** This document is the implementation contract. See
[data-model.md](data-model.md) for the shared database and [web.md](web.md) for the
dashboard surface.

## Why tables are a distinct kind

Enso already has file-backed jobs, skills, and reference docs. Those formats remain the
right choice when the content is authored prose or procedure. Tables cover a different
shape: repeated records whose value comes from filtering, grouping, joining, and
aggregating them.

| Kind | Best for | Source of truth |
| --- | --- | --- |
| **Jobs** | Scheduled procedures | `~/.enso/jobs/` Markdown |
| **Skills** | Reusable instructions | `~/.enso/skills/` Markdown |
| **Docs** | Reference knowledge | `~/.enso/docs/` Markdown |
| **Tables** | Structured, queryable facts | `~/.enso/enso.db` SQLite tables |

A table should generally store the underlying facts, including their units and event
times, rather than only a rendered report or precomputed summary. Reports can then be
recomputed without losing provenance.

## Storage and ownership

Tables reuse `~/.enso/enso.db`; every Enso write connection ensures WAL mode for both run
history and registered data. One database keeps backup and agent access simple, and
personal metrics do not justify a second connection lifecycle. The user-data namespace
is kept separate from Enso internals:

- `runs`, `_enso_*`, and `sqlite_*` are reserved and can never be registered.
- Enso-owned metadata uses the `_enso_` prefix.
- A user table has a validated SQLite identifier, limited to lowercase `snake_case`.
- Run retention applies only to `runs`; Enso never automatically prunes user tables.
- `enso.db` and its journal/WAL sidecars are owner-only (`0600`). Enso hardens
  existing files when it next opens them, not only newly created databases.

Tables and run history share the same operation-scoped connection policy. Enso never
caches a connection: each read opens read-only with a 500 ms lock timeout, and each
catalog write opens a fresh connection with a five-second writer-acquisition budget and an explicit
transaction that rolls back before closing on failure.

The registration catalog is created lazily by the first registration attempt:

```sql
CREATE TABLE IF NOT EXISTS _enso_tables (
    table_name  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

`table_name` is the physical SQLite identifier and stable identity. `name` is the
human-readable label shown in the dashboard. `description` is the agent discovery
surface: it should say what one row represents, what the table is useful for, and any
important scope. Catalog timestamps are ISO 8601 UTC; re-registration updates
`updated_at` while preserving `created_at`.

Registration is explicit. An ordinary SQLite table can exist without becoming an Enso
Table; views, virtual tables, and virtual-table shadow tables are not registerable. Only a valid
catalog entry appears in `enso table list` or the dashboard. This prevents SQLite
implementation details, abandoned experiments, and unrelated application tables from
leaking into the product surface. A stale catalog entry whose physical table was removed
remains visible as unavailable so the operator can diagnose it, without causing the whole
Tables page to fail.

## Identifier and schema rules

Table names must match `[a-z][a-z0-9_]*` and contain at most 63 characters. In addition
to making shell and SQL use predictable, the restricted form gives routes a stable,
unambiguous identity. Every SQL statement still quotes identifiers; validation is not a
substitute for quoting.

Enso deliberately does not prescribe one universal row schema. The bundled skill applies
these conventions when an agent designs one:

- use an `INTEGER PRIMARY KEY` or a meaningful declared primary/unique key
- choose normal SQLite affinities (`INTEGER`, `REAL`, `TEXT`, `BLOB`) rather than opaque
  serialized payloads when the values need to be queried
- store timestamps as ISO 8601 UTC text and make units explicit in column names or table
  documentation
- add indexes for recurring filters, joins, or ordering, but not speculatively
- enable foreign-key enforcement before writes, use fail-fast scripted transactions,
  and make repeated inserts/upserts idempotent

Schema evolution is ordinary SQLite. Dropping a table or making a destructive migration
requires operator confirmation; Enso provides no destructive table command.

## CLI

The CLI is a discovery and registration layer, not a replacement SQL client:

```bash
enso table list
enso table schema weight_entries
enso table register weight_entries \
  --name "Weight" \
  --description "One body-weight measurement per row, recorded in kilograms."
```

- `list` shows every catalogued table with its physical name, display name, description,
  availability, and column count; the scan is capped at 500 entries and reports
  truncation.
- `schema <table>` verifies registration and prints column metadata from
  `PRAGMA table_xinfo`, including type, nullability, default, and primary-key position,
  plus its defining SQL and indexes.
- `register <table>` validates the identifier, verifies that an ordinary physical table
  already exists, requires a non-empty discovery description, and writes its metadata.
  The display name defaults from the identifier. Re-registering updates metadata without
  changing the table or its rows.

There are intentionally no `create`, `query`, `insert`, `drop`, or migration commands.
Agents already know SQLite and can use `sqlite3 ~/.enso/enso.db`; a custom query language
would create a second, weaker interface to maintain. The catalog commands exist because
discovery metadata and the UI visibility boundary are Enso-specific.

## Discovery: the `tables` skill

A bundled `tables` skill at `src/enso/skills/tables/SKILL.md` makes agent behaviour
consistent. It instructs the agent to:

1. Run `enso table list` before inventing a new table.
2. Inspect the relevant registered schema before querying or writing.
3. Prefer raw, well-typed facts with explicit timestamps and units.
4. Create or migrate user tables with private, fail-fast `sqlite3` transactions and
   foreign-key enforcement, never touching `runs`, `_enso_*`, or `sqlite_*`.
5. Register a new table with a useful name and discovery description.
6. Verify writes by reading back the affected records.
7. Confirm before dropping a table, deleting records in bulk, or making a destructive
   schema change.

The bundled shared launch instructions at `~/.enso/AGENTS.md` mention `enso table` and point to the skill. Existing untouched prompt copies advance through the known-pristine hash mechanism; customized
copies remain untouched. Bundled-skill seeding gives customized installations the feature
guidance independently of the prompt update.

## Web UI

The Tables UI is an inspection surface. It never accepts SQL and does not edit user
schemas or rows. An absent database or catalog renders as an empty list without creating
either; registration is the only Tables operation that writes Enso metadata.

| Route | Method | Status | Purpose |
| --- | --- | --- | --- |
| `/tables` | GET | Implemented | Registered table cards: name, description, physical name, column count |
| `/tables/{name}` | GET | Implemented | Column summary and bounded row preview |

The dashboard adds a registered-table count and the primary navigation adds **Tables**.
Unavailable catalog entries remain visible but do not link to a detail preview.

### Detail view

The detail page shows column names/types and a horizontally scrollable grid. Reads are
deliberately bounded:

- rows are fetched 50 at a time by default with `LIMIT`/`OFFSET`, never by loading the
  whole table; requested limits cap at 100 and offsets at 100,000
- at most 50 data/schema columns and 240 characters from each text cell reach the renderer
- CREATE SQL is capped at 20,000 characters; at most 25 indexes and 4,000 characters
  from each index definition are rendered, with every truncation made explicit
- `NULL`, BLOB values, and truncated text have explicit representations
- values are HTML-escaped like all other untrusted content
- the page avoids an unconditional `COUNT(*)`, which can turn a preview into a full-table
  scan

Pagination is deterministic: previews order by declared primary-key columns, falling back
to an unshadowed SQLite rowid alias for ordinary rowid tables. This is a stability rule,
not a claim that the key is the table's most meaningful analytical order.

Preview queries use a short-lived read-only connection with a 500 ms busy timeout and run
outside the web event loop. They expose no mutation path through the web app. The table
identifier comes from a validated registration record and is still quoted. User-supplied
sort expressions, filters, and arbitrary SQL are not accepted in v1.

## Failure behaviour

- A missing database or catalog produces an empty list without creating either.
- A stale catalog record is marked unavailable in the list and returns not found at
  detail/schema.
- A lock timeout becomes a retryable **Database busy** `503`; open, permission,
  corruption, and other access failures become **Database unavailable**. Raw SQLite
  errors are logged, never rendered, and the request never retries indefinitely.
- One invalid catalog table name cannot hide other valid registered tables.
- A browser request can never mutate a registered table's schema or rows.
- Reaching the maximum offset stops forward pagination instead of linking back to the
  same capped page.

## Non-goals

- Row, column, or schema editing in the dashboard.
- An arbitrary SQL editor in the dashboard.
- A custom query language or ORM.
- CSV/JSON import and export, charts, saved queries, or views as first-class UI objects.
- Automatic retention, deduplication, aggregation, or backup of user data.
- Surfacing every table found in SQLite without explicit registration.

These may be added after the core loop is proven: an agent stores structured data
consistently, the operator can discover and inspect it, and both use the same durable
source of truth.

## Implementation map

| Area | Change |
| --- | --- |
| `sqlite_store.py` | Shared operation-scoped connections, transactions, timeouts, and failure classification |
| `tables.py` *(new)* | Catalog initialization, validation, registration, schema inspection, and bounded previews |
| `cli.py` | Adds the `table` group with `list`, `schema`, and `register` |
| `skills/tables/SKILL.md` *(new)* | Bundled authoring/query workflow and safety rules |
| `prompts/AGENTS.md` | Adds CLI discovery guidance and points to the tables skill in the shared launch instructions |
| `web/app.py` | Adds read-only table list/detail routes and dashboard count |
| `web/templates/` | Adds table list/detail views and navigation |
| `tests/` | Covers catalog safety, identifiers, CLI, bounded rendering, locking, and run-write concurrency |
