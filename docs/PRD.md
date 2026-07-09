# Enso Web UI & Tasks — Product Requirements

> **Status: proposed.** This describes a feature set not yet built. It is the
> source of truth for the design; the specs under [`docs/specs/`](specs/) own the
> details. Nothing here has shipped — see [`CHANGELOG.md`](../CHANGELOG.md) for what has.

## Summary

Today Enso is driven entirely from chat (Telegram/Slack): you message an agent, it
works on your machine, the reply comes back to the thread. That stays. This adds a
**local web UI** — a place to *see* the system rather than converse with it — and a
new first-class concept, **Tasks**: one-off units of work the agent picks up and
completes on its own.

The web UI is a read/write dashboard for:

- **Jobs** and their **recent runs** — output, status, timing (run history does not
  exist as stored data today; this feature introduces it).
- **Tasks** — create, edit, tag, set status, attach files, and watch them get done.
- **Skills** — browse the bundled/installed skills.
- **AGENTS.md** — read (and edit) the system prompt.

There is **no chat in the web UI** — chat lives in Telegram/Slack. The web UI is for
overview, organisation, and one-off task management.

## Goals

- Give a single, glanceable view of what Enso is doing and has done — jobs, their
  runs, and open tasks — that chat can't provide.
- Make one-off work a **managed object**, not a fire-and-forget message: a task has a
  title, description, attachments, a status, and tags, and it persists until done.
- Let the agent create and manage tasks the same way it already creates jobs — via a
  skill and the `enso` CLI — so "make a task to …" from chat Just Works.
- Keep Enso **file-first**: authored intent (jobs, tasks) stays as inspectable,
  greppable, git-friendly Markdown. Only machine-generated history (runs) goes in a DB.
- Notify on completion through the **existing** chat transport — a finished task pings
  you in Telegram/Slack, wherever you already are.

## Non-goals (v1)

- **No chat in the web UI.** Conversation stays in Telegram/Slack.
- **No multi-user / accounts / login.** Enso is a personal, single-operator tool. The
  web UI binds to localhost; remote access is via Tailscale (see
  [architecture.md](specs/architecture.md)). An optional shared token is the only auth.
- **No public hosting.** This is not a SaaS; it runs on your machine next to `enso serve`.
- **No task dependencies / subtasks / due dates / recurrence.** Recurrence is what
  **jobs** are for. Tasks are one-off. Richer workflow is expressed with tags for now.
- **No rich text editor / WYSIWYG.** Descriptions are Markdown in a textarea.
- **No replacing jobs.** Jobs (scheduled, cron) and tasks (one-off, queue-drained) are
  distinct and both stay.

## Vocabulary

- **Job** — a *scheduled* background task defined by a `JOB.md` file, run on a cron by
  the scheduler. Recurring. Unchanged by this work except that its runs are now recorded.
- **Task** — a *one-off* unit of work defined by a `TASK.md` file. Has a title,
  Markdown description, attachments, a **status**, and **tags**. Drained by the
  built-in **task runner**, not scheduled by the user. See [tasks.md](specs/tasks.md).
- **Run** — one execution of a job or a task: when it started/ended, its exit status,
  and its captured output. Recorded in SQLite. See [data-model.md](specs/data-model.md).
- **Task runner** — a built-in, always-present job that claims open tasks and completes
  them. The one piece of new execution machinery, and it reuses the job pipeline.
- **Transport / notify** — the existing chat delivery layer (Telegram/Slack). Task
  completion notifications ride it, unchanged.

## Key decisions

| Decision | Choice |
| --- | --- |
| Authored intent (jobs, tasks) | **Files** — Markdown + YAML frontmatter, source of truth, edited by human and agent alike |
| Run history | **SQLite** (`~/.enso/enso.db`) for metadata; **output blobs on disk** (`~/.enso/runs/<id>.log`). The one genuinely DB-shaped, append-only dataset |
| Tasks storage | `~/.enso/tasks/<slug>/TASK.md` + `attachments/` — mirrors the jobs layout exactly |
| Task status | Fixed enum: `todo` → `in_progress` → `done`, plus `blocked` / `cancelled`. Richer/ad-hoc workflow = **tags** |
| Tags | Freeform, many per task; double as projects/labels. No built-in tags |
| Task execution | A **built-in job** (the task runner) drains tasks through the existing job pipeline — no second execution path |
| Frontmatter | Real YAML (`pyyaml`), via a shared parser used by both jobs and tasks (replaces the current regex parser) |
| Web server | **Starlette + Uvicorn + Jinja2 + HTMX**, running as a coroutine *inside* `enso serve`'s event loop, sharing the `Runtime` |
| Web access | Bind **localhost** by default; Tailscale for remote; optional bearer token. No login |
| Web capability | **Read/write, scoped to `~/.enso/`** — full CRUD on tasks, jobs, and Enso's own skills; upload attachments; run-now; edit AGENTS.md. Skills discovered outside `~/.enso/` are read-only |
| Notifications | Reuse `transport.notify` / `enso message send`; completion pings your existing chat |
| Agent authoring | A bundled `tasks` skill + `enso task` CLI, mirroring how `jobs` works today |

## Personas

