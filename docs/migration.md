# Home migrations

From **0.2.0 onward**, users run `enso update apply` to install a release and migrate their
existing home. The command waits for active work, stops services, takes a temporary rollback
snapshot, runs the new release's migrations, restarts services, and verifies readiness.
[Installation](install.md#upgrading) owns the command, recovery, and cleanup behavior.
Homes from before 0.2.0 are unsupported; there is no automatic conversion for that old layout.

This page explains how to author a change to database schema, configuration, file formatting,
or the home's layout. A release updates both its fresh-install definitions and any migration
needed to bring existing content to that same structure.

## One ordered registry

[`src/enso/migrations.py`](../src/enso/migrations.py) contains ordinary Python functions and a
tuple of `Migration` records. Each record has a consecutive integer revision, a short name,
a function declaring the home-relative paths it may touch, and the function making the change:

```python
MIGRATIONS = (
    Migration(1, "example state and priority", lambda paths: ("enso.db",), migrate_example),
    Migration(
        2,
        "move workflow files",
        lambda paths: ("old-workflows", "workflows"),
        move_workflows,
    ),
)
```

This is an illustrative future registry. The registry shipped with 0.2.0 is empty: that
release is home revision **0**. A release with no structural changes needs no migration.
Home revisions are independent of release versions and SQLite's `user_version`; one home
step may update several files and the database together.

Append steps; never renumber, remove, or change a published step. Keep all previous steps in
later releases. Someone skipping releases runs every missing revision in order. For example,
a home at revision 0 upgrading to a release containing revisions 1 and 2 runs both.

The private `.migrations.json` file contains only `{"revision": 2}`: the last completed home
revision. An existing 0.2.0 home with no marker starts at revision 0. Enso refuses malformed
markers, newer home revisions, and gaps in the registry before changing the home. The marker
is operating state and must not be edited to bypass a migration.

Fresh `enso init` or `enso setup` stamps the latest revision before seeding the current
scaffold, so it skips historical conversions and can safely resume interrupted setup.
Rerunning initialization over existing content never marks pending migrations complete.
Existing homes with pending steps must finish the managed upgrade first.

## Declare everything the step changes

`plan(paths)` calls each pending step's `paths` function without changing the home. These
functions must only inspect the old layout; they must not parse old data through new config
or database readers that already expect the new format.

Declare every path a step can change, including files whose references it rewrites. For a
move, include **both the source and destination**, even if the destination does not exist yet.
If a step creates parent directories, declare the highest new parent it creates. For example,
moving `old-workflows` into a new `automation/workflows` declares `old-workflows` and
`automation`. The updater validates the paths and snapshots their original contents or
absence before any migration runs.

Paths must stay inside the Enso home. The updater's own runtime, releases, journal, and
rollback files are not migration targets. Source and destination roots must be real paths;
unexpected symlinks or occupied destinations stop the update. Do not merge conflicting user
files, follow a path outside the home, or overwrite custom content to make the upgrade pass.
Choose a clear error naming the conflict so the operator can fix it and retry.

For a folder move, the migration checks the old location and destination, moves the existing
files, updates any stored references, and validates their new format. Update the corresponding
`Paths` properties, [`layout.py`](../src/enso/layout.py), scaffold, and owning product docs in
the same change. Normal startup must use the resulting current layout directly.

## Database changes are SQLite transactions

Update the latest schema and `SCHEMA_VERSION` in [`db.py`](../src/enso/db.py) for fresh databases.
The migration opens an existing database directly with `sqlite3`; the normal `db.reader` and
`db.initialize` functions require the current schema and cannot read an older one.

For example, replacing an illustrative `_enso_example.completed` field with `state` and adding
`priority` requires converting old rows before dropping the old field:

```python
import sqlite3
from contextlib import closing

from .config import Paths
from .maintenance import UpdateError


def migrate_example(paths: Paths) -> None:
    if not paths.db.exists():
        return  # A home with no database will create the latest schema normally.
    with closing(sqlite3.connect(paths.db)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        revision = connection.execute("PRAGMA user_version").fetchone()[0]
        if application != 0x454E534F:
            raise UpdateError("database is not an Enso database")
        if revision == 2:
            return  # This step's database change is already complete.
        if revision != 1:
            raise UpdateError(f"expected database schema 1, found {revision}")
        connection.execute(
            "ALTER TABLE _enso_example ADD COLUMN state TEXT NOT NULL DEFAULT 'open'"
        )
        connection.execute(
            "ALTER TABLE _enso_example ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
        )
        connection.execute("UPDATE _enso_example SET state = 'done' WHERE completed = 1")
        connection.execute("ALTER TABLE _enso_example DROP COLUMN completed")
        connection.execute("PRAGMA user_version = 2")
```

The explicit `BEGIN IMMEDIATE` includes schema changes, row backfills, and `user_version` in
one transaction. An exception rolls them back together. Handle indexes, foreign keys, and
triggers affected by a removed column; where necessary, rebuild the table inside that same
transaction. Preserve unrelated Enso records and registered user tables. Do not use
`executescript` inside the transaction: its implicit transaction behavior can split the change.

## Failure, retry, and cleanup

The new release runs `apply(paths)` after the updater has stopped all writers and saved the
snapshot, before loading configuration, initializing the database, or updating bundled files.
Each completed step advances `.migrations.json` atomically. A failed step leaves its revision
unrecorded and stops the sequence; later steps do not run.

Write each step so a retry can recognize its already-completed shape without repeating a
destructive change. A database version check can establish that its transaction committed.
A file move can recognize a valid destination when the source is gone; if both exist, stop
for the conflict. Keep changes local to declared home paths and avoid external services or
other side effects that a filesystem snapshot cannot undo.

The updater owns rollback for the **whole update**, including completed earlier steps,
partially changed files, the database, and the migration marker. A migration error or failed
restart restores the previous code and saved home state before reopening work. Recovery
that cannot finish retains its snapshot and keeps work paused.

After the new services are healthy and success is recorded, the updater removes temporary
rollback data automatically. Those snapshots protect an in-progress update; they are not
long-term backups to restore after users have created newer work. No reverse migration
registry or accumulating patch files are required.

## Checks for a migration change

Test a real old schema and representative user files against the new step. Verify transformed
rows, new defaults, removed fields, renamed paths, preserved content, and the final marker.
Test skipping several releases, an already-completed step, destination conflicts, and a
failure after some work has changed. Compare fresh and upgraded homes at the new version.

The migration unit tests live in [`tests/test_migrations.py`](../tests/test_migrations.py).
[Upgrade tests](upgrade-testing.md) owns the disposable installed-release checks, including
service restart, successful cleanup, and complete database/file rollback after failure.
