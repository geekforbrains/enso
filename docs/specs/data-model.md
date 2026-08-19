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
├── .git/                # local-only content history; no Enso-created remote
├── .gitignore           # Enso-owned protective runtime/credential exclusions
├── config.json          # settings, including `web` and `runs` blocks
├── AGENTS.md            # canonical shared Enso instructions; native root for Claude/Codex
├── CLAUDE.md -> AGENTS.md
├── state.json           # durable route settings plus retained session/job state
├── messages.json        # background message queue (plus a .lock twin for cross-process writes)
├── update.json          # updater-owned metadata (installed revision, pending confirmation)
├── slack-app-manifest.yaml  # copied only by fresh or incomplete Slack setup
├── enso.log             # service log
├── enso.db              # SQLite: runs, Slack delivery/drafts/audit, and registered user tables
├── cache/
│   └── slack.json       # Slack name↔ID directory cache (`enso slack`)
├── secrets/             # *.env files loaded into the `enso serve` environment at
│   └── 1password.env    #   startup, so jobs inherit credentials a service manager
│                        #   would not otherwise pass through. Existing vars win.
├── docs/                # user-owned reference docs; paths have at most eight segments
│   ├── enso/content_model.md  # three files copied only by genuinely fresh setup
│   ├── enso/layout.md          # Markdown + frontmatter; identity is relative path
│   └── operator.md             # editable confirmed-operator template; see docs.md
├── jobs/                # user jobs
│   └── <name>/          # JOB.md plus a persistent .run.lock coordination file
├── runs/                # captured output, one file per run
│   └── <run_id>.log
├── runtime/             # protected provider-policy staging and other ephemeral state
├── skills/              # canonical global skills; user-owned after fresh setup
├── .agents/
│   └── skills -> ../skills
├── .claude/
│   └── skills -> ../skills
├── workspaces/          # flat, name-derived roots used by transports, routes, and jobs
│   ├── default/         # exact path: ~/.enso/workspaces/default
│   │   ├── AGENTS.md
│   │   ├── CLAUDE.md -> AGENTS.md
│   │   ├── skills/      # canonical workspace skills; initially empty
│   │   ├── .agents/skills -> ../skills
│   │   ├── .claude/skills -> ../skills
│   │   ├── knowledge/
│   │   │   └── README.md
│   │   ├── drafts/
│   │   └── uploads/
│   └── client-a/        # every other workspace has the same structure
└── policies/            # optional user-chosen home for protected native policy sources
    └── <policy>/{claude,codex,grok}/  # never created implicitly by Enso
