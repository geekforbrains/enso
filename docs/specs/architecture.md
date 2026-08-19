# Architecture

How the web UI, run recording, and registered data tables fit into Enso.
Read [PRD.md](../PRD.md) first for the what & why. Sibling specs:
[data-model.md](data-model.md), [tables.md](tables.md), [web.md](web.md).

## Runtime and process layout

`enso serve` builds a `Runtime` (`core.py`) and starts a Telegram or Slack transport.
Each `Runtime` owns a `JobRunner` (`job_runner.py`) at `runtime.jobs`, which holds the
scheduler, the job execution pipeline, and failure alerting. The transport starts
`Runtime.jobs.run_scheduler` alongside its chat loop; the scheduler loads `JOB.md` files
every 60 seconds and fires due jobs through `_execute_job`.

`enso web` builds its own `Runtime` and runs Starlette/Uvicorn as a separate process.
The dashboard and bot therefore do not share memory or an event loop. They coordinate
through the same files under `~/.enso/`, the canonical workspace tree, and the
shared SQLite database. Starting `enso serve` does not start the dashboard.

```
       enso serve process                 enso web process
  ┌────────────────────────┐        ┌──────────────────────┐
  │ transport + scheduler  │        │ Starlette / Uvicorn  │
  │ bot Runtime            │        │ dashboard Runtime    │
  └───────────┬────────────┘        └──────────┬───────────┘
              └──────────────┬─────────────────┘
                             ▼
                 files under ~/.enso/ and
                 canonical workspaces
                             +
                 SQLite (enso.db, WAL mode)
```

The dashboard's **Run now** action calls the same `Runtime.jobs.run_now` execution path
as the CLI, but on the dashboard's Runtime instance. Its in-memory scheduler state is
not shared with the bot process. File writes are atomic and SQLite uses WAL mode to
handle this cross-process boundary.

Configuration pages deliberately render `runtime.config`, the snapshot with which the
dashboard process started. A pure view-model layer joins that catalog to parsed jobs,
Telegram, exact Slack routes, and the existing Slack directory cache without resolving
transport credentials or making network requests. Native policy checks run only on policy
detail and return normalized status, paths, digests, warnings, and MCP names; raw policy
contents never enter template context. List and overview pages therefore say configured for
structurally valid bindings and reserve ready for successful policy-detail checks. Config
edits are not a web capability and neither the web process nor the bot hot-reloads them.

Instruction-file reads and writes use a separate hardened filesystem boundary. It opens
absolute roots and descendants through pinned directory descriptors with no symlink
following, enforces current-user ownership, regular-file/single-link/protected-mode rules,
caps discovery and UTF-8 content, and carries a content revision from read to atomic save.
Only the shared file and valid canonical workspace-root instruction files are writable;
nested workspace instruction files are inspection-only. Alternate, external, nested, and
symlinked workspace roots are invalid; their instruction content is never inspected or
rendered.

## Stack

Enso's runtime deps are deliberately tiny (`typer`, `rich`, `croniter`), with transport
libraries behind extras. The web UI follows the same rule — a `web` extra, nothing
pulled into the base install.

| Concern         | Choice                                            | Why                                                                                                                |
| --------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| ASGI framework  | **Starlette**                                     | Minimal, async-native, and well suited to server-rendered pages                                                    |
| Server          | **Uvicorn**                                       | Serves the standalone dashboard process                                                                            |
| Templates       | **Jinja2**                                        | Server-rendered HTML; the UI is views + forms, not an SPA                                                          |
| Navigation      | **Native browser links and forms**                | Full-page requests and redirects without a client application bundle                                               |
| Forms           | **Starlette `request.form()` + python-multipart** | Parses CSRF-protected URL-encoded writes                                                                           |
| SQLite store    | **`sqlite3`** (stdlib)                            | Operation-scoped connections and explicit transactions without another dependency; WAL mode for concurrent readers |
| Job frontmatter | **PyYAML `BaseLoader` + legacy fallback**         | Valid YAML scalars stay strings; malformed older headers remain loadable; raw web edits avoid reserialization      |

