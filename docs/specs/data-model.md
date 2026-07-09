# Data model

The storage layer for tasks (files), runs (SQLite + log files), and the config that
governs both. See [architecture.md](architecture.md) for how these are written, and
[tasks.md](tasks.md) for task lifecycle semantics.

The governing split: **authored intent is files, machine-generated history is SQLite.**
Jobs and tasks are hand/agent-edited Markdown you want to grep, diff, and back up. Runs
are append-only telemetry no one edits by hand — the one dataset that earns a database.

## Directory layout under `~/.enso/`

```
~/.enso/
├── config.json          # existing — gains `web` and `tasks` blocks
├── state.json           # existing — session/job-last-run state
├── messages.json        # existing — background message queue
├── enso.log             # existing — service log
├── enso.db              # NEW — SQLite: the runs table
├── jobs/                # existing — user jobs
│   └── <name>/JOB.md
├── tasks/               # NEW — one-off tasks
│   └── <slug>/
│       ├── TASK.md          # frontmatter + Markdown description
│       └── attachments/     # optional; files the agent reads by path
│           └── <file>
├── runs/                # NEW — captured output, one file per run
│   └── <run_id>.log
├── skills/              # existing — Enso-owned skills (editable via UI). External
│                        #   "parent" skills live OUTSIDE ~/.enso (e.g. ~/.claude/skills),
│                        #   discovered read-only via web.external_skill_roots
└── workspace/           # existing — working_dir (AGENTS.md, CLAUDE.md, tools/)
```

`tasks/` and `runs/` mirror the conventions already in place: `tasks/<slug>/` is the
exact shape of `jobs/<name>/`, and `runs/` is a flat blob store keyed by run id.

## Tasks (files)

A task is a directory `~/.enso/tasks/<slug>/` with a `TASK.md`:

```markdown
---
title: Research OFX parsers for the import feature
status: todo
tags: [fettia, research]
notify: true
provider: claude          # optional — overrides the task-runner default
model: sonnet             # optional
created: 2026-07-09T14:03:00Z
updated: 2026-07-09T14:03:00Z
attachments:              # optional — annotates files in attachments/
  - file: spec.pdf
    caption: current draft spec
---

Compare Go OFX libraries for 1.x (SGML) and 2.x (XML). Summarise trade-offs and
recommend one. The current draft is attached.
```

### Frontmatter fields

| Field | Required | Type | Notes |
| --- | --- | --- | --- |
| `title` | yes | string | Human label; shown everywhere. Source of the slug |
| `status` | no | enum | `todo` \| `in_progress` \| `blocked` \| `done` \| `cancelled`. Default `todo` |
| `tags` | no | list | Freeform; double as projects/labels. No built-in tags |
| `notify` | no | bool | Default `false`. `true` → ping chat on completion via `enso message send` |
| `provider` | no | string | Override which CLI runs it; else the task-runner default |
| `model` | no | string | Model override; else the task-runner default |
| `created` | auto | ISO 8601 UTC | Set on create, never changed |
| `updated` | auto | ISO 8601 UTC | Bumped on every write (drives ordering, staleness) |
| `attachments` | no | list | Optional per-file metadata (caption). The **folder is authoritative** for existence |
| `blocked_reason` | no | string | Set by the agent when `status: blocked`; shown in the UI |

The body after the frontmatter is the **description** (Markdown), the primary instruction
to the agent.

### Slug

Derived from `title` like jobs derive their dir name: lowercased, spaces → `-`,
non-alphanumerics stripped, collisions suffixed (`-2`, `-3`). The slug is the directory
name and the stable identifier in URLs and the CLI; renaming a `title` does **not** move
the directory.

### Status enum

Fixed lifecycle, deliberately small (richer workflow is expressed with tags, per the PRD):

| Status | Meaning |
| --- | --- |
| `todo` | Open, unclaimed — the task runner will pick it up |
| `in_progress` | Claimed; an agent is (or was) working it. Set *before* the agent starts |
| `blocked` | Agent stopped and needs input; `blocked_reason` explains. Not re-claimed |
| `done` | Completed |
| `cancelled` | Abandoned by the operator; never runs |

Only `todo` is claimable. `blocked`/`cancelled`/`done`/`in_progress` are skipped by the
runner. See [tasks.md](tasks.md) for transitions and claim semantics.

### Attachments

- Files live in `~/.enso/tasks/<slug>/attachments/`. Uploaded via the web UI, dropped in
  by the agent, or placed by hand.
- The agent receives their **absolute paths** in the injected prompt and reads them as
  ordinary files — no special handling.
- Frontmatter `attachments` is optional annotation (captions, ordering); the folder's
  actual contents are the source of truth for what exists. A file present in the folder
  but absent from frontmatter is still a valid attachment.
