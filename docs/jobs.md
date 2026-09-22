# Jobs

Jobs handle standing responsibilities such as nightly backups or every new email.
Use [Heartbeat](heartbeat.md) for one future action or a situation followed until resolved.

A job lives at `~/.enso/workspaces/<workspace>/jobs/<job>/JOB.md`. Its location supplies
its workspace and name; commands use the full reference, such as `meteor:forum-watch`.
The service checks jobs once a minute for a due cron slot or a ready [stage](#stage-jobs).

Choose an **agent** for work requiring judgment, or a **command** for a script that can
finish the work itself. An agent job's Markdown body is its prompt; a command job's body
is an optional description. Every agent trigger starts a fresh session. Both kinds share
scheduling, locks, secrets, timeouts, history and alerts.

Use a **gate** to skip execution when there is no work. Use **postrun** to validate or
react to the result; it can request bounded follow-up turns from an agent. A command that
handles its own no-work case needs no separate gate.

A complete command job:

```markdown
---
name: Refresh reporting data
schedule: "*/15 * * * *"
enabled: true
command: bash refresh.sh
timeout: 300
concurrency:
  group: reporting
  on_busy: wait
  max_wait: 300
---

Refresh the reporting cache atomically, retaining the previous version on failure.
```

An agent job with every common option shown:

```markdown
---
name: Meteor Forum Watch      # required
schedule: "0 14 * * *"        # required unless stage is set: five-field cron, in the machine's local time
agent:
  provider: claude            # required within agent
  model: sonnet               # required within agent
  effort: high                # required within agent
  max_followups: 2            # optional, default 2; 0 disables postrun-requested turns
project: EN                   # optional, with stage: the project this job serves
stage: todo                   # optional, with project: one of its agent stages
concurrency:                 # optional: shared execution and postrun protection
  group: meteor              # required within concurrency
  on_busy: skip              # required: skip or wait, no default
enabled: true                 # required
secrets: [GITHUB_TOKEN]        # optional: names supplied to the whole run
gate:                        # optional
  command: bash gate.sh       # required within gate
  timeout: 300                # optional, default 120
postrun:                     # optional
  command: bash postrun.sh    # required within postrun
  timeout: 120                # optional, default 120 per invocation
timeout: 1200                 # optional, default 900
notify: slack:C0BP5BQF6UF     # optional: where failure alerts go
catch_up: false               # optional: run a missed slot late (default false)
misfire_grace_seconds: 300    # optional: how late a slot may still fire (default 300)
---

The prompt. {{gate_output}} is replaced with the gate's stdout.
```

Agent jobs require `agent.provider`, `agent.model`, and `agent.effort`, validated against
`config.json`. They never inherit a default agent, but workspace provider-argument overrides
apply. Effort is normalized by the [provider's rules](configuration.md#providers), and the
run records the effective effort.

`notify` accepts `slack:C…`, `telegram:<id>`, or a bare id when only one transport is
configured. Without it, alerts use the first configured notification target (Slack before
Telegram). See [Scheduling](#scheduling) for cron rules.

`project` and `stage` must appear together and name an executable stage in the same workspace.
A stage job may omit `schedule`. Command and integration stages omit both `agent` and
`command`: their execution is defined in [`PROJECT.md`](configuration.md#projects).

## Concurrency groups

A group lets different jobs take turns using a shared resource, such as a database.
Every `concurrency` block requires nonempty `group` and explicit `on_busy: wait` or
`on_busy: skip`. There is no default policy.

```yaml
concurrency:
  group: reporting
  on_busy: wait
  max_wait: 300  # optional positive seconds, only with wait
```

`wait` joins a first-in, first-out queue shared by the scheduler and manual processes.
Without `max_wait`, it has no waiting deadline. `skip` finishes immediately if the group
is busy or another job is already queued; it cannot jump ahead of waiters. Expiring a wait
also records `skipped`. Omit `concurrency` when the job needs no shared-resource protection.

Each job keeps its own lock while waiting, so repeated triggers cannot build up copies:
at most one run of that job is pending or executing. The row remains `running` during the
wait, and recorded duration includes it. Waiting does not consume the execution timeout.
Admission rechecks the job definition before execution; a removed, changed, or newly
disabled scheduled job is skipped. Maintenance releases waiting jobs so they do not delay
an update. A service restart interrupts pending waits; the queue is not a durable backlog
that replays after restart.

The gate runs **before** group admission. Group members can run their gates concurrently;
the group protects the admitted command or agent, its postrun, follow-ups and workflow
acceptance. A skipped run may still invoke its reaction postrun without the group lock, so
that hook must not mutate the protected resource. A failed reaction can make the run an
error and send an alert; contention by itself does not alert.

Group locks release when their owner dies, and crashed waiters cannot strand the queue.
Project `max_concurrency` separately limits task executions (default 1), with one owner per
worktree. A full project skips the attempt; a group wait does not queue project capacity.

## The frontmatter

Frontmatter must be a YAML mapping with unique, recognized keys. Enso reports independent
problems together and never runs an invalid job. `job show`, `job list`, `enso doctor`, and
the [viewer](web.md) name the file and its problems; Enso does not repair job content.

- `name` and `enabled` are required; `schedule` is required unless `stage` is set.
- A standalone job requires exactly one of `agent` or `command`. Agent jobs need a prompt.
- Text fields must be nonempty strings. `enabled` and `catch_up` must be YAML booleans.
- Timeouts, `concurrency.max_wait`, and `misfire_grace_seconds` must be positive integers.
  `agent.max_followups` can also be zero.
- Nested blocks require the fields shown above. `concurrency.max_wait` is valid only with
  `on_busy: wait`.
- `project` and `stage` must name a configured project and one of its non-human stages.

Quote values that YAML would otherwise treat as syntax or another type:

```yaml
name: "Daily: Review"     # an unquoted colon makes the line invalid YAML
schedule: "*/15 * * * *"  # a leading * is a YAML alias
agent:
  provider: example
  model: "4.5"            # unquoted it is the number 4.5, not a model name
  effort: high
notify: "12345"           # a bare Telegram id is an integer
timeout: 600              # never quoted: this one really is a number
```

Syntax errors report line and column without echoing the YAML. Field errors name the
field and, where needed, its rejected value.

## Workflow

```bash
enso job create --name "Meteor Forum Watch" --provider claude --model sonnet \
  --effort high --schedule "0 14 * * *" --workspace meteor
enso job create --name "Refresh reporting data" --command 'bash refresh.sh' \
  --schedule "*/15 * * * *" --workspace meteor \
  --concurrency-group reporting --on-busy wait --max-wait 300
nvim ~/.enso/workspaces/meteor/jobs/meteor-forum-watch/JOB.md
enso job show meteor:meteor-forum-watch         # fields, problems, next and last run, prompt
enso job run meteor:meteor-forum-watch          # execute now, only when its effects are intended
```

`job create` writes a disabled scaffold under a slug of the name and refuses invalid
schedules. `--workspace` overrides `ENSO_WORKSPACE`; an existing workspace is required.
Show, run, and run-history filters require the full job reference. Lists use the selected
workspace unless `--all-workspaces` is given. Job paths cannot contain symlinks, and the
frontmatter cannot override workspace ownership.

Inspect the scaffold with `job show`, check shell syntax with `bash -n`, and test scripts
with fixtures when live effects would be premature. `job run` executes immediately, even
while disabled, using the job and group locks. It sends no runner alerts, but its scripts
and prompt can send messages or perform other actions. Run it only when those immediate
effects are intended; otherwise report live execution as untested. Set `enabled: true`
after validation and testing; the scheduler picks it up next tick.

Invalid jobs stay skipped until fixed; `workspace audit --fix` never edits their content.
Names beginning `enso-` are reserved for Enso-installed jobs.

## Execution order

1. The scheduler checks current job definitions for due work; `job run` triggers immediately.
   Invalid definitions never run. Idle stage polling creates no row.
2. Enso takes the per-job lock and opens a `running` row. An overlapping trigger creates
   no row or hook invocation.
3. Declared secrets resolve once. Failure ends the run before any process starts.
4. The gate runs once: exit 0 continues, exit 1 records `no_work`, and other failures record
   `gate_error`.
5. The optional concurrency group admits, waits, or skips the run. Before execution, Enso
   rechecks admission and the job definition. Stage jobs then claim a task and prepare any
   required worktree; the gate has no newly assigned task context.
6. The command or provider runs. Postrun checks each result and may request bounded turns
   in a successful agent's session. Stage acceptance follows postrun, running required
   checks and any allowed repair before committing the transition.
7. Stage lifecycle events run when ownership permits. Enso settles the claim and closes
   the row, retaining attempts and diagnostics. Job and acquired group locks span execution,
   follow-ups and acceptance; cleanup happens after task users finish.
8. Scheduled and ready-triggered failures send [alerts](#alerts). A new trigger starts a
   fresh agent session.

Gate closure/failure, group skipping, and executor failure can still run a reaction postrun,
which cannot request another turn. Secret-resolution failure, cancellation, and unexpected
runner exceptions bypass further hooks. Cancellation closes the run as `error`; restart
recovery never replays an interrupted script.

## Stage jobs

A stage job serves one executable stage of a [project](tasks.md#projects-and-stages): its
`project` and `stage` bind it, and its prompt is that stage's instructions, what done means
there and what to check. `enso job create --project KEY --stage NAME` scaffolds one, and
with those two flags `--schedule` is optional.

```bash
enso job create --name "Enso todo" --provider claude --model opus --effort high \
  --workspace dev --project EN --stage todo
```

An enabled, valid stage job is checked each minute unless already running:

| `schedule` | Trigger |
| --- | --- |
| absent | `ready`, when an unclaimed task waits in its stage |
| present | `schedule`, at a due cron slot when a task waits |

Idle polling creates no run row. A race where another run takes the waiting task can
produce `no_work`. Manual runs always record a result, including `no_work` when idle;
when a task is claimed, text output prints its reference and JSON returns it as `task`.
`job list` shows `ready (EN/todo)` for an unscheduled stage job; it has no `next_run`.

When it fires, Enso claims the ready task with the highest priority for that run, prepares
a worktree when that stage needs one, writes the [Task block](tasks.md#the-task-block) and the
repository's own instructions ahead of the prompt, and sets `ENSO_TASK` (and
`ENSO_TASK_DIR`) in the environment. The gate, provider turns, and postrun are the ordinary
ones; `{{gate_output}}` is still substituted. The gate runs before the task is claimed,
so it does not receive the newly assigned `ENSO_TASK` or `ENSO_TASK_DIR`.
An agent's advance/return submits a handoff
and the agent finishes its turn. The stage and claim remain unchanged through provider
completion, job postrun, and workflow acceptance. Enso then runs the selected required
checks, sends actual failures back for a bounded repair when allowed, and commits the
transition only on acceptance. Provider success alone never proves that a task advanced.
Command and integration stages use the same transaction/evidence path without a provider.
A failed or interrupted execution preserves its diagnostic and work for recovery.
[Tasks](tasks.md#claims-and-readiness) owns the claim rules and
[Concepts](concepts.md#how-a-stage-job-run-flows) an overview.

### Workflow checks and lifecycle events

[Stage checks](tasks.md#stage-transactions-and-checks) enforce acceptance after execution
stops. Repairs and returns have finite budgets that persist across restarts. Stages without
checks still require a submitted handoff before acceptance.

[Lifecycle scripts](tasks.md#lifecycle-scripts) react to accepted transitions or worktree
cleanup. They are persisted separately from job gate/postrun invocations, use stable event
IDs, and may be delivered more than once. A failed reaction raises attention without undoing
the move. Use checks for lint/tests and `hooks["after:done"]` for completion reactions.

Task ownership lasts through checks and repair; worktree-using lifecycle events prevent
reuse or cleanup until delivered. Integration is an explicit stage with its own repository
lock. Worktrees do not isolate shared services or ports.

## Bundled jobs

Maintenance jobs start in the required [`default` operator workspace](workspaces.md#operator-workspace),
alongside any other jobs with installation-wide responsibilities. Their settings and scripts
remain yours to customize.

`enso setup` and `enso config apply` install `enso-audit` and `enso-update` into
`~/.enso/workspaces/default/jobs/` when their directories are missing. `enso init` prepares the home but does
not install jobs. Both maintenance jobs run deterministic commands without an agent.
Existing job directories are preserved.

### Nightly health audit

`enso-audit` is an enabled command job that runs at `0 3 * * *` local time with
`catch_up: true` (a machine asleep at 03:00 runs it on wake). Its command is
`enso doctor --attention --notify --quiet`; it has no agent or gate and makes no repairs.

[`--attention`](cli.md#operating) selects every health problem plus the installation-hygiene
warnings: unexpected entries, irregular links, credentials other users can read, and stale
generated files. An orphan workspace or unedited `AGENTS.md` template stays quiet. A report
containing only hygiene warnings explicitly says the installation is healthy.

Reports go to the configured notification target, including manual runs from chat, and
enter the outbox under the job's workspace. Each notice includes up to eight findings,
errors first, with a next action and a count of remaining findings. Run
`enso doctor --attention` for the complete report.

Healthy checks stay quiet. Both a healthy check and a delivered report record `ok`;
inspection or delivery failures record `error` and can trigger a runner failure alert.
Unresolved findings are reported nightly, without deduplication or recovery notices.

The job is yours to customize. Setup and config apply preserve an existing directory in full.
Managed upgrades convert the recognized bundled audit to a command while preserving schedule,
enabled state and notification overrides. Customized prompts or scripts, including deleted
scripts, remain agent jobs. Bundle refresh preserves edits and tracked deletions; see
[Customizing](customizing.md#the-bundled-skills). To keep a job off, set `enabled: false` in
`JOB.md`; a later explicit setup or config apply can reinstall a deleted directory.
Try it with `enso job run default:enso-audit`. The service's `PATH` includes the `enso` binary;
a hand-written unit must do the same.

### Nightly release check

`enso-update` is an enabled command job that runs at `30 3 * * *` local time with
`catch_up: true`. Its command is `enso update check --notify --quiet`; it has no agent or
gate. A completed check records `ok`, including when no newer release needs a notice.

A check uses the installation's saved release feed, falling back to official GitHub releases.
A managed install sends one notice per newer stable version to the default notification
target using the same title, status, details, and action format as the audit. An unmanaged
install also receives a notice explaining adoption. Notices are recorded
only after successful delivery, separately for managed and unmanaged installations.
Repeated notices, offline checks, and delivery failures remain quiet; failed checks and sends
are retried on the next scheduled run. A manual
`enso job run default:enso-update` also uses the default notification target rather than a calling
chat's origin.

The job never upgrades Enso. The operator asks in chat or runs `enso update apply` when
ready. Disable it with `enabled: false` to stop nightly checks; manual `enso update check`
still works. See [Upgrading](install.md#upgrading) and [CLI updates](cli.md#updates).

## Secrets

An optional `secrets` list in `JOB.md` names the credentials the run needs:

```yaml
secrets:
  - GITHUB_TOKEN
  - GOOGLE_PASSWORD
```

Names must be unique and pass the shared [secret-name rules](cli.md#secrets); the list may
be empty. Resolve all names once, before gate. A missing name, unavailable key or corrupt
value records an `error` and starts no gate, agent, command, check or postrun. It is never
an ordinary gate's `no_work` result. Like a failing gate, the same failure alerts once per
24 hours however often the job retries, and the next run that resolves its secrets and passes
its gate sends one `✅ [<workspace>:<job>] secrets recovered`.

Gate, the agent or command, workflow checks/repairs, postrun, and every follow-up
share one in-memory snapshot. Declared secrets override same-name inherited variables for
that run only. Enso context and process-control variables are protected. A running process
and later turns in that run retain the original values after deletion/recreation; the next
run resolves current values. Job definitions and generated prompts contain names only.

Project setup/teardown and deferred lifecycle scripts are independent project operations.
They can run outside a job or retry later; use `enso secret run` in those commands when they
need credentials. Chat and Heartbeat agents use that same CLI as needed. Nothing exports
every saved secret into the service environment.

## Commands and working directories

The job's `command`, `gate.command` and `postrun.command` are explicit shell commands run
with `bash -c`. Use `bash run.sh` for a Bash script, `python3 run.py` for a Python script,
or invoke an executable directly. A standalone command starts beside `JOB.md`, as do both
job hooks. A project stage command starts beside `PROJECT.md`; it must enter
`ENSO_TASK_DIR` itself when working on the task's code. Agents start in their Enso workspace.

The main command exits `0` for success and any nonzero code for failure. Exit `1` is a
failure here, even though a gate uses it for `no_work`. A deterministic job that finds
nothing to update can succeed. Its output and exit code go to run history and postrun,
including on failure and timeout. There is no automatic LLM fallback.

Job scripts run normal tools directly and own their dependencies. Use commands such as
`uv run --project /path/to/project report.py` for a project's Python environment and
`enso message` for Enso-owned operations. Do not depend on Enso's internal
interpreter path or import private `enso.*` modules from a job script.

Commands, gate and postrun scripts execute with the service account's access, outside the
provider's policy or sandbox. Provider turns use the workspace's effective arguments,
with no Enso policy prerequisites; see [Provider permissions](configuration.md#provider-permissions-and-installation-trust).

## Gate scripts

The gate runs before either executor so no execution begins when there is nothing to do.
Its stdout is bounded to the final 1 MiB, then stripped of leading and trailing whitespace
before replacing `{{gate_output}}`; truncation is logged. Include that placeholder in the
prompt to pass data: stdout is not automatically appended. Gate runs once per trigger,
including when postrun later requests follow-ups.

Provider prompts also face operating-system argument/environment limits. Keep gate output
concise; for large data, return a file path and tell the agent what to read.

| Gate outcome | Run status | Effect |
| --- | --- | --- |
| exit 0 | execution or group admission proceeds | the agent prompt substitutes stdout into `{{gate_output}}`; a command's text is not templated |
| exit 1 | `no_work` | skipped silently |
| exit 2 or more, timeout, missing script, launch failure | `gate_error` | alerted |

Only a `ENSO_ERROR: <summary>` line on stderr reaches the alert (collapsed to one line, at
most 500 characters); stdout never does. The same failure alerts once per 24 hours, and the
next healthy gate sends one `✅ [<workspace>:<job>] gate recovered`. Map command failures to exit 2
deliberately, since Python's default exit 1 reads as "no work".

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

## Postrun scripts

Postrun runs after a command or each provider turn, before the run row closes. It can perform ordinary
cleanup and exit, or check a condition and send a corrective message into the same session:

| Postrun outcome | Effect |
| --- | --- |
| exit 0 | finish; stdout is not sent to the LLM |
| exit 10 with nonempty stdout | for a successful agent turn, send stdout as the next message in the same session, then check again; a command job fails the check |
| any other exit, missing script, launch failure, or timeout | fail the postrun and finish the run |

`agent.max_followups` defaults to **2**, so an ordinary agent job can run at most three provider turns.
Set it to any nonnegative integer; `0` still runs the check but disallows extra
turns. On a stage job, workflow-requested repair turns do not consume this postrun allowance,
so the run can contain more provider turns.
Only exit `10` requests more LLM work: a script crash that exits `1` is a failure. An empty
or oversized follow-up message, an exhausted limit, or an unavailable session fails the
check. Feedback is limited to 64 KiB of stdout. Whitespace-only messages are refused; other
messages are sent verbatim. Enso refuses oversized messages instead of sending a truncated
instruction.

Follow-ups keep the run ID, workspace, provider, model, effort and permissions. Only the
new message is sent; the provider session retains the prompt, gate data and previous turns.
Enso never substitutes a fresh session if resumption fails. Jobs with postrun and agent
stage jobs capture a resumable session from the first turn for follow-ups or workflow
repair; ordinary jobs without postrun use batch execution. Structured turns follow the
shared [session identity rules](configuration.md#session-identity).

Postrun also runs once for `no_work`, `gate_error`, group `skipped`, and executor failure
or timeout, so a script can react to those outcomes. Those calls cannot start a follow-up:
only a successful provider turn with a usable session can continue. An overlap rejected by
the per-job lock creates no row and runs no hook. Cancellation or an unexpected runner
exception closes the row as `error` and bypasses further hooks; service startup recovery
fails an interrupted run without replaying scripts.

The **command output or latest provider turn's output** arrives on stdin. The outcome being checked arrives
in the environment; the database row remains `running` until checking finishes:

| Variable | Value |
| --- | --- |
| `ENSO_RUN_ID` | The same run ID throughout the loop |
| `ENSO_RUN_STATUS` | The current outcome: `ok`, `error`, `timeout`, `no_work`, `gate_error`, or `skipped` |
| `ENSO_RUN_EXIT_CODE` | Command or latest provider exit code; `1` for a closed gate; gate exit code on failure when available; otherwise empty |
| `ENSO_RUN_DURATION_MS` | Elapsed time since the run began, including earlier hooks but excluding this hook |
| `ENSO_RUN_ATTEMPT` | Execution attempt, starting at 1; 0 when no executor ran |
| `ENSO_RUN_FOLLOWUPS_REMAINING` | Additional postrun-requested provider turns still allowed |
| `ENSO_JOB`, `ENSO_WORKSPACE`, `ENSO_HOME` | The same values the gate and provider get |

Validate the actual expected result: a clean working tree alone does not prove that the
required commit or artifact exists. Postrun can run repeatedly, so make its effects safe to
repeat. For cleanup without a follow-up, do the work and exit 0. Do not recursively run the
same job: its lock remains held. Task completion reactions belong in project lifecycle hooks.

Both hooks run from the job directory. Each postrun invocation has `postrun.timeout` seconds
before its process group is stopped. `timeout` is the main command's allowance or the
combined provider execution allowance across the initial turn, follow-ups and workflow
repairs. Hooks and workflow checks have their own budgets. The execution allowance begins
when execution starts and excludes group waiting. Recorded duration includes waiting,
the gate, execution and postrun, so it can exceed `timeout` without an execution timeout.

Timeout and cancellation cleanup targets the subprocess's process group with `SIGTERM`
and, when needed, `SIGKILL`. This also applies to workflow checks. A command that starts a
new session or detached process group has created a service outside that cleanup boundary;
Enso does not discover or stop arbitrary detached descendants. Keep check processes in the
foreground, or make the command own its services and bounded teardown. Verify both timeout
and cancellation cleanup before enabling such a workflow. In particular, a Playwright
web server may run in a separate group; successful test teardown alone does not prove
interrupted runs release its port. The current termination grace is at most one second,
so a signal-forwarding wrapper with slower teardown cannot guarantee detached-service cleanup.

A failed postrun changes an otherwise `ok`, `no_work`, or `skipped` result to `error`.
An existing executor error, timeout, or gate failure keeps its primary status and gains
the hook diagnostic. Exit `0` cannot turn failed execution into a successful run. The
executor/gate exit code retains its meaning; the postrun exit code is recorded separately
with the attempt. `enso job run` exits 1 for a failed check, even if the executor exited 0.

The diagnostic follows the gate's rule: a `ENSO_ERROR: <summary>` line on stderr, else
the exit status, timeout, missing script, or the reason a follow-up was refused. It is
returned as `postrun_error` in `job run --json`, printed on stderr in manual output, and
stored in run history. Scheduled runs alert on the final failure; intermediate requests
that are repaired successfully send no failure alert. Feedback and attempt output remain
in history and should be treated as sensitive job data.

## Scheduling

- Slots are wall-clock times in the machine's zone, so `0 9 * * *` stays 09:00 across a DST
  change.
- One instance per job: an overlapping trigger is skipped with a log line and no run row.
  Different jobs run in parallel; a shared [concurrency group](#concurrency-groups) uses its
  required `on_busy` policy after the gate. A waiting run keeps the per-job lock, preventing
  repeated ticks from creating more pending runs. Jobs never wait on chat.
- A job first seen by the scheduler is remembered, not fired. A slot missed by more than
  `misfire_grace_seconds` (the machine was asleep, the service was down) is skipped unless
  `catch_up: true`.
- Catch-up runs once when a missed slot is found; it does not replay every missed slot.
  The scheduler stamps its dispatch time before launching and leaves that timestamp
  unchanged when the run finishes. A manual run does not move this scheduling anchor.
- The per-job lock is `runtime/locks/jobs/<workspace>/<job>.lock`, shared with `enso job run`
  in another process.

Cron is exactly five fields, `minute hour day-of-month month day-of-week`: `0 9 * * *`
daily at 09:00, `30 6 * * 1-5` weekdays at 06:30, `*/15 * * * *` every 15 minutes,
`0 9 * * 1` Mondays.

Extra seconds/year fields, aliases such as `@daily`, malformed cron, and impossible dates
such as February 31 are rejected. An invalid schedule affects only its own job.

## Alerts

The runner sends alerts for scheduled and ready-triggered runs; a manual run never alerts.
A successful or quiet run can still send a gate recovery notice. A failed postrun makes
the run a failure. Prompts and scripts can send messages themselves with
`enso message send`, including during manual runs.

- executor exit `N`: `⚠️ [<workspace>:<job> (exit N)]` plus the output tail
- timeout: `⚠️ [<workspace>:<job>] timed out after Ns` plus the tail
- gate failure: `⚠️ [<workspace>:<job>] gate failed` plus the diagnostic
- unresolved secrets: `⚠️ [<workspace>:<job>] secrets unavailable` plus the diagnostic
- postrun failure: `⚠️ [<workspace>:<job>] postrun failed` plus the diagnostic

## Run history

Run and scheduler-state tables store the workspace and local job name separately.
CLI/JSON history, alerts, task actors, `ENSO_JOB`, and viewer links use the qualified
reference. Moving a job directory creates a different identity; old history is retained
under its original reference.

Every trigger that passes the per-job lock creates a row in `enso.db`, including `no_work`,
`gate_error`, and concurrency-group `skipped` outcomes (a stage job with nothing ready is
the one silence: see [Stage jobs](#stage-jobs)), with status, exit code, duration,
the command or final provider turn's output tail (up to 1 MiB), the error, session ID when captured,
and final postrun diagnostic. Attempts separately retain their number, status,
exit code, duration, output tail, session ID, and postrun exit code, stdout and diagnostic.
Execution attempts start at 1; a reaction hook when no executor ran uses attempt 0. A per-job lock
collision returns `skipped` without a run id or row. Manual runs exit 1 for either kind of `skipped` result.

```bash
enso runs list [--job WORKSPACE:JOB] [--workspace W] [--all-workspaces] [-n N]
enso runs show ID            # a unique id prefix is enough
```

A run's `trigger` is `schedule`, `manual`, or `ready` for a stage job fired by waiting work.
Its `kind` is `agent`, `command`, or `integration`. Non-agent runs have null provider,
model and effort fields; command execution is not represented as a fictitious provider.
`runs show` includes the ordered attempt history; its `--json` result adds an `attempts`
array to the run row. `runs list --json` returns run rows without loading attempt bodies.
The newest `runs.keep` finished rows within `runs.max_age_days` are kept; running rows are never pruned.
On service startup, a row still marked running is closed as an error only if its per-job
lock is free or its job directory is gone. A manual run still holding its lock is left
alone. The [web viewer](web.md) reads the same rows.

## Migrating existing jobs

Home revision 5 converts existing workspace `JOB.md` files to this format. It nests agent
settings, converts script paths to explicit hook commands, renames the gate placeholder,
and gives old concurrency groups `on_busy: skip` to preserve their behavior. It also adds
execution kind to retained run history. See [Home migrations](migration.md#job-format-migration)
for preservation rules and preview/apply commands. Normal job loading accepts only the
current format; it does not silently interpret legacy fields.
