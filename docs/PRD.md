# Enso Web UI — Product Requirements

> **Status: proposed.** This describes a feature set not yet built. It is the
> source of truth for the design; the specs under [`docs/specs/`](specs/) own the
> details. Nothing here has shipped — see [`CHANGELOG.md`](../CHANGELOG.md) for what has.

## Summary

Today Enso is driven entirely from chat (Telegram/Slack): you message an agent, it
works on your machine, the reply comes back to the thread. That stays. This adds a
**local web UI** — a place to *see* the system rather than converse with it — and
**run history**, the stored record of what the agents have done.

The web UI is a read/write dashboard for:

- **Jobs** and their **recent runs** — output, status, timing (run history does not
  exist as stored data today; this feature introduces it). Jobs are fully editable from
  the UI, including their prompt body.
- **Skills** — browse the bundled/installed skills; edit Enso's own.
- **AGENTS.md** — read (and edit) the system prompt.

There is **no chat in the web UI** — chat lives in Telegram/Slack. The web UI is for
overview, organisation, and managing the scheduled work Enso already runs.

## Goals

- Give a single, glanceable view of what Enso is doing and has done — jobs and their
  runs — that chat can't provide.
- Make jobs **editable in place**: schedule, provider/model, prompt body, and prerun
  from a browser, not just a text editor over SSH.
- Keep Enso **file-first**: authored intent (jobs, skills) stays as inspectable,
  greppable, git-friendly Markdown. Only machine-generated history (runs) goes in a DB.
- Surface run history where you already work — the web UI and the `enso runs` CLI.

## Non-goals (v1)

- **No chat in the web UI.** Conversation stays in Telegram/Slack.
- **No multi-user / accounts / login.** Enso is a personal, single-operator tool. The
  web UI binds to localhost; remote access is via Tailscale (see
  [architecture.md](specs/architecture.md)). An optional shared token is the only auth.
- **No public hosting.** This is not a SaaS; it runs on your machine next to `enso serve`.
- **No built-in one-off task queue.** Enso's unit of background work stays the scheduled
  **job**; one-off work is handled from chat or an external tracker (Todoist), not a
  managed task object inside Enso.
- **No rich text editor / WYSIWYG.** Prompts are Markdown in a textarea.

## Vocabulary

- **Job** — a *scheduled* background unit of work defined by a `JOB.md` file, run on a
  cron by the scheduler. Recurring. This work records its runs and makes it editable
  from the web UI.
- **Run** — one execution of a job: when it started/ended, its exit status,
  and its captured output. Recorded in SQLite. See [data-model.md](specs/data-model.md).
- **Transport / notify** — the existing chat delivery layer (Telegram/Slack). Job
  completion and failure notifications ride it, unchanged.

## Key decisions

| Decision | Choice |
| --- | --- |
| Authored intent (jobs, skills) | **Files** — Markdown + YAML frontmatter, source of truth, edited by human and agent alike |
| Run history | **SQLite** (`~/.enso/enso.db`) for metadata; **output blobs on disk** (`~/.enso/runs/<id>.log`). The one genuinely DB-shaped, append-only dataset |
| Frontmatter | Real YAML (`pyyaml`), via a shared parser (replaces the current regex parser) |
| Web server | **Starlette + Uvicorn + Jinja2 + HTMX**, running as a coroutine *inside* `enso serve`'s event loop, sharing the `Runtime` |
| Web access | Bind **localhost** by default; Tailscale for remote; optional bearer token. No login |
| Web capability | **Read/write, scoped to `~/.enso/`** — full CRUD on jobs and Enso's own skills; edit job prompts; run-now; edit AGENTS.md. Skills discovered outside `~/.enso/` are read-only |
| Notifications | Reuse `transport.notify` / `enso message send`; job pings ride your existing chat |

## Personas

One persona: the operator. Runs Enso as a personal service on their own machine, talks
to it from their phone via Telegram/Slack, and wants a laptop/desktop (or
phone-over-Tailscale) surface to organise scheduled work and audit what the agents have
been doing — without turning Enso into a hosted product.

## Features

### F1 — Run history

- Every provider execution and failed job prerun records a **run** row in SQLite;
  intentional prerun no-work does not. Rows include kind (`job`), name, trigger
  (`schedule`/`manual`), start/end times, exit code,
  status (`running`/`ok`/`error`/`timeout`, plus `prerun_error`/`prerun_timeout` for
  failed job gates), and a pointer to its output log on disk.
- Captured output is written to `~/.enso/runs/<run_id>.log`; the row stays lean.
- A run is created at **spawn** (status `running`) and finalised at exit, so a crash
  mid-run leaves a visible `running` row rather than nothing.
- Retention: a configurable cap prunes old runs (and their logs) so history doesn't
  grow without bound. See [data-model.md](specs/data-model.md).
- Surfaced in the web UI (per job and a global recent-runs feed) and via
  `enso runs list` / `enso runs show <id>`.

### F2 — Web UI: dashboard & runs

- `/` — overview: recent runs feed and jobs at a glance.
- `/runs/<id>` — a single run's full output, status, timing, and what triggered it.
- Read-only views; the data comes from SQLite (runs) and the file scans (jobs).

### F3 — Web UI: jobs (full CRUD)

- `/jobs` — list with schedule, provider/model, enabled state.
- `/jobs/<name>` — frontmatter, prompt, prerun, recent runs, **Run now**, enable/disable.
- **Create, edit, and delete** jobs from the UI: name, schedule, provider, model, enabled,
  timeout, notify, the prompt body (`JOB.md` body), and the optional prerun script — all
  writing to `~/.enso/jobs/<name>/`. Editing the prompt has its own focused endpoint,
  mirroring in-place skill editing. Delete requires confirmation (destructive).

### F4 — Web UI: skills & AGENTS.md

- `/skills` lists two tiers: **Enso skills** — everything under `~/.enso/skills/`, whether
  user-created or seeded from Enso's starter set at install — **fully editable and
  deletable**; and **external / "parent"** skills auto-discovered from the underlying CLIs'
  own skill roots (e.g. `~/.claude/skills/`), shown **read-only with their source path** for
  awareness. Seeded starter skills are ordinary files after install — Enso seeds them once
  and never re-syncs or resurrects them — so a "shipped with Enso" origin label is cosmetic.
- **Create, edit, and delete Enso's own skills** (those under `~/.enso/skills/`): the
  `SKILL.md` frontmatter + body and any tool scripts. The UI **never writes outside `~/.enso/`**.
- `/agents` — renders `AGENTS.md` (the system prompt); editable, writing back to the file
  in the working directory (with its `CLAUDE.md` symlink intact).

## Success criteria (v1)

- Editing a job's schedule, prompt body, or prerun from the web UI writes back to
  `~/.enso/jobs/<name>/` and the next scheduled run uses the new definition.
- Every job execution leaves a run row with retrievable output, visible in the web
  UI and via `enso runs`.
- The web UI runs inside `enso serve` with no separate process to manage, reachable at
  `http://localhost:<port>` and over the tailnet.
- Existing chat, jobs, and messaging behaviour are unchanged.

## Future ideas (explicitly out of v1)

Editing external "parent" skills in place (v1 keeps them read-only); run output streaming
(live tail) rather than post-hoc; a proper auth layer if Enso ever goes multi-operator;
full-text search over runs.