- On completion, the agent can send an attachment back to chat with
  `enso message attach <path>`.

## Runs (SQLite)

`~/.enso/enso.db`, opened in **WAL mode** (concurrent readers never block the writer).
One table. Created lazily via `CREATE TABLE IF NOT EXISTS` on first use — no migration
tooling, consistent with Enso's zero-ceremony config files.

```sql
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,      -- uuid4 hex; also the log filename
    kind         TEXT NOT NULL,         -- 'job' | 'task'
    name         TEXT NOT NULL,         -- job dir_name or task slug
    title        TEXT,                  -- display name at run time (job.name / task.title)
    trigger      TEXT NOT NULL,         -- 'schedule' | 'manual' | 'task-runner'
    status       TEXT NOT NULL,         -- 'running' | 'ok' | 'error' | 'timeout'
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
`output_path` + `output_bytes` so the UI can show size and lazy-load the body.

### Lifecycle

1. **create** (`runs.create`) at spawn: insert a row with a fresh `id`, `status='running'`,
   `started_at=now`, `trigger`, `provider`, `model`. Returns the `id`; the pipeline opens
   `runs/<id>.log` for the captured output.
2. **finish** (`runs.finish`) at exit: set `ended_at`, `duration_ms`, `exit_code`, and a
   terminal `status` — `ok` (exit 0), `error` (nonzero), or `timeout` (killed by the job
   budget). Set `output_bytes` from the log size.
3. A row left in `running` after a service restart is a **crash marker** — surfaced in the
   UI as "interrupted", reconciled lazily (a `running` row whose `started_at` predates
   process start and isn't in `_running_job_tasks` is stale).

### Retention

`runs.prune()` runs opportunistically (e.g. after each finish, throttled) and enforces
`tasks`/global caps from config: keep at most `runs_keep` rows **and** drop rows older
than `runs_max_age_days`, deleting the associated `.log` files. Defaults chosen so an
every-15-minutes job doesn't accumulate forever. Pruning never deletes a `running` row.

## Config additions

`config.json` gains two blocks, backfilled with defaults by `_with_config_defaults` the
same way provider keys are backfilled today (existing installs get the defaults without
losing user settings):

```jsonc
{
  "web": {
    "enabled": true,
    "host": "127.0.0.1",     // bind localhost; set tailnet IP / 0.0.0.0 for remote
    "port": 8765,
    "token": "",             // optional bearer token; empty = localhost-only trust
    "external_skill_roots": ["~/.claude/skills"]  // read-only "parent" skills to surface
  },
  "tasks": {
    "enabled": true,
    "schedule": "*/5 * * * *",  // task-runner tick (cron); how often to drain todos
    "provider": "claude",
    "model": "sonnet",
    "batch": 1,                 // tasks claimed per run (1 = one clean run per task)
    "notify_default": false,    // default `notify` for tasks that don't set it
    "runs_keep": 500,           // retention: max run rows to keep
    "runs_max_age_days": 30     // retention: drop runs older than this
  }
}
```

Notes:

- `web.enabled=false` skips starting the server inside `serve` (chat/jobs still run).
- `web.external_skill_roots` are scanned **read-only** to surface skills the agent can use
  that live outside `~/.enso/` (the CLIs' user-level skill dirs). The UI lists them with
  their path and never writes to them. Defaults to Claude's user skills; add Codex/Gemini
  roots as needed. Enso-owned skills (under `~/.enso/skills/`) are always editable.
- `tasks.enabled=false` unregisters the built-in task-runner job (tasks can still be
  created and browsed; they just won't auto-run).
- `runs_keep` / `runs_max_age_days` govern retention for **all** runs (job and task), not
  only task runs, despite living under `tasks` — kept here to avoid a third top-level
  block. A future `runs` block can promote them if needed.

## Cross-cutting rules

- **Timestamps** are ISO 8601 **UTC** everywhere in stored data (`created`, `updated`,
  run times). The UI localises for display; cron **schedules** stay in the system's local
  timezone, matching existing job behaviour (do not convert schedules to UTC).
- **IDs**: run ids are uuid4 hex. Task identity is the slug; job identity is the dir name.
- **Atomic file writes**: every `TASK.md`/`JOB.md`/`AGENTS.md` write is temp-file +
  `os.replace`, never an in-place truncate — a concurrent reader sees old-or-new, never
  partial.
- **Frontmatter** is real YAML (`pyyaml`) via the shared `frontmatter.py`. The legacy
  regex parser in `jobs.py` is retired; existing `JOB.md` files (simple `key: value`
  scalars) parse identically under YAML, so no job files need editing.
