---
name: jobs
description: Use this skill to create, inspect, test, change, pause, or troubleshoot scheduled Enso jobs when the user asks for recurring work, cron-like automation, background monitoring, or anything that should run automatically on a schedule.
---

# Jobs

Background jobs run autonomously through the Enso service. Each job selects a named workspace and derives its policy from that workspace, then spawns a configured CLI agent on a cron schedule. Scheduled failures notify the configured destination automatically. Manual runs print their result and suppress Enso's automatic failure notification. Successful jobs are silent unless their prompt deliberately sends a message. Job alerts and `enso message send` output are text-only; interactive structured-response and persistent-surface envelopes are not parsed on this path.

## Workflow

1. **Discover**: Run `enso job --help`, `enso job list`, and `enso config check`; select a configured provider, model, and workspace.
2. **Scaffold**: `enso job create --name "Name" --provider <provider> --model <model> --schedule "0 9 * * *" --workspace <workspace>` creates the directory and a `JOB.md` with `enabled: false`.
3. **Edit**: Write the prompt in the `JOB.md` body and add a prerun script if needed.
4. **Test**: Run `enso job run <name>` and fix any validation or runtime errors.
5. **Enable**: Set `enabled: true` only after the manual run succeeds. The scheduler picks it up on its next tick.

Record one scoped Git commit after each coherent change to durable job files:

```bash
git -C ~/.enso add <changed-path> [<changed-path>...]
git -C ~/.enso commit -m "<summary>"
```

Stage explicit paths such as a job's `JOB.md`, `prerun.sh`, or `prerun.py`. Never use broad staging such as `git add -A`, and never `--force`-add run output, caches, credentials, or other paths the managed `.gitignore` excludes. History is local only — never add a remote, push, pull, fetch, or run destructive history or worktree commands. If the active policy denies a Git operation, report that boundary; never widen or rewrite the policy.

## CLI

```bash
enso job list                    # show all jobs with status
enso job run <name>              # manual run (output to stdout)
enso job create --name "Name" --provider <provider> --model <model> --schedule "0 9 * * *" --workspace <workspace>
```

## Directory structure

```
~/.enso/jobs/
└── <job-name>/
    ├── JOB.md           # Job definition (frontmatter + prompt)
    └── prerun.sh        # Optional gate/data-gathering script
```

## JOB.md format

```markdown
---
name: Human-readable name
schedule: "0 9 * * *"
provider: claude
model: sonnet
workspace: company
enabled: true
prerun: prerun.sh
---

The prompt goes here. Use {{prerun_output}} to inject prerun results.
```

### Frontmatter fields

| Field                   | Required | Description                                                                                |
| ----------------------- | -------- | ------------------------------------------------------------------------------------------ |
| `name`                  | yes      | Display name (shown in notifications)                                                      |
| `schedule`              | yes      | Cron: `minute hour dom month dow`                                                          |
| `provider`              | yes      | Provider configured in `~/.enso/config.json`                                               |
| `model`                 | yes      | Model configured for that provider                                                         |
| `workspace`             | yes      | Lowercase kebab-case entry from `workspaces`; `~/.enso/workspaces/<name>` is the provider cwd |
| `enabled`               | yes      | `true` or `false` — disabled jobs are skipped                                              |
| `prerun`                | no       | Script filename in the job directory                                                       |
| `prerun_timeout`        | no       | Max seconds for the prerun (default 120)                                                   |
| `notify`                | no       | Explicit destination accepted by the configured transport for failure alerts               |
| `timeout`               | no       | Max seconds for the run (default 900)                                                      |
| `catch_up`              | no       | `true` to run a missed schedule late (default `false`)                                     |
| `misfire_grace_seconds` | no       | How late a missed run may still fire (default 300)                                         |

`provider` and `model` are validated against the configured providers and their model lists — a job naming an unknown provider or model is rejected at creation and fails with a clear error instead of running. The cron schedule is validated at creation too; if a hand-edited schedule later becomes invalid, the scheduler skips that job (with a log warning) rather than run it.

`workspace` uses the same named object as interactive conversation bindings. Its name derives the only valid root, `~/.enso/workspaces/<name>`; configuration cannot supply another path. It is mandatory and selects exactly one top-level policy; Enso never accepts a job-level policy override or falls back to an unrestricted launch. The workspace policy selects the provider's native policy plumbing. Enso does not reinterpret what that native policy means.

By default a job that misses its scheduled time by more than `misfire_grace_seconds` (e.g. the machine was asleep) is skipped rather than run late; set `catch_up: true` when a late run is better than no run.

