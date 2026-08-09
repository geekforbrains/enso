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

Transport and dashboard credentials do not have to be environment projections. A
supported config value can use a direct 1Password reference named
`<key>_1password`:

```jsonc
{
  "transports": {
    "telegram": {
      "bot_token_1password": {
        "item": "Enso - Transport - Telegram",
        "field": "TELEGRAM_BOT_TOKEN"
      }
    },
    "slack": {
      "bot_token_1password": {
        "item": "Enso - Transport - Slack",
        "field": "SLACK_BOT_TOKEN"
      },
      "app_token_1password": {
        "item": "Enso - Transport - Slack",
        "field": "SLACK_APP_TOKEN"
      }
    }
  },
  "web": {
    "token_1password": {
      "item": "Enso - Web - Dashboard",
      "field": "WEB_TOKEN"
    }
  }
}
```

At daemon or dashboard construction, setup validation, or a token-dependent CLI
invocation, Enso sources `~/.enso/lib/1password.sh` and calls its `op_secret` function
with the configured item and field as positional arguments. The resolved value remains
process-local and is not written back to config or exported. Literal `bot_token`,
`app_token`, and `web.token` keys are the backward-compatible fallback only when the
matching reference key is absent. A present but malformed, unavailable, or empty
reference fails closed. Literal credentials must be strings; other JSON types are
rejected rather than normalized to empty values. The helper's own service-account credential can still be
bootstrapped through `secrets/1password.env`; referenced transport reconfiguration also
requires the helper's `op_set_secret` function.

The setup wizard preserves this storage choice. Reconfiguring a referenced Telegram bot
token or Slack bot/app token updates the existing 1Password field through
`op_set_secret`; the replacement value reaches the helper shell over stdin and is never
placed in process argv. Enso keeps the reference object, removes any stale literal for
that key, and aborts without writing a plaintext fallback if the helper update fails.
Slack prevalidates both previous referenced values before changing either field and
best-effort restores an earlier field if a later update fails. Sections with no
reference retain the legacy literal setup flow.

`docs/` is the one file-backed kind identified by a **relative path** rather than a
directory name, and the only one Enso ships no starter content for — so it needs neither
seeding nor deletion markers. Deleting a doc prunes the empty parents it leaves behind.

## Shared SQLite database

`~/.enso/enso.db`, opened in **WAL mode** (readers normally continue while a writer commits).
Internal tables are created lazily via `CREATE TABLE IF NOT EXISTS` on first use — no
migration tooling, consistent with Enso's zero-ceremony config files. `runs` and every
name beginning `_enso_` or `sqlite_` are reserved for Enso/SQLite; registered user tables
must use a lowercase `snake_case` name. See [tables.md](tables.md) for validation and
registration behaviour.

Enso does not cache or share SQLite connection objects. Each storage operation opens and
closes its own connection on the thread that uses it. Read connections are read-only and
wait at most 500 ms for a lock; writes have a five-second writer-acquisition budget and
use an explicit `BEGIN IMMEDIATE` / `COMMIT` transaction with rollback on every failure. WAL improves
read/write concurrency, but SQLite still has one writer at a time, so bounded waits and
clean transaction ownership remain necessary.

Async runtimes execute each complete SQLite operation in a worker thread. This keeps a
busy wait out of the bot and web event loops while preserving `sqlite3`'s simple,
synchronous transaction boundary. A lock timeout is classified as **busy** (retryable);
open, permission, corruption, and other database failures are **unavailable**.

The database plus its `-wal`, `-shm`, and rollback-journal sidecars are forced to `0600`.
Creation uses an owner-only placeholder before SQLite opens the path, avoiding a
permissive-umask window; later opens also repair looser modes from existing installs.
Permission repair uses descriptor-free metadata calls: opening and closing a live SQLite
file in the same process can release that process's POSIX record locks.

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

The runtime offloads create, finish, output bookkeeping, and retention as complete
worker-thread operations. Run-history telemetry remains best-effort and cannot abort the
job being observed.

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
    "token": "",             // optional shared token; see token_1password below
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
- With neither `web.token` nor `web.token_1password`, authentication is disabled;
  `allowed_hosts` is a DNS-rebinding guard, not an identity check. Protect any remotely
  reachable dashboard with a strong token or trusted tailnet/reverse-proxy access
  controls.