`pyproject.toml` defines:

```toml
[project.optional-dependencies]
web = ["starlette>=0.37", "uvicorn>=0.30", "jinja2>=3.1", "python-multipart>=0.0.18"]
```

`python-multipart` is required by Starlette's form parser even though the current UI does not upload files. `pyyaml` is a base dependency (jobs need it independently of the web UI). The compiled
Tailwind stylesheet is vendored, so the UI has **no external CDN dependency** and works
offline. Package data includes `web/templates/**` and `web/static/**`.

## Run recording

Run history is captured by the `JobRunner` around the shared job-execution pipeline used
for background work:

- `_execute_job` — scheduled jobs.
- `run_now` — CLI and dashboard manual runs (recorded with trigger `manual`).

Interactive **chat** requests are *not* runs — they are session-based and ephemeral, and
belong to the transport, not the run log.

A parsed job that fails execution configuration/binding validation or has a failed prerun records a terminal run even though no provider starts. A job file that cannot be loaded, intentional prerun no-work, and triggers skipped because a schedule is invalid, too late, disabled, or already locked do not create runs; `enso config check` reports load errors.

## Registered data tables

Agents and the operator use ordinary SQLite tools to define and populate user tables in
`~/.enso/enso.db`. Enso adds only the product-specific layer around them:

- `tables.py` owns validation, the `_enso_tables` discovery catalog, schema inspection,
  and bounded row previews.
- `enso table list`, `schema`, and `register` expose catalog operations without wrapping
  general SQL.
- The bundled `tables` skill teaches agents to inspect before writing, use transactions,
  preserve explicit units/timestamps, and confirm destructive changes.
- The web process lists registered tables through short-lived connections with a bounded
  busy timeout. Its routes cannot create a table, mutate a row, or accept arbitrary SQL.

The catalog is the trust and visibility boundary. Merely creating a SQLite table does not
publish it in Enso, and reserved `runs`, `_enso_*`, and `sqlite_*` names cannot cross that
boundary. Detail queries resolve a validated catalog entry, quote the identifier, and cap
rows, columns, and rendered cell sizes. See [tables.md](tables.md) for the full contract.

## Provider execution

The provider registry is the single source of truth for supported CLI names, default
models, setup detection, service environment keys, chat selectors, and job validation.
Existing configs are backfilled when a provider is added, while configured provider paths
and custom models are preserved.

Claude and Codex expose structured event streams. Antigravity's headless mode emits one
plain-text response, so the shared runner supports both streamed events and completed
stdout without changing the transport contract. Every provider reports activity as
`status` events, whatever the shape of its stdout: Claude derives them from `tool_use`
blocks (preferring the model-written `description` when a tool supplies one), Codex from
`item.started`, and Antigravity from `poll_progress` — an optional hook the runner drains
concurrently with the process, for providers whose stdout carries no progress at all.
Progress is decorative by contract: whatever `poll_progress` raises is swallowed and the
status message falls back to the elapsed timer, so a provider may read best-effort
sources for it.

The transient status message carries a fixed header (provider, model, effort) plus the
latest reported action. Its elapsed counter is rewritten every second through 30 seconds
so a new request visibly remains alive, then every five seconds to conserve transport
rate limits. Each scheduled edit includes the latest action available at that tick. A
failed edit is retried; status is abandoned only after `STATUS_MAX_EDIT_FAILURES`
consecutive failures.

Each interactive provider turn has the shared `agent.timeout` budget from `config.json`
(1,800 seconds by default; `0` disables it). Queue wait does not count. When the budget is
reached, Enso cancels the provider, terminates its process tree, changes the progress
message to a timeout notice, and stores conversation-scoped background context for the
next turn. That context warns the next active provider that partial filesystem or
session work may remain. Scheduled jobs retain their separate per-job timeout.