```

Deleting an Enso-owned skill removes its complete directory. Installed bundle copies are
ordinary user-owned content: deletion creates no marker because startup, setup repair,
and upgrades never reseed them. Enso also does not remove guessed tool copies when a skill
is deleted. External skills remain read-only in the dashboard.

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

Enso initializes `~/.enso` as a local Git repository, but `config.json` is always
covered by its protective ignore rules, so ordinary scoped staging cannot capture a
literal credential from this file. A repository that already tracks protected content
is diagnosed by `enso config check` instead of assumed safe. The repository has no
Enso-created remote and is not a complete configuration backup.

### Content-history safety boundary

Content history is ordinary local Git, protected by the managed `.gitignore` block that
Enso writes before the repository ever exists. The protected set includes `config.json`
and its lock, `secrets/`, `enso.db` and its sidecars, `state.json`, the message queue,
audits, run output, caches, logs, uploads, drafts, updater state, job locks and
generated output, and native policy homes. Environment and authentication files remain
excluded even when nested below an otherwise versionable directory, and the same
directory names are re-allowed in structural identifier slots so a job, skill, or
workspace legitimately named `logs` still versions normally. Durable scripts must refer
to credential locations rather than contain secret values.

The intended versionable layer is the human-authored content: root and workspace
instructions and discovery links, canonical skills, global reference docs, workspace
`knowledge/`, and durable job definition or support files. Agents record it with
ordinary scoped commits:

```bash
git -C ~/.enso add <changed-path> [<changed-path>...]
git -C ~/.enso commit -m "<summary>"
```

The root prompt instructs agents to stage explicit paths only, never use broad staging
or `--force`-add ignored paths, and never add a remote, push, pull, fetch, or run
destructive history or worktree commands. Because Git stops applying ignore rules to a
path once it is tracked, `enso config check` reports any tracked file the protective
rules would exclude; repairing tracking is a deliberate operator action. Fresh setup
records the seeded tree in one baseline commit; Enso exposes no other history surface.

A supported config value can use a direct 1Password reference named `<key>_1password`:

```jsonc
{
  "transports": {
    "telegram": {
      "bot_token_1password": {
        "item": "Enso - Transport - Telegram",
        "field": "TELEGRAM_BOT_TOKEN"
      },
      "workspace": "default"
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

The fresh or incomplete setup wizard preserves this storage choice. Reconfiguring a
referenced Telegram bot token or Slack bot/app token in that wizard updates the existing
1Password field through `op_set_secret`; the replacement value reaches the helper shell
over stdin and is never placed in process argv. Enso keeps the reference object, removes
any stale literal for that key, and aborts without writing a plaintext fallback if the
helper update fails. Slack prevalidates both previous referenced values before changing
either field and best-effort restores an earlier field if a later update fails. Sections
with no reference retain the literal setup flow. Structural-only setup does not enter
transport configuration.

`docs/` is the one file-backed kind identified by a **relative path** rather than a
directory name. Enso packages `enso/content_model.md`, `enso/layout.md`, and `operator.md`
as fresh-setup-only starters; it does not create placeholder account, browser, network,
service, project, or business docs. Installed starters and later docs are equally
user-owned. Deletion creates no marker because startup, repair, completed setup reruns,
and upgrades never reseed docs; deleting a doc also prunes the empty parents it leaves
behind. `enso doc list` derives the current index from the files and frontmatter on every
call, so edited, created, and deleted docs need no separate catalog update.

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

Run history records scheduled and manual job executions only. Interactive Slack and Telegram turns are not `runs`; optional Slack route auditing uses `_enso_audit` instead. A parsed job that fails provider/model, workspace/policy, or native-policy validation creates a terminal error run before any prerun or provider process starts. A job file that cannot be parsed or lacks required frontmatter is reported by `enso config check` and skipped, so it creates no run. Intentional prerun no-work also creates no run.

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

### Read and mutation semantics

Reading `config.json` is strict and non-mutating. A malformed, non-UTF-8, or non-object
file is reported and its bytes are preserved; reads may apply defaults in memory but do
not persist them. `enso config check` follows the same read-only rule. A missing file
fails every operational command closed. Setup alone may request an in-memory fresh-setup
candidate, without creating the directory or file during that initial read.

The setup-only in-memory candidate contains `setup.completed_at: null`, and setup persists
that marker before seeding any starter content. `null` therefore means initial setup was
started but has not completed. Only an explicit setup run in that state may seed missing
fresh-install content. After the complete initial tree exists, setup creates one baseline
Git commit and only then replaces `null` with an ISO 8601 completion timestamp that
includes a timezone.

A seed or commit failure leaves the marker `null`; retrying explicit setup preserves
matching files already created and fills only missing pieces. If the baseline committed
but the timestamp write failed, the next retry recognizes the existing history and
records completion without making a second initial commit. An
absent `setup` block identifies a pre-feature installation, not an interrupted setup, and
does not make it eligible for automatic starter-content seeding. A completed timestamp,
ordinary startup, `enso web`, `enso config check`, structural repair, and upgrades are
also non-seeding. Invalid setup markers fail configuration loading.

Explicit `enso setup` on a completed or pre-feature configuration validates the existing
execution catalog before repository mutation and then performs structural-only repair. It
does not rewrite `config.json` or synthesize a `setup` marker; provider, workspace,
transport, messaging, and service configuration remain untouched.

Every command that performs a config read-modify-write transaction must hold Enso's
owner-only cross-process config lock from the read through the atomic replacement. A
failed transaction leaves the previous configuration intact. The lock prevents two
workspace or policy mutations from silently overwriting each other; it is runtime state
and never versionable.

`enso workspace create` applies that rule to one complete candidate, not a partial entry.
It strictly rereads the current object under the lock, rejects a malformed file, validates
the requested lowercase kebab-case name, derived root, explicit existing policy, positive
concurrency (default `1`), and complete `load_catalog()` result before persistence. Fresh
setup is the only code path that automatically creates unrestricted policy `admin` and
binds workspace `default`; later workspace creation never supplies an authority default.
There is no workspace path option.

The filesystem and config are separately atomic publications. Creation builds the full
workspace in a temporary sibling and exclusively renames it to its final root, then
atomically saves config and runs the installation check. If config saving fails, the
previous config remains and the published directory is reported as unused. If the later
check fails, the now-configured directory remains visible for operator
repair; Enso never guesses that user-visible content is safe to delete. Creation refuses
an existing destination, including a manually migrated or partially created root.
Changes are not hot-reloaded by `enso serve` or `enso web`, so a successful catalog
mutation requires restart.

`enso policy create` uses the same locked strict-read, candidate-validation, and atomic
save transaction. It accepts exactly one explicit authority source: `--unrestricted`, or
`--policy-dir <path>` naming an existing user-authored restricted source. Both forms
require one or more repeated `--provider` values and a `--default-provider` from that
set; repeated `--chat-command`, explicit `--all-chat-commands`, and restricted-only
repeated `--env-passthrough` map to the existing catalog fields. Passing neither chat
form persists no commands. The unrestricted form publishes only a catalog entry. The
restricted form validates the complete physical owner-protected directory and selected
provider files before publishing only its catalog entry. It never creates or changes the
canonical source. Fresh setup's unrestricted `admin` is the sole automatic policy
creation.

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

- `agent.timeout` applies equally to Claude, Codex, Grok, and Antigravity interactive
  turns. Timeout recovery context is durable and scoped to the originating conversation;
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

## Execution catalog and transport bindings

The top-level `workspaces` and `policies` blocks form a transport-independent execution catalog. Every workspace selects exactly one reusable policy. Telegram selects one workspace in `transports.telegram.workspace`; every Slack exact route and every job also selects one workspace. Transports, routes, and jobs derive the workspace's policy and cannot override it. `transports.slack` keeps Slack credentials, options, and exact DM/channel routes together. Channel routes may carry the optional `mention_required` / `thread_mention_required` response triggers, which shape when a routed channel dispatches but select nothing.

See [teams.md](teams.md) for route behavior, [slack-triggers.md](slack-triggers.md) for channel response triggers, and [permissions.md](permissions.md) for provider launches.

### Filesystem layout

A practical installation uses one flat, name-derived workspace tree:

```text
~/.enso/
├── AGENTS.md                          # canonical shared launch instructions
├── CLAUDE.md -> AGENTS.md
├── skills/                            # canonical global skills
├── .agents/skills -> ../skills
├── .claude/skills -> ../skills
├── workspaces/
│   ├── default/                       # fresh-install workspace -> admin policy
│   ├── company/                       # shared content root and provider cwd
│   ├── automation/
│   ├── acme/                          # exact root ~/.enso/workspaces/acme
│   └── acme-internal/
└── policies/                            # one optional user-chosen source location
    ├── staff/
    │   ├── claude/settings.json
    │   ├── codex/{config.toml,rules/*.rules}
    │   └── grok/config.toml
    └── client-readonly/
        ├── claude/settings.json
        ├── codex/{config.toml,rules/*.rules}
        └── grok/config.toml
```

Beside `claude/settings.json`, a policy's `claude/` directory may hold an optional conventional `claude/mcp.json` declaring that policy's exact Claude MCP server set. Its presence turns MCP on for the policy and is hashed into the launch's `policy_revision`; absence means zero MCP servers. Like every policy source file, it must be a protected owner-only regular file, and a present-but-unusable file fails the launch closed. See [permissions.md](permissions.md#granting-credentials-and-mcp-servers-to-a-restricted-policy).

A workspace is a shared content root and provider cwd, not a security boundary. Its
lowercase kebab-case name determines the root exactly as
`~/.enso/workspaces/<name>`; configuration stores no `path`. The workspace container and
each root must be physical directories, not symlinks, and a workspace root may not have a
direct `.git` entry of any kind. A repository deeper inside ordinary workspace content is
allowed. External roots, nested workspace names, alternate paths, and compatibility
symlinks are invalid.

Every created workspace has focused `AGENTS.md`, `CLAUDE.md -> AGENTS.md`, canonical
`skills/`, `.agents/skills -> ../skills`, `.claude/skills -> ../skills`, `knowledge/`,
`drafts/`, and `uploads/`. The local skill source starts empty. A global and local skill
directory with the same name makes the workspace invalid; Enso does not rely on a
provider-specific discovery order. The CLIs may additionally load native user, managed,
plugin, system, or bundled scopes, so discovery remains functionality rather than an
allowlist.

The supported lifecycle commands are `enso workspace list`, `show <name>`,
`create <name> --policy <policy> [--concurrency <n>]`, and `repair <name>`. `list` and
`show` are read-only. Creation publishes the scaffold and configuration; the new
versionable entries (`AGENTS.md`, `CLAUDE.md`, `.agents/skills`, `.claude/skills`, and
`knowledge/README.md`) are recorded in local history by a later scoped commit. Empty
directories cannot enter Git, and configuration, `drafts/`, and `uploads/` remain
ignored, so local history is content history rather than a complete configuration
backup.

Fresh setup seeds the global prompt, bundled skills, and the three starter docs once.
Atomic workspace creation seeds that workspace's prompt and `knowledge/README.md` once.
All seeded files become user-owned immediately: startup, the dashboard, configuration
checks, software upgrades, completed or pre-feature setup reruns, and setup repair never
upgrade, overwrite, resurrect, or retire them. Explicit setup repair owns structural
directories and known discovery links only; conflicts and missing user content are
preserved and reported. Existing installations adopt desired starter resources manually
after reviewing them; they must not fabricate a `null` setup marker to invoke fresh
seeding.

`enso workspace repair <name>` is the focused repair path for a configured workspace. It
creates missing structural directories and exact known links only. It never creates or
rewrites `AGENTS.md`, skill definitions, docs, or `knowledge/README.md`; missing seeded/user-owned
content remains missing and is reported when it prevents a valid launch.

A policy directory belongs to a policy and stays outside every writable workspace. This
separation lets one policy serve several project directories. Policy paths are expanded
and canonicalized before topology checks or child-process use, and must not overlap a
workspace. Aliases and hard links must not provide a writable path back to protected
policy bytes.

The supported policy lifecycle commands are `enso policy list`, `show <name>`, and
`create <name>`. List output summarizes capabilities, consumers, and validation; show
adds safe path, revision, warning, environment-name, and MCP-server metadata without
secret values or native file contents.
Creation always requires exactly one of `--unrestricted` and `--policy-dir <path>`, plus
explicit providers and a default provider. A restricted path has no default: before
registration, the user or agent creates an existing complete provider-native directory,
makes its directories and regular files physical and owner-safe, and tests them against
the installed provider CLIs. Enso registers and later validates the canonical content
but never generates, copies, changes permissions, rewrites, upgrades, or repairs it.
Source-tree examples are explanatory starting points, not trusted or certified presets,
and copies are user-owned. `enso config check` remains the complete validator; there is
no second policy-only validator or mutation surface for repair, deletion, or rebinding.

Enso initializes a new `~/.enso` local Git worktree on `main`, after installing its
protective ignore block. It accepts an existing repository only when Git reports that
exact directory as the worktree root; corrupt, outer, and ambiguous repository states
fail setup. Enso never creates, changes, or contacts a remote, and it writes repository-
local fallback author details only when Git has no effective identity. Shared instructions
are canonical at `~/.enso/AGENTS.md`, with `CLAUDE.md -> AGENTS.md`. The canonical source
must be a stable, owner-owned regular non-symlink file with no additional hard links or
group/other write bits, valid UTF-8 no larger than 20 KiB, and no NUL bytes.

Immediately before every provider spawn, Enso revalidates that source plus the exact
name-derived physical workspace, the root and workspace discovery links, global/local
skill-name uniqueness, the absence of a direct workspace `.git` entry, and `~/.enso` as
the exact Git worktree root. Claude and Codex then discover the live shared and workspace
files natively from that root; Enso does not pass a duplicate
`--append-system-prompt-file` or `developer_instructions` override. Grok receives the
just-validated shared text once through `--rules`, and unrestricted Agy receives it once
in Enso's prompt envelope. The validation hash is diagnostic only: native providers open
the live files after spawn rather than consuming an Enso-pinned snapshot. Missing or
unsafe instructions or partial discovery fail `enso config check`, job preflight, and the
actual launch closed. A company workspace that can access sibling client directories
should explicitly tell the agent to read the selected client's protected instructions
rather than relying on implicit discovery after changing directories.

An attachment-bearing Telegram or Slack turn gets a unique `uploads/<random-id>/` directory within its resolved workspace. These files persist until the operator removes them; Enso does not treat uploads as temporary or apply automatic retention. Enso config, secrets, policies, database, jobs, and provider credentials are not linked into restricted workspaces.

The Enso service has no configured process working directory. Only provider subprocesses and their session operations receive a cwd, always from the resolved workspace.

### Configuration

The catalogs are parsed independently of either transport. Telegram requires `workspace` inside `transports.telegram`; Slack requires `account_id` inside `transports.slack`, where any exact routes are declared in the `dms` and `channels` maps.

The following is the resulting persisted shape, not the primary policy/workspace
authoring workflow. Use `enso policy create` and `enso workspace create` so each complete
candidate is validated under the config lock; edit exact transport routes and bindings
directly only where no focused command exists. Fresh setup supplied the illustrated
unrestricted `admin` entry automatically. Every illustrated restricted `policy_dir` was
created and populated by its user before registration.

```jsonc
{
  "transports": {
    "telegram": {
      "bot_token": "...",
      "allowed_users": ["123456789"],
      "notify_channel": "123456789",
      "workspace": "default"
    },
    "slack": {
      "bot_token": "xoxb-...",
      "app_token": "xapp-...",
      "rich_messages": true,
      "persistent_surfaces": true,
      "account_id": "T0ENSO",
      "channel_defaults": {
        "mention_required": false,
        "thread_mention_required": false
      },
      "dms": {
        "U01OWNER": {
          "workspace": "company",
          "audit": false
        }
      },
      "channels": {
        "C0ACME": {
          "workspace": "acme",
          "audit": true,
          "mention_required": true,
          "thread_mention_required": true
        },
        "C0ACMEINTERNAL": {
          "workspace": "acme-internal",
          "audit": false
        }
      }
    }
  },

  "workspaces": {
    "default": {
      "policy": "admin",
      "concurrency": 1
    },
    "company": {
      "policy": "admin",
      "concurrency": 1
    },
    "acme": {
      "policy": "client-readonly",
      "concurrency": 1
    },
    "acme-internal": {
      "policy": "staff",
      "concurrency": 1
    },
    "automation": {
      "policy": "automation",
      "concurrency": 1
    }
  },

  "policies": {
    "admin": {
      "unrestricted": true,
      "providers": ["claude", "codex", "grok", "agy"],
      "default_provider": "claude",
      "chat_commands": "*"
    },
    "staff": {
      "policy_dir": "~/.enso/policies/staff",
      "providers": ["claude", "codex", "grok"],
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

  "audit": {
    "on_failure": "block",
    "max_age_days": 365
  }
}
```

Schema rules:

- Workspace names are at most 64 characters of lowercase letters and numbers separated by
  single hyphens. Each name derives exactly `~/.enso/workspaces/<name>`; a `path` field is
  invalid. `workspaces.<name>.policy` is required and names exactly one configured policy.
  `concurrency` is a positive integer and defaults to `1`. Workspaces do not contain
  provider, command, skill, or permission settings; those belong to the selected policy.
- Policy names remain portable identifiers matching
  `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; they cannot contain path separators or traversal
  segments.
- `policies.<name>` requires a non-empty `providers` list and a `default_provider` from that list. `chat_commands` is either a unique list or the explicit string `"*"`; omission means none. It governs Enso chat commands only, not provider-native tools, slash commands, skills, plugins, hooks, or MCP servers.
- A restricted policy may add `env_passthrough`, a list of environment-variable names (names, never values) copied from the service environment into the child environment. Names must match `[A-Z][A-Z0-9_]*`, be unique, and not name launch-controlled (such as `CODEX_HOME`, `GROK_HOME`, `GROK_SANDBOX`, and `GROK_FOLDER_TRUST`) or `ENSO_`-prefixed variables; the key is invalid alongside `unrestricted: true`. See [permissions.md](permissions.md#granting-credentials-and-mcp-servers-to-a-restricted-policy).
- A policy uses exactly one mode: explicit `unrestricted: true`, or native policy files under an explicit `policy_dir`. A restricted policy must supply `policy_dir`; it must be absolute or start with `~/`, and no name-derived fallback is inferred. Unrestricted mode does not imply providers or commands. Codex `config.toml` may not define top-level `developer_instructions`: provider-specific hidden instructions would add a competing layer outside the canonical root/workspace `AGENTS.md` contract. Grok's `--rules` flag remains reserved for the one explicit shared-instruction delivery Enso supplies to every Grok launch.
- `transports.telegram.workspace` is required whenever Telegram is configured and must name a usable workspace. Telegram derives providers, default provider, Enso chat commands, native policy, cwd, uploads, and concurrency from that workspace. `allowed_users` remains a non-empty list of unique exact numeric strings, and only private chats dispatch.
- `transports.slack` is the single Slack configuration object: credentials, transport-wide rendering and notification options, `account_id`, and exact route maps coexist there. The legacy top-level `routes` key is rejected.
- `transports.slack.account_id` must match the Slack account returned by the configured credentials.
- `transports.slack.rich_messages` and `transports.slack.persistent_surfaces` default to `true`. An explicit JSON `false` disables that feature; a non-boolean value fails closed as disabled. Persistent surfaces are effective only while rich messages are enabled. These are transport-wide rendering controls, not route permissions, and changes require a restart.
- `transports.slack.dms` is keyed by exact Slack user ID. `transports.slack.channels` is keyed by exact channel ID. There are no named DM rules, groups, allowlists, default routes, or wildcards: `transports.slack.channel_defaults` supplies settings defaults for routed channels, never synthesizes a route, and unrouted channels stay unrouted. An unrouted explicit contact receives only the fixed transport-level access response; it does not create an implicit route.
- Channel routes and `transports.slack.channel_defaults` accept the optional booleans `mention_required` and `thread_mention_required` (see [slack-triggers.md](slack-triggers.md)). Effective values resolve route key, then `channel_defaults`, then the built-in `true`, which reproduces the original mention-gated behavior. `channel_defaults` must be an object with no unknown keys, both settings must be booleans wherever they appear, and neither key is valid on a DM route.
- Every route requires a known `workspace`; it derives that workspace's policy and cannot override it. `audit` is optional and defaults to `false`.
- A missing workspace, policy, provider, or native policy is an error. Nothing falls back to an implicit cwd, another workspace or policy, or unrestricted execution.
- `config.json` is loaded at service startup. Startup and `enso config check` validate the
  repository, physical workspace topology, exact discovery links, and root/workspace
  skill-name uniqueness without seeding or repair. Slack loads and validates its route
  catalog then; jobs are loaded from disk on scheduler ticks and manual runs and
  revalidated before execution. `config.json` changes take effect only after restart,
  and invalid bindings never receive permissive defaults.

Several routes may select the same workspace and therefore share its policy, files, and
workspace concurrency. Provider/model/effort choices remain independent per exact Slack
route, while provider sessions and the process-memory incoming-message queue remain
independent per Slack root/thread conversation. To give two channels different policies
or separate files, configure separate workspaces; several workspaces may reuse the same
policy.

### Runtime settings and session state

`state.json` schema v3 separates durable route preferences from retained conversation
state. `route_preferences` is keyed by an opaque settings key and stores an optional
provider, one model per provider, and one effort per provider/model. Slack derives the key
from the authenticated account and exact DM/channel route, so every root and thread shares
the setting but a route on another account does not; Telegram derives it from the private
chat ID alone. Explicit `default` choices remove entries instead of pinning the value that
happens to be the default today.

The effective provider resolves from a policy-allowed route choice, then
`policy.default_provider`; the model resolves from a configured route choice, then the
first configured model for that provider; effort resolves from a route choice, then the
provider CLI default. Merely resolving these values never creates a preference. A route
provider that the current policy does not allow remains stored but inactive, so the policy
default applies without a migration or cleanup and the choice can become active again if a
later policy permits it. A provider that is still policy-allowed remains effective even
when its native launch is unusable: provider work reports the configuration error instead
of silently falling back, while non-launch commands remain available for inspection and
repair.

Conversation state keeps a separate opaque key. Slack binds it to account, channel,
thread/root, workspace, and policy; Telegram binds it to private chat, workspace, and
policy. Provider sessions, compact seeds, and last-active participation state persist
under that key. The incoming-message queue, running process/task, and locks use it only in
memory: queues are never written to `state.json` and disappear on restart. Only Telegram
exposes queue inspection through `/queue`; Slack's `!stop` can clear the current
conversation's queue.

On load, `ENSO_SESSION_TTL_DAYS` removes stale provider sessions, compact seeds, and
conversation activity but never route preferences. Loading a v1 or v2 file drops the old
conversation-scoped provider/model/effort selections because no reliable route can be
reconstructed from them, preserves sessions, compact seeds, last-active timestamps,
job-last-run data, and job-failure alerts, and rewrites the file as v3. Unknown or invalid
v3 preference values are omitted during the same validated rewrite.

### Job bindings

Every `~/.enso/jobs/<name>/JOB.md` requires a `workspace` name in addition to `provider` and `model`:

```yaml
workspace: automation
```

The job's provider and model remain authoritative. The workspace's policy must allow that provider. The provider process uses the named workspace as cwd, receives the policy's native files, and participates in the workspace's process-local concurrency semaphore. Once a job is parsed, an unknown, incomplete, or unsafe binding creates an error run and notifies through the normal job failure path before prerun or provider execution. A missing required frontmatter field prevents the job from loading, is reported by `enso config check`, and creates no run or notification. There is no implicit cwd, alternate workspace, or unrestricted fallback.

An optional prerun script is trusted host-side code, invoked through Bash with the job directory as cwd. It deliberately remains outside the provider native policy. Prerun output may be injected into the prompt, so the resulting data is still untrusted input to the provider.

The `.run.lock` file is a persistent lock target, not a temporary in-flight marker. Its advisory lock coordinates the scheduler, CLI, and dashboard across processes for the same job. Workspace semaphores are process-local and do not serialize separate Enso processes.

Scheduled successes are silent unless the prompt explicitly calls `enso message send`. Host-side failure and prerun-recovery alerts use the job's `notify` destination or the configured transport `notify_channel`, independently of Slack routes. `enso job run` suppresses those automatic alerts but cannot suppress a message explicitly sent by the provider process.

### Transport authorization

Slack always requires `transports.slack.account_id`; `transports.slack.allowed_users` is invalid. Routes are never synthesized because creating one grants access. Each authorized DM user and channel has an exact entry in `transports.slack.dms` or `transports.slack.channels` selecting a known workspace; the workspace selects its policy.

Slack outbound delivery resolves an explicit destination, then an interactive origin, then `transports.slack.notify_channel`. It is not inferred from an inbound route and never broadcasts.

Telegram always uses exact numeric strings under `transports.telegram.allowed_users`, accepts private chats only, and requires a known `transports.telegram.workspace`. It derives that workspace's policy and cannot override provider or command controls. `allowed_user_ids` and the `"*"` wildcard are invalid. Telegram outbound delivery resolves an explicit destination, then an interactive origin, then `transports.telegram.notify_channel`; it never broadcasts to the allowlist.

Removed configuration shapes are rejected rather than translated or used as runtime
fallbacks. Versioned migration guides own the historical schema and operator transition
procedures; this specification describes only the current data model.

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

The delivery ID is an opaque digest derived from the authenticated Slack account, channel, and canonical source-message timestamp. A duplicate is acknowledged without another provider run or response. This includes unrouted DMs and explicit channel mentions, whose fixed access response is therefore sent at most once; a channel mention still arrives as `message` and `app_mention` twins, which the delivery claim keeps to one dispatch. Non-mention channel messages that the route's effective response triggers ignore are dropped before any ledger claim, so a busy fully-ignored channel writes no rows. The ledger contains no message text. Pending claims left by a service crash are closed during startup rather than replayed automatically. Rows older than seven days are pruned at service startup.

### Slack persistent-surface drafts

App Home and Canvas requests use a private one-time draft store. A draft contains the exact validated publication, its original envelope, trusted route origin, destination lease, and—in the channel Canvas case—the server-resolved target snapshot. The model never supplies an account, recipient, channel, Canvas ID, route, or policy.

The physical `access_profile` column retains its historical name for database compatibility; new drafts store the workspace's derived policy name there.

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

The existing turn table is retained for database compatibility. New routed rows associate the Slack delivery with its exact route, workspace, sender, provider, model, actual launch policy revision, request text, available final response, outcome, and delivery status. The two group columns remain in the table but are populated with empty values because the routing model no longer has groups. Policy identity is not duplicated in a new column; the retained binding and policy revisions identify what was launched, while historical configuration remains an operator concern.

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
- **Atomic dashboard writes**: edits to `JOB.md`, Enso-owned `SKILL.md`, and canonical shared `~/.enso/AGENTS.md`
  use a temp file plus `os.replace`, so a concurrent reader sees old-or-new, never a
  partial write. Doc edits follow the same rule.
- **Frontmatter compatibility**: `jobs.py` parses valid YAML mappings with PyYAML's
  `BaseLoader`, keeping scalar values as strings, then falls back to the legacy line
  parser for malformed older headers such as an unquoted `name: Daily: Review`.
  Dashboard body/toggle writes edit the raw fenced text atomically without reserializing
  it; CLI scaffolding emits valid YAML.
