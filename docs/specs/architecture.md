# Architecture

How the web UI, run recording, and the task runner fit into Enso as it exists today.
Read [PRD.md](../PRD.md) first for the what & why. Sibling specs:
[data-model.md](data-model.md), [tasks.md](tasks.md), [web.md](web.md).

## Where things run today

`enso serve` builds one `Runtime` (`core.py`) and starts a transport (Telegram or
Slack). Inside a single asyncio event loop it runs two long-lived concerns:

- the **transport** — receives chat messages, dispatches them to a provider CLI
  subprocess, streams status back;
- the **job scheduler** (`Runtime.run_job_scheduler`) — a 60-second tick that loads
  `JOB.md` files and fires due jobs through `_execute_job`.

State is files under `~/.enso/`: `config.json`, `state.json`, `messages.json`,
`jobs/<name>/JOB.md`, `skills/<name>/`, and the `workspace/` working dir. There is no
database and **no run history** — a job execution leaves only a `job_last_run`
timestamp in `state.json`.

This feature adds a third concern to the same loop (the web server), a new persisted
dataset (runs, in SQLite), and a built-in job (the task runner). None of it changes the
transport or the interactive-chat path.

## The three tasks in one loop

```
                     enso serve
                         │
        ┌────────────────┼────────────────────┐
        ▼                ▼                     ▼
  transport         job scheduler          web server
  (TG / Slack)      (60s tick)             (Starlette/Uvicorn
        │                │                  as a coroutine)
        │                │                     │
        └──────── shared Runtime + ~/.enso/ ───┘
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
     files (jobs, tasks,        SQLite (runs)
     skills, AGENTS.md)         + runs/<id>.log
```

The web server runs **in-process**, as another coroutine on the same event loop, not as
a separate service. This is the key architectural choice and it buys a lot:

- **No IPC, no second process to supervise.** The launchd/systemd unit still runs one
  thing: `enso serve`.
- **Shared `Runtime`.** The web app holds a reference to the same `Runtime` the
  scheduler uses, so "Run now" from a job page calls the same `_execute_job`, and reads
  of in-memory state (`_running_job_tasks`, job last-run) are consistent.
- **No locking.** Because it is one event loop, a web request handler and the scheduler
  never run truly concurrently — there is no shared-memory data race to guard. The only
  cross-process contention is on the **files** (the agent subprocess and the CLI also
  write them) and on **SQLite**, both handled below.

Implemented as: `uvicorn.Server(config).serve()` awaited as one of the tasks in
`serve`'s `asyncio.gather`, alongside the transport and `run_job_scheduler`. A
standalone `enso web` command runs the same app without the transport, for development.

## Stack

Enso's runtime deps are deliberately tiny (`typer`, `rich`, `croniter`), with transport
libraries behind extras. The web UI follows the same rule — a new `web` extra, nothing
pulled into the base install.

| Concern | Choice | Why |
| --- | --- | --- |
| ASGI framework | **Starlette** | Minimal, async-native, no ORM/opinions; composes as a coroutine in the existing loop. FastAPI would work but its pydantic/OpenAPI weight buys nothing for server-rendered pages |
| Server | **Uvicorn** | Runs as `Server.serve()` inside our loop; no separate process |
| Templates | **Jinja2** | Server-rendered HTML; the UI is views + forms, not an SPA |
| Interactivity | **HTMX** (vendored, no build step) | Inline status/tag/run-now updates without a JS toolchain — matches "no chat, just views" |
| Uploads/forms | **python-multipart** | Attachment upload + form posts |
| Run store | **`sqlite3`** (stdlib) | No dependency; WAL mode for concurrent readers; see [data-model.md](data-model.md) |
| Frontmatter | **`pyyaml`** | Real YAML for job/task frontmatter (lists, quoting); replaces the regex parser |

`pyproject.toml` gains:

```toml
[project.optional-dependencies]
web = ["starlette>=0.37", "uvicorn>=0.30", "jinja2>=3.1", "python-multipart>=0.0.9"]
```

`pyyaml` moves into base `dependencies` (both jobs and tasks need it, independent of the
web UI). HTMX is vendored as a static asset so the UI has **no external CDN dependency**
and works offline. Package data grows to include `web/templates/**` and `web/static/**`.

## Run recording

Run history is captured by the `Runtime`, around the two places a provider subprocess is
spawned for background work:

- `_execute_job` — scheduled jobs and the task runner.
- `job_run` / the new `task run` CLI — manual runs (recorded with trigger `manual`).

Interactive **chat** requests are *not* runs — they are session-based and ephemeral, and
belong to the transport, not the run log.

The recording seam (see [data-model.md](data-model.md) for the schema):

1. Before spawn: `runs.create(kind, name, trigger)` → a row with `status='running'`,
   `started_at=now`, and an allocated `run_id`; open `~/.enso/runs/<run_id>.log`.
2. During: the subprocess's captured stdout/stderr is written to the log file (the job
   pipeline already collects this output for notification; it now also persists it).
3. After: `runs.finish(run_id, exit_code, status)` sets `ended_at`, `exit_code`, and a
   terminal `status` (`ok` / `error` / `timeout`).

A small `runs.py` module owns the SQLite connection and these calls, mirroring how
`messages.py` owns the messages file. The DB is opened lazily and created on first use
(`CREATE TABLE IF NOT EXISTS`), so existing installs need no migration step.

