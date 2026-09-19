# Workflows

[Projects](projects.md) covers project ownership, files, and scripts.

## Design

Before changing a workflow, inspect its stages, active tasks, stage jobs, worktrees, and
recorded evidence. Confirm the repository's real validation commands.

Choose the smallest flow that meets the need. A single unchecked `work` stage is complete;
Git, checks, reviews, and multiple stages are optional. The development preset adds
planning, checked implementation, review, and engine-run integration. A custom stage may
be handled by an agent, a person, a command, or integration.

Put executable acceptance requirements in stage checks, agent instructions in the stage
job, and reactions to accepted moves in lifecycle hooks. Do not invent a command or replace
a missing check with a command that always succeeds.

## Checks and integration

A required check passes only when its command exits successfully. A successful agent reply
is not check evidence. Check results are tied to the task candidate, specification, and
workflow; changes invalidate stale evidence.

Use finite repair and return budgets. When validation rules intentionally change, review
and approve those inputs rather than weakening checks or fabricating a pass. An integration
stage rebases, rechecks, and lands the recorded candidate against its target; it does not
push or deploy.

## Worktrees

Enable worktrees only for stages that need repository files. Choose the target branch and
worktree root deliberately; changing the configuration does not relocate or retarget an
existing task.

Worktrees isolate files, not ports, processes, credentials, databases, or services. Give
shared resources an explicit concurrency limit and make setup safe to retry. Do not remove
retained task directories or branches to hide a cleanup failure.

## Lifecycle hooks

`setup` prepares a new worktree. Transition hooks run after an accepted move, and `teardown`
runs before cleanup. A failed transition hook does not undo the move and may be delivered
again, so external effects must be idempotent or deduplicated with `ENSO_EVENT_ID`. A hook
must not move the task recursively.

## Migration and recovery

Before replacing a workflow, stop new admissions, drain active runs, and inspect existing
task stages, return destinations, jobs, scripts, and worktrees. Migration preserves task
history and retained work while keeping replaced definitions as disabled backups. Do not
delete those backups or user files as cleanup.

After configuration, run `enso config check`, inspect every generated job, and exercise a
failure, repair, acceptance, and retained worktree before enabling a broad queue. Use
`enso workflow show REF --json` for the recorded outcome. `verify`, `retry`, and
`approve-rules` are explicit operator actions; diagnose the failure before using them.
