# Tasks

## Working a task

A `[Task …]` block is the authoritative context for that run. Work only on its task and
in its working directory. On recovery, inspect the timeline and existing work before
continuing.

Use the move offered by the block:

- `advance` when the stage's work is complete.
- `return` when an earlier stage must redo something.
- `block` when a decision, dependency, or external problem prevents progress. Say what
  unblocks it; use `--after REF` when it depends on another task.

During a run, `advance` and `return` only submit a handoff. Enso changes the stage after
the run ends and any checks pass. State what changed, attach evidence, say what the next
stage should verify, then end the turn. If a check fails, fix that failure and resubmit.

In a stage run, never drop a task, use `--force`, release it for another attempt, or move
another task. Block with the reason instead.

## Evidence and scope

Attach commits and useful paths, URLs, or pages with `enso task ref`, or with `--ref` when
advancing. Use `enso task note` for useful context and `--attention` when a person needs
to look.

Create a separate task for work outside the current scope, using `--from REF`. Put it in
the backlog unless it is ready; use `--after REF` if it depends on the current task.

## Repository tasks

Use the worktree and target named in the Task block. Never edit, commit, or switch branches
in the main checkout. Commit task changes and leave tracked files clean before advancing.
Let a configured integration stage land the work. Do not force-delete retained worktrees
or branches.

## Managing the board

Use `enso task list` to find work and `enso task show REF` to inspect a task. Outside a
stage run, tasks can be added or edited, blocked tasks resumed, and unfinished tasks
dropped. A live run's claim cannot be forced; wait for or stop the run first.

Use `enso workflow show REF --json` for the durable transaction and check history. Task
commands use the selected workspace; broaden a list with `--all-workspaces` only when the
request calls for it.