## The task runner

The task runner is a **built-in `Job`** the runtime injects into the scheduler's job
list — it is not a `JOB.md` under `~/.enso/jobs/`. Rationale:

- It must **always exist** and stay versioned with Enso (a scaffolded file would drift,
  and "only if missing" installs never get updates).
- Its schedule, provider, model, and batch size are **config-driven**
  (`config.json → tasks`), not frontmatter the user edits.
- It keeps `~/.enso/jobs/` meaning "the user's jobs".

Mechanically it is still a `Job` object flowing through the unchanged pipeline: the
scheduler's `load_jobs()` result is augmented with the built-in task-runner job (gated on
`tasks.enabled`), it gets the same `_running_job_tasks` concurrency guard and semaphore,
and it runs via `_execute_job`. Its "prerun" is internal Python (claim the oldest `todo`
task and inject it) rather than a `prerun.sh`. Full behaviour in [tasks.md](tasks.md).

_Tradeoff considered:_ making it a real scaffolded `JOB.md` would be more consistent with
"jobs are just files" and let the user tune it in place. Rejected because always-present +
config-driven + auto-updating matters more here; noted so a future revisit is informed.

## Concurrency & consistency

Three writers touch the file layer: the **web request handler**, the **agent
subprocess** (via `enso task`/Edit), and occasionally the **operator by hand**. At
personal scale the model is deliberately simple:

- **File writes are atomic** — temp file in the same dir + `os.replace`, exactly as
  `save_state`/`save_config` already do. A reader never sees a half-written `TASK.md`,
  `JOB.md`, `SKILL.md`, or `AGENTS.md`.
- **Last write wins.** Two writers racing on the same task is rare (single operator) and
  not worth optimistic-locking machinery. The web edit form can carry the file's mtime
  and warn on a stale save, but conflict *resolution* is out of scope for v1.
- **SQLite in WAL mode** handles the one genuinely concurrent-write path — the scheduler
  finalising a run while a web request reads the runs feed — without blocking readers.

Task **claiming** is the one place a race would matter (double-processing a task). It is
prevented structurally: the runner sets `status: in_progress` (an atomic `TASK.md`
rewrite) *before* spawning the agent, and the scheduler's existing "already running" guard
stops the built-in job from overlapping itself. A task seen as `todo` by one claim is
`in_progress` on disk before any second look.

## Access & security

The web UI is a **single-operator, local** surface. It is not hardened for the public
internet and the PRD makes that a non-goal.

- **Bind localhost by default** (`web.host = 127.0.0.1`). Nothing is exposed off-machine
  unless the operator opts in.
- **Remote = Tailscale.** To reach it from a phone, bind the tailnet interface (or front
  it with `tailscale serve`); traffic is WireGuard-encrypted, so plain HTTP on the
  tailnet is fine — the same model Gavin's other projects use for dev access.
- **Optional bearer token** (`web.token`): when set, every request must present it (query
  param to bootstrap a cookie, then a session cookie). When unset, localhost-only is the
  security boundary. There is **no user/login system** — that would be over-engineering a
  personal tool.
- The web app can trigger real work (run-now, edit AGENTS.md, create tasks the agent will
  execute). That is acceptable precisely because access is already restricted to the
  operator; it is *not* a capability to expose broadly.
- **Writes never leave `~/.enso/`.** The UI creates/edits/deletes tasks, jobs, and
  Enso-owned skills under `~/.enso/`, plus the working-dir `AGENTS.md`. External/"parent"
  skills discovered from the CLIs' own roots (e.g. `~/.claude/skills/`) are surfaced
  read-only — Enso manages its own dir and only observes the rest. This is the ownership
  model as much as a safety boundary.

## What this touches in the codebase

| Area | Change |
| --- | --- |
| `core.py` | Record runs around `_execute_job`; register the built-in task-runner job; add a `run_task`/claim path; hold the web server task in `serve` |
| `cli.py` | `enso web`; `enso task` subcommands; `enso runs` subcommands; web task wired into `serve` |
| `config.py` | `web` (incl. `external_skill_roots`) and `tasks` config blocks, backfilled with defaults (same pattern as provider backfill) |
| `jobs.py` | Frontmatter parsing moves to a shared `frontmatter.py` (pyyaml); `Job` unchanged otherwise |
| new `tasks.py` | `Task` dataclass, load/parse/create/claim, status transitions |
| new `runs.py` | SQLite connection + `create`/`finish`/`list`/`get`/`prune` |
| new `frontmatter.py` | Shared YAML frontmatter read/write used by jobs and tasks |
| new `web/` | Starlette app; routes + templates for task/job/skill CRUD, runs, and AGENTS.md; external-skill discovery (read-only); vendored static assets |
| `prompts/AGENTS.md` | Add a Tasks section |
| new `skills/tasks/SKILL.md` | Teach the agent to author tasks |
| `pyproject.toml` | `web` extra; `pyyaml` into base deps; new package data |

> The skill create/edit/delete surface assumes seeded starter skills are ordinary,
> user-owned files — it depends on the seed-once fix tracked in
> [issue #7](https://github.com/geekforbrains/enso/issues/7). Until that lands, edits to a
> seeded skill are reverted on the next `serve`.
