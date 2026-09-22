# Configure or recover a workflow

Inspect stages, active tasks, jobs, worktrees, and evidence before changing the flow.
Confirm actual validation commands. Choose the smallest useful flow: one unchecked `work`
stage is valid; repositories, checks, reviews, and integration are optional. The development
preset adds planning, checked implementation, review, and engine-run integration.

Put acceptance requirements in stage checks, agent instructions in stage jobs, and reactions
to accepted transitions in lifecycle hooks. Use `enso-jobs` for job definitions: agent stages
declare `agent`; command/integration stages omit both `agent` and `command` because
`PROJECT.md` owns execution. Do not replace missing validation with an always-passing command.

## Checks and worktrees

Checks pass by command success, never by an agent's claim. Evidence is tied to the candidate,
specification, and workflow; changes invalidate it. Use finite repair/return budgets. When
rules intentionally change, review and approve the inputs instead of weakening checks.
Integration rebases, rechecks, and lands the recorded candidate; it never pushes or deploys.

Enable worktrees only where stages need repository files. Choose the target and root
deliberately: configuration changes do not relocate or retarget existing tasks. Worktrees
do not isolate ports, databases, or services; use a job concurrency group for shared
resources. Its lock starts after the gate. Project `max_concurrency` separately limits
task execution. Preserve retained worktrees/branches when cleanup fails.

## Lifecycle and recovery

`setup` must tolerate retries in a retained directory. Transition hooks run after acceptance;
`teardown` runs before cleanup. Failed transition hooks do not undo moves and may repeat;
deduplicate external effects with `ENSO_EVENT_ID`. Hooks must not recursively move tasks.

Before replacing a workflow, stop admissions, drain runs, and inspect task stages, return
destinations, jobs, scripts, and worktrees. Preserve history, retained work, and disabled
backups produced by migration.

Run `enso config check`, inspect generated jobs, and exercise failure, repair, acceptance,
and retained-worktree behavior before enabling a broad queue. Inspect durable outcomes with
`enso workflow show REF --json`. `verify`, `retry`, and `approve-rules` are explicit operator
actions; diagnose the failure before using them.
