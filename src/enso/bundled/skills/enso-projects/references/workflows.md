# Configure or recover a workflow

Inspect stages, active tasks, jobs, worktrees, and evidence before changing the flow.
Confirm actual validation commands. Choose the smallest useful flow: one unchecked `work`
stage is valid; repositories, checks, reviews, and integration are optional. For code, start
from `enso workflow init KEY --preset dev --lint CMD --test CMD`: `build → review → qa → merge`,
where a second model reviews, a person tries the task's worktree, and Enso merges it.

Define `workflow: 2`, initially `enabled: false`, with ordered `paths` and explicit stage
`inputs`, `output`, `instructions`, permitted `routes` and `return_to` destinations.
Put acceptance requirements in stage checks, executor instructions in stage jobs, and reactions
to task moves in lifecycle hooks. Use `enso-jobs` for job definitions: agent stages
declare `workflow: 2` and `agent`; command/integration stage jobs declare `workflow: 2` and omit both `agent` and `command` because
`PROJECT.md` owns execution. Do not replace missing validation with an always-passing command.
Only ready work belongs in the first agent stage; keep open decisions in `backlog`. Ask for
short handoffs written for whoever reads them next.

## Checks and worktrees

Checks pass by command success, never by an agent's claim. Evidence is tied to the candidate,
specification, and workflow; changes invalidate it. Use finite repair/return budgets. Edits
to existing tests or check files need a person's approval before they land: read the diff,
then `enso workflow approve-rules REF --message "why it is sound"`, ideally during QA.
Integration rebases, rechecks, and lands the recorded candidate; it never pushes or deploys.

Enable worktrees only where stages need repository files. They default to `.worktrees/<REF>`
beside `PROJECT.md`, outside the repository; keep that unless there's a reason, and exclude
`.worktrees/` from anything that archives the Enso home. Configuration changes do not relocate
or retarget existing tasks. Worktrees do not isolate ports, databases, or services; use a job
concurrency group for shared resources. Its lock starts after the gate. Project
`max_concurrency` separately limits task execution. Preserve retained worktrees/branches
when cleanup fails.

## Lifecycle and recovery

`setup` must tolerate retries in a retained directory. Transition hooks run after each move,
including blocks; `teardown` runs before cleanup. Known failed deliveries can retry;
interrupted deliveries become `uncertain` and require a person's receipt or explicit retry
via `workflow resolve-event`. Failed hooks do not undo moves; deduplicate external effects with `ENSO_EVENT_ID`. Hooks must not
recursively move tasks.

Tell people when work needs them with `after:blocked` and a hook on each human stage; the
development preset's `notify.sh` sends one line for both. Every stage run that is not accepted
blocks its task, and while `after:blocked` exists the stage job sends no alert of its own.
Hooks get the reason in `ENSO_MESSAGE` and, for a block, `ENSO_BLOCK_KIND`: `decision`,
`approval`, or `failure`.

Before replacing a workflow, stop admissions, drain runs, and inspect task stages, return
destinations, jobs, scripts, and worktrees. Preserve history, retained work, and disabled
backups produced by migration.

Legacy definitions/jobs/tasks remain inactive until explicitly replaced or adopted. Do not
automatically translate or enable them. After authoring a replacement, use `workflow enable`
and separately enable the intended stage jobs.

Run `enso config check`, inspect generated jobs, and exercise failure, repair, acceptance,
and retained-worktree behavior before enabling a broad queue. Inspect durable outcomes with
`enso workflow show REF --json`. `verify`, `retry`, and `approve-rules` are explicit operator
actions; diagnose the failure before using them.
