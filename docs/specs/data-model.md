# Data model

The storage layer for jobs and reference material (files), runs and Slack delivery state (SQLite + log files), registered data tables (SQLite), and the config that governs them. See
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
├── enso.db              # SQLite: runs, Slack delivery/drafts/audit, and registered user tables
├── cache/
│   └── slack.json       # Slack name↔ID directory cache (`enso slack`)
├── secrets/             # *.env files loaded into the `enso serve` environment at
│   └── 1password.env    #   startup, so jobs inherit credentials a service manager
│                        #   would not otherwise pass through. Existing vars win.
├── docs/                # operator reference docs, nested to any depth.
│   ├── homelab.md       #   Markdown + frontmatter; identity is the relative path.
│   └── stuff/sub_stuff.md   #   See [docs.md](docs.md)
├── jobs/                # user jobs
│   └── <name>/          # JOB.md plus a persistent .run.lock coordination file
├── runs/                # captured output, one file per run
│   └── <run_id>.log
├── skills/              # Enso-owned skills (editable via UI)
│   └── .deleted/        # deletion markers preventing bundled skills from being reseeded
├── workspace/           # global working_dir for private Telegram interaction
├── workspaces/          # named content roots used by Slack routes and every job
│   ├── company/
│   └── clients/<name>/
└── policies/            # protected native policies, keyed by access profile
    └── <access>/{claude,codex}/
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

### Supplying credentials

Enso does not require any particular secret manager. There are three supported ways to
give it a credential, in increasing order of how well they hold up once more than one
person is involved:

1. **A literal value in `config.json`.** Simplest, and fine for a personal install. The
   file is written `0600`. The cost is that the secret is now in a file you may want to
   back up, sync, or version-control — see the note below.
1. **An environment projection via `secrets/*.env`.** Keeps credentials out of
   `config.json` and lets an existing secret-management workflow write the file.
1. **A direct reference to a secret manager.** Enso ships one implementation, for
   1Password, described below. Nothing else depends on it: with no reference key
   configured, Enso never invokes the helper, and every other feature works unchanged.

If the operator chooses to version-control `~/.enso`, a literal credential in `config.json` becomes a credential in git history. Enso does not initialize this repository. Keep sensitive state ignored, or use option 2 or 3 so tracked config contains a reference rather than a secret.

A supported config value can use a direct 1Password reference named `<key>_1password`:

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

Run history records scheduled and manual job executions only. Interactive Slack and Telegram turns are not `runs`; optional Slack route auditing uses `_enso_audit` instead. A parsed job that fails provider/model, workspace/access, or native-policy validation creates a terminal error run before any prerun or provider process starts. A job file that cannot be parsed or lacks required frontmatter is reported by `enso config check` and skipped, so it creates no run. Intentional prerun no-work also creates no run.

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
   preruns and invalid job bindings create the row when classified while preserving the
   earlier pipeline start time; intentional no-work creates no row. Returns the `id`;
   output uses `runs/<id>.log`.
1. **finish** (`runs.finish`) at exit: set `ended_at`, `duration_ms`, `exit_code`, and a
   terminal `status` — `ok` (exit 0), `error` (nonzero), `timeout` (killed by the job
   budget), `prerun_error`, or `prerun_timeout`. Intentional prerun no-work (`exit 1`)
   creates no row. Set `output_bytes` from the log size.
1. A row left in `running` after a process restart remains as a **crash marker**. There
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
    "timeout": 1800           // interactive turn timeout in seconds; 0 disables
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

## Execution catalog and Slack routes

The top-level `workspaces` and `access` blocks form a transport-independent execution catalog. Every job selects one entry from each block. Slack additionally requires `routes.slack`, where every exact DM user or channel selects the same pair. Telegram interaction remains private and uses its global `working_dir`; Telegram jobs still use the catalog.

See [teams.md](teams.md) for route behavior and [permissions.md](permissions.md) for provider launches.

### Filesystem layout

A practical installation may use:

```text
~/.enso/
├── workspace/                         # private Telegram working_dir
├── workspaces/
│   ├── company/                       # shared content root and provider cwd
│   └── clients/
│       ├── acme/                      # project content + native instructions/skills
│       └── globex/
└── policies/
    ├── staff/
    │   ├── claude/settings.json
    │   └── codex/{config.toml,rules/*.rules}
    └── client-readonly/
        ├── claude/settings.json
        └── codex/{config.toml,rules/*.rules}
```

Beside `claude/settings.json`, a profile's `claude/` directory may hold an optional conventional `claude/mcp.json` declaring that profile's exact Claude MCP server set. Its presence turns MCP on for the profile and is hashed into the launch's `policy_revision`; absence means zero MCP servers. Like every policy source file, it must be a protected owner-only regular file, and a present-but-unusable file fails the launch closed. See [permissions.md](permissions.md#granting-credentials-and-mcp-servers-to-a-restricted-profile).

