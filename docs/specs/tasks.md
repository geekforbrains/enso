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
5. **Complete & reconcile.** The agent reports the outcome via
   `enso task status <slug> done --result "…"` (or `blocked --reason "…"`). After the agent
   exits, the runner **reconciles**: it re-reads the task and, if the agent left it
   `in_progress`, infers the terminal state from the run (clean exit → `done`, otherwise
   `blocked`) and backfills a `result` from the captured output. The runner — not the
   agent — then messages the operator (see Notification).

`batch` (default 1) controls how many tasks a single tick claims. One-per-tick keeps each
run mapped to exactly one task (clean history, bounded runtime); raise it to drain a
backlog faster. The scheduler's existing "already running" guard prevents the runner from
overlapping itself, so a long task simply defers the next drain to the following tick.

### Prompt template (bundled)

The runner injects something like:

```
# Task: {title}
{description}

Attachments:
{attachment_paths}

Complete this task now. When you finish, record the outcome:
    enso task status {slug} done --result "<concise summary / the answer>"
If you cannot complete it (missing info, needs a decision, blocked):
    enso task status {slug} blocked --reason "<what is blocking you>"
The --result / --reason text is exactly what the operator sees. Do not send any
chat message yourself — Enso notifies the operator based on the final status.
```

The agent records the outcome through the `enso task` CLI it already has; the runner owns
all messaging, so the prompt never tells the agent to notify. If the agent forgets to set
a terminal status, reconciliation (step 5) backfills one from the run's exit code.

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
- **Failure.** If the agent exits nonzero or times out without reporting a terminal status,
  reconciliation marks the task `blocked` (with a reason naming the exit code or timeout)
  and the operator is always notified. The run is also recorded `error`/`timeout` with the
  full output in the run log. (A hard crash that kills the process *before* reconciliation
  runs leaves the task `in_progress` — see Crash visibility.)

## Notification

The **runner** sends outcome notifications through the transport (`transport.notify`), so
the rule is centralised rather than left to the agent:

- **`blocked` → always notify.** A task that needs the operator's attention always pings
  chat, regardless of the `notify` flag.
- **`done` → notify only when `notify: true`** (or `tasks.notify_default`). The message
  carries the task's `result`. Otherwise the task completes silently and the operator sees
  it in the web UI.
- **`cancelled` is manual** — the operator sets it (and may move a task to any state by
  hand); the runner never sets or notifies it.

Messaging happens only in the serve process, which holds a transport; a standalone
`enso web` or a one-off `enso task run` records the outcome but sends nothing.

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
