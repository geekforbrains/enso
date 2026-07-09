# Tasks

One-off units of work the agent picks up and completes on its own. This spec owns task
**behaviour** — lifecycle, the task runner, claiming, notification, and how the agent
authors tasks. The file format and schema live in [data-model.md](data-model.md); how it
all fits the runtime is in [architecture.md](architecture.md).

## Tasks vs jobs

| | **Job** | **Task** |
| --- | --- | --- |
| Cadence | Recurring, cron-scheduled | One-off |
| Defined by | `~/.enso/jobs/<name>/JOB.md` | `~/.enso/tasks/<slug>/TASK.md` |
| Trigger | The scheduler, on schedule | The task runner, when `status: todo` |
| Lifespan | Runs forever on its cron | Runs until `done`/`cancelled`, then rests |
| State | `enabled` on/off | `status` through a lifecycle |
| Organisation | — | `tags` (projects/labels) |

They share the *execution* pipeline (provider subprocess, timeout, notify, run
recording) but model different intents. Recurrence is a job; a discrete "do this thing
once" is a task. The user never puts a task on a cron — that is what jobs are for.

## Lifecycle

```
        create
          │
          ▼
        todo ──────claim──────▶ in_progress ───done───▶ done
          │                        │
          │                        ├──blocked──▶ blocked ──(operator edits)──▶ todo
          │                        │
          └────cancel───▶ cancelled◀────cancel──┘
```

- **todo** — open and unclaimed. The only claimable state.
- **in_progress** — claimed; set by the runner *before* the agent starts (crash-safe
  claim). If the process dies mid-run the task stays `in_progress` and is visible in the
  UI for manual retry — it is never silently re-run.
- **blocked** — the agent could not finish and needs operator input; it writes
  `blocked_reason`. Not re-claimed until the operator moves it back to `todo`.
- **done** — completed. Terminal.
- **cancelled** — abandoned by the operator. Terminal; never runs.

Transitions are just `TASK.md` frontmatter rewrites (atomic), performed by the runner,
the agent, the `enso task` CLI, or the web UI — all editing the same file.

## The task runner

A built-in, always-present job (see [architecture.md](architecture.md) § The task
runner) that drains open tasks through the normal job pipeline. It is registered by the
runtime, gated on `tasks.enabled`, and scheduled by `tasks.schedule` (default every 5
minutes).

### One tick

1. **Gate.** Scan `~/.enso/tasks/` for tasks with `status: todo`. None → the run is a
   no-op (nothing spawned, no run row), exactly like a job prerun exiting non-zero.
2. **Claim.** Take the oldest `todo` (by `created`), rewrite its frontmatter to
   `status: in_progress`, bump `updated`. This write happens **before** any agent
   spawns, so a crash or an overlapping tick can never double-claim it.
3. **Build the prompt.** Inject the task's title, description, attachment absolute paths,
   and its slug into a bundled prompt template (below).
4. **Run.** Spawn the provider (the task's `provider`/`model` override, else the
   `tasks` config default) through `_execute_job`. A **run** is recorded with
   `kind='task'`, `name=<slug>`, `trigger='task-runner'`.
5. **Complete.** The agent sets `status: done` (or `blocked` + `blocked_reason`) by
   editing `TASK.md`, and — if `notify` is true — calls `enso message send` to ping chat.

`batch` (default 1) controls how many tasks a single tick claims. One-per-tick keeps each
run mapped to exactly one task (clean history, bounded runtime); raise it to drain a
backlog faster. The scheduler's existing "already running" guard prevents the runner from
overlapping itself, so a long task simply defers the next drain to the following tick.

### Prompt template (bundled)

The runner injects something like:

```
You are completing a one-off task. When finished, set its status.

Task: {title}
Slug: {slug}
Tags: {tags}
Attachments:
{attachment_paths}

--- Description ---
{description}
--- End description ---

When done, edit ~/.enso/tasks/{slug}/TASK.md frontmatter: set `status: done`
(or `status: blocked` with a `blocked_reason:` if you need input), and bump
`updated`. {notify_clause}
```

`{notify_clause}` is present only when `notify: true`:
_"This task requests notification — when finished, run `enso message send` with a short
summary (and `enso message attach` for any files) so the operator hears about it in chat."_

The agent already knows how to edit files and call `enso message send`; the task runner
adds no new tool surface, only an instruction.

### Claiming & safety

- **Atomic claim.** `status: todo → in_progress` is an atomic `TASK.md` rewrite done
  before spawn. The runner re-reads the file at claim time and skips it if it is no longer
  `todo` (someone else — the web UI, a hand edit — moved it).
- **No overlap.** The built-in job carries the same `_running_job_tasks[dir_name]` guard
  as user jobs, so two ticks never process concurrently.
- **Crash visibility.** A task stuck in `in_progress` (agent died, machine rebooted) is a
  visible signal in the UI, not a silent loss. The operator moves it back to `todo` to
  retry. (A future enhancement could auto-reap `in_progress` tasks with no active run and
  a stale `updated`, but v1 keeps it manual and honest.)
- **Failure.** If the agent process exits nonzero or times out, the run is recorded
  `error`/`timeout` and the task is left `in_progress` (not auto-`blocked`) — the run log
  holds the detail; the operator decides. Job-style failure notification still fires.

## Notification

`notify` reuses the existing chat delivery path — no new channel:

- `notify: true` → on completion the agent runs `enso message send` (and optionally
  `enso message attach`), which posts to the operator's Telegram/Slack via the configured
  transport and `notify_channel`, and queues the text as background context for the next
  chat turn.
- `notify: false` (default, or `tasks.notify_default`) → the task completes silently; the
  operator sees it in the web UI on their next visit.

This is intentionally the same mechanism jobs use for their own messaging, so "notify me
when this task is done" needs no special infrastructure — the completion ping travels the
road that already exists.

## Agent-authored tasks

Creating tasks is a first-class agent capability, mirroring how the `jobs` skill lets the
agent create jobs.

### CLI (`enso task`)

```bash
enso task create --title "Research OFX parsers" [--tags fettia,research] [--notify]
                 [--provider claude] [--model sonnet]   # scaffolds tasks/<slug>/TASK.md
enso task list [--status todo] [--tag fettia]           # list/filter
enso task show <slug>                                    # print frontmatter + description
enso task run <slug>                                     # run one now (records a manual run)
enso task status <slug> <status> [--reason "..."]        # move through the lifecycle
```

`enso task create` scaffolds the directory and a `TASK.md` (status `todo`) with the body
left for the agent/operator to fill — the same shape as `enso job create`. Attachments are
added by writing into `attachments/` (the agent) or via upload (the web UI).

### The `tasks` skill

A bundled skill (`src/enso/skills/tasks/SKILL.md`), parallel to the `jobs` skill,
teaching the agent: when a request is a one-off ("research X", "draft Y", "process this
file") vs recurring (→ a job); how to scaffold a task, write a good description, drop
attachments, and set `notify`; and that it should **not** put one-off work on a cron.

### AGENTS.md

The system prompt gains a **Tasks** section next to the existing Background Jobs section:
a short pointer that tasks are for one-off work, jobs are for recurring work, and to use
the `tasks` skill when creating them — so "make a task to …" from chat routes correctly.

## Web UI

The task surfaces (board, create, detail/edit, run-now, attachments) are specified in
[web.md](web.md). All web mutations write back to the same `TASK.md` / `attachments/`
files described here — the web UI, the CLI, and the agent are three doors into one
file-based model.