One persona: the operator. Runs Enso as a personal service on their own machine, talks
to it from their phone via Telegram/Slack, and wants a laptop/desktop (or
phone-over-Tailscale) surface to organise one-off work and audit what the agents have
been doing — without turning Enso into a hosted product.

## Features

### F1 — Run history

- Every job and task execution records a **run** row in SQLite: kind (`job`/`task`),
  name, trigger (`schedule`/`manual`/`task-runner`), start/end times, exit code,
  status (`running`/`ok`/`error`/`timeout`), and a pointer to its output log on disk.
- Captured output is written to `~/.enso/runs/<run_id>.log`; the row stays lean.
- A run is created at **spawn** (status `running`) and finalised at exit, so a crash
  mid-run leaves a visible `running` row rather than nothing.
- Retention: a configurable cap prunes old runs (and their logs) so history doesn't
  grow without bound. See [data-model.md](specs/data-model.md).
- Surfaced in the web UI (per job, per task, and a global recent-runs feed) and via
  `enso runs list` / `enso runs show <id>`.

### F2 — Tasks as files

- A task is a directory `~/.enso/tasks/<slug>/` containing `TASK.md` (YAML frontmatter
  + Markdown description body) and an optional `attachments/` folder.
- Frontmatter: `title` (required), `status`, `tags`, `notify`, optional `provider`/
  `model` overrides, and `created`/`updated` timestamps. Body = the description.
- Attachments live in `attachments/`; the agent reads them as ordinary files by
  absolute path. Frontmatter may annotate them (captions) but the folder is authoritative.
- Files are the **source of truth**: the web UI, the `enso task` CLI, and the agent all
  read and write the same `TASK.md`. Writes are atomic (temp-file + rename).
- Full format in [tasks.md](specs/tasks.md); schema in [data-model.md](specs/data-model.md).

### F3 — The task runner (built-in job)

- A built-in, always-present job drains open tasks. On each tick it **claims** the
  oldest `todo` task (flipping it to `in_progress` *before* the agent starts, so a
  crash never re-claims it), injects the task's title/description/attachment paths into
  the prompt, and runs it through the normal job pipeline.
- The agent does the work, then sets `status: done` (or `blocked` with a reason). If the
  task's `notify` is true, it pings the operator's chat via `enso message send`.
- Default: one task per run (clean per-task run records, bounded runtime); batch size and
  schedule are configurable. Concurrency is guarded exactly like user jobs.
- Reuses the entire existing execution path — scheduler, semaphore, timeout, notify. See
  [tasks.md](specs/tasks.md).

### F4 — Agent-authored tasks

- A bundled **`tasks` skill** (parallel to the `jobs` skill) plus `enso task create/list/
  run/show` teach the agent to create and manage tasks itself.
- "Make a task to research X and attach the PDF" from chat results in a real `TASK.md`
  the operator later sees in the web UI.
- AGENTS.md gains a Tasks section pointing at the skill.

### F5 — Web UI: dashboard & runs

- `/` — overview: recent runs feed, open-task counts by status, jobs at a glance.
- `/runs/<id>` — a single run's full output, status, timing, and what triggered it.
- Read-only views; the data comes from SQLite (runs) and the file scans (jobs/tasks).

### F6 — Web UI: tasks (read/write)

- `/tasks` — board/list view, filterable by status and tag (tags act as project lanes);
  a "New task" action.
- `/tasks/new` — create form: title, Markdown description, tags, notify toggle,
  attachment upload.
- `/tasks/<slug>` — detail/edit: change title/description/status/tags/notify, add/remove
  attachments, **Run now**, and this task's run history.
- All mutations write back to `TASK.md` / `attachments/` — the same files the agent reads.

### F7 — Web UI: jobs (full CRUD)

- `/jobs` — list with schedule, provider/model, enabled state.
- `/jobs/<name>` — frontmatter, prompt, prerun, recent runs, **Run now**, enable/disable.
- **Create, edit, and delete** jobs from the UI: name, schedule, provider, model, enabled,
  timeout, notify, the prompt body, and the optional prerun script — all writing to
  `~/.enso/jobs/<name>/`. Delete requires confirmation (destructive).

### F8 — Web UI: skills & AGENTS.md

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

- Sending "make a task to …" from Telegram/Slack produces a `TASK.md` that appears in the
  web UI within one task-runner tick, and — once done — the agent flips it to `done` and
  (if asked) notifies the operator in chat.
- Creating a task from the web UI, marking `notify`, and hitting nothing else results in
  the task being completed and the operator pinged — no chat interaction required.
- Every job/task execution leaves a run row with retrievable output, visible in the web
  UI and via `enso runs`.
- The web UI runs inside `enso serve` with no separate process to manage, reachable at
  `http://localhost:<port>` and over the tailnet.
- Existing chat, jobs, and messaging behaviour are unchanged.

## Future ideas (explicitly out of v1)

Task templates; recurring tasks that spawn from a job; per-task provider/effort presets in
the UI; task comments/activity log; editing external "parent" skills in place (v1 keeps
them read-only); run output streaming (live tail) rather than post-hoc; multiple notify
destinations per task; a proper auth layer if
Enso ever goes multi-operator; export/import of tasks; full-text search over runs and tasks.