- `web.external_skill_roots` are scanned **read-only** to surface skills the agent can use
  that live outside `~/.enso/` (the CLIs' user-level skill dirs). The UI lists them with
  their path and never writes to them. Defaults to Claude's user skills; add Codex roots
  as needed. Enso-owned skills (under `~/.enso/skills/`) are always editable.
- `runs.keep` / `runs.max_age_days` govern retention for **all** run history, pruned after
  each terminal finish (see § Retention).
- Upgrades migrate legacy `tasks.runs_keep` / `tasks.runs_max_age_days` values into this
  block when an explicit `runs` value does not already take precedence, then remove the
  obsolete `tasks` block.

## Teams

The storage half of [teams.md](teams.md), which owns Slack route semantics; native CLI
invocation is [permissions.md](permissions.md). This section is the sole owner of the
teams config schema and the audit and delivery-ledger tables.

### Workspace and policy layout

Teams mode adds named Slack workspaces without moving or retiring the existing
`working_dir`. Telegram and legacy Slack continue to use `working_dir`; a Slack route
uses the path of its named workspace.

```
~/.enso/
├── workspace/                         # existing working_dir; unchanged
├── workspaces/
│   ├── ops/                           # AGENTS.md, CLAUDE.md, tools/, uploads/
│   └── acme/
└── policies/
    ├── ops/
    └── acme/
        ├── claude/settings.json       # native Claude settings
        └── codex/
            ├── config.toml            # native Codex config/profile source
            └── rules/*.rules
```

The policy directory is outside the provider cwd so working code cannot casually rewrite
the control file governing it. Enso passes or stages these files through each CLI's native
configuration mechanism without translating them. The operator remains responsible for
policy content, filesystem protection, and testing; see [permissions.md](permissions.md).

Each turn gets a unique `uploads/<turn-id>/` directory under its workspace. System
prompts, tools, uploads, provider state, background messages, and session keys are all
resolved from the same immutable execution key. Jobs, docs, `config.json`, `enso.db`, and
the complete shared skill root are never linked into a policy-controlled workspace.

Skills stay authored under `~/.enso/skills`. A workspace exposes only its allowlisted
skills. A symlink is not inherently read-only: the selected native policy or an OS mount
must prevent writes to shared sources.

### Teams config

Four top-level blocks participate in Slack teams mode. `routes.slack` is the explicit
opt-in switch.

```jsonc
{
  "groups": {
    "admin": { "slack": ["U01ADMIN"] },
    "team":  { "slack": ["U02DEV", "U03PM"] }
  },
  "workspaces": {
    "ops": {
      "path": "~/.enso/workspaces/ops",
      "unrestricted": true,
      "providers": ["claude", "codex", "agy"],
      "default_provider": "claude",
      "skills": "*",
      "chat_commands": "*",
      "concurrency": 1
    },
    "acme": {
      "path": "~/.enso/workspaces/acme",
      "policy_dir": "~/.enso/policies/acme",
      "providers": ["claude", "codex"],
      "default_provider": "claude",
      "skills": ["docs"],
      "chat_commands": ["status", "clear", "stop", "help"],
      "concurrency": 1
    }
  },
  "routes": {
    "slack": {
      "account_id": "T0ENSO",
      "dms": {
        "owner": {
          "allow": ["admin"],
          "workspace": "ops",
          "audit": false
        },
        "project-team": {
          "allow": ["team"],
          "workspace": "acme",
          "audit": true
        }
      },
      "channels": {
        "C0ACME": {
          "allow": ["team", "admin"],
          "workspace": "acme",
          "audit": true,
          "context_from": "allowed"
        }
      }
    }
  },
  "audit": {
    "on_failure": "block",
    "max_age_days": 365
  }
}
```

Schema rules:

- `groups.<name>.slack` is a unique list of Slack user IDs. Slack is the only group
  platform in v1; a Telegram key is invalid.
- `workspaces.<name>.path` is required. `policy_dir` defaults to
  `~/.enso/policies/<workspace-name>` for policy-controlled workspaces. Expanded real
  workspace paths must be distinct, non-nested, and owned safely. A policy-controlled
  workspace may not overlap the legacy `working_dir`, because Telegram or legacy Slack
  can still run there without its policy. Every policy directory must be outside all
  workspace paths and the legacy `working_dir`.
- `unrestricted` defaults to `false`. Only explicit `true` selects today's yolo
  invocation. It does not imply providers, skills, or commands.
- `unrestricted: true` is invalid alongside an explicit `policy_dir` or native policy
  found at the canonical default path. Enso never chooses between policy-controlled and
  unrestricted modes by precedence.
- `providers`, `skills`, and `chat_commands` default to empty. The latter two accept
  either a unique string list or the explicit string `"*"`.
- `default_provider` is required when `providers` is non-empty and must name one of them.
  Provider declaration order is not a default. `!use` selections are scoped to the
  conversation, workspace, and binding revision.
- `concurrency` is a positive integer and defaults to `1`. Chat turns, compaction, and
  jobs share the workspace semaphore.
- `routes.slack.dms` is a map of named DM routes. `routes.slack.channels` is keyed by
  exact Slack channel ID; there is no default or wildcard route.
- `routes.slack.account_id` is required and must equal the Slack team/workspace ID
  returned by the configured credentials at startup. Mismatch disables teams dispatch;
  these routes are never applied to another Slack account.
- Every route requires `allow` and `workspace`. `allow` defaults to empty for defensive
  parsing, but validation reports the omission. Unknown groups/workspaces disable the
  route.
- `audit` defaults to `false`; `context_from` defaults to `"allowed"` and accepts
  only `"allowed"` or `"everyone"`.
- `audit.on_failure` accepts `"block"` or `"warn"` and defaults to `"block"`.
  `audit.max_age_days` is a non-negative integer and defaults to `365`; `0` means
  indefinite retention.
- A known Slack user may match any number of groups for channel authorization but at most
  one DM route. Ambiguous DM matches are configuration errors that disable Slack teams
  dispatch until corrected.
- Missing provider policy or a rejected native config blocks that provider; it blocks the
  turn when that provider is selected. An unavailable workspace blocks every provider.
  There is no fallback to `working_dir` or another workspace.
- A parse or schema error in `groups`, `workspaces`, or `routes` disables Slack teams
  dispatch. The config loader must not replace invalid security config with permissive
  defaults; valid unrelated transports may continue.

### Compatibility and migration

- With no `routes.slack`, Enso stays in legacy Slack mode only when the pre-existing
  `transports.slack.allowed_users` key is present, and uses it with `working_dir`.
- With neither `routes.slack` nor the legacy allowlist, Slack dispatch is blocked until
  the operator adds one.
- `routes.slack` and the legacy Slack allowlist are mutually exclusive. Enso rejects the
  ambiguous combination instead of choosing precedence.
- Opting into teams mode never synthesizes routes from an old allowlist. The initial route
  maps are empty, so access remains blocked until the operator adds exact entries.
- Existing files and sessions are not moved. Only an explicitly unrestricted workspace
  may point at the old `working_dir`; a policy-controlled workspace must use a separate
  path.
- Telegram keeps its existing user-ID allowlist and `working_dir`, with non-private chat
  types rejected; no teams migration or `routes.telegram` exists.

### Jobs in teams mode

When `routes.slack` activates teams mode, every enabled `JOB.md` requires a `workspace`
frontmatter field naming a configured workspace. There is no `jobs.default_workspace` and
no fallback to `working_dir`. Existing jobs without the field remain on disk but are not
scheduled; startup and the dashboard show a configuration error until the operator makes
an explicit choice.

The job's existing `provider` must be in that workspace's provider allowlist and have a
usable native policy unless the workspace is explicitly unrestricted. `prerun` is valid
only for unrestricted workspaces. A job notification may not target an audited Slack
route.

Queued execution captures the complete job-file digest, a digest of the relevant
workspace config, and the provider policy revision. Enso reloads and compares them after
acquiring both the per-job lock and workspace semaphore and immediately before any
`prerun` or provider process. A mismatch cancels the captured execution; it does not apply
old authorization to new content or silently run the new version.

### Slack delivery ledger

Teams mode deduplicates Slack delivery independently of optional text auditing. For v1
`message` and `app_mention` events, `delivery_id` is the SHA-256 digest of a versioned,
length-prefixed tuple containing the authenticated account ID, channel ID, and canonical
source message timestamp. Both Slack event types for one message and every retry therefore
produce the same opaque ID. Any future interactive event type must define and test an
equally stable canonical key before it can dispatch. The transport atomically claims that
ID in a bounded metadata-only ledger before routing:

```sql
CREATE TABLE IF NOT EXISTS _enso_slack_events (
    account_id      TEXT NOT NULL,
    delivery_id     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    completed_at    TEXT,
    status          TEXT NOT NULL,       -- 'pending' | 'completed' | 'abandoned'
    audit_turn_id   TEXT,
    PRIMARY KEY (account_id, delivery_id)
);
```

A duplicate claim acknowledges the Slack retry without executing a command, spawning a
provider, or delivering another response. This remains true when `audit: false` or an
audit write uses `on_failure: "warn"`. Failure to claim the ledger blocks execution; it
never degrades to at-least-once dispatch. Every normal terminal path, including silence,
configuration refusal, provider failure, and delivery failure, marks the claim
`completed` in a finalization path. At startup, claims left `pending` by a crash are marked
`abandoned`, suppress the original event if it is retried, and are never replayed. A linked
pending audit row is completed as `outcome = 'error'`,
`terminal_reason = 'service_restart'`, with no delivery. All claims are pruned seven days
after receipt. The ledger stores no user ID, channel ID, message text, or text hash; its
opaque digest and timestamps are operational metadata, not a conversation audit.

### Audit log

Interactive chat is still not a run — see
[architecture.md § Run recording](architecture.md#run-recording). The Slack audit trail
uses one row per human-facing turn rather than separate input/output events, so an incident
can be reconstructed without joining rows heuristically.

```sql
CREATE TABLE IF NOT EXISTS _enso_audit (
    id                TEXT PRIMARY KEY,   -- turn id; uuid4 hex
    received_at       TEXT NOT NULL,      -- ISO 8601 UTC
    completed_at      TEXT,
    transport         TEXT NOT NULL,      -- 'slack' in v1
    account_id        TEXT NOT NULL,      -- Slack team/workspace id
    delivery_id       TEXT NOT NULL,      -- stable Slack retry/delivery id
    route_id          TEXT NOT NULL,      -- 'slack.dm.owner' | 'slack.channel.C0ACME'
    channel_id        TEXT NOT NULL,
    thread_id         TEXT,
    source_message_id TEXT NOT NULL,      -- Slack event/message ts
    conversation_id   TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    user_name         TEXT,               -- snapshot at receipt; may be blank
    groups_json       TEXT NOT NULL,      -- JSON array membership snapshot
    authorized_groups_json TEXT,           -- JSON array of groups that satisfied allow
    workspace_id      TEXT,
    binding_revision  TEXT,
    policy_revision   TEXT,
    kind              TEXT,               -- 'provider' | 'command'; null until known
    decision          TEXT NOT NULL,      -- 'accepted' | 'ignored' | 'unconfigured' | 'denied'
    outcome           TEXT NOT NULL,      -- 'pending' | 'completed' | 'ignored' | 'blocked' | 'error' | 'timeout' | 'stopped'
    terminal_reason   TEXT,
    delivery_status   TEXT NOT NULL,      -- 'not_attempted' | 'pending' | 'delivered' | 'failed'
    provider          TEXT,
    model             TEXT,
    request_text      TEXT NOT NULL,
    response_text     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_source
    ON _enso_audit (account_id, delivery_id);
CREATE INDEX IF NOT EXISTS idx_audit_received
    ON _enso_audit (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_route
    ON _enso_audit (route_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_user
    ON _enso_audit (user_id, received_at DESC);
```

The request is the triggering human text after transport normalization and mention
removal, before fetched context, compact seeds, background messages, or provider wrappers.
The response is the final user-visible Slack text, including a configuration or provider
error. Attachment bytes, fetched context, status edits, tool calls, and reasoning are not
stored.

`decision` records routing and capability handling: `ignored` for an unknown or
disallowed sender on an audited exact route, `unconfigured` for an authorized but
unusable route, `denied` for a workspace capability refusal, and `accepted` once either a
provider turn or chat command is permitted. `kind` distinguishes those accepted paths;
provider and model remain null for commands. Ignored rows finish immediately with
`outcome = 'ignored'`; denied and unconfigured rows finish with `outcome = 'blocked'`;
every terminal row has `completed_at`. Accepted work starts as `pending` and records its
terminal result and delivery state; it may finish as `blocked` if mandatory
pre-execution revalidation fails. `terminal_reason` records a stable reason such as
`resolution_changed` or `access_revoked` without overloading the human response.

For an audited route, Enso inserts the inbound row before any command, context fetch,
attachment download, or provider spawn. It stores the final response before attempting
Slack delivery, then updates `delivery_status`. Storage failure blocks an authorized turn
when `audit.on_failure` is `"block"`, the default. An unauthorized sender remains silent
even when the audit write fails.

The delivery ledger is the idempotency authority. The audit unique index is additional
defence against duplicate text rows and links back through `_enso_slack_events.audit_turn_id`.

When a queued `accepted` turn fails revalidation, its audit row is completed with
`outcome = 'blocked'` (or `'ignored'` for revoked access) and the relevant
`terminal_reason`; the original intake decision is not rewritten. A still-authorized sender
receives a recorded configuration-changed error, while revoked access leaves
`response_text` null and `delivery_status = 'not_attempted'`. Completion is idempotent, so
a row already made terminal by revalidation is never overwritten by the runtime's final
bookkeeping. At startup, any turn left `pending` by a crash is closed as
`outcome = 'error'`, `terminal_reason = 'service_restart'`, preserving a delivery status
the crash had already recorded.

`_enso_audit` is Enso-owned and reserved by the existing `_enso_` prefix rule, so it never
appears in the registered-tables catalog. The operator must deny policy-controlled
workspaces access to `enso.db` through the provider's native policy or outer isolation;
Enso does not compile that requirement into provider rules.

`audit.max_age_days` defaults to `365`; `0` explicitly selects indefinite retention.
Pruning uses `completed_at` when present and `received_at` otherwise. Disabling a route's
audit prevents new rows but does not delete existing evidence. It also does not disable
Slack retention, provider session history, or uploads. Teams-mode operational logs contain
metadata only, never request or response text.

Enso refuses `enso message send`, job alerts, and other out-of-band sends to an audited
Slack route, since a message outside the one-row-per-turn contract would be a gap in “what
Enso said” — supporting them would require a separate outbound-event audit schema.

## Cross-cutting rules

- **Timestamps** are ISO 8601 **UTC** in Enso-owned stored data (run times). The tables
  skill applies the same convention to user schemas unless their domain requires
  something else. The UI localises run times for display; cron **schedules** stay in the
  system's local timezone, matching existing job behaviour (do not convert schedules to
  UTC).
- **IDs**: run ids and audit turn ids are uuid4 hex. Job identity is the dir name.
- **Atomic dashboard writes**: edits to `JOB.md`, Enso-owned `SKILL.md`, and `AGENTS.md`
  use a temp file plus `os.replace`, so a concurrent reader sees old-or-new, never a
  partial write. Doc edits follow the same rule.
- **Frontmatter compatibility**: `jobs.py` parses valid YAML mappings with PyYAML's
  `BaseLoader`, keeping scalar values as strings, then falls back to the legacy line
  parser for malformed older headers such as an unquoted `name: Daily: Review`.
  Dashboard body/toggle writes edit the raw fenced text atomically without reserializing
  it; CLI scaffolding emits valid YAML.
