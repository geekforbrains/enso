# Migration

Follow version steps in order; later releases append another section here. Conversion needs
an operator instruction to change that installation. Developing Enso or publishing a release
does not authorize converting an existing home.

## 0.1.x → 0.2.0

This is a manual rebuild, **not `enso update apply` from 0.1.x**. Published 0.1.0–0.1.2 use
config version 1 and SQLite `application_id = 0`, `user_version = 5`. This procedure supports
that shape. For an unreleased snapshot, another schema or a failed precondition, stop and
prepare a conversion for that exact installation. Never change version markers to bypass
checks. The application has no compatibility reader or migration engine; the offline recipes
on this page are the conversion.

Use a verified **0.2.0** release bundle. If it is not published yet, stop a real conversion;
a development rehearsal can use a recorded source commit and its installed wheel.

### 1. Drain, back up, and establish rollback

Use Bash for these commands. Record the actual home, executable version, launcher, provider
paths, service definitions and code/environment supplying the old executable. Replace these
example paths when different. The conversion directory must be private, persistent, outside
the home, and absent before this operation:

```bash
umask 077
export ENSO_CONVERT_HOME="$HOME/.enso"
export ENSO_CONVERT_ROOT="$HOME/enso-conversion-$(date +%Y%m%d-%H%M%S)"
export ENSO_CONVERT_OLD="$ENSO_CONVERT_ROOT/old-home"
export ENSO_OLD_LAUNCHER="$(command -v enso)"
mkdir -m 700 "$ENSO_CONVERT_ROOT"
"$ENSO_OLD_LAUNCHER" --version > "$ENSO_CONVERT_ROOT/old-version.txt"
"$ENSO_OLD_LAUNCHER" config check --json
```

Arrange a quiet window: stop new chat traffic, scheduled triggers and other CLI writers,
then let accepted work finish. Inspect active runs and claimed tasks with the old CLI;
resolve interrupted claims and uncertain Heartbeat actions deliberately, never by clearing
claims in SQL. Stop the daemon and viewer with their owning manager. For standard Enso user
services, then back up the stopped home:

```bash
"$ENSO_OLD_LAUNCHER" service stop
"$ENSO_OLD_LAUNCHER" web stop
"$ENSO_OLD_LAUNCHER" service status
"$ENSO_OLD_LAUNCHER" web status
cp -a "$ENSO_CONVERT_HOME" "$ENSO_CONVERT_OLD"
cp -a "$ENSO_OLD_LAUNCHER" "$ENSO_CONVERT_ROOT/old-launcher"
```

Back up existing service files and overrides separately: macOS uses
`~/Library/LaunchAgents/com.enso.agent.plist` and `com.enso.web.plist`; Linux uses
`~/.config/systemd/user/enso.service` and `enso-web.service`. Include any custom supervisor.
If the launcher points outside the home, also copy its complete checkout or environment and
record its original absolute path. Stop if the old runtime cannot be restored. Keep old code
and its database together; provider authentication stays on the host.

Export a consistent SQLite snapshot from the stopped original. It preserves every table,
including history that will remain archived:

```bash
python3 - <<'PY'
import os, sqlite3
from pathlib import Path
home = Path(os.environ['ENSO_CONVERT_HOME'])
target = Path(os.environ['ENSO_CONVERT_ROOT']) / 'export.db'
assert not target.exists(), 'do not overwrite an earlier export'
with sqlite3.connect((home / 'enso.db').as_uri() + '?mode=ro', uri=True) as old:
    assert old.execute('PRAGMA application_id').fetchone()[0] == 0
    assert old.execute('PRAGMA user_version').fetchone()[0] == 5
    assert old.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    for table, predicate in (
        ('runs', "status='running'"), ('_enso_beat_runs', "status='running'"),
        ('_enso_tasks', 'claim_run_id IS NOT NULL'),
        ('_enso_beats', 'claim_run_id IS NOT NULL'),
    ):
        assert not old.execute(f'SELECT 1 FROM {table} WHERE {predicate} LIMIT 1').fetchone(), table
    with sqlite3.connect(target) as export:
        old.backup(export)
        assert export.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
print('Export verified; retain it with the old code and home.')
PY
```

