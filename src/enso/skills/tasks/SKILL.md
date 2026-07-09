---
name: tasks
description: Create and manage one-off tasks for Enso to complete on its own. Use when the user wants to hand off a discrete piece of work to be done later ("make that a task", "add a task to…", "track this", "can you take care of X later"), as opposed to a recurring job or something to do right now.
---

# Tasks

A **task** is a one-off unit of work that Enso's task-runner completes on its own,
in the background — not something the current session does now. Tasks are files
under `~/.enso/tasks/<slug>/TASK.md` and are drained automatically by the
task-runner (every few minutes) while their status is `todo`.

## Task vs job vs do-it-now

- **Do it now** — the user wants the answer in this conversation. Just do it.
- **Task** (this skill) — a discrete, one-off piece of work for later / the
  background ("make that a task", "add a task to research X"). One-shot.
- **Job** (the `jobs` skill) — recurring work on a schedule ("every morning…",
  "check X hourly"). A task is not a job.

## Making a task

```bash
enso task create --title "Short title" --description "Self-contained instructions" \
  [--tags project,label] [--notify] [--provider claude] [--model sonnet]
```

**The description must stand on its own.** The task-runner spawns a *fresh* agent
with **none of this conversation's context**, so put everything it needs into the
description: what to do, the relevant links/paths/names, what "done" looks like,
and where any output should go. If the user said "make that a task" about
something discussed above, summarise that discussion into the description — the
future agent cannot see this chat.

- `--notify` — ping the user in their chat (Telegram/Slack) when the task
  finishes. A **blocked** task always notifies, regardless of this flag.
- `--tags` — freeform labels; they double as projects/filters in the web UI.
- Attachments: drop files into `~/.enso/tasks/<slug>/attachments/` — the runner
  hands their paths to the agent.

New tasks start as `todo` and the runner picks them up automatically. Don't run
them yourself unless the user asks you to do it immediately.

## Lifecycle (managed for you)

`todo → in_progress → done` (or `blocked`). The runner claims the oldest `todo`
task, flips it to `in_progress`, does the work, then records the outcome:

- **done** — with a `result` summarising what happened (shown on the task).
- **blocked** — with a `reason`; the user is always notified in chat.

`cancelled` is a manual state the user sets; you generally don't set it. The user
can move a task to any state by hand.

## Managing tasks

```bash
enso task list [--status todo] [--tag project]   # see tasks
enso task show <slug>                              # full detail + result
enso task status <slug> <state> [--reason "…"] [--result "…"]
enso task run <slug>                               # run one immediately
```

Statuses: `todo`, `in_progress`, `blocked`, `done`, `cancelled`.

## Examples

User: "I keep meaning to compare our OFX parser options — make that a task."

```bash
enso task create --title "Compare OFX parser libraries" --tags fettia,research \
  --description "Compare Go OFX libraries for parsing 1.x (SGML) and 2.x (XML)
bank statements. List the main options, note trade-offs (maintenance, spec
coverage, API ergonomics), and recommend one. Write the recommendation as the
task result."
```

User: "Draft a reply to that email and let me know when it's ready."

```bash
enso task create --title "Draft reply to Jane's Q3 email" --notify \
  --description "Draft a reply to Jane's email about the Q3 timeline (Gmail,
subject 'Q3 timeline'). Warm and concise; propose the 15th. Put the draft text
in the task result."
```
