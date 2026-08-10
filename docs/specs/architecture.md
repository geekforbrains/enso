# Architecture

How the web UI, run recording, and registered data tables fit into Enso.
Read [PRD.md](../PRD.md) first for the what & why. Sibling specs:
[data-model.md](data-model.md), [tables.md](tables.md), [web.md](web.md).

## Runtime and process layout

`enso serve` builds a `Runtime` (`core.py`) and starts a Telegram or Slack transport.
The transport starts `Runtime.run_job_scheduler` alongside its chat loop; the scheduler
loads `JOB.md` files every 60 seconds and fires due jobs through `_execute_job`.

`enso web` builds its own `Runtime` and runs Starlette/Uvicorn as a separate process.
The dashboard and bot therefore do not share memory or an event loop. They coordinate
through the same files under `~/.enso/`, the configured workspaces, and the shared SQLite
database. Starting `enso serve` does not start the dashboard.

```
       enso serve process                 enso web process
  ┌────────────────────────┐        ┌──────────────────────┐
  │ transport + scheduler  │        │ Starlette / Uvicorn  │
  │ bot Runtime            │        │ dashboard Runtime    │
  └───────────┬────────────┘        └──────────┬───────────┘
              └──────────────┬─────────────────┘
                             ▼
                 files under ~/.enso/ and
                 the configured workspaces
                             +
                 SQLite (enso.db, WAL mode)
```

The dashboard's **Run now** action calls the same `Runtime.run_job_now` execution path
as the CLI, but on the dashboard's Runtime instance. Its in-memory scheduler state is
not shared with the bot process. File writes are atomic and SQLite uses WAL mode to
handle this cross-process boundary.

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

Run history is captured by the `Runtime` around the shared job-execution pipeline used
for background work:

- `_execute_job` — scheduled jobs.
- `run_job_now` — CLI and dashboard manual runs (recorded with trigger `manual`).

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
Existing configs are backfilled when a provider is added, while configured paths and
custom models are preserved.

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
(900 seconds by default; `0` disables it). Queue wait does not count. When the budget is
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

## Request resolution

Slack routing is described in [teams.md](teams.md), native CLI invocation in [permissions.md](permissions.md), and storage in [data-model.md](data-model.md). Slack always uses exact routes. Telegram retains its exact numeric user-ID allowlist and global `working_dir` and rejects non-private chat types.

Conversation work carries an immutable `ExecutionContext` instead of reading a singleton cwd. Telegram's context contains the global `working_dir` and unrestricted launch. Slack's context contains the routed workspace path, access profile, stable session key, captured model selection, audit callbacks, and workspace concurrency. The native provider launch is prepared only after the workspace slot is acquired. The same context is threaded through dispatch, uploads, commands, compaction, and provider execution.

The top-level workspace/access catalog is parsed independently of Slack so jobs can use it on any transport. Slack routes are loaded and validated when `enso serve` starts. They are not hot-reloaded; changing authorization requires a restart. For each Slack event the transport performs this fixed sequence before fetching surrounding context or downloading attachments:

1. Verify the event belongs to the configured Slack account.
1. Accept an ordinary `im` message or an explicit mention in a channel. Ignore ordinary channel messages.
1. Resolve an `im` conversation by exact sender ID, or any other conversation by exact channel ID. A thread inherits its parent channel route.
1. Claim the Slack delivery ID for retry deduplication.
1. If no route exists, send the fixed DM response directly or the fixed channel response in a thread, then stop without resolving execution state.
1. Resolve a configured route's workspace and access profile.
1. Validate the selected provider's launch plumbing and start optional audit recording for the route.
1. Process the command or provider request using the resolved execution context; prepare the native launch after acquiring the workspace slot.

There are no groups, sender rankings, wildcard routes, Slack allowlists, or composed policies. A configured channel authorizes every human member who can post there; an administrator posting in a client channel gets the client channel's access profile. An invalid configured route never falls back to another workspace, access profile, global `working_dir`, or unrestricted launch. A configured route that cannot launch reports a configuration error.

The two no-route replies are fixed transport strings. They do not invoke an LLM, select a workspace or access profile, construct an `ExecutionContext`, fetch message context or attachments, or start an audit turn. A globally invalid Slack configuration or wrong-account event remains silent and is logged.

### Execution and session keys

Cwd alone does not isolate sessions. Provider, model, effort, session, compaction, lock, queue, process, and activity state use a route-scoped `chat_key`. A Slack thread is distinct from its parent channel, and two channels sharing one workspace keep separate sessions. Provider selection is scoped to that conversation; `!use` never changes another route's selection.

