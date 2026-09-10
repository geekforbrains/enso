# Jobs

Jobs own standing responsibilities, such as nightly backups or handling every new email.
Use [Heartbeat](heartbeat.md) for one future action or a particular situation followed until
resolved. Repeated checking can serve a beat; a recurring digest remains a job even with
an end date. Both systems use the same scheduling and execution infrastructure.

A job is `~/.enso/jobs/<name>/JOB.md`: YAML frontmatter and a prompt. The service checks
every job once a minute and runs the ones whose cron slot has passed, or, for a
[stage job](#stage-jobs), whose stage has a task ready.

A job uses the same providers and workspace instructions as a chat turn, but names its own
agent, starts a fresh session for each trigger, and has its own timeout. A prerun can skip
the LLM or supply data, and a postrun can finish the work or request another message in the
same session. The default limit is two follow-ups after the initial provider turn.

```markdown
---
name: Meteor Forum Watch      # required
schedule: "0 14 * * *"        # required unless stage is set: five-field cron, in the machine's local time
provider: claude              # required
model: sonnet                 # required
effort: high                  # required
workspace: meteor             # required: ~/.enso/workspaces/meteor must exist
project: EN                   # optional, with stage: the project this job serves
stage: todo                   # optional, with project: one of its agent stages
concurrency_group: meteor     # optional: serialize provider work and postrun checks
enabled: true                 # required
prerun: prerun.sh             # optional: gate, run with bash from the job directory
prerun_timeout: 300           # optional, default 120
postrun: postrun.sh           # optional: check/reaction, run with bash from the job directory
postrun_timeout: 120          # optional, default 120 per invocation
max_followups: 2              # optional, default 2; 0 prevents extra provider turns
timeout: 1200                 # optional, default 900
notify: slack:C0BP5BQF6UF     # optional: where failure alerts go
catch_up: false               # optional: run a missed slot late (default false)
misfire_grace_seconds: 300    # optional: how late a slot may still fire (default 300)
---

The prompt. {{prerun_output}} is replaced with the prerun's stdout.
```

`provider`, `model`, and `effort` are required on every job and validated against
`config.json`; jobs never inherit the default or workspace agent triple. Workspace
provider-argument overrides still apply. Effort follows the provider's rules at run time:
ordered ladders clamp to the model's maximum, Antigravity uses its embedded model effort,
and OpenCode passes the exact variant through; see [Providers](configuration.md#providers).
The run row records the normalized effort, including Antigravity's model-embedded level
even when the job requested less; see [Configuration](configuration.md#antigravity).
`notify` is `slack:C…`, `telegram:<id>`, or a bare id when only one transport is
configured; without it, alerts go to the first configured transport with a `notify` target
(Slack before Telegram). A `schedule` is exactly five cron fields
(see [Scheduling](#scheduling)). `project` and `stage` come together or not at all: `project`
must be a key in `config.json`'s [`projects`](configuration.md#projects) and `stage` one of
that project's agent stages (`approve is a human stage; a job cannot serve it`). With a
`stage`, `schedule` is optional, and still validated when present.

`concurrency_group` is optional non-empty text; a stage job without one is in
`project:<KEY>`, so the stages of one project serialise and different projects run in
parallel. Jobs in the same group run their preruns independently, but Enso admits only one
selected run through provider execution and postrun checks at a time. It uses an advisory
file lock under `jobs/.concurrency/`, shared with manual runs, so the operating system
releases it if a process or the service dies; a stale database flag cannot strand a group.
The group lock stays held through postrun and every follow-up, so another grouped job cannot
change the workspace between the work and its check. A group collision produces `skipped`
and still runs its reaction hook, without the group lock or a collision alert. That hook
cannot request provider work; a hook failure can make the result an error and send an alert.

## The frontmatter

The frontmatter is one YAML mapping and the fields above are all of it. Nothing is guessed
and nothing is coerced, so a `JOB.md` says exactly one thing or it says nothing:

| Rule | What happens otherwise |
| --- | --- |
| The block is valid YAML and a `key: value` mapping | reported with the line and column of the fault |
| Each key appears once | `answers 'name' twice at line 4, column 1` |
| Every key is one of the fields above | `'retries' is not a recognized field` |
| The seven required fields are present (`schedule` may be absent on a stage job) | `effort is required` |
| `project` and `stage` are both present or both absent, name a configured project and one of its agent stages | `approve is a human stage; a job cannot serve it` |
| Text fields are non-empty text | `effort must be non-empty text` |
| `enabled` and `catch_up` are YAML booleans | `enabled must be true or false` |
| The three timeouts and `misfire_grace_seconds` are positive YAML integers | `timeout must be a positive integer` |
| `max_followups` is a nonnegative YAML integer | `max_followups must be a nonnegative integer` |
| The prompt body is not empty | `the prompt body is empty` |

Every one of those a file breaks is reported together, alongside everything `config.json`
can see about the fields it did get right — an unusable schedule, an unconfigured agent, a
missing workspace directory, a notify target that resolves to nothing — so one pass fixes
the file. A field that is absent or the wrong type is reported once and not again as
whatever depended on it. A present-but-empty field is a problem, not an omission: `effort:`
with nothing after it is YAML null, and `catch_up: ""` is empty text where a boolean
belongs. A file with any problem never runs; it stays visible in `job show`, `job list`,
`enso doctor`, and the [web viewer](web.md), which name the file to edit.

Quoting is how you say "this is text". YAML reads an unquoted value as whatever it looks
like, so quote anything it would read as something else:

```yaml
name: "Daily: Review"     # an unquoted colon makes the line invalid YAML
schedule: "*/15 * * * *"  # a leading * is a YAML alias
model: "4.5"              # unquoted it is the number 4.5, not a model name
notify: "12345"           # a bare Telegram id is an integer
timeout: 600              # never quoted: this one really is a number
```

A syntax fault names a line and a column and nothing else: the block Enso could not read
is never quoted back, since a prompt and a notify target are not things to echo into a log
or a chat message. A field problem names its field, and repeats a value only where the
value is the thing being rejected, as an unusable schedule is.

## Workflow

```bash
enso job create --name "Meteor Forum Watch" --provider claude --model sonnet \
  --effort high --schedule "0 14 * * *" --workspace meteor
$EDITOR ~/.enso/jobs/meteor-forum-watch/JOB.md
enso job show meteor-forum-watch         # fields, problems, next and last run, prompt
enso job run meteor-forum-watch          # execute now, only when its effects are intended
```

`job create` writes a disabled scaffold under a slug of the name. Inspect it with `job show`,
check shell syntax with `bash -n`, and test scripts with fixtures or stub services when real
effects would be premature. `job run` executes immediately even while disabled and uses both
job and group locks; it is not a dry run. The runner sends no alerts, but the prompt and
scripts can send messages or perform other actions. A request for scheduled work does not
by itself authorize performing those actions during setup. Use a manual run when its
immediate effects are safe and authorized; otherwise report the live execution left untested.
Set `enabled: true` after validation and appropriate testing pass; the scheduler picks it up
on its next tick. `job create` refuses
an invalid schedule before it writes job files. A `JOB.md` with problems is logged once and
skipped until fixed, and `job show`, `job list`, `enso doctor` and the
[web viewer](web.md) all report the same problem; doctor names the file to open. Nothing
repairs a job for you: Enso cannot know which minute a broken schedule meant, so it never
rewrites one, and `enso workspace audit --fix` does not touch job content. Names beginning
`enso-` are reserved for jobs Enso installs; `enso workspace audit` warns about one it
did not.

## Stage jobs

A stage job serves one agent stage of a [project](tasks.md#projects-and-stages): its
`project` and `stage` bind it, and its prompt is that stage's instructions, what done means
there and what to check. `enso job create --project KEY --stage NAME` scaffolds one, and
with those two flags `--schedule` is optional.

```bash
enso job create --name "Enso todo" --provider claude --model opus --effort high \
  --workspace dev --project EN --stage todo
```

It gains a second trigger next to cron: work being ready. On each minute tick, for every
enabled, valid stage job that is not already running:

| `schedule` | What happens |
| --- | --- |
| absent | fires with trigger `ready` when an unclaimed task waits in the stage; otherwise nothing, with no run row and no log line at info level |
| present | at a cron slot that has passed, fires with trigger `schedule` only when a task waits; otherwise the slot is consumed silently, with no row |

So idle polling leaves no rows, and a schedule only restricts when the job may pick work up.
The readiness check comes before the per-job lock and the prerun, so a task that another run
takes in between leaves this run a `no_work` row with trigger `ready`; that race is the one
way an idle tick records anything.
`enso job run` on a stage job is trigger `manual` and records a `no_work` row when nothing is
ready, so you get an answer; when it claims a task it prints `task: EN-041`, and `--json`
carries the reference as `task`. `job list` shows `ready (EN/todo)` in the schedule column of
an unscheduled stage job, and `job show` prints `group` (the effective concurrency group) and
no `next_run` for it.

When it fires, Enso claims the ready task with the highest priority for that run, prepares
a worktree for a repo project, writes the [Task block](tasks.md#the-task-block) and the
repository's own instructions ahead of the prompt, and sets `ENSO_TASK` (and
`ENSO_TASK_DIR`) in the environment. The prerun, provider turns, and postrun are the ordinary
ones; `{{prerun_output}}` is still substituted. Moving the task hands it off and clears the
claim; it does not stop the provider process or skip the remaining postrun checks. The agent
must finish after the handoff and cannot move the task again from that run. A run that ends
still holding its claim has it released and recorded, and the next run sees that as recovery.
[Tasks](tasks.md#claims-and-readiness) owns the claim rules and
[Concepts](concepts.md#how-a-stage-job-run-flows) the full run order.

## Bundled jobs

`enso setup` and `enso config apply` install `enso-audit` and `enso-update` into
`~/.enso/jobs/` when their directories are missing. `enso init` prepares the home but does
not install jobs. Existing job directories and their agent choices are preserved.

### Nightly health audit

`enso-audit` is enabled, runs at `0 3 * * *` local time with `catch_up: true` (a machine
asleep at 03:00 runs it on wake), works in the `default` workspace, and uses the provider,
model, and effort in the configuration when it is first installed. Later default-agent
changes do not rewrite it.

Its prerun runs `enso doctor --json` and inverts the doctor's exit code, since the two
contracts read `0` and `1` the other way round:

| Doctor | Prerun | Run |
| --- | --- | --- |
| exit 0: healthy, warnings included | exit 1 | `no_work`; nothing spent, nothing sent |
| exit 1 with the report on stdout: problems | exit 0, the report on stdout | the agent gets the report in `{{prerun_output}}` |
| exit 1 with no report (a crash), or anything else: the doctor itself failed | exit 2 with a `ENSO_ERROR:` line | `prerun_error`, alerted |

The agent explains each problem in plain words, says which ones
`enso workspace audit --fix` would repair (the doctor marks them), and sends the summary
with `enso message send`. It fixes nothing; the operator decides. A home that stays broken
hears about it every night: each of those is a provider run, not a repeated prerun
failure, so nothing suppresses it.

Where the scheduled summary goes: `enso message send` without `--to` uses the transport's
notify target. A manual run inside a chat turn can inherit that conversation as its
destination; see [Environment for agents](cli.md#environment-for-agents).
A `notify` on the job moves only the runner's own alerts (`prerun failed` and the
like), never the agent's send; to deliver the summary elsewhere, add `--to <target>` to the
`enso message send` line in the prompt. With no notify target at all (the Slack step of
`enso setup` lets the channel stay blank), the send fails and the summary is only in run
history; setup says so when it installs the job.

The job is yours to customize. Setup and config apply preserve an existing directory in full,
including edited prompts or schedules and deleted scripts. Managed upgrades refresh only
files that still match a recorded bundled baseline; they preserve edits and tracked deletions,
including removal of the whole bundle. Historical job directories without baselines stay
untouched. [Customizing](customizing.md#the-bundled-skills) owns these rules. To keep a job off,
set `enabled: false` in `JOB.md`; a later explicit setup or config apply can reinstall a
deleted directory. Try the audit with
`enso job run enso-audit`, which prints `no work` on a healthy home; with the service
installed but stopped the doctor reports that, so expect a summary then. The service's
`PATH` includes the `enso` binary, so the prerun calls plain `enso`; under a unit written
by hand without it, the run alerts `prerun failed` with
`enso doctor exited with status 127`.

### Nightly release check

`enso-update` is enabled and runs at `30 3 * * *` local time with `catch_up: true`. Its
prerun runs `enso update check --notify --quiet`, then exits 1 so the provider gate stays
closed. The explicit agent triple is stamped into the job like any other job, but this
check never starts that agent or spends provider tokens. A completed check appears as
`no_work` in run history, including when it delivered a release notice.

A managed install checks its saved release feed and sends one notice per newer stable
version to the default notification target. It records the version only after delivery
succeeds. Unchanged releases, missing feeds, unmanaged checkouts, offline checks, and delivery
failures remain quiet; the next scheduled check retries. A manual
`enso job run enso-update` also uses the default notification target rather than a calling
chat's origin.

The job never upgrades Enso. The operator asks in chat or runs `enso update apply` when
ready. Disable it with `enabled: false` to stop nightly checks; manual `enso update check`
still works. Older homes can install the new job by applying their existing valid config.
See [Upgrading](install.md#upgrading) and [CLI updates](cli.md#updates).

## Prerun scripts

The prerun runs before the provider so nothing is spent when there is nothing to do.
Its stdout is bounded to the final 1 MiB, then stripped of leading and trailing whitespace
before replacing `{{prerun_output}}`; truncation is logged. Include that placeholder in the
prompt to pass data: stdout is not automatically appended. Prerun runs once per trigger,
including when postrun later requests follow-ups.

| Prerun outcome | Run status | Effect |
| --- | --- | --- |
| exit 0 | `ok` / `error` / `timeout` / `skipped` | the provider runs with stdout in `{{prerun_output}}`, unless its concurrency group is busy |
| exit 1 | `no_work` | skipped silently |
| exit 2 or more, timeout, missing script, launch failure | `prerun_error` | alerted |

Only a `ENSO_ERROR: <summary>` line on stderr reaches the alert (collapsed to one line, at
most 500 characters); stdout never does. The same failure alerts once per 24 hours, and the
next healthy prerun sends one `✅ [<name>] prerun recovered`. Map command failures to exit 2
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

Postrun runs after each provider turn, before the run row closes. It can perform ordinary
cleanup and exit, or check a condition and send a corrective message into the same session:

| Postrun outcome | Effect |
| --- | --- |
| exit 0 | finish; stdout is not sent to the LLM |
| exit 10 with nonempty stdout | send stdout as the next message in the same provider session, then run postrun again |
| any other exit, missing script, launch failure, or timeout | fail the postrun and finish the run |

`max_followups` defaults to **2**, so at most three provider turns occur in one run. Set it
in `JOB.md` to any nonnegative integer; `0` still runs the check but disallows extra turns.
Only exit `10` requests more LLM work: a script crash that exits `1` is a failure. An empty
or oversized follow-up message, an exhausted limit, or an unavailable session fails the
check. Feedback is limited to 64 KiB of stdout. Whitespace-only messages are refused; other
messages are sent verbatim. Enso refuses oversized messages instead of sending a truncated
instruction.

A follow-up is immediate, within the same run ID, workspace, provider, model, effort and
permissions. It receives only the new message; the provider session keeps the original
prompt, prerun data and previous turns. Prerun is not repeated. The next scheduled or manual
trigger starts a fresh session. Enso never silently falls back to a fresh session if it
cannot resume the current one. Jobs with postrun use structured provider output from their
first turn to capture and validate the session ID; jobs without postrun keep batch execution.

Postrun also runs once for `no_work`, `prerun_error`, group `skipped`, and provider failure
or timeout, so a script can react to those outcomes. Those calls cannot start a follow-up:
only a successful provider turn with a usable session can continue. An overlap rejected by
the per-job lock creates no row and runs no hook. Cancellation or an unexpected runner
exception closes the row as `error` and bypasses further hooks; service startup recovery
fails an interrupted run without replaying scripts.

The **latest provider turn's output** arrives on stdin. The outcome being checked arrives
in the environment; the database row remains `running` until checking finishes:

| Variable | Value |
| --- | --- |
| `ENSO_RUN_ID` | The same run ID throughout the loop |
| `ENSO_RUN_STATUS` | The current outcome: `ok`, `error`, `timeout`, `no_work`, `prerun_error`, or `skipped` |
| `ENSO_RUN_EXIT_CODE` | Latest provider exit code; `1` for `no_work`; prerun exit code on a prerun failure when available; otherwise empty |
| `ENSO_RUN_DURATION_MS` | Elapsed time since the run began, including earlier hooks but excluding this hook |
| `ENSO_RUN_ATTEMPT` | Provider turn number, starting at 1; 0 when no provider ran |
| `ENSO_RUN_FOLLOWUPS_REMAINING` | Additional provider turns still allowed |
| `ENSO_JOB`, `ENSO_WORKSPACE`, `ENSO_HOME` | The same values the prerun and provider get |

For example, this check asks the agent to finish committing its work. Set `REPO` to the
repository the job works on, and have the job prompt describe the expected commit:

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
  printf '%s\n' \
    'The repository still has uncommitted changes. Review and commit the work for this job, then finish.'
  printf '%s\n' "$STATE"
  exit 10
fi
exit 0
```

A clean working tree checks for remaining changes; it does not prove a particular commit
was created. If the job requires a specific commit or artifact, check that condition too.
The script can run more than once, so put actions intended only after acceptance, such as
archiving a result, after its checks pass. For ordinary cleanup, do the work and exit `0`;
no extra provider turn runs. Do not recursively call `enso job run` for the same job from
postrun: its per-job lock is still held.

Both hooks run from the job directory. Each postrun invocation has `postrun_timeout` seconds
before its process tree is killed. `timeout` is the combined provider execution allowance
across the initial turn and follow-ups; hooks use their own budgets and do not reset the
provider allowance. The recorded final duration includes prerun, all provider turns and
all postrun calls, so it can exceed `timeout` without a provider timeout.

A failed postrun changes an otherwise `ok`, `no_work`, or `skipped` result to `error`.
An existing provider error, timeout, or prerun failure keeps its primary status and gains
the hook diagnostic. Exit `0` cannot turn a failed provider into a successful run. The
provider/prerun exit code retains its meaning; the postrun exit code is recorded separately
with the attempt. `enso job run` exits 1 for a failed check, even if the provider exited 0.

The diagnostic follows the prerun's rule: a `ENSO_ERROR: <summary>` line on stderr, else
the exit status, timeout, missing script, or the reason a follow-up was refused. It is
returned as `postrun_error` in `job run --json`, printed on stderr in manual output, and
stored in run history. Scheduled runs alert on the final failure; intermediate requests
that are repaired successfully send no failure alert. Feedback and attempt output remain
in history and should be treated as sensitive job data.

## Scheduling

- Slots are wall-clock times in the machine's zone, so `0 9 * * *` stays 09:00 across a DST
  change.
- One instance per job: an overlapping trigger is skipped with a log line and no run row.
  Different jobs run in parallel; a shared `concurrency_group` limits their provider
  execution and postrun checks, with a collision after prerun producing `skipped`. Jobs never
  wait on chat.
- A job first seen by the scheduler is remembered, not fired. A slot missed by more than
  `misfire_grace_seconds` (the machine was asleep, the service was down) is skipped unless
  `catch_up: true`.
- Catch-up runs once when a missed slot is found; it does not replay every missed slot.
  The scheduler stamps its dispatch time before launching and leaves that timestamp
  unchanged when the run finishes. A manual run does not move this scheduling anchor.
- The per-job lock is `jobs/<name>/.run.lock`, shared with `enso job run` in another
  process.

Cron is exactly five fields, `minute hour day-of-month month day-of-week`: `0 9 * * *`
daily at 09:00, `30 6 * * 1-5` weekdays at 06:30, `*/15 * * * *` every 15 minutes,
`0 9 * * 1` Mondays.

Five fields is the whole accepted form, because the scheduler only wakes once a minute.
A sixth seconds or year column would promise a resolution Enso does not have, and an alias
such as `@daily` hides which minute it means, so both are rejected along with ordinary
malformed cron.

## Alerts

The runner sends alerts for scheduled and ready-triggered runs; a manual run never alerts.
A successful or quiet run can still send a prerun recovery notice. A failed postrun makes
the run a failure. Prompts and scripts can send messages themselves with
`enso message send`, including during manual runs.

- provider exit `N`: `⚠️ [<name> (exit N)]` plus the output tail
- timeout: `⚠️ [<name>] timed out after Ns` plus the tail
- prerun failure: `⚠️ [<name>] prerun failed` plus the diagnostic
- postrun failure: `⚠️ [<name>] postrun failed` plus the diagnostic

## Run history

Every trigger that passes the per-job lock creates a row in `enso.db`, including `no_work`,
`prerun_error`, and concurrency-group `skipped` outcomes (a stage job with nothing ready is
the one silence: see [Stage jobs](#stage-jobs)), with status, exit code, duration,
the final provider turn's output tail (up to 1 MiB), the error, session ID when captured,
and final postrun diagnostic. Attempts separately retain their number, status,
exit code, duration, output tail, session ID, and postrun exit code, stdout and diagnostic.
Provider turns start at 1; a reaction hook when no provider ran uses attempt 0. A per-job lock
collision returns `skipped` without a run id or row. Manual runs exit 1 for either kind of `skipped` result.

```bash
enso runs list [--job NAME] [-n N]
enso runs show ID            # a unique id prefix is enough
```

A run's `trigger` is `schedule`, `manual`, or `ready` for a stage job fired by waiting work.
`runs show` includes the ordered attempt history; its `--json` result adds an `attempts`
array to the run row. `runs list --json` returns run rows without loading attempt bodies.
The newest `runs.keep` finished rows within `runs.max_age_days` are kept; running rows are never pruned.
On service startup, a row still marked running is closed as an error only if its per-job
lock is free or its job directory is gone. A manual run still holding its lock is left
alone. The [web viewer](web.md) reads the same rows.
