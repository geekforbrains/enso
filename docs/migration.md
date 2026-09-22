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

This registry is illustrative; the file holds the real steps. The registry shipped with
0.2.0 is empty: that release is home revision **0**. A release with no structural changes
needs no migration.
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
Existing homes with pending steps must finish the managed upgrade, or an explicit local
development migration, before initialization.

For an editable checkout, [Development's manual migration commands](development.md#manual-development-migrations)
preview and apply this same registry without publishing or increasing the package version.
They preserve installed bundles and use the release snapshot helpers for recovery. A revision
already applied to a live development home must not be edited and replayed; append the next
revision, and use disposable old-layout fixtures when testing a step repeatedly.

## Job format migration

Home revision **5** brings every existing workspace's jobs, including disabled jobs, to
the [current `JOB.md` format](jobs.md). Managed `enso update apply` runs it as part of the
normal snapshotted update. For an editable installation, preview with
`scripts/dev-migrate` and explicitly apply with `scripts/dev-migrate --apply`, as described
in [Development](development.md#manual-development-migrations). Routine source refresh
does not perform this conversion.

The conversion:

- Moves provider, model, effort and follow-up settings into `agent`.
- Renames `prerun` to `gate`, writes explicit `bash <quoted-script-path>` hook commands
  with their timeouts, and replaces `{{prerun_output}}` with `{{gate_output}}`.
- Converts `concurrency_group` to `concurrency` with explicit `on_busy: skip`, preserving
  existing contention behavior. Choose `wait` yourself for jobs that should take turns.
- Converts the recognized, unchanged bundled release checker into a direct command job,
  retaining its enabled state and schedule and removing its obsolete, matching gate script.
- Upgrades SQLite to schema **4**, adding executor identity and transient group waiters.
  Historical command-sentinel agent fields become null, genuine agent settings are retained,
  and `prerun_error` becomes `gate_error` in run and attempt history. Existing historical
  `no_work` outcomes are not reinterpreted as successful commands.

YAML frontmatter is normalized, so YAML comments and its original formatting are not
preserved. Markdown body whitespace and line endings are preserved apart from the gate
placeholder replacement, and file permissions are retained. Custom scripts are not edited.
A custom script that does useful work and always exits `1` stays a gate; the migration
cannot infer its intent. Convert such a job explicitly to `command` after reviewing it.
Bundled receipts are adjusted only when their prior recorded hashes match, so edited
content does not become a falsely pristine baseline.

Preflight rejects malformed YAML, conflicting old/new fields, missing executors,
unresolved stage definitions, symbolic links and oversized job documents before changing
jobs or the database. It names the conflicting file for repair rather than guessing.
Files are replaced atomically, and the enclosing migration snapshot protects rollback
across both files and database changes. Reapplying an interrupted conversion recognizes
already-converted files. Fresh installs already use the new format.

Home revision **6** separately converts the recognized bundled `enso-audit` into
`command: enso doctor --attention --notify --quiet`. It recognizes both historical
job formats when previewing an upgrade that includes revision 5. Conversion requires
the shipped audit instructions and an unchanged shipped gate script; only that matching
script is removed. Enabled state, schedule, notification settings, timeout and concurrency
choices are retained. Customized prompts, scripts, hook behavior and missing scripts keep
their agent definition. Retained custom pairs are detached from bundled-file ownership so
a later bundle refresh cannot overwrite the job or retire its still-needed script.
Deleted definitions stay deleted. The step preflights every workspace, replaces definitions
atomically, preserves permissions and can resume partial publication. Fresh installations
seed the command job directly; revision 5 remains unchanged.

## Workspace settings migration

Home revision **7** converts each existing workspace's `WORKSPACE.md` frontmatter to
`workspace.json`. Agent and provider-argument overrides keep their values, including empty
argument lists. The migration deletes the old Markdown file and deliberately discards its
body and YAML comments; these were never loaded as agent instructions. Working guidance
belongs in `AGENTS.md`. Workspaces without overrides remain without a settings file.

All conversions are checked before writing. Malformed settings, linked or nonregular paths,
and conflicting JSON destinations stop the preview and migration. An exact JSON file left
by an interrupted conversion is recognized on retry. Both filenames are covered by the
updater's rollback snapshot, and JSON is published atomically before its source is removed.
Normal startup accepts only the new format and reports any remaining legacy file.

Managed updates apply this step automatically. Editable installations use the
[manual development migration](development.md#manual-development-migrations) before refresh.
Installed instructions and skills are not rewritten by this format conversion; any custom
references or scripts that edit `WORKSPACE.md` must be updated to the new JSON format.

## Declare everything the step changes

`plan(paths)` calls each pending step's `paths` function without changing the home. These
functions must only inspect the old layout; they must not parse old data through new config
or database readers that already expect the new format.

Declare every path a step can change, including files whose references it rewrites. For a
move, include **both the source and destination**, even if the destination does not exist yet.
If a step creates parent directories, declare the highest new parent it creates. For example,
moving `knowledge` into a new `shared/knowledge` declares `knowledge` and `shared`, plus each
workspace content root holding links the step rewrites. Declare the containing
directory rather than each user file: a snapshot takes at most 4096 paths, none ending in
`.lock`. The updater validates the paths and snapshots their original contents or absence
before any migration runs.

Paths must stay inside the Enso home. The updater's own runtime, releases, journal, and
rollback files are not migration targets. Lock files hold nothing to restore, so a step that
only removes them, as revision 1 does, declares no paths. Source and destination roots must be real paths;
unexpected symlinks or occupied destinations stop the update. Do not merge conflicting user
files, follow a path outside the home, or overwrite custom content to make the upgrade pass.
Choose a clear error naming the conflict so the operator can fix it and retry. Raise a
conflict the old layout already shows from `paths` as well as `apply`, so the development
preview reports it and a release update fails before any home change.

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

The shared `enso_home` test fixture is stamped with the latest revision, because a scratch
home has the current layout. A test that needs an older home deletes `.migrations.json` or
writes its own revision. Test a shipped step by calling that registry entry directly, as the
revision 1 test does, so later revisions never run against its fixture.

The migration unit tests live in [`tests/test_migrations.py`](../tests/test_migrations.py).
[Upgrade tests](upgrade-testing.md) owns the disposable installed-release checks, including
service restart, successful cleanup, and complete database/file rollback after failure.