Claude accepts an Enso-generated session ID and Codex emits its ID in the event stream.
Antigravity generates its own ID but exposes it only in diagnostics: each invocation
uses a private temporary `--log-file`, Enso captures the authoritative active
conversation ID after the process exits, and the file is immediately removed. `/clear`
forgets the stored provider session, so the next message starts and captures a new one.

Antigravity's progress comes from the same conversation: its trajectory is a SQLite file
per conversation whose `steps` table gains rows as the agent works, each tool step
embedding a model-written `toolAction` label. The poller resolves the conversation ID
from the session on a resume, or by watching the `--log-file` for it on a fresh
conversation, then reads the store read-only. Both the catalog and the trajectory are
undocumented Antigravity internals; if either format moves, progress degrades to the
elapsed timer and nothing else breaks.

Antigravity additionally pins every conversation to a *project* at creation, and its
print mode never derives one from the working directory — without an explicit flag,
conversations land in the default scratch project rather than the workspace. When Enso
starts a fresh conversation (first chat turn, post-`/clear`, or any job run) it resolves
the project for the active workspace from Antigravity's catalog (`~/.gemini/config/projects/`,
matching plain and git-folder file URIs) and passes `--project`, falling back to
`--new-project` the first time a directory is used; the created project is then found by
lookup on subsequent runs. Resume keeps the conversation's existing pin, so no project
flags are sent, and `/clear` only forgets the conversation — projects are durable
per-workspace state, like Claude's project directories, and are never deleted.

The recording seam (see [data-model.md](data-model.md) for the schema):

1. Before provider spawn: `runs.create(kind, name, trigger)` → a row with
   `status='running'`, the pipeline start time, and an allocated `run_id`. A parsed job's
   failed execution configuration/binding check or prerun creates the same row when the
   failure is classified, backdated to pipeline start; an unloadable job file and
   intentional no-work create no row.
1. During: provider output is captured for the run log. Failed preruns store only their
   bounded safe diagnostic; raw prerun stdout/stderr is never copied into history or a
   transport notification.
1. After: `runs.finish(run_id, exit_code, status)` sets `ended_at`, `exit_code`, and a
   terminal `status` (`ok` / `error` / `timeout`; job gates may instead finish as
   `prerun_error` / `prerun_timeout`). Intentional no-work (`exit 1`) creates no row.

`runs.py` owns these operations and `sqlite_store.py` owns their shared connection policy.
Connections are opened lazily per operation and the DB is created on first write
(`CREATE TABLE IF NOT EXISTS`), so existing installs need no migration step. The async
runtime sends each complete telemetry operation through a worker thread; no SQLite
connection is cached, shared across threads, or allowed to block the event loop.

## Slack output resolution

Slack's output contract is described in [slack-output.md](slack-output.md). On an authorized interactive turn, the Slack context advertises transport-neutral structured-message instructions when rich output is enabled and persistent-surface draft instructions when both rich settings and a trusted route origin are available. Context, attachments, and history remain untrusted input; they cannot provide destination IDs or authorization.

After the provider's final response has been assembled, `Runtime` resolves it in this order:

1. Parse an exact whole-response persistent-surface draft and ask the Slack context to stage it.
1. Otherwise parse an exact whole-response structured message and ask the context to render its typed blocks.
1. Otherwise send the ordinary answer through Slack's standard-Markdown path.

Only successful final provider output follows this order. Errors, commands, status updates, direct notifications, and scheduled-job messages remain on their existing text paths. The transport-neutral context methods fall back to complete text for every other transport. Slack rendering keeps a top-level accessibility/notification fallback and retries only known block-validation rejection as text; it does not retry an ambiguous post.

Staging a surface stores a validated exact payload and posts an inert preview with requester-bound controls. It does not call a persistent Slack API. The Bolt block-action listener acknowledges the click first, then validates the Slack payload and draft scope, reauthorizes the exact route, creates required audit evidence, atomically claims the destination lease, and revalidates any channel Canvas target/revision. Only that handler invokes Canvas or App Home APIs, without another provider turn. Interrupted or ambiguous mutations are terminal and never replayed automatically.

