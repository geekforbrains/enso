# Work a task

A `[Task …]` block owns the run's scope, working directory, and available moves. On recovery,
inspect its timeline and existing work before continuing.

- `advance` submits completed stage work; `return` requests earlier-stage rework.
- `block` records what must change before progress; `--after REF` identifies a dependency.

Advance/return submit a handoff; Enso moves the stage only after execution and checks pass.
State what changed, attach evidence, say what the next stage should verify, then end the
turn. If a check requests repair, fix the failure and resubmit. Inside a stage run, never
drop a task, use `--force`, release it for another attempt, or move another task; block
with the reason instead.

Use the Task block's worktree and target. Never edit, commit, or switch branches in the
main checkout. Commit task changes and leave tracked files clean before advancing; let
configured integration land them. Do not force-delete retained worktrees or branches.

## Evidence and board changes

Inspect with `enso task list` and `enso task show REF`; use `enso workflow show REF --json`
for acceptance/check history. Attach commits, paths, and URLs with `enso task ref REF KIND
VALUE` or `advance --ref KIND:VALUE`. Use `enso task note REF TEXT --attention` when a person
needs to look; omit `--attention` for ordinary context.

Record out-of-scope work separately with `enso task add TITLE --project KEY --from REF`;
use `--backlog` unless ready and `--after REF` for a dependency. Outside stage runs, tasks
can be edited, resumed, or dropped. A live claim cannot be forced: wait for or stop its run.