The key is serialized as structured data rather than by splitting a delimiter-bearing string. This matters because provider and route identifiers can already contain punctuation. It includes the Slack location, thread, workspace name, and access-profile name; it deliberately remains stable across `!use`, model, and policy-revision changes so stop, queues, and per-provider sessions remain reachable.

### Workspace content and concurrency

- The resolved workspace supplies the subprocess cwd, persistent uploads, native project instructions, native workspace skills, and session scope. It is a shared content root, not a security boundary.
- An access profile supplies provider availability, default provider, allowed Enso chat commands, and native policy selection. It supplies no content and does not govern provider-native slash commands or skills.
- Several routes may share one workspace and therefore its files and concurrency limit while retaining separate sessions and access profiles.
- A client route that shares files with a staff route must not be able to rewrite instructions, skill definitions, or provider control files trusted by the staff route.
- Each workspace has a process-local semaphore shared by chats and compaction. The default is one active turn; operators may raise it when concurrent writes are safe.
- Background messages are scoped to a conversation execution key, and operational logs avoid prompt previews.
- Scheduled jobs are not Slack routes, but every job selects a named workspace and access profile. Its provider runs in that workspace under the profile's native policy and shares the process-local workspace semaphore.
- A job prerun is trusted host-side Bash executed from the job directory before the provider launch. It is not constrained by the native policy.
- A persistent per-job `.run.lock` coordinates the scheduler, CLI, and dashboard across processes. Workspace semaphores remain process-local, so two separate Enso processes are not serialized merely because their jobs share a workspace.
- Invalid job bindings or a provider disallowed by the selected profile fail before prerun and provider execution. There is no global or unrestricted job fallback.
- Scheduled successes are silent unless the provider explicitly sends a message. Host-side failure and recovery notifications use the job's destination independently of Slack routing; manual runs suppress those automatic notifications but cannot suppress a provider-originated send.

## Concurrency & consistency

The bot, dashboard, CLI, agent subprocesses, and operator can all touch the file layer.
At personal scale the model is deliberately simple:

- **Dashboard writes are atomic** — a temp file in the same directory plus `os.replace`.
  A reader never sees a half-written `JOB.md`, `SKILL.md`, or `AGENTS.md` from a web edit.
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
- The web app can trigger real work (run-now, edit a job's prompt, edit AGENTS.md). That
  is acceptable precisely because access is already restricted to the operator; it is
  *not* a capability to expose broadly.
- **Write boundary:** job prompts and Enso-owned skills are edited under `~/.enso/`;
  `AGENTS.md` is edited at its fixed path in the configured working directory.
  External/"parent" skills discovered from other CLI roots are read-only. User-selected
  job and skill paths are resolved and checked against their owning root before writes.
  The Tables web surface is read-only; agents may write only validated, non-reserved user
  tables through standard SQLite tooling.

## Implementation map

| Area                     | Change                                                                                        |
| ------------------------ | --------------------------------------------------------------------------------------------- |
| `core.py`                | Records scheduled runs around `_execute_job` and enforces retention                           |
| `cli.py`                 | Provides standalone `enso web` and manual job-run commands                                    |
| `config.py`              | Backfills `web` (including `allowed_hosts` / `external_skill_roots`) and `runs` defaults      |
| `jobs.py`                | Loads YAML scalars with `BaseLoader`, then falls back for malformed legacy headers            |
| `frontmatter.py`         | Provides fence-aware raw edits and YAML serialization, writing through `fsutil`               |
| `fsutil.py`              | Owns atomic text writes, containment checks, pristine-file hashing, and SQLite file hardening |
| `sqlite_store.py`        | Owns operation-scoped connections, transactions, bounded timeouts, and failure classification |
| `docs.py`                | Owns reference-doc path validation, the bounded recursive listing, scaffolding, and deletion  |
| `runs.py`                | Owns SQLite `create`/`finish`/`list_runs`/`get`/`prune` operations                            |
| `tables.py`              | Owns the registration catalog, identifier validation, schema inspection, and bounded previews |
| `skills/tables/SKILL.md` | Guides safe, consistent agent table creation and data access                                  |
| `web/`                   | Contains the Starlette app, current routes/templates, discovery, and vendored assets          |
| `pyproject.toml`         | Defines the `web` extra, base `pyyaml` dependency, and package data                           |

Missing bundled skill files are seeded. Existing copies update only when their hash
matches a known pristine prior version; customized files and symlinks remain untouched.

The task-system removal migrates only artifacts that exactly match the former bundled
files: the pristine `tasks` skill is removed and the pristine task-era `AGENTS.md` is
replaced. Customized copies are preserved and logged with a manual-cleanup warning.
