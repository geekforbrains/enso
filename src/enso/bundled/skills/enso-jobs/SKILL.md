---
name: enso-jobs
description: Create, inspect, test, change, pause, or troubleshoot Enso jobs for standing recurring responsibilities and task-stage workers. Finite future actions and specific situations followed until resolved belong to enso-heartbeat.
---

# Jobs

Use a job for an ongoing responsibility: daily backups, every incoming email from someone,
or a recurring digest. An end date does not turn that responsibility into a beat. For one
email tomorrow or a particular thread followed through agreement, load `enso-heartbeat`.
Handle work ready to finish now in the conversation or its tracked task.

If a recurring request has an end date, enforce that cutoff in its prerun and arrange to
disable the job when its period ends. `JOB.md` has no expiry field; do not invent one.

## How Enso sets it up

A job is `$ENSO_HOME/workspaces/<workspace>/jobs/<job>/JOB.md` (`~/.enso` is the default home): YAML frontmatter plus a prompt, beside any prerun and postrun scripts. The Enso service reads every `JOB.md` and the current valid configuration once a minute. Job edits take effect on the next scheduler tick without a restart; a running job keeps its original definition and configuration. The `enso` skill covers configuration reloads and their restart exceptions. Enso runs enabled jobs whose cron slot has passed, or whose stage has a task ready, in their containing workspace, with the provider, model, and effort it declares. A run is the same agent under the same `AGENTS.md` and skills as a chat turn; only nobody is waiting. Each trigger starts fresh; postrun can request up to two follow-up messages in that same session by default. Every run leaves a row in `enso.db`. Failures alert the job's `notify` target (else the transport's default), and success is silent unless the prompt sends a message itself.

A job's `notify` only routes runner-generated alerts; it does not become the destination of commands inside the prompt. Scheduled jobs have no originating conversation, so an untargeted `enso message send "text"` uses the first configured transport's notify target. To send a successful result somewhere specific, name it explicitly: `enso message send --to slack:C0BP5BQF6UF "text"`.

Use `<workspace>:<job>` for every job reference, for example `team:digest`.
`job create` selects its workspace from `--workspace` or `ENSO_WORKSPACE`; neither defaults
to `default`. Job and run lists currently cover the installation. `ENSO_JOB` contains
the qualified reference, and job and concurrency-group locks live in `runtime/locks/`.

Names beginning `enso-` are reserved for jobs Enso installs; `enso workspace audit` warns about one it did not. The bundled jobs are `default:enso-audit`, which reports nightly health problems and fixes nothing, and `default:enso-update`, which checks for releases and notifies without invoking a model or installing updates.

Each workspace also has `<workspace>:enso-memory`, enabled hourly by default to refine its own
captured conversations using `enso-memory`. Its hooks pin one batch, validate the result,
and skip the provider when quiet. Customize its agent or schedule normally; writing
preferences belong in the memory skill.

## Workflow

1. `enso job list` to see jobs in `ENSO_WORKSPACE`; select another with `--workspace NAME` or use `--all-workspaces` for the installation. Run lists use the same scope. `enso config show` lists providers and models, and `enso config check` validates configuration.
2. `enso job create --name "Name" --provider claude --model sonnet --effort high --schedule "0 9 * * *" --workspace default` scaffolds a disabled job.
3. Write the prompt in the `JOB.md` body; add a prerun script if the job should gate itself or gather data, and a postrun script to check completion, request a correction, or process the output.
4. `enso job show <workspace>:<job>` to check the definition without running it. Check shell syntax with `bash -n` and test scripts against fixtures or stub services where real effects would be premature.
5. Use `enso job run <workspace>:<job>` only when executing its actions now is safe and within the request. It runs even when disabled; it is not a dry run. A request for a scheduled send or publication does not by itself authorize sending or publishing during setup.
6. Once validation and appropriate testing pass, set `enabled: true`. The scheduler picks it up on its next minute tick. Report what was tested and any live execution left untested.

`enso job show <workspace>:<job>` prints the fields, problems, next and last run, and the prompt. `enso runs list [--job WORKSPACE:JOB]` and `enso runs show ID` read run history; final output, hook errors and ordered attempts are kept in the database. `runs show --json` adds an `attempts` array with each turn and postrun result; `runs list --json` keeps the final run rows without attempt bodies.

## JOB.md

```markdown
---
name: Meteor Forum Watch      # required
schedule: "0 14 * * *"        # required: five-field cron, local time
provider: claude              # required for agent stages; omit for command/integration
model: sonnet                 # required with provider
effort: high                  # required with provider
project: EN                   # optional, with stage: a stage job for that project
stage: todo                   # optional, with project: one of its agent stages
concurrency_group: project-x  # optional: serialize provider work and postrun checks
enabled: true                 # required
prerun: prerun.sh             # optional: gate, run with bash from the job directory
prerun_timeout: 300           # optional, default 120
postrun: postrun.sh           # optional: check/reaction, run with bash from the job directory
postrun_timeout: 120          # optional, default 120 per invocation
max_followups: 2              # optional, default 2 extra turns; 0 means check without retry
timeout: 1200                 # optional, default 900
notify: C0BP5BQF6UF           # optional: runner alerts only; slack:C…, telegram:<id>, or bare id with one transport
catch_up: false               # optional: run a missed slot late (default false)
misfire_grace_seconds: 300    # optional: how late a slot may still fire
---

The prompt. {{prerun_output}} is replaced with the prerun's stdout.
```

