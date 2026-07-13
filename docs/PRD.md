# Enso Web UI — Product Requirements

> **Status: partially implemented.** Run history and the read/write dashboard ship
> today. Items explicitly marked **Planned** are the remaining v1 scope. The specs
> under [`docs/specs/`](specs/) own implementation details and route-level status;
> [`CHANGELOG.md`](../CHANGELOG.md) records releases.

## Summary

Enso's conversational surface remains chat (Telegram/Slack): you message an agent, it
works on your machine, and the reply comes back to the thread. The shipped **local web
UI** provides a place to *see* the system rather than converse with it, backed by
persisted **run history** for jobs.

The web UI is a read/write dashboard for:

- **Jobs** and their **recent runs** — output, status, and timing. The UI can run a job,
  enable or disable it, and edit its prompt body; full job CRUD is planned.
- **Skills** — browse the bundled/installed skills; edit Enso's own.
- **AGENTS.md** — read (and edit) the system prompt.

There is **no chat in the web UI** — chat lives in Telegram/Slack. The web UI is for
overview, organisation, and managing the scheduled work Enso already runs.

## Goals

- Give a single, glanceable view of what Enso is doing and has done — jobs and their
  runs — that chat can't provide.
- Make jobs **editable in place**. Prompt editing ships today; schedule,
  provider/model, prerun, create, and delete forms are planned.
- Keep Enso **file-first**: authored intent (jobs, skills) stays as inspectable,
  greppable, git-friendly Markdown. Only machine-generated history (runs) goes in a DB.
- Surface run history in the web UI. A matching `enso runs` CLI is planned.

## Non-goals (v1)

- **No chat in the web UI.** Conversation stays in Telegram/Slack.
- **No multi-user / accounts / login.** Enso is a personal, single-operator tool. The
  web UI binds to localhost; remote access is via Tailscale (see
  [architecture.md](specs/architecture.md)). A configured shared token is the only
  built-in authentication; leaving it empty disables authentication.
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
| Frontmatter | PyYAML `BaseLoader` for valid job metadata, with a legacy line-parser fallback for malformed older files; raw web edits preserve formatting |
| Web server | **Starlette + Uvicorn + Jinja2 + HTMX**, run separately with `enso web` and sharing the file/SQLite model with `enso serve` |
| Web access | Bind **localhost** by default; Tailscale for remote; Host allowlist and optional shared token. No login |
| Web capability | **Read/write, scoped to owned files** — edit job prompts, toggle/run jobs, edit Enso-owned skills and `AGENTS.md`; full job/skill CRUD is planned. External skills are read-only |
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
- Surfaced in the web UI (per job and a global recent-runs feed).
- **Planned:** `enso runs list` / `enso runs show <id>` CLI access.

### F2 — Web UI: dashboard & runs

- `/` — overview: recent runs plus enabled-job and visible-skill counts at a glance.
- `/runs/<id>` — a run's output preview, on-disk log path, status, timing, and trigger.
- Read-only views; the data comes from SQLite (runs) and file scans (jobs and skills).

### F3 — Web UI: jobs (partially implemented)

- `/jobs` — list with schedule, provider/model, enabled state.
- `/jobs/<name>` — configuration, prompt, prerun state, recent runs, **Run now**,
  enable/disable, and confirmed directory deletion.
- Editing the prompt has a focused endpoint that rewrites only the `JOB.md` body,
  mirroring in-place skill editing.
- Deleting a job removes its whole directory, including companion and prerun files;
  recorded run history remains available.
- **Planned:** create and fully edit jobs from the UI: name, schedule, provider, model,
  enabled, timeout, notify, prompt body, and optional prerun script.

### F4 — Web UI: skills & AGENTS.md

- `/skills` lists two tiers: **Enso skills** — everything under `~/.enso/skills/`, whether
  user-created or seeded from Enso's starter set at install — **editable**; and
  **external / "parent"** skills auto-discovered from the underlying CLIs'
  own skill roots (e.g. `~/.claude/skills/`), shown **read-only with their source path** for
  awareness. Missing bundled files are seeded unless explicitly deleted, known pristine
  older copies can advance during upgrades, and customized files or symlinks remain
  untouched.
- Enso-owned skill directories can be edited or deleted after confirmation. **Planned:**
  create skills and edit their tool scripts. The skills UI never writes outside
  `~/.enso/`.
- `/agents` — renders `AGENTS.md` (the system prompt); editable, writing back to the file
  in the working directory (with its `CLAUDE.md` symlink intact).

## Success criteria (v1 target)

- Editing a job's schedule, prompt body, or prerun from the web UI writes back to
  `~/.enso/jobs/<name>/` and the next scheduled run uses the new definition.
- Every job execution leaves a run row with retrievable output, visible in the web UI.
- The web UI runs via `enso web`, reachable at `http://localhost:<port>` and, when
  deliberately bound there, over the tailnet.
- Existing chat, jobs, and messaging behaviour are unchanged.

## Future ideas (explicitly out of v1)

Editing external "parent" skills in place (v1 keeps them read-only); run output streaming
(live tail) rather than post-hoc; a proper auth layer if Enso ever goes multi-operator;
full-text search over runs.