## Request resolution

Workspace and Slack routing are described in [teams.md](teams.md), native CLI invocation in [permissions.md](permissions.md), and storage in [data-model.md](data-model.md). Slack always uses exact routes. Telegram retains its exact numeric user-ID allowlist, rejects non-private chat types, and requires one configured workspace. Both transports derive the workspace's single policy.

Every conversation and job carries an immutable `ExecutionContext`; there is no singleton or service-level cwd. An interactive context contains the resolved workspace and policy, a stable conversation key, a separate durable-settings key, the frozen effective provider/model/effort and their provenance, workspace concurrency, and transport-specific audit/message choices. Jobs carry their explicit provider and model without a settings key. Native policy preparation happens after the workspace slot is acquired, while filesystem discovery and the current shared instruction source are revalidated for each actual spawn rather than cached on the context. The same context is threaded through dispatch, uploads, commands, compaction, session clearing, and provider execution.

The top-level workspace/policy catalog is parsed independently of either transport so Telegram, Slack, and jobs use the same bindings. Telegram requires `transports.telegram.workspace` and applies that workspace's provider and command controls after exact-user authorization. Slack credentials, transport options, and exact routes share the `transports.slack` block; its routes are loaded and validated when `enso serve` starts. Bindings are not hot-reloaded; changing authorization requires a restart. For each Slack event the transport performs this fixed sequence before fetching surrounding context or downloading attachments:

1. Verify the event belongs to the configured Slack account.
1. Accept an ordinary `im` message. Whether a channel message dispatches depends on the resolved route's effective `mention_required`/`thread_mention_required` response triggers; the full decision order lives in [slack-triggers.md](slack-triggers.md). A mention is detected from the message text, not from which Slack event type delivered it, and non-mention drops happen before the delivery-ledger claim.
1. Resolve an `im` conversation by exact sender ID, or any other conversation by exact channel ID. A thread inherits its parent channel route.
1. Claim the Slack delivery ID for retry deduplication.
1. If no route exists, explicit contact (a mention, or any DM) gets the fixed DM response directly or the fixed channel response in a thread, then processing stops without resolving execution state; a non-mention message in an unrouted channel is dropped silently.
1. Resolve a configured route's workspace and that workspace's policy.
1. Resolve the route's durable settings through the current policy and start optional audit recording for the route.
1. Process the command or provider request using the resolved execution context; commands that launch a provider validate it first, and every native launch is prepared again after acquiring the workspace slot.

There are no groups, sender rankings, wildcard routes, Slack allowlists, or composed policies. `transports.slack.channel_defaults` supplies response-trigger settings to channels that are already routed — it never authorizes a location, so an unrouted channel stays unrouted and the no-wildcard invariant stands. A configured channel authorizes every human member who can post there; an administrator posting in a client channel gets the routed workspace's policy. An invalid configured route never falls back to another workspace, policy, implicit cwd, or unrestricted launch. A configured route that cannot launch reports a configuration error. Removed route schemas are rejected rather than interpreted; versioned migration guides own transition procedures.

The two no-route replies are fixed transport strings. They do not invoke an LLM, select a workspace or policy, construct an `ExecutionContext`, fetch message context or attachments, or start an audit turn. A globally invalid Slack configuration or wrong-account event remains silent and is logged.

### Settings and conversation keys

Cwd alone does not define either preferences or sessions. Interactive state has two independent namespaces: durable route settings for provider/model/effort, and retained conversation state for provider sessions, compaction, locks, queues, processes, and activity. Every accepted turn freezes the effective values into its `ExecutionContext`, so a queued turn cannot silently change provider because someone edits the route setting behind it.

A Slack settings key includes the authenticated account and exact DM/channel route ID; every root and thread on that route shares it, while two routes bound to one workspace remain independent. A Telegram settings key contains only the private chat ID, so the chat keeps its choices if its workspace or policy binding changes. Provider is stored per settings key, model per settings key and provider, and effort per settings key, provider, and model. `use default`, `model default`, and `effort default` delete the corresponding explicit choice.