If no database exists, stop to confirm that there is no retained work before using the
[fresh setup](install.md#setup) path and converting its files. Do not create a blank old
database just to pass this recipe.

**Rollback before new work:** stop all services, move the failed new home aside for diagnosis,
restore `old-home` to the original home path, restore the launcher, external code/environment
and service files to their recorded paths, then start the old services. Environments and Git
worktrees contain absolute paths: the backup is not runnable at its temporary location.
**Never start 0.1.x against the new database.** The new database uses
`application_id = 0x454E534F`, `user_version = 1`; old code might accept that lower number and
damage it. Once 0.2.0 accepts new work, restoring the old snapshot loses that work: stop and
reconcile both histories before considering rollback.

For the home portion of a pre-admission rollback, with services confirmed stopped and
`failed-home` absent:

```bash
test ! -e "$ENSO_CONVERT_ROOT/failed-home" || exit 1
mv "$ENSO_CONVERT_HOME" "$ENSO_CONVERT_ROOT/failed-home"
cp -a "$ENSO_CONVERT_OLD" "$ENSO_CONVERT_HOME"
```

Restore the saved launcher entry and any external code/service files before restarting;
do not execute the old launcher while the new database still occupies the original path.

### 2. Record decisions; do not infer them

Inventory every workspace, job, project, registered table, task, active/paused beat,
worktree and user-authored home entry. Record its destination or explicit archive disposition
in a private conversion log, without credentials. Ask the operator and stop for:

- **Trust:** accept removing `restricted` within one trusted installation, or separate the
  affected work into different installations first. Preserve provider arguments and native
  provider policy files; never substitute blanket bypass flags. Bound channel participants
  can use Enso and their eligible live discussion is captured. Workspaces are not confidential.
- **Access:** review each channel, DM and Telegram binding independently. Telegram previously
  required both a binding and `allowed_users` membership. A bound ID outside that list would
  gain access if the list were simply removed. Choose the exact new binding map; do not add
  every allowlisted user, invent an owner, or treat Slack channel access as DM access.
- **History:** ask, “May job/beat run output, outbox history and provider sessions remain only
  in the backup, with fresh sessions in 0.2.0, while tables, tasks, timelines, beats and action
  receipts stay live?” **Archive-only is the default proposal, not consent.** This recipe
  implements that choice. If live run history or resumed sessions are required, stop for a
  reviewed mapping before importing.
- **Execution:** confirm repo integration targets and retained worktree paths; decide whether
  overdue active beats should execute after startup. Resolve uncertain external actions.
  Review the new enabled 15-minute memory jobs. Preserve disabled/customized jobs unless the
  operator explicitly chooses otherwise.

Start the approved binding document from the old map, edit only agreed changes, and retain
the comparison privately:

```bash
jq '.bindings' "$ENSO_CONVERT_OLD/config.json" > "$ENSO_CONVERT_ROOT/bindings.json"
# Review and edit bindings.json before continuing.
```

The recipe retains **all** tasks and beats, including closed ones, to preserve dependencies
and avoid reference reuse. Tasks keep IDs, numbers, stages, timestamps, timelines and refs.
Beats keep states, saved agents, checkpoints, handled-event positions, events, action receipts
and sequence high-water marks. Historical event run IDs remain provenance into `export.db`,
not live run rows. New captures and memory processing state start empty; no history is fetched.

### 3. Install new code at the final home path, without starting it

Download the four official v0.2.0 assets and require that manifest version. The installer
checks wheel and constraint hashes. Do not substitute a moving latest-release installer:

```bash
mkdir "$ENSO_CONVERT_ROOT/release"
gh release download v0.2.0 --repo geekforbrains/enso \
  --dir "$ENSO_CONVERT_ROOT/release" \
  --pattern install.sh --pattern release.json --pattern '*.whl' --pattern constraints.txt
jq -e '.version == "0.2.0"' "$ENSO_CONVERT_ROOT/release/release.json"
mv "$ENSO_CONVERT_HOME" "$ENSO_CONVERT_ROOT/retired-home"
sh "$ENSO_CONVERT_ROOT/release/install.sh" \
  --manifest "$ENSO_CONVERT_ROOT/release/release.json" \
  --home "$ENSO_CONVERT_HOME" --bin-dir "$ENSO_CONVERT_ROOT/bin" \
  --extras slack,telegram,web
export ENSO_NEW="$ENSO_CONVERT_ROOT/bin/enso"
export ENSO_NEW_PYTHON="$ENSO_CONVERT_HOME/runtime/current/bin/python"
"$ENSO_NEW" --version
```

Require `enso 0.2.0`. The private launcher postpones switching the old command until
verification. Install at the final home path: managed environments embed it and must not be
built in a staging home and renamed. Do not use `--adopt`, copy old runtime receipts, or run
`serve`, `setup`, jobs or service installation during offline conversion.

### 4. Restore content and convert configuration

Copy saved workspaces, shared knowledge and user data, preserving paths, bytes and file modes.
For these usual directories, before they exist in the new home:

```bash
for name in workspaces knowledge secrets browser; do
  if [ -d "$ENSO_CONVERT_OLD/$name" ]; then
    test ! -e "$ENSO_CONVERT_HOME/$name" || exit 1
    cp -a "$ENSO_CONVERT_OLD/$name" "$ENSO_CONVERT_HOME/$name"
  fi
done
"$ENSO_NEW" init --json
```

Inspect symlinks before copying: workspace directories, note roots and managed definitions
must be real paths. Stop on conflicts. Restore other inventoried user content to approved
locations; preserve home Git history deliberately if used for notes, without committing the
conversion automatically. Do not copy old config/database, runtime/lock/PID files,
`.bundles.json`, global jobs, Heartbeat scripts or workflows wholesale.

`init` supplies current bundles and the default scaffold without starting services. Convert
workspace/project config into frontmatter with the installed Python. The original provider
settings, full agent triples and explicit empty argument lists are preserved. Occupied
output files and missing workspace owners stop this one-time step:

```bash
"$ENSO_NEW_PYTHON" - <<'PY'
import json, os
from pathlib import Path
import yaml
root = Path(os.environ['ENSO_CONVERT_ROOT'])
home = Path(os.environ['ENSO_CONVERT_HOME'])
old = json.loads((root / 'old-home/config.json').read_text())
assert old['version'] == 1
def write(path, fields):
    assert path.parent.is_dir(), f'missing owner: {path.parent}'
    with path.open('x') as stream:
        stream.write('---\n' + yaml.safe_dump(fields, sort_keys=False) + '---\n')
for name, fields in old.get('workspaces', {}).items():
    fields = dict(fields)
    fields.pop('restricted', None)
    write(home / 'workspaces' / name / 'WORKSPACE.md', fields)
for key, fields in old.get('projects', {}).items():
    fields = dict(fields)
    owner = home / 'workspaces' / fields.pop('workspace')
    assert owner.is_dir() and not owner.is_symlink(), owner
    directory = owner / 'projects' / key
    directory.mkdir(parents=True, exist_ok=True)
    write(directory / 'PROJECT.md', fields)
new = {k: v for k, v in old.items() if k not in ('workspaces', 'projects')}
new['version'] = 2
new['bindings'] = json.loads((root / 'bindings.json').read_text())
if 'telegram' in new.get('transports', {}):
    new['transports']['telegram'].pop('allowed_users', None)
with (root / 'config.v2.json').open('x') as stream:
    json.dump(new, stream, indent=2)
    stream.write('\n')
PY
```

Review the files before applying. `config.json` owns installation settings;
`WORKSPACE.md` owns only optional `agent` and `providers`. Project frontmatter has no `key`
or `workspace`; its containing directories supply them. Do not remove any other credentials
or provider settings. [Configuration](configuration.md#configuration-ownership-in-020)
owns the complete formats.

### 5. Move jobs, project scripts and Heartbeat gates

For every old `jobs/NAME/JOB.md`, read its `workspace` field, copy the **whole** directory to
`workspaces/WORKSPACE/jobs/NAME/`, then remove only that frontmatter key. Preserve the prompt,
agent, schedule, disabled state, concurrency group and hooks. Missing owners or collisions
require a decision. For a verified `nightly` job owned by `team`:

```bash
test ! -e "$ENSO_CONVERT_HOME/workspaces/team/jobs/nightly" || exit 1
mkdir -p "$ENSO_CONVERT_HOME/workspaces/team/jobs"
cp -a "$ENSO_CONVERT_OLD/jobs/nightly" "$ENSO_CONVERT_HOME/workspaces/team/jobs/nightly"
# Remove only workspace: team from the copied JOB.md frontmatter.
```

Do this before config apply seeds missing bundled jobs. Replace unmodified old audit/update
bundles with the current shipped ones; merge customized prompts/scripts and preserve
`enabled: false`. Record deliberate deletions so newly seeded counterparts are not accidentally
enabled. An existing custom `memory` job can coexist with `enso-memory`; review whether both
should run, since a custom harvester could process the same captures twice.

| Old usage | New usage |
| --- | --- |
| `enso job run nightly` | `enso job run team:nightly` |
| `enso runs --job nightly` | `enso runs --job team:nightly` |
| `$ENSO_HOME/jobs/nightly/helper.sh` | `$ENSO_HOME/workspaces/team/jobs/nightly/helper.sh` |
| `$ENSO_JOB` used as a directory name | It now contains `team:nightly`; use the job script's working directory for its files. |

Copy project scripts (often under `$ENSO_HOME/workflows/KEY/`) beside their new `PROJECT.md`.
Rewrite setup, stage, check and lifecycle commands to reference those scripts. Commands now
start beside the definition; scripts needing task code must explicitly
`cd "${ENSO_TASK_DIR:?}"` before running repository commands. Keep external `repo` locations.
Set `base` to the approved integration target; do not infer it from the current checkout.
Review old `task land` calls against the [task integration rules](tasks.md). Do not change
stage sequences or acceptance requirements merely to relocate a definition.

Restore retained old-home worktrees to their **original absolute paths** under
`worktrees/KEY/REF`, keeping dirty/untracked files, and explicitly set that project's
`worktree_root` to the absolute original `worktrees/KEY` directory. Verify
`git -C REPO worktree list --porcelain` and each tree's branch/status. Enso can adopt a
registered tree at that configured path; it no longer searches legacy locations. Do not
prune, recreate, move or rebase worktrees during conversion. Stop if a base or registration
is unclear.

For every saved beat, read `definition.workspace` from `export.db`. Copy
`old-home/heartbeat/HB-NNN/` to `workspaces/WORKSPACE/heartbeat/HB-NNN/`, keeping `gate.sh`
and helpers but excluding the old `run.lock`. Retain the beat ID and `gate: "gate.sh"`.
Scripts now start in that owning beat directory. Home-level `heartbeat/` may still exist
for operational locks; it is no longer the script location.

Search all user-authored instructions, jobs, prompts and scripts, including external repos:

```bash
rg -n --hidden -g '!runtime/**' -g '!.git/**' \
  '(~/.enso|\$\{?ENSO_HOME\}?)/(jobs|workflows|heartbeat)|allowed_users|restricted|enso (job|runs|task|project|workflow|heartbeat|knowledge|memory)' \
  "$ENSO_CONVERT_HOME/workspaces" "$ENSO_CONVERT_HOME/skills" "$ENSO_CONVERT_HOME/AGENTS.md"
```

Also search for the actual old absolute home path. Rewrite global paths using the confirmed
owner, bare jobs using `WORKSPACE:NAME`, and commands that select context with
`--workspace NAME` or `ENSO_WORKSPACE`. Heartbeat creation/listing select context; commands
addressing an existing beat use its saved owner. Cross-workspace lists use `--all-workspaces`
where supported; shared knowledge uses `--shared`. Read command help before changing syntax;
do not blindly replace historical prose or quoted examples.

### 6. Preserve notes and merge bundled guidance

Knowledge stays `enso.note/v1` at its existing shared/workspace roots. Preserve valid UUIDs,
known timestamps, attachments and relative links. Inspect malformed notes and use
`enso knowledge adopt PATH --expected-hash HASH` with the correct workspace/shared selector,
or repair metadata deliberately. Do not infer dates from filesystem timestamps.
[Knowledge](knowledge.md#the-note-format) owns the rules.

0.1.x has no supported maintained-memory schema. Ask which workspace owns each user-maintained
historical note; there is no shared memory root. Preserve originals, then import experience as
`enso.memory/v1` with a unique UUID, actual occurrence, and `sources: []`. Record the old path
and provenance in the body. Never reinterpret old database row numbers as capture IDs. For
an undated body file:

```bash
"$ENSO_NEW" memory create imported-discussion.md --workspace team \
  --occurred unknown --file "$ENSO_CONVERT_ROOT/imported-body.md" --json
```

Known dates belong under `memory/YYYY/MM/DD/`; unknown dates use `memory/undated/`. If a note
already has a valid stable identity or known document timestamps, preserve them through
explicit file editing rather than generating replacements. Keep current facts in knowledge,
repair relative links after moves, and audit both formats.

Use the **new** bundled skills; merge local preferences from the saved versions and restore
custom skills under their unique names. Compare saved home/workspace `AGENTS.md` with the new
templates and merge user guidance, replacing obsolete modes/paths. Current-fact lookup must
point to `enso-knowledge`, earlier-discussion recall to `enso-memory` in the selected workspace.
Preserve provider-native policies and host authentication. Rebuild discovery links with
`workspace audit --fix`; do not copy the old bundle manifest to label old files as current.
[Customizing](customizing.md#the-bundled-skills) owns subsequent bundle preservation.

### 7. Apply configuration and import retained records

After reviewing definitions, scripts, bundles and bindings:

```bash
"$ENSO_NEW" config apply --file "$ENSO_CONVERT_ROOT/config.v2.json" --expected-hash missing --json
"$ENSO_NEW" workspace audit --fix --json
"$ENSO_NEW" table list --json
```

Require `ok: true` and resolve preservation/conflict messages. `table list` initializes a
fresh database without starting a scheduler or provider. Do not copy old schema/version
statements into it.

Inspect registered table SQL, indexes, triggers and foreign keys in `export.db` first.
A missing table, virtual table, unregistered dependency or trigger touching operational
state requires a specific preservation plan. Review executable SQL as user-authored code.
The recipe below imports ordinary registered tables, data, indexes, triggers and catalog
metadata, then tasks/timelines/refs and beats/events. Its only operational-row addition is
`_enso_tasks.workspace`, obtained from the converted project. It compares retained rows
before committing and refuses a nonempty destination:

```bash
"$ENSO_NEW_PYTHON" - <<'PY'
import os, sqlite3
from pathlib import Path
from enso.config import Paths, load_config
from enso.db import APPLICATION_ID, SCHEMA_VERSION
from enso.tables import valid_name
root = Path(os.environ['ENSO_CONVERT_ROOT'])
paths = Paths(Path(os.environ['ENSO_CONVERT_HOME']))
config = load_config(paths)
def q(value):
    return '"' + value.replace('"', '""') + '"'
with sqlite3.connect(paths.db, uri=True) as con:
    con.row_factory = sqlite3.Row
    assert con.execute('PRAGMA application_id').fetchone()[0] == APPLICATION_ID
    assert con.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    con.execute('ATTACH DATABASE ? AS old', ((root / 'export.db').as_uri() + '?mode=ro',))
    assert con.execute('PRAGMA old.application_id').fetchone()[0] == 0
    assert con.execute('PRAGMA old.user_version').fetchone()[0] == 5
    for row in con.execute("SELECT name FROM main.sqlite_master WHERE type='table'"):
        assert not con.execute(f'SELECT 1 FROM main.{q(row[0])} LIMIT 1').fetchone(), row[0]
    con.execute('BEGIN IMMEDIATE')
    names = {valid_name(r[0]) for r in con.execute('SELECT table_name FROM old._enso_tables')}
    objects = []
    def compare(table, columns):
        fields = ','.join(map(q, columns))
        assert list(con.execute(f'SELECT {fields} FROM main.{q(table)} ORDER BY {fields}')) == list(
            con.execute(f'SELECT {fields} FROM old.{q(table)} ORDER BY {fields}')), table
    for name in sorted(names):
        schema = con.execute('SELECT sql FROM old.sqlite_master WHERE type=? AND name=?',
                             ('table', name)).fetchone()
        assert schema and schema[0].lstrip().upper().startswith('CREATE TABLE'), name
        for fk in con.execute(f'PRAGMA old.foreign_key_list({q(name)})'):
            assert fk['table'] in names, f'unregistered dependency: {name} -> {fk["table"]}'
        con.execute(schema[0])
        objects.extend(r[0] for r in con.execute(
            "SELECT sql FROM old.sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
            "AND sql IS NOT NULL ORDER BY type,name", (name,)))
        columns = [r['name'] for r in con.execute(f'PRAGMA old.table_xinfo({q(name)})') if r['hidden'] == 0]
        without_rowid = con.execute(
            "SELECT wr FROM pragma_table_list WHERE schema='old' AND name=?", (name,)).fetchone()[0]
        if not without_rowid:
            alias = next((a for a in ('rowid', '_rowid_', 'oid') if a not in columns), None)
            assert alias, f'rowid shadowed: {name}; review explicitly'
            columns.insert(0, alias)
        fields = ','.join(map(q, columns))
        con.execute(f'INSERT INTO main.{q(name)} ({fields}) SELECT {fields} FROM old.{q(name)}')
        compare(name, columns)
    for table in ('_enso_tables', '_enso_tasks', '_enso_task_events', '_enso_task_refs',
                  '_enso_beats', '_enso_beat_events'):
        old_columns = [r['name'] for r in con.execute(f'PRAGMA old.table_info({q(table)})')]
        new_columns = [r['name'] for r in con.execute(f'PRAGMA main.table_info({q(table)})')]
        assert set(new_columns) == set(old_columns) | ({'workspace'} if table == '_enso_tasks' else set())
        for source in con.execute(f'SELECT * FROM old.{q(table)}').fetchall():
            row = dict(source)
            if table == '_enso_tasks':
                project = config.projects[row['project']]
                row['workspace'] = project.workspace
                stages = {'backlog', 'blocked', 'done', 'cancelled', *(s.name for s in project.stages)}
                assert row['stage'] in stages
                assert row['previous_stage'] is None or row['previous_stage'] in stages
            if table in ('_enso_tasks', '_enso_beats'):
                assert row['claim_run_id'] is None
            fields = ','.join(map(q, row))
            con.execute(f'INSERT INTO main.{q(table)} ({fields}) VALUES ({",".join("?" for _ in row)})',
                        tuple(row.values()))
        compare(table, old_columns)
    for name, seq in con.execute('SELECT name,seq FROM old.sqlite_sequence').fetchall():
        if name in names | {'_enso_beats', '_enso_beat_events'}:
            con.execute('DELETE FROM main.sqlite_sequence WHERE name=?', (name,))
            con.execute('INSERT INTO main.sqlite_sequence(name,seq) VALUES (?,?)', (name, seq))
    for table, column, parent, key in (
        ('_enso_task_events', 'task_id', '_enso_tasks', 'id'),
        ('_enso_task_refs', 'task_id', '_enso_tasks', 'id'),
        ('_enso_beat_events', 'beat_id', '_enso_beats', 'id'),
        ('_enso_tasks', 'after_ref', '_enso_tasks', 'ref'),
        ('_enso_tasks', 'from_ref', '_enso_tasks', 'ref'),
    ):
        assert not con.execute(f'SELECT 1 FROM main.{q(table)} c LEFT JOIN main.{q(parent)} p '
            f'ON c.{q(column)}=p.{q(key)} WHERE c.{q(column)} IS NOT NULL AND p.{q(key)} IS NULL '
            'LIMIT 1').fetchone(), f'broken relationship: {table}.{column}'
    for sql in objects:  # Triggers are installed after data: import must not replay actions.
        con.execute(sql)
    assert not con.execute('PRAGMA main.foreign_key_check').fetchall()
    assert con.execute('PRAGMA main.integrity_check').fetchone()[0] == 'ok'
print('Import committed; table rows and task/beat history verified.')
PY
```

A failure rolls back the transaction. Inspect it against the untouched export; do not delete
troublesome records or use `INSERT OR IGNORE`. A rerun stops if anything is already imported.
The source is read-only. Fresh captures, memory processing receipts, sessions, job state and
run tables remain empty. This is a one-time offline restoration, not a live task-editing
interface; use `enso task` for ordinary changes.

### 8. Verify, then enable work

Compare `task list --all --all-workspaces --json` and `task show REF --workspace OWNER --json`
with the export: IDs, ownership, stages, dependencies, timeline and ref counts must match.
Check `heartbeat list --all --all-workspaces --json` and each retained beat's events/action
receipts (use `heartbeat show REF --json` and paginated `heartbeat history REF --json`).
For Heartbeat lists larger than one page, repeat with `--limit 500 --offset N`; for event history,
use `--before ID` until all retained events have been checked, not just the first page.
Validate active/paused definitions and gates without changing their state:

```bash
"$ENSO_NEW_PYTHON" - <<'PY'
import os
from pathlib import Path
from enso import heartbeat
from enso.config import Paths, load_config
config = load_config(Paths(Path(os.environ['ENSO_CONVERT_HOME'])))
offset = 0
while beats := heartbeat.list_beats(config.paths, limit=500, offset=offset):
    for beat in beats:
        heartbeat.validate_definition(beat.definition.as_dict(), config, at_consumed=beat.at_consumed)
        heartbeat.validate_gate(config.paths, beat)
    offset += len(beats)
print('Active and paused beat definitions and gate syntax validated.')
PY
```

Resolve uncertain actions before resuming. Check shell syntax without executing
gates or project scripts with side effects. `table list` and `table schema NAME` must match
the retained catalog; the import compared every row.

```bash
"$ENSO_NEW" project list --all-workspaces --json
"$ENSO_NEW" job list --all-workspaces --json
"$ENSO_NEW" knowledge audit --shared --json
# Repeat these two audits for every workspace, including unbound ones:
"$ENSO_NEW" knowledge audit --workspace default --json
"$ENSO_NEW" memory audit --workspace default --json
"$ENSO_NEW" config check --json
"$ENSO_NEW" doctor --json
```

Require successful config check and doctor; explain warnings. Stopped services are expected.
Doctor checks structure, not factual truth, command safety or provider authentication. Review
job enablement, schedules, memory harvesting and active/paused beats before starting anything.

Once the recorded authorization covers admitting work, move the original launcher entry
(including a symlink) into the conversion directory, copy `$ENSO_CONVERT_ROOT/bin/enso` to
the original launcher path, and put that directory first on `PATH`. Reinstall/start only the
services previously in use, using the new CLI and final home, following
[Installation](install.md#the-service).
`enso service install` and `enso web install` start standard services; never run them during
rehearsal. Custom supervisors need their own reviewed switch. Verify status/logs and an
authorized conversation in its selected workspace. Record when new work was first admitted;
rollback to the old snapshot is no longer safe. Retain the old code, full home, export and
decisions together.

### Rehearsal evidence and limits

Rehearse in a fresh scratch home before a real conversion. Use synthetic registered tables
with indexes, triggers, foreign keys and binary data; tasks with dependencies/timelines;
active and paused beats with gates/action receipts; workspace overrides and jobs. Run these
same offline snippets and CLI checks, compare records/file bytes, and confirm a second import
refuses to overwrite them. Use fake executables/transport clients and stub service **status**
inspection in the rehearsal doctor call. Never manage host services, use real accounts or send
external messages.

The development rehearsal uses the schema from `v0.1.2`, an installed wheel outside the source
checkout, and this page's code blocks. It covers offline conversion and doctor/config checks,
not credentials, native service replacement, model recall quality or workspace security
isolation. See also [workspace acceptance coverage](development.md#workspace-acceptance-coverage).
