# Data model

The storage layer for jobs and reference material (files), runs (SQLite + log files),
registered data tables (SQLite), and the config that governs them. See
[architecture.md](architecture.md) for how these are written and
[tables.md](tables.md) for the data-table contract.

The governing split: **authored prose and procedure are files; structured, queryable
records are SQLite.** Jobs, skills, and docs are hand/agent-edited Markdown you want to
grep, diff, and back up. Runs are append-only telemetry, while user data tables hold facts
whose value comes from filtering, joining, and aggregation.

## Directory layout under `~/.enso/`

```
~/.enso/
├── config.json          # settings, including `web` and `runs` blocks
├── state.json           # session/job-last-run state
├── messages.json        # background message queue (plus a .lock twin for cross-process writes)
├── update.json          # updater-owned metadata (installed revision, pending confirmation)
├── slack-app-manifest.yaml  # copy of the bundled Slack app manifest (written by `enso setup`)
├── enso.log             # service log
├── enso.db              # SQLite: run history plus registered user data tables
├── cache/
│   └── slack.json       # Slack name↔ID directory cache (`enso slack`)
├── secrets/             # *.env files loaded into the `enso serve` environment at
│   └── 1password.env    #   startup, so jobs inherit credentials a service manager
│                        #   would not otherwise pass through. Existing vars win.
├── docs/                # operator reference docs, nested to any depth.
│   ├── homelab.md       #   Markdown + frontmatter; identity is the relative path.
│   └── stuff/sub_stuff.md   #   See [docs.md](docs.md)
├── jobs/                # user jobs
│   └── <name>/JOB.md    # plus a .run.lock twin while a run is in flight
├── runs/                # captured output, one file per run
│   └── <run_id>.log
├── skills/              # existing — Enso-owned skills (editable via UI). External
│                        #   "parent" skills live OUTSIDE ~/.enso (e.g. ~/.claude/skills),
│                        #   discovered read-only via web.external_skill_roots
│   └── .deleted/        # deletion markers preventing bundled skills from being reseeded
└── workspace/           # existing — working_dir (AGENTS.md, CLAUDE.md, tools/)
```

Deleting an Enso-owned skill removes its complete directory. For a bundled skill, a
zero-byte marker at `skills/.deleted/<name>.deleted` records the explicit deletion so it
is not silently recreated the next time the agent service installs its system prompts.
Custom and external skill names do not receive markers; external skills cannot be deleted
by the dashboard.

`runs/` mirrors a convention already in place: it is a flat blob store keyed by run id.

`secrets/` is read, never written. `enso serve` parses each `*.env` in filename order at
startup — skipping blanks and `#` comments, tolerating a leading `export `, and stripping
surrounding quotes — and sets only keys absent from the environment, so an explicit export
still wins. The dashboard process does not read it. Values are never logged; only the key
names that were loaded are.

`docs/` is the one file-backed kind identified by a **relative path** rather than a
directory name, and the only one Enso ships no starter content for — so it needs neither
seeding nor deletion markers. Deleting a doc prunes the empty parents it leaves behind.

## Shared SQLite database

`~/.enso/enso.db`, opened in **WAL mode** (concurrent readers never block the writer).
Internal tables are created lazily via `CREATE TABLE IF NOT EXISTS` on first use — no
migration tooling, consistent with Enso's zero-ceremony config files. `runs` and every
name beginning `_enso_` or `sqlite_` are reserved for Enso/SQLite; registered user tables
must use a lowercase `snake_case` name. See [tables.md](tables.md) for validation and
registration behaviour.

The database plus its `-wal`, `-shm`, and rollback-journal sidecars are forced to `0600`.
Creation uses an owner-only placeholder before SQLite opens the path, avoiding a
permissive-umask window; later opens also repair looser modes from existing installs.

## Runs

```sql
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,      -- uuid4 hex; also the log filename
    kind         TEXT NOT NULL,         -- 'job'
    name         TEXT NOT NULL,         -- job dir_name
    title        TEXT,                  -- display name at run time (job.name)
    trigger      TEXT NOT NULL,         -- 'schedule' | 'manual'
    status       TEXT NOT NULL,         -- also 'prerun_error' | 'prerun_timeout' for jobs
    exit_code    INTEGER,               -- NULL while running
    provider     TEXT,                  -- e.g. 'claude'
    model        TEXT,                  -- e.g. 'sonnet'
    started_at   TEXT NOT NULL,         -- ISO 8601 UTC
    ended_at     TEXT,                  -- ISO 8601 UTC; NULL while running
    duration_ms  INTEGER,              -- filled on finish
    output_path  TEXT,                  -- '~/.enso/runs/<id>.log'; NULL if no output
    output_bytes INTEGER               -- size of the log, for the UI
);

CREATE INDEX IF NOT EXISTS idx_runs_started    ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_kind_name  ON runs (kind, name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status     ON runs (status);
```

### Why metadata-in-DB, output-on-disk