Resolution never writes a default back as a selection. An absent or currently policy-disallowed provider choice uses `policy.default_provider`; its stored choice is retained and becomes effective again if a later policy permits it. A policy-allowed but native-unusable provider remains the effective choice and fails provider work through the existing configuration-error path instead of silently switching providers; non-launch commands remain available to inspect status or select another authorized provider. Model falls back to the provider's first configured model, and effort to the provider CLI's own default. `status` identifies those sources as route selection, policy default, provider default, or CLI default. The `use` picker is narrower than policy authorization: it shows only authorized providers whose current native launch check succeeds.

Conversation keys retain the authority boundary. A Slack key contains account, channel, thread/root, workspace, and policy; a Telegram key contains private chat, workspace, and policy. Changing a workspace or its selected policy therefore starts a new session scope without erasing the route setting. A Slack thread is distinct from every other root but shares its channel's settings. Per-provider sessions, compact seeds, stop/lock/process state, and the in-memory incoming-message queue use this conversation key. Telegram alone exposes `/queue`; Slack can clear its queue through `!stop` but has no `!queue` command.

Conversation activity is also the persisted thread-participation marker used when `thread_mention_required: false`; the other marker is a thread root Enso posted itself, read from Slack's `parent_user_id` ([slack-triggers.md](slack-triggers.md)). `ENSO_SESSION_TTL_DAYS` prunes stale provider sessions, compact seeds, and this activity marker. Route settings are durable and never participate in that pruning. Queues are not persisted and disappear when the process exits.

State schema v3 stores route settings separately. Loading v1 or v2 deliberately discards the ambiguous conversation-scoped provider/model/effort entries, preserves provider sessions, compact seeds, last-active activity, and job state, then rewrites the file as v3. Compound persisted keys use structured records rather than delimiter splitting because opaque conversation identifiers may contain punctuation.

### Workspace content and concurrency

- A lowercase kebab-case workspace name derives its only root as
  `~/.enso/workspaces/<name>`. The workspace container and root are physical directories,
  not symlinks; a root `.git` entry is forbidden, while repositories deeper in ordinary
  content are allowed. Configuration cannot store an alternate, external, or nested path.
- The resolved workspace supplies the subprocess cwd, persistent uploads, focused local project instructions, native workspace skills, and session scope. It is a shared content root, not a security boundary.
- A policy supplies provider availability, default provider, allowed Enso chat commands, and native policy selection. It supplies no content and does not govern provider-native slash commands or skills. Each workspace names exactly one policy, while one policy may be reused by many workspaces.
- Canonical shared instructions live at `~/.enso/AGENTS.md` with
  `CLAUDE.md -> AGENTS.md`. Immediately before each spawn, Enso validates the exact
  name-derived physical workspace, the root and local discovery links, skill-name
  uniqueness, the absence of a workspace-root `.git` entry, and `~/.enso` as the exact
  Git worktree root. It then validates and hashes the current owner-owned,
  non-hard-linked, non-symlink UTF-8 source with no group/other write bits. Claude and
  Codex discover the live global and focused workspace files natively from that Git
  boundary; Enso does not also inject them. Grok receives the just-validated shared text
  once through `--rules`, and unrestricted Agy receives it once through Enso's prompt
  envelope. The hash is diagnostic and does not claim to pin the bytes a native provider
  reads after spawning.
- Global skills are canonical under `~/.enso/skills/`, exposed through the relative links
  `.agents/skills -> ../skills` and `.claude/skills -> ../skills`. Each workspace owns an
  initially empty `skills/` with the same two relative discovery views. A duplicate skill
  directory name across global and workspace scope makes that workspace invalid rather
  than relying on provider precedence.
