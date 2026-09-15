---
name: enso-workflow
description: Design, configure, migrate, and troubleshoot Enso task workflows, required checks, lifecycle scripts, and Git worktree development. Use when the user wants a pipeline or a different process; use enso-tasks to execute an already configured task.
---

# Workflows

Choose the smallest workflow that meets the user's goal. Checks, Git, and multiple stages
are optional: `work → done` is a complete workflow. Once a required check is configured,
Enso runs it and refuses advancement until it passes. An agent's successful reply is not
check evidence.

## Inspect, then configure

Read `enso project list --json`, `enso job list --json`, and `enso config show` before
changing an existing process. Inspect project instructions and the repository's actual
validation commands. Use runtime `--help` for accepted syntax; never invent a package
script or silently replace an absent check with `true`.

For a new project, create its workspace and project first using `enso-workspace` and
`enso project add`. Configure the workflow with:

```bash
enso workflow init KEY --preset basic
enso workflow init KEY --preset dev --lint 'npm run lint' --test 'npm test' --base main
```

The commands are examples: select ones the project actually provides. The dev preset
requires a repository plus both commands, and creates `plan → implement → review →
integrate → done`. Plan runs in the Enso workspace without a worktree. Implementation
uses its task worktree and required checks, with two repair opportunities. Review can
return work to implementation within a finite budget. Integration runs without a model,
serializes landing, and checks the candidate against the target before accepting done.
The basic preset has no required checks and also supports non-Git work.

`--worktree-root PATH` selects the root. The default is `<repo>/.worktrees`; relative paths
are repository-relative, and absolute, `~`, or sibling locations are allowed. A task keeps
its recorded path and target across config changes. Choose the target explicitly for dev.
Check whether development tools watch nested directories: Git exclusion alone may not
prevent rebuilds. Prefer a sibling root for those repositories, and verify both independent
dependency paths and the running application's health. Change roots between active runs;
do not relocate an existing task checkout as part of a configuration edit.

For an existing pipeline, use `--migrate` after inspecting its current jobs and tasks.
The migration preserves old job files/scripts as disabled definitions and remaps familiar
`triage`/`todo` stages to `plan`/`implement`. Preserve custom instructions and useful scripts
when moving them into the new flow. Stop new admissions and drain active runs before a live
migration; retain backups and check task counts, retained worktrees, and stage mappings.
Do not remove old branches or user files merely because configuration changed.

## Customizing the flow

Project `stages` accepts strings (`"work"`, `"approve:human"`) or objects. Stage objects use
`name`, optional `worktree`, `checks`, `max_repairs`, `return_to`, and `max_returns`. Choose
at most one of `human: true`, a `command` string, or `integrate: true`; otherwise an agent
stage is served by a job. A check is `{ "name": "lint", "command": "npm run lint",
"timeout": 600 }`, optionally with repository-relative `protect` patterns. Set a repair
or return budget to zero to disallow that loop. No check format or language is required
beyond a process exit code and captured output.

Put acceptance requirements in stage checks, agent instructions in the stage's `JOB.md`,
and reactions in project `hooks`. Do not use a postrun script to claim that a task passed
its workflow. Ordinary job prerun/postrun behavior remains available through `enso-jobs`.

Project `max_concurrency` limits simultaneous task executions. Different tasks can be in
different stages; one task worktree has one execution owner. Keep an explicit job
`concurrency_group` for genuinely shared resources, such as a test database. Worktrees do
not isolate ports, processes, secrets, or services. Use per-task setup for mutable dependencies.

`setup` prepares a newly created worktree. `hooks.after_transition` and `hooks["after:done"]`
(or another stage) react to accepted task transitions; `hooks.teardown` runs before cleanup.
Commands run with bash, bounded output, and `script_timeout` (default 600 seconds).
Lifecycle context includes `ENSO_EVENT_ID`, `ENSO_TASK`, `ENSO_PROJECT`, `ENSO_FROM_STAGE`,
`ENSO_TO_STAGE`, `ENSO_TASK_DIR`, `ENSO_PROJECT_REPO`, `ENSO_BRANCH`, `ENSO_BASE`, and
`ENSO_ATTEMPT`. Delivery may repeat: make effects idempotent or deduplicate by event ID.
After-transition failure is visible and retried; it does not undo an accepted transition.
Do not recursively move the task from a lifecycle script.

## Validate and recover

Run `enso config check` and inspect each generated `enso job show NAME` before enabling it.
Use a scratch task/home for checks with side effects unless a real trial is authorized.
Exercise one failing check, a repair, acceptance, and a retained worktree; verify the web
task page shows the same outcome as `enso workflow show REF --json`. A manual `enso job run`
executes immediately even if its job is disabled.

Checks are bound to the candidate, spec, and selected workflow. Existing test or validation
rule changes may require the operator to review and explicitly `enso workflow approve-rules
REF --message "what was reviewed"`; never weaken checks just to clear a failure. New tests
are allowed. A successful command proves that command's outcome, not complete correctness.

`enso workflow verify REF --message "handoff"` lets an operator run configured acceptance
without a provider. `enso workflow retry REF --message "why another attempt is justified"`
explicitly resets an exhausted budget and retries failed lifecycle delivery; it never marks
checks passed. These are operator actions, unavailable through the supported in-run CLI.
Show the user the actual failure before proposing either. Never clear actor variables,
edit Enso's database, or fabricate check results to bypass acceptance.

Enso's controller is not an OS security boundary. An unrestricted process running as the
same account can tamper with local files and state; enforcing a hostile-agent boundary
requires provider/OS isolation and separately protected controller privileges. Preserve
that distinction when describing guarantees.