Provider, model, and effort are validated against `config.json`; effort follows the provider's rules, with a log line when it changes, and Antigravity reports the level embedded in its model id even when the job requests less. Schedules use the machine's local time.

The frontmatter is one YAML mapping and those fields are all of it. Nothing is guessed or coerced: the block must be valid YAML, each key may appear only once, an unrecognized field is a problem, required fields must be present (`schedule` becomes optional on a stage job, and command/integration stages omit the provider triple), text fields must be non-empty text, `enabled` and `catch_up` must be YAML booleans, the three timeouts and `misfire_grace_seconds` must be positive YAML integers, and `max_followups` must be a nonnegative YAML integer, all written without quotes. A present-but-empty field is a problem rather than an omission. Every problem in a file is reported together, including what `config.json` says about the fields it did get right, and a file with any of them never runs.

Quote anything YAML would read as something other than text, and leave real numbers unquoted:

```yaml
name: "Daily: Review"     # an unquoted colon makes the line invalid YAML
schedule: "*/15 * * * *"  # a leading * is a YAML alias
model: "4.5"              # unquoted it is the number 4.5, not a model name
notify: "12345"           # a bare Telegram id is an integer
timeout: 600              # never quoted: this one really is a number
```

Fix a reported problem by editing the file; `enso job show <workspace>:<job>` and `enso doctor` name the line and column of a syntax fault, and never quote the block back at you. Enso never rewrites a `JOB.md`.

Cron is exactly five fields, `minute hour day-of-month month day-of-week`. `0 9 * * *` daily at 09:00, `30 6 * * 1-5` weekdays at 06:30, `*/15 * * * *` every 15 minutes, `0 9 * * 1` Mondays at 09:00. Enso checks jobs once a minute, so a sixth seconds or year field and aliases such as `@daily` are rejected. An invalid schedule stops `job create` before it writes anything, and in an existing `JOB.md` it is a problem that `job show`, `enso doctor` and the web viewer report against that file; edit the file by hand, since Enso never guesses what the schedule meant.

## Stage jobs

A job with `project` and `stage` serves one agent stage of a project on the task board (see `enso-projects`): `project` must be a configured key and `stage` one of its agent stages, never a human one. Both or neither. Its second trigger is work being ready: each minute Enso checks whether an unclaimed task waits in that stage and fires only then, so idle polling leaves no run row. Keep `schedule` to restrict when that may happen; a slot with nothing ready is skipped silently. `enso job run` on a stage job records `no_work` when nothing waits. Project `max_concurrency` limits concurrent task executions; an explicit `concurrency_group` separately serializes jobs that share a resource. Different stages can work on different task worktrees concurrently.

```bash
enso job create --name "Enso todo" --provider claude --model opus --effort high \
  --workspace dev --project EN --stage todo      # --schedule is optional here
```

When it fires, Enso claims the readiest task, prepares a worktree for a repo project, and writes a `[Task …]` block ahead of the prompt: the spec, the stage and its moves, the working directory, the last handoff, and the repository's own instructions. The prompt is the stage's instructions: what done means here and what to check, with `{{prerun_output}}` still substituted. An advance or return submits a handoff; the agent finishes its turn, then Enso runs the stage checks and accepts the transition. The claim stays held through checking and repair. A failed required check prevents advancement, even if the provider says it succeeded. Command and integration stages run without a provider. Use `enso-projects` for project, task, and workflow operations.

## Prerun scripts

Use a prerun when a recurring job should gate itself or gather context. Stage jobs already wait for ready tasks; do not add a second gate without a useful condition. A prerun runs before the provider, while real failures stay visible.

- stdout replaces `{{prerun_output}}` in the prompt; include the placeholder or the data is not passed
- exit 0: run the job
- exit 1: nothing to do; skip silently (recorded as `no_work`)
- exit 2 or more, a timeout, a missing script, or a launch failure: `prerun_error`, alerted

Print one safe line to stderr as `ENSO_ERROR: <summary>` for a useful alert; nothing else from the prerun reaches the alert. The same failure alerts once per 24 hours, and the next healthy prerun sends one recovery message. Map command failures to exit 2 deliberately: Python's default exit 1 would read as "no work".

```bash
#!/usr/bin/env bash
set -uo pipefail
if ! RESULT=$(some-command); then
  echo "ENSO_ERROR: some-command failed" >&2
  exit 2
fi
[[ -z "$RESULT" ]] && exit 1
echo "$RESULT"
```

The prerun runs once per trigger, even when postrun requests follow-ups. Its stdout is stripped and bounded to the final 1 MiB; truncation is logged. It runs from the job directory with `ENSO_JOB`, `ENSO_RUN_ID`, `ENSO_WORKSPACE`, and `ENSO_HOME` set. The provider then runs in the workspace directory with the same variables, plus `ENSO_TASK` and `ENSO_TASK_DIR` on a stage job.