A workspace is a shared content root and provider cwd, not a security boundary. It may contain project knowledge, `AGENTS.md`/`CLAUDE.md`, and provider-native `.agents/skills/` and `.claude/skills/` directories. The CLIs may additionally load native user, managed, plugin, system, or bundled skill scopes; project placement is not an allowlist. When a named workspace is missing its instruction file, Enso seeds a small `AGENTS.md` plus a `CLAUDE.md` symlink but does not add global skill links.

A policy directory belongs to an access profile and stays outside all writable workspaces and Telegram's global `working_dir`. This separation lets one profile serve several project directories. Paths are expanded and canonicalized before topology checks or child-process use. Workspaces may live at normalized operator-chosen paths, but configured workspace roots must not overlap each other; policy paths must not overlap any workspace. Aliases and hard links must not provide a writable path back to protected policy bytes.

Enso does not initialize `~/.enso` as a Git repository. Instruction discovery follows each provider's native behavior from the route's starting cwd. A company workspace that can access sibling client directories should explicitly tell the agent to read the selected client's protected instructions rather than relying on implicit discovery after changing directories.

An attachment-bearing Slack turn gets a unique `uploads/<random-id>/` directory within its resolved workspace. These files persist until the operator removes them; Enso does not treat uploads as temporary or apply automatic retention. Telegram instead stores downloads directly in `uploads/` under its global `working_dir`. Enso config, secrets, policies, database, jobs, and provider credentials are not linked into restricted workspaces.

### Configuration

The catalogs are parsed independently of Slack. `routes.slack` is additionally required when Slack is the active transport.