### Schedule (cron syntax)

```
┌───────────── minute (0-59)
│ ┌─────────── hour (0-23)
│ │ ┌───────── day of month (1-31)
│ │ │ ┌─────── month (1-12)
│ │ │ │ ┌───── day of week (0-6, 0=Sun)
│ │ │ │ │
* * * * *
```

Examples:

- `0 9 * * *` — daily at 9:00 AM
- `30 6 * * 1-5` — weekdays at 6:30 AM
- `*/15 * * * *` — every 15 minutes
- `0 9 * * 1` — Mondays at 9:00 AM

## Prerun scripts

**Most jobs should have a prerun script.** It runs before the LLM is invoked, avoiding wasted tokens when there is nothing to do while keeping real failures visible.

- **stdout** is captured and injected into the prompt wherever `{{prerun_output}}` appears
- **exit 0** = proceed with the job
- **exit 1** = intentional no-work result; skip silently
- **exit 2 or greater** = failure; skip the provider, record the error, and notify
- **timeout, missing script, or launch failure** = failure with the same behavior

Only use exit `1` deliberately. Shell wrappers must map command failures to exit `2`, and Python entrypoints must catch unexpected exceptions rather than allowing Python's default exit `1` to collide with the no-work sentinel.

For a useful alert without leaking source data, write one safe summary line to stderr as `ENSO_ERROR: <summary>`. Enso never copies prerun stdout or arbitrary stderr into notifications. Repeated identical failures are suppressed for 24 hours; a changed failure alerts immediately, and the next healthy prerun sends one recovery.

### When to use prerun

- **Checking for new data**: unprocessed items, new emails, calendar changes
- **Gathering context**: fetching today's events, pulling API data
- **Gating on conditions**: skip if weekend, skip if no meetings, etc.

### When to skip prerun

- Jobs that should always run unconditionally (e.g. morning overview, daily journal prompt)

### Template

```bash
#!/usr/bin/env bash
# prerun.sh — gate the job and gather data
set -uo pipefail

# 1. Check if there's work to do
if ! RESULT=$(some-command-here); then
  echo "ENSO_ERROR: data check failed" >&2
  exit 2
fi

# 2. Exit exactly 1 when the command succeeded but found no work
if [[ -z "$RESULT" ]]; then
  exit 1
fi

# 3. Output data for the prompt (injected as {{prerun_output}})
echo "$RESULT"
```

Enso invokes the file through `bash` from the job directory, so it does not require an executable bit. The prerun is trusted host-side automation and is not sandboxed by the policy; only the provider CLI launch receives the selected native policy. Treat every prerun and everything it emits into `{{prerun_output}}` accordingly.

## Examples

### Daily overview (no prerun — always runs)

```markdown
---
name: Daily Overview
schedule: "30 6 * * *"
provider: claude
model: sonnet
workspace: default
enabled: true
---

Generate today's daily overview note. Check the calendar for events
and yesterday's incomplete tasks.
```

### YouTube playlist summaries (prerun gates on new videos)

```markdown
---
name: YouTube Summaries
schedule: "*/15 * * * *"
provider: claude
model: haiku
workspace: default
enabled: true
prerun: prerun.sh
---

Summarise this video and create a note:

{{prerun_output}}
```

`prerun.sh`:

```bash
#!/usr/bin/env bash
set -uo pipefail
if ! VIDEO=$(python3 check_playlist.py); then
  echo "ENSO_ERROR: playlist check failed" >&2
  exit 2
fi
if [[ -z "$VIDEO" ]]; then
  exit 1
fi
echo "$VIDEO"
```

### Weekday-only meeting prep (prerun checks for new attendees)

```markdown
---
name: Meeting Prep
schedule: "0 7 * * 1-5"
provider: claude
model: sonnet
workspace: default
enabled: true
prerun: prerun.sh
---

Research these meeting attendees and create notes:

{{prerun_output}}
```

`prerun.sh`:

```bash
#!/usr/bin/env bash
set -uo pipefail
if ! NEW_PEOPLE=$(osascript get-new-attendees.js); then
  echo "ENSO_ERROR: attendee lookup failed" >&2
  exit 2
fi
if [[ -z "$NEW_PEOPLE" ]]; then
  exit 1
fi
echo "$NEW_PEOPLE"
```

## Tips

- Choose from the providers and models in the current Enso configuration; use a lower-cost model for frequent simple work and a stronger model only when the task warrants it.
- Test with `enso job run <name>` before relying on the schedule
- Check logs with `enso service logs` if a job isn't firing
- Set `enabled: false` to pause a job without deleting it