- Global reference docs live below `~/.enso/docs/`. A genuinely fresh setup starts with
  `enso/content_model.md`, `enso/layout.md`, and `operator.md`; `enso doc list` computes
  discovery dynamically from whatever docs currently exist. Installed starters are
  user-owned and may be edited or deleted without a tombstone or later resurrection.
- Fresh setup persists `setup.completed_at: null` before seeding the global prompt,
  bundled skills, starter docs, default-workspace prompt, and workspace knowledge index.
  It creates one baseline Git commit of the complete initial tree, then records a
  timezone-bearing completion timestamp. A seed or commit failure leaves `null` for an
  explicit retry that preserves completed pieces; a timestamp-write failure after the
  baseline retries only the state transition and does not create another initial commit.
  Timestamped and pre-feature configurations never seed. Explicit `enso setup` on either
  state validates the existing catalog before repository mutation and performs
  structural-only repair without provider, workspace, transport, messaging, or service
  reconfiguration. It does not rewrite `config.json` or synthesize a `setup` marker.
  Ordinary `serve`, `web`, and `config check` paths validate read-only, and structural
  repair or upgrades never recreate, upgrade, or delete seeded content.
- The workspace CLI is the lifecycle boundary after setup. `list` and `show` are
  read-only. `create` requires a valid lowercase kebab-case name and an explicit existing
  policy, defaults concurrency to one, and accepts no path. Under the config mutation
  lock it strictly reloads config, validates the complete candidate catalog, stages the
  full scaffold in a sibling directory, publishes it with an exclusive atomic rename,
  saves config atomically, and runs the installation check; the new scaffold is
  recorded in local history by a later scoped commit. Fresh setup's full-authority
  unrestricted `admin`/`default`
  binding is the only implicit authority creation.
- Workspace publication is never silently undone. A config-save failure leaves a clearly
  reported unused directory; a later check failure leaves the configured
  directory for repair and an explicit retry. Creation refuses any existing destination.
  `workspace repair` creates only missing structural directories and exact discovery
  links, preserves all seeded/user content, and reports launch-blocking omissions.
  Running bot and dashboard processes keep their loaded bindings until restart.
- The policy CLI is the authority-registration boundary after setup. `list` and `show`
  expose safe catalog and validation metadata without native contents or secrets.
  `create` requires exactly one explicit authority source (`--unrestricted` or an
  existing `--policy-dir`), one or more explicit providers, and a default provider. A
  restricted source is complete, physical, owner-protected, and user-authored before
  registration; Enso validates it but never generates, copies, changes permissions,
  rewrites, upgrades, or repairs its canonical content. Fresh setup's full-authority
  unrestricted `admin` is the sole automatic creation, and no implicit restricted
  directory exists.
  `enso config check` remains the complete validator; deletion, rebinding, repair, and
  presets are outside this lifecycle.
- Several routes may share one workspace and therefore its files, policy, and concurrency limit while retaining independent route settings and separate sessions.
- A client route that shares files with a staff route must not be able to rewrite instructions, skill definitions, or provider control files trusted by the staff route.
- Each workspace has a process-local semaphore shared by chats and compaction. The default is one active turn; operators may raise it when concurrent writes are safe.
- Background messages are scoped to a conversation execution key, and operational logs avoid prompt previews.
- Scheduled jobs are not Slack routes, but every job selects a named workspace. Its provider runs in that workspace under the workspace's policy and shares the process-local workspace semaphore.
- A job prerun is trusted host-side Bash executed from the job directory before the provider launch. It is not constrained by the native policy.
- A persistent per-job `.run.lock` coordinates the scheduler, CLI, and dashboard across processes. Workspace semaphores remain process-local, so two separate Enso processes are not serialized merely because their jobs share a workspace.
- Invalid job bindings or a provider disallowed by the workspace's policy fail before prerun and provider execution. There is no implicit cwd, alternate workspace, or unrestricted job fallback.
- The launchd and systemd service definitions intentionally set no process working directory. Only a resolved provider execution gets a cwd, so daemon startup location cannot become an accidental transport fallback.
- Scheduled successes are silent unless the provider explicitly sends a message. Host-side failure and recovery notifications use the job's destination independently of Slack routing; manual runs suppress those automatic notifications but cannot suppress a provider-originated send.