## Postrun scripts

Postrun runs after each provider turn while the run row is still `running`. Its stdin is
only the latest provider output, and its exit code controls what happens next:

- **0:** finished. Do ordinary cleanup or accept the result and exit; stdout does not start
  another LLM turn.
- **10:** stdout is the next message in the same session. Print a specific explanation of
  what remains and the data needed to finish it; Enso resumes, then runs postrun again.
- **Anything else:** failed check or script. Print a safe `ENSO_ERROR: <summary>` line on
  stderr for the diagnostic, otherwise Enso uses the exit status.

`max_followups` in `JOB.md` defaults to **2 additional provider turns** (three total). Any
nonnegative integer overrides it; `0` still validates but does not permit another turn.
Only exit `10` requests follow-up work. A timeout, a missing script, or a script crash is
an error. An exit-10 request fails if stdout is empty/whitespace-only, exceeds 64 KiB, the
limit is exhausted, or a usable session is unavailable. Accepted feedback is sent verbatim,
never treated as a shell command. Enso never starts a fresh session as a fallback.

The environment is:

| Variable | Meaning |
| --- | --- |
| `ENSO_RUN_STATUS` | Current outcome: `ok`, `error`, `timeout`, `no_work`, `prerun_error`, or `skipped` |
| `ENSO_RUN_EXIT_CODE` | Latest provider exit, prerun exit when applicable, or empty |
| `ENSO_RUN_DURATION_MS` | Total elapsed time before this hook, including earlier hooks |
| `ENSO_RUN_ATTEMPT` | Current provider turn, starting at 1; 0 when no provider ran |
| `ENSO_RUN_FOLLOWUPS_REMAINING` | Extra provider turns still allowed |
| `ENSO_JOB`, `ENSO_RUN_ID`, `ENSO_WORKSPACE`, `ENSO_HOME` | Same values throughout the run |
| `ENSO_TASK`, `ENSO_TASK_DIR` | Stage jobs only: the claimed task, and its worktree on a repo project |

Postrun also reacts once to no-work, prerun failures, group collisions and provider failures
or timeouts. Those outcomes cannot start more LLM work. Branch on status when only
successful provider output should be checked. Per-job overlaps have no row or hook;
cancellation and unexpected runner errors bypass further hooks. Startup recovery never
replays an interrupted hook or resumes an old job session.

For a repository job, replace `REPO` below with the actual repository path; on a stage job
of a repo project, use `$ENSO_TASK_DIR`, the task's worktree. This checks for uncommitted
changes; add a check of the expected commit itself if that is the condition that matters.
For task pipelines, put acceptance commands in stage `checks` instead of parsing a
provider transcript in postrun. Handoff submission retains the claim until acceptance:

```bash
#!/usr/bin/env bash
set -uo pipefail
[[ "$ENSO_RUN_STATUS" == ok ]] || exit 0
REPO=/path/to/project
if ! STATE=$(git -C "$REPO" status --porcelain); then
  echo "ENSO_ERROR: could not inspect the repository" >&2
  exit 2
fi
if [[ -n "$STATE" ]]; then
  printf '%s\n' 'Review and commit the work for this job, then finish. Remaining changes:' "$STATE"
  exit 10
fi
exit 0
```

The hook may run several times. For task pipelines, put effects intended after an accepted
transition in project lifecycle hooks; postrun completion is not task acceptance. Do not call `enso job run` recursively:
the job lock is still held. Each follow-up keeps the original session, workspace, agent,
permissions and run ID; prerun data and prior context are already in that session. Each
new scheduled or manual trigger starts fresh.

Each hook runs from the job directory and has its own `postrun_timeout`. All provider turns
share `timeout` as a combined provider execution allowance; hook time does not reset or
consume it. Final run duration includes hooks, so it can exceed the provider allowance.

A failed check changes `ok`, `no_work` or `skipped` to `error`; an existing error, timeout
or prerun failure retains its primary status and adds `postrun_error`. A successful hook
cannot erase a provider failure. Manual runs return exit 1 for failed checks. Scheduled
runs alert on final failure, not on successfully repaired intermediate requests. Final
`postrun_error` and each attempt's output, session ID, hook exit, stdout and diagnostic are
stored in history; reaction hooks without a provider use attempt 0. Treat that history as
sensitive job data.

## Behaviour to know

- One instance per job: an overlapping trigger is skipped, not queued. Jobs sharing a
  `concurrency_group` run their preruns independently but hold the group lock through provider
  turns and postrun checks; a collision is `skipped` unless its reaction hook fails. Other jobs run in parallel.
- A job missed by more than `misfire_grace_seconds` (the machine was asleep) is skipped unless `catch_up: true`.
- `enso job run` never alerts; it prints the result and exits 1 unless the status is `ok` or `no_work`.
- A job whose `JOB.md` has problems is logged once and skipped until fixed.
- Pause a job with `enabled: false`; delete its directory to remove it.
- Prefer a cheaper model for frequent simple work.