```jsonc
{
  "transports": {
    "slack": {
      "rich_messages": true,
      "persistent_surfaces": true
    }
  },

  "workspaces": {
    "company": {
      "path": "~/.enso/workspaces/company",
      "concurrency": 1
    },
    "acme": {
      "path": "~/.enso/workspaces/clients/acme",
      "concurrency": 1
    }
  },

  "access": {
    "admin": {
      "unrestricted": true,
      "providers": ["claude", "codex", "agy"],
      "default_provider": "claude",
      "chat_commands": "*"
    },
    "staff": {
      "policy_dir": "~/.enso/policies/staff",
      "providers": ["claude", "codex"],
      "default_provider": "claude",
      "chat_commands": "*"
    },
    "client-readonly": {
      "policy_dir": "~/.enso/policies/client-readonly",
      "providers": ["claude"],
      "default_provider": "claude",
      "chat_commands": ["status", "clear", "stop", "help"]
    },
    "automation": {
      "policy_dir": "~/.enso/policies/automation",
      "providers": ["claude", "codex"],
      "default_provider": "claude",
      "chat_commands": []
    }
  },

  "routes": {
    "slack": {
      "account_id": "T0ENSO",
      "dms": {
        "U01OWNER": {
          "workspace": "company",
          "access": "admin",
          "audit": false
        }
      },
      "channels": {
        "C0ACME": {
          "workspace": "acme",
          "access": "client-readonly",
          "audit": true
        },
        "C0ACMEINTERNAL": {
          "workspace": "acme",
          "access": "staff",
          "audit": false
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

- `workspaces.<name>.path` is required and resolves to an absolute directory. `concurrency` is a positive integer and defaults to `1`. Workspaces do not contain provider, command, skill, or permission settings.
- `access.<name>` requires a non-empty `providers` list and a `default_provider` from that list. `chat_commands` is either a unique list or the explicit string `"*"`; omission means none. It governs Enso chat commands only, not provider-native tools, slash commands, skills, plugins, hooks, or MCP servers.
- A policy-controlled profile may add `env_passthrough`, a list of environment-variable names (names, never values) copied from the service environment into the child environment. Names must match `[A-Z][A-Z0-9_]*`, be unique, and not name launch-controlled or `ENSO_`-prefixed variables; the key is invalid alongside `unrestricted: true`. See [permissions.md](permissions.md#granting-credentials-and-mcp-servers-to-a-restricted-profile).
- An access profile uses exactly one mode: explicit `unrestricted: true`, or native policy files under `policy_dir`. For a restricted profile the directory defaults to `~/.enso/policies/<access-name>`. Unrestricted mode does not imply providers or commands.
- `routes.slack.account_id` must match the Slack account returned by the configured credentials.
- `transports.slack.rich_messages` and `transports.slack.persistent_surfaces` default to `true`. An explicit JSON `false` disables that feature; a non-boolean value fails closed as disabled. Persistent surfaces are effective only while rich messages are enabled. These are transport-wide rendering controls, not route permissions, and changes require a restart.
- `routes.slack.dms` is keyed by exact Slack user ID. `routes.slack.channels` is keyed by exact channel ID. There are no named DM rules, groups, allowlists, defaults, or wildcards. An unrouted explicit contact receives only the fixed transport-level access response; it does not create an implicit route.
- Every route requires a known `workspace` and `access`. `audit` is optional and defaults to `false`.
- A missing workspace, access profile, provider, or native policy is an error. Nothing falls back to `working_dir`, another profile, or unrestricted execution.
- `config.json` is loaded at service startup. Slack loads and validates its route catalog then; jobs are loaded from disk on scheduler ticks and manual runs and revalidated before execution. `config.json` changes take effect only after restart, and invalid bindings never receive permissive defaults.

Several routes may select the same workspace with different access profiles. Their files and workspace concurrency are shared, but their sessions, provider choices, queues, and chat commands remain scoped to each Slack conversation.

### Job bindings

Every `~/.enso/jobs/<name>/JOB.md` requires `workspace` and `access` names in addition to `provider` and `model`:

```yaml
workspace: company
access: automation
```

The job's provider and model remain authoritative. The access profile must allow that provider. The provider process uses the named workspace as cwd, receives the profile's native policy, and participates in the workspace's process-local concurrency semaphore. Once a job is parsed, an unknown, incomplete, or unsafe binding creates an error run and notifies through the normal job failure path before prerun or provider execution. A missing required frontmatter field prevents the job from loading, is reported by `enso config check`, and creates no run or notification. There is no global or unrestricted fallback.

An optional prerun script is trusted host-side code, invoked through Bash with the job directory as cwd. It deliberately remains outside the provider native policy. Prerun output may be injected into the prompt, so the resulting data is still untrusted input to the provider.

The `.run.lock` file is a persistent lock target, not a temporary in-flight marker. Its advisory lock coordinates the scheduler, CLI, and dashboard across processes for the same job. Workspace semaphores are process-local and do not serialize separate Enso processes.

Scheduled successes are silent unless the prompt explicitly calls `enso message send`. Host-side failure and prerun-recovery alerts use the job's `notify` destination or the configured transport `notify_channel`, independently of Slack routes. `enso job run` suppresses those automatic alerts but cannot suppress a message explicitly sent by the provider process.

### Transport authorization and migration

Slack always requires `routes.slack`; `transports.slack.allowed_users` is invalid. Routes are never synthesized because creating one grants access. Each authorized DM user and channel must be migrated to an exact route selecting a known workspace and access profile.

Slack outbound delivery resolves an explicit destination, then an interactive origin, then `transports.slack.notify_channel`. It is not inferred from an inbound route and never broadcasts.

Telegram always uses exact numeric strings under `transports.telegram.allowed_users` and accepts private chats only. `allowed_user_ids` and the `"*"` wildcard are invalid. Telegram outbound delivery resolves an explicit destination, then an interactive origin, then `transports.telegram.notify_channel`; it never broadcasts to the allowlist.

Configurations from the earlier teams branch are rejected when they contain `groups`, route `allow`, route `context_from`, or access fields inside a workspace. Operators migrate them by creating explicit `access` profiles, adding `access` to every route and job, keying each DM by a Slack user ID, and removing groups and route allowlists.

### Slack delivery ledger

Slack routing keeps a small metadata-only ledger to suppress duplicate Slack retries independently of optional auditing:

```sql
CREATE TABLE IF NOT EXISTS _enso_slack_events (
    account_id      TEXT NOT NULL,
    delivery_id     TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    completed_at    TEXT,
    status          TEXT NOT NULL,
    audit_turn_id   TEXT,
    PRIMARY KEY (account_id, delivery_id)
);
```

The delivery ID is an opaque digest derived from the authenticated Slack account, channel, and canonical source-message timestamp. A duplicate is acknowledged without another provider run or response. This includes unrouted DMs and explicit channel mentions, whose fixed access response is therefore sent at most once. The ledger contains no message text. Pending claims left by a service crash are closed during startup rather than replayed automatically. Rows older than seven days are pruned at service startup.

### Slack persistent-surface drafts

App Home and Canvas requests use a private one-time draft store. A draft contains the exact validated publication, its original envelope, trusted route origin, destination lease, and—in the channel Canvas case—the server-resolved target snapshot. The model never supplies an account, recipient, channel, Canvas ID, route, or access profile.

```sql
CREATE TABLE IF NOT EXISTS _enso_surface_drafts (
    draft_id          TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    route_kind        TEXT NOT NULL,
    workspace_id      TEXT NOT NULL,
    access_profile    TEXT NOT NULL,
    route_audit       INTEGER NOT NULL,
    user_id           TEXT NOT NULL,
    channel_id        TEXT NOT NULL,
    thread_ts         TEXT,
    conversation_type TEXT NOT NULL,
    audit_turn_id     TEXT,
    target_key        TEXT NOT NULL,
    publication_hash  TEXT NOT NULL,
    publication_json  TEXT,
    target_hash       TEXT,
    target_json       TEXT,
    source_text       TEXT,
    status            TEXT NOT NULL,
    message_ts        TEXT,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    completed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_surface_drafts_expiry
ON _enso_surface_drafts (status, expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_surface_drafts_publishing_target
ON _enso_surface_drafts (target_key) WHERE status='publishing';
```

A new draft starts `pending`, is bound exactly once to the bot message containing its controls, and expires after 15 minutes. Publish atomically changes it to `publishing`; Cancel changes it to `cancelled`. Publish completes as `published`, `failed`, `partial`, or `unknown`. Maintenance also uses `expired`, `superseded`, and `revoked`. The unique partial index prevents concurrent Enso mutations of one App Home or channel Canvas target while allowing independent standalone Canvases.

Every terminal transition sets `publication_json`, `target_json`, and `source_text` to SQL `NULL`. This is logical application-level scrubbing, not a promise of forensic erasure from SQLite WAL pages or storage media; filesystem permissions around `~/.enso/enso.db` remain the at-rest boundary. Live maintenance runs every five minutes to expire pending rows and prune terminal metadata after seven days. It deliberately leaves `publishing` rows alone so a slow Slack call cannot lose its lease. Startup reconciliation changes a pre-crash `publishing` row to `unknown`, fails unsafe unbound or legacy channel drafts, expires overdue drafts, and never replays a Slack mutation. If Slack succeeds but the terminal database write transiently fails, Enso retries only that local write; it never repeats the external API call.

On a route with `audit: true`, Publish and Cancel create a separate `kind='surface_confirmation'` audit turn before claim or external mutation. `audit.on_failure='block'` leaves the draft pending if that required evidence cannot be created. The original and confirmation audit turns retain the exact rendered preview plus the actor, decision, outcome, and delivery result according to `audit.max_age_days`; scrubbing `_enso_surface_drafts` therefore does not erase audit evidence on an audited route.

### Optional audit log

A route with `audit: true` asks Enso to record its triggering message and terminal outcome. An unrouted DM or channel mention has no route and creates no audit row; its fixed response is represented only by the metadata-only delivery ledger. The audit store is operational evidence, not a complete transcript or security boundary. It excludes surrounding Slack context, attachments, status edits, reasoning, tool calls, native provider history, and unrelated outbound messages.

The existing turn table is retained for database compatibility. New routed rows associate the Slack delivery with its exact route, workspace, sender, provider, model, actual launch policy revision, request text, available final response, outcome, and delivery status. The two group columns remain in the table but are populated with empty values because the routing model no longer has groups. Access-profile identity is not duplicated in a new column; the exact route identifies the configured profile at the time, while retained historical configuration is an operator concern.

```sql
CREATE TABLE IF NOT EXISTS _enso_audit (
    id                TEXT PRIMARY KEY,
    received_at       TEXT NOT NULL,
    completed_at      TEXT,
    transport         TEXT NOT NULL,
    account_id        TEXT NOT NULL,
    delivery_id       TEXT NOT NULL,
    route_id          TEXT NOT NULL,
    channel_id        TEXT NOT NULL,
    thread_id         TEXT,
    source_message_id TEXT NOT NULL,
    conversation_id   TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    user_name         TEXT,
    groups_json       TEXT NOT NULL,
    authorized_groups_json TEXT,
    workspace_id      TEXT,
    binding_revision  TEXT,
    policy_revision   TEXT,
    kind              TEXT,
    decision          TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    terminal_reason   TEXT,
    delivery_status   TEXT NOT NULL,
    provider          TEXT,
    model             TEXT,
    request_text      TEXT NOT NULL,
    response_text     TEXT
);
```

`audit.on_failure` is `"block"` or `"warn"` and defaults to `"block"`. `audit.max_age_days` is a non-negative integer, defaults to `365`, and uses `0` for indefinite retention. Pruning runs at service startup, so a long-running process may temporarily exceed the age target until its next restart. Disabling audit stops new records but does not delete prior evidence or alter Slack and provider retention.

Audit does not change routing, jobs, or ordinary `enso message send` behavior. If complete outbound-accounting is required, it needs a separate outbound-event design rather than stretching a one-row inbound-turn record.

`_enso_audit`, `_enso_slack_events`, and `_enso_surface_drafts` are reserved Enso tables and never appear in the registered-tables catalog. Restricted policies must deny access to `~/.enso/enso.db`.

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