## Concurrency & consistency

The bot, dashboard, CLI, agent subprocesses, and operator can all touch the file layer.
At personal scale the model is deliberately simple:

- **Dashboard writes are atomic** — a temp file in the same directory plus `os.replace`.
  A reader never sees a half-written `JOB.md`, `SKILL.md`, or shared `~/.enso/AGENTS.md` from a web edit.
- **Config mutations are serialized and atomic** — workspace and policy creation hold the
  owner-only cross-process config lock from a strict reread through candidate validation
  and atomic replacement. A malformed file aborts without defaults or overwrite. The
  workspace scaffold is a separately atomic publication, so a post-publication failure
  is reported and preserved rather than hidden behind destructive rollback. Policy
  creation has no filesystem publication: it registers only an existing restricted
  source or an explicit unrestricted catalog entry after the complete candidate passes.
- **Content history is plain scoped Git** — `~/.enso` is an exact local-only Git
  worktree whose managed protective `.gitignore` block is written before the repository
  ever exists, so configuration, credentials, databases, and runtime state cannot enter
  history through ordinary staging. Agents record scoped `git add <paths>`/`git commit`
  calls per the root prompt, fresh setup records one baseline commit, and
  `enso config check` reports any tracked file the protective rules would exclude. Enso
  never creates or contacts a remote and exposes no snapshot, restore, reset, or delete
  commands.
- **Last write wins.** Optimistic locking and conflict resolution are out of scope for
  this single-operator tool.
- **SQLite in WAL mode** allows the bot, dashboard, CLI, and agent subprocesses to share
  run history and registered data while readers normally continue during writes. Every
  Enso operation owns a short-lived connection; reads wait at most 500 ms and writes have
  a five-second writer-acquisition budget. Writes always roll back and close on
  failure. Async callers run the complete operation in a worker thread.
- **Contention is visible and bounded.** A lock timeout is a retryable **Database busy**
  state; access, open, and corruption failures are **Database unavailable**. Database-backed
  list/detail routes return `503`, while mixed pages keep their non-database content and
  scope the alert to the failed section. `/health` never touches SQLite.
- **SQLite files are private.** The database and data-bearing sidecars are created and
  repaired to owner-only `0600`, including databases from older installations. Repairs
  never open and close a live SQLite file, which would release process-scoped POSIX locks.

## Access & security

The web UI is a **single-operator, local** surface. It is not hardened for the public
internet and the PRD makes that a non-goal.

- **Bind localhost by default** (`web.host = 127.0.0.1`). Nothing is exposed off-machine
  unless the operator opts in.
- **Host-header allowlist:** loopback names and a concrete bind address are accepted
  automatically; remote names/IPs must be listed in `web.allowed_hosts`. Wildcard binds
  (`0.0.0.0` / `::`) widen the listen interface only and never trust arbitrary hosts.
- **Remote = Tailscale.** To reach it from a phone, bind the tailnet interface (or front
  it with `tailscale serve`) and allow the hostname/IP clients use; traffic is
  WireGuard-encrypted, so plain HTTP on the tailnet is fine for local development access.
- **Optional shared token** (`web.token`): a matching query parameter bootstraps an
  HTTP-only, SameSite cookie. When unset, authentication is disabled entirely; the safe
  default then relies on the loopback bind. Remote access needs a strong token or trusted
  tailnet/reverse-proxy controls. The Host allowlist is not authentication. There is no
  user/account system.
- **Cross-site write protection:** every state-changing form carries a random,
  process-scoped CSRF token (custom clients may send `X-CSRF-Token`). Missing or invalid
  tokens fail before the handler runs.
- **Browser hardening:** responses deny framing, disable MIME sniffing, use a
  no-referrer policy, and mark HTML as `no-store`.
