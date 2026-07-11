# Architecture

How the web UI and run recording fit into Enso as it exists today.
Read [PRD.md](../PRD.md) first for the what & why. Sibling specs:
[data-model.md](data-model.md), [web.md](web.md).

## Runtime and process layout

`enso serve` builds a `Runtime` (`core.py`) and starts a Telegram or Slack transport.
The transport starts `Runtime.run_job_scheduler` alongside its chat loop; the scheduler
loads `JOB.md` files every 60 seconds and fires due jobs through `_execute_job`.

`enso web` builds its own `Runtime` and runs Starlette/Uvicorn as a separate process.
The dashboard and bot therefore do not share memory or an event loop. They coordinate
through the same files under `~/.enso/`, the configured workspace, and the runs SQLite
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
                 the configured workspace
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

| Concern | Choice | Why |
| --- | --- | --- |
| ASGI framework | **Starlette** | Minimal, async-native, and well suited to server-rendered pages |
| Server | **Uvicorn** | Serves the standalone dashboard process |
| Templates | **Jinja2** | Server-rendered HTML; the UI is views + forms, not an SPA |
| Interactivity | **HTMX** (vendored; no runtime build) | Inline toggle/run actions without a client application bundle |
| Forms | **python-multipart** | Parse form posts |
| Run store | **`sqlite3`** (stdlib) | No dependency; WAL mode for concurrent readers; see [data-model.md](data-model.md) |
| Job frontmatter | **PyYAML `BaseLoader` + legacy fallback** | Valid YAML scalars stay strings; malformed older headers remain loadable; raw web edits avoid reserialization |

`pyproject.toml` defines:

```toml
[project.optional-dependencies]
web = ["starlette>=0.37", "uvicorn>=0.30", "jinja2>=3.1", "python-multipart>=0.0.9"]
```

`pyyaml` is a base dependency (jobs need it independently of the web UI). HTMX and the
compiled Tailwind stylesheet are vendored, so the UI has **no external CDN dependency**
and works offline. Package data includes `web/templates/**` and `web/static/**`.

## Run recording

Run history is captured by the `Runtime` around the shared job-execution pipeline used
for background work:

- `_execute_job` — scheduled jobs.
- `run_job_now` — CLI and dashboard manual runs (recorded with trigger `manual`).

Interactive **chat** requests are *not* runs — they are session-based and ephemeral, and
belong to the transport, not the run log.

The recording seam (see [data-model.md](data-model.md) for the schema):

1. Before provider spawn: `runs.create(kind, name, trigger)` → a row with
   `status='running'`, the pipeline start time, and an allocated `run_id`. A failed job
   prerun creates the same row when the failure is classified, backdated to gate start;
   intentional no-work creates no row.
2. During: provider output is captured for the run log. Failed preruns store only their
   bounded safe diagnostic; raw prerun stdout/stderr is never copied into history or a
   transport notification.
3. After: `runs.finish(run_id, exit_code, status)` sets `ended_at`, `exit_code`, and a
   terminal `status` (`ok` / `error` / `timeout`; job gates may instead finish as
   `prerun_error` / `prerun_timeout`). Intentional no-work (`exit 1`) creates no row.

A small `runs.py` module owns the SQLite connection and these calls, mirroring how
`messages.py` owns the messages file. The DB is opened lazily and created on first use
(`CREATE TABLE IF NOT EXISTS`), so existing installs need no migration step.

## Concurrency & consistency

The bot, dashboard, CLI, agent subprocesses, and operator can all touch the file layer.
At personal scale the model is deliberately simple:

- **Dashboard writes are atomic** — a temp file in the same directory plus `os.replace`.
  A reader never sees a half-written `JOB.md`, `SKILL.md`, or `AGENTS.md` from a web edit.
- **Last write wins.** Optimistic locking and conflict resolution are out of scope for
  this single-operator tool.
- **SQLite in WAL mode** allows the bot, dashboard, and CLI processes to read and write
  run history without blocking readers.

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

## Implementation map

| Area | Change |
| --- | --- |
| `core.py` | Records scheduled runs around `_execute_job` and enforces retention |
| `cli.py` | Provides standalone `enso web` and manual job-run commands |
| `config.py` | Backfills `web` (including `allowed_hosts` / `external_skill_roots`) and `runs` defaults |
| `jobs.py` | Loads YAML scalars with `BaseLoader`, then falls back for malformed legacy headers |
| `frontmatter.py` | Provides fence-aware raw edits plus YAML serialization and atomic writes |
| `runs.py` | Owns SQLite `create`/`finish`/`list`/`get`/`prune` operations |
| `web/` | Contains the Starlette app, current routes/templates, discovery, and vendored assets |
| `pyproject.toml` | Defines the `web` extra, base `pyyaml` dependency, and package data |

Missing bundled skill files are seeded. Existing copies update only when their hash
matches a known pristine prior version; customized files and symlinks remain untouched.

The task-system removal migrates only artifacts that exactly match the former bundled
files: the pristine `tasks` skill is removed and the pristine task-era `AGENTS.md` is
replaced. Customized copies are preserved and logged with a manual-cleanup warning.