Run output is an agent transcript — often KBs, sometimes large. Keeping it in a `.log`
file rather than a `TEXT` column keeps the DB small and fast to query, keeps output
**greppable** (`rg` across `~/.enso/runs/` still works — Enso's file-first ethos), and
makes retention a matter of deleting a row and unlinking a file. The row carries
`output_path` + `output_bytes` so the UI can show the size, a bounded preview, and the
path to the full log.

### Lifecycle

1. **create** (`runs.create`) before provider spawn: insert a row with a fresh `id`,
   `status='running'`, pipeline `started_at`, `trigger`, `provider`, and `model`. Failed
   preruns create the row when classified while preserving the earlier gate start time;
   intentional no-work creates no row. Returns the `id`; output uses `runs/<id>.log`.
2. **finish** (`runs.finish`) at exit: set `ended_at`, `duration_ms`, `exit_code`, and a
   terminal `status` — `ok` (exit 0), `error` (nonzero), `timeout` (killed by the job
   budget), `prerun_error`, or `prerun_timeout`. Intentional prerun no-work (`exit 1`)
   creates no row. Set `output_bytes` from the log size.
3. A row left in `running` after a process restart remains as a **crash marker**. There
   is currently no automatic stale-run reconciliation.

### Retention

`runs.prune()` runs after each terminal finish and enforces the caps from the `runs`
config block: keep at most `runs.keep` rows **and** drop rows older than
`runs.max_age_days`, deleting the associated `.log` files. Defaults chosen so an
every-15-minutes job doesn't accumulate forever. Pruning never deletes a `running` row.

Prerun notification suppression lives in `state.json` under
`job_failure_alerts`. It stores only a fingerprint, transport/destination metadata,
timestamps, and a suppression count — never the diagnostic or prerun source output.

## Registered data tables

User-defined data tables share `enso.db` with run history but remain explicitly separated
through a small Enso-owned catalog:

```sql
CREATE TABLE IF NOT EXISTS _enso_tables (
    table_name  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
```

Only an existing ordinary, non-virtual table with a valid row in `_enso_tables` is an Enso
Table; views and virtual tables are excluded. This explicit registration boundary keeps
internal tables and unrelated SQLite experiments out of CLI and dashboard discovery. A
catalog entry whose table later disappears remains visible as unavailable for diagnosis.
The physical `table_name` is stable identity; `name` and the required non-empty
`description` are display/discovery metadata.
`created_at` and `updated_at` are ISO 8601 UTC catalog timestamps; updating registration
metadata preserves the former and advances the latter.

Enso does not own the schemas or rows of registered tables. Agents create, migrate,
query, and update them using standard SQLite, guided by the bundled `tables` skill. The
dashboard opens them read-only and fetches bounded previews. Run pruning is scoped to
`runs` and its log files; user tables have no automatic retention or deletion. Full
behaviour is specified in [tables.md](tables.md).

## Config blocks

The three defaulted blocks documented here are backfilled by
`_with_config_defaults` without replacing user settings:

```jsonc
{
  "agent": {
    "timeout": 900            // interactive turn timeout in seconds; 0 disables
  },
  "web": {
    "enabled": true,
    "host": "127.0.0.1",     // bind localhost; set tailnet IP / 0.0.0.0 for remote
    "port": 1337,
    "token": "",             // optional shared token; empty = no authentication
    "allowed_hosts": [],      // extra accepted Host names/IPs for remote access
    "external_skill_roots": ["~/.claude/skills"]  // read-only "parent" skills to surface
  },
  "runs": {
    "keep": 500,             // retention: max run rows to keep
    "max_age_days": 30       // retention: drop runs older than this
  }
}
```

Notes:

- `agent.timeout` applies equally to Claude, Codex, and Antigravity interactive turns.
  Timeout recovery context is durable and scoped to the originating conversation;
  scheduled jobs continue to use the `timeout` in each `JOB.md`.
- The `providers` block is derived from the provider registry. Upgrades add missing
  provider entries and their default model lists while preserving existing provider
  paths, custom models, and unknown per-provider settings.
- The dashboard is a separate `enso web` process. `web.enabled` tells the self-updater
  whether to install web dependencies; `enso web` itself is started and stopped
  separately from `enso serve`.
- Host-header checks always allow loopback and a concrete `web.host`. Add remote DNS
  names or IPs to `web.allowed_hosts`. Binding `0.0.0.0` or `::` changes only the listen
  interface; it does not allow arbitrary request hosts, and `"*"` is not accepted as an
  allowlist entry.
- An empty `web.token` disables authentication; `allowed_hosts` is a DNS-rebinding guard,
  not an identity check. Protect any remotely reachable dashboard with a strong token or
  trusted tailnet/reverse-proxy access controls.
- `web.external_skill_roots` are scanned **read-only** to surface skills the agent can use
  that live outside `~/.enso/` (the CLIs' user-level skill dirs). The UI lists them with
  their path and never writes to them. Defaults to Claude's user skills; add Codex roots
  as needed. Enso-owned skills (under `~/.enso/skills/`) are always editable.
- `runs.keep` / `runs.max_age_days` govern retention for **all** run history, pruned after
  each terminal finish (see § Retention).
- Upgrades migrate legacy `tasks.runs_keep` / `tasks.runs_max_age_days` values into this
  block when an explicit `runs` value does not already take precedence, then remove the
  obsolete `tasks` block.

## Cross-cutting rules

- **Timestamps** are ISO 8601 **UTC** in Enso-owned stored data (run times). The tables
  skill applies the same convention to user schemas unless their domain requires
  something else. The UI localises run times for display; cron **schedules** stay in the
  system's local timezone, matching existing job behaviour (do not convert schedules to
  UTC).
- **IDs**: run ids are uuid4 hex. Job identity is the dir name.
- **Atomic dashboard writes**: edits to `JOB.md`, Enso-owned `SKILL.md`, and `AGENTS.md`
  use a temp file plus `os.replace`, so a concurrent reader sees old-or-new, never a
  partial write. Doc edits follow the same rule.
- **Frontmatter compatibility**: `jobs.py` parses valid YAML mappings with PyYAML's
  `BaseLoader`, keeping scalar values as strings, then falls back to the legacy line
  parser for malformed older headers such as an unquoted `name: Daily: Review`.
  Dashboard body/toggle writes edit the raw fenced text atomically without reserializing
  it; CLI scaffolding emits valid YAML.