- The web app can trigger real work (run-now, edit a job's prompt, edit shared `~/.enso/AGENTS.md`). That
  is acceptable precisely because access is already restricted to the operator; it is
  *not* a capability to expose broadly.
- **Write boundary:** job prompts, Enso-owned skills, and the canonical shared
  `AGENTS.md` are edited under `~/.enso/`; the shared file's fixed path is
  `~/.enso/AGENTS.md`.
  External/"parent" skills discovered from other CLI roots are read-only. User-selected
  job and skill paths are resolved and checked against their owning root before writes.
  The Tables web surface is read-only; agents may write only validated, non-reserved user
  tables through standard SQLite tooling.

## Implementation map

| Area                     | Change                                                                                        |
| ------------------------ | --------------------------------------------------------------------------------------------- |
| `core.py`                | Resolves native policy, revalidates discovery at each interactive spawn, and handles provider output |
| `instructions.py`        | Validates the live shared source and complete root/workspace discovery boundary without mutation |
| `job_runner.py`          | Revalidates discovery before trusted prerun and again at the actual batch-provider boundary   |
| `providers/`             | Uses native Claude/Codex discovery and one explicit Grok/Agy shared-instruction delivery       |
| `policy.py`              | Builds native launches and revisions them against the current launch contract                 |
| `cli.py`                 | Provides standalone web, manual job-run, and workspace/policy lifecycle commands              |
| `config.py`              | Backfills `web` (including `allowed_hosts` / `external_skill_roots`) and `runs` defaults      |
| `formatting.py`          | Converts legacy Markdown and supplies standard-Markdown-aware splitting                       |
| `outbound.py`            | Owns strict typed message/surface contracts, parsers, and Slack-aligned limits                |
| `surface_drafts.py`      | Owns expiring confirmation drafts, atomic claim/lease, scrubbing, and crash reconciliation    |
| `transports/slack.py`    | Renders rich blocks, stages previews, handles confirmed actions, and invokes Slack APIs       |
| `slack_manifest.yaml`    | Declares Socket Mode, App Home, interactivity, events, and required bot scopes                |
| `jobs.py`                | Loads YAML scalars with `BaseLoader`, then falls back for malformed legacy headers            |
| `frontmatter.py`         | Provides fence-aware raw edits and YAML serialization, writing through `fsutil`               |
| `fsutil.py`              | Owns atomic text writes, containment checks, hashing, and SQLite file hardening                |
| `scaffolding.py`         | Creates canonical trees, exclusively seeds fresh content, and conservatively repairs structure |
| `repository.py`          | Establishes the exact local Git boundary, protective ignore rules, and fallback identity        |
| `sqlite_store.py`        | Owns operation-scoped connections, transactions, bounded timeouts, and failure classification |
| `docs.py`                | Owns reference-doc path validation, the bounded recursive listing, scaffolding, and deletion  |
| `starter_docs/`          | Packages the three fresh-only user-owned reference starters                                   |
| `runs.py`                | Owns SQLite `create`/`finish`/`list_runs`/`get`/`prune` operations                            |
| `tables.py`              | Owns the registration catalog, identifier validation, schema inspection, and bounded previews |
| `skills/*/SKILL.md`      | Bundles portable workflows for docs, jobs, policy, Slack, tables, and workspace management    |
| `web/`                   | Contains the Starlette app, current routes/templates, discovery, and vendored assets          |
| `pyproject.toml`         | Defines the `web` extra, base `pyyaml` dependency, and package data                           |

The bundled root prompt, global skills, and starter docs are fresh-setup-only package
resources; the workspace prompt and knowledge index are new-workspace-creation-only
resources. Once installed, their copies are user-owned: startup and upgrades do not run
content installers, maintain pristine hashes or deletion tombstones, remove retired
content, or clean up copied tool files. Existing installations adopt bundle changes only
through an explicit operator-reviewed migration, never by fabricating an incomplete
fresh-setup marker.
