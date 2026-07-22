---
name: jobs
description: Create and manage scheduled background jobs. Use when the user asks to set up recurring tasks, cron jobs, or anything that should run automatically on a schedule.
---

# Jobs

Background jobs are scheduled tasks that run autonomously via the
Enso service. Each job spawns a CLI agent on a cron schedule.
Jobs that fail notify the user automatically. Successful jobs are
silent by default — use `enso message send` in the prompt to
send alerts to the user.

## Workflow

1. **Scaffold**: `enso job create --name "Name" --provider claude --model sonnet --schedule "0 9 * * *"`
   — creates the directory and a `JOB.md` with `enabled: false`
2. **Edit**: Write the prompt in the JOB.md body, add a prerun script if needed
3. **Enable**: Set `enabled: true` in the frontmatter
4. **Test**: `enso job run <name>` to verify it works
5. The job scheduler picks it up automatically on the next tick

## CLI

```bash
enso job list                    # show all jobs with status
enso job run <name>              # manual run (output to stdout)
enso job create --name "Name" --provider claude --model sonnet --schedule "0 9 * * *"
enso job create --name "Name" --provider codex --model terra --schedule "0 9 * * *"
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
enabled: true
prerun: prerun.sh
---

The prompt goes here. Use {{prerun_output}} to inject prerun results.
```

### Frontmatter fields

| Field      | Required | Description |
|------------|----------|-------------|
| `name`     | yes      | Display name (shown in notifications) |
| `schedule` | yes      | Cron: `minute hour dom month dow` |
| `provider` | yes      | `claude` or `codex` |
| `model`    | yes      | Model name (e.g. `sonnet`; Codex: `sol`, `terra`, or `luna`) |
| `enabled`  | yes      | `true` or `false` — disabled jobs are skipped |
| `prerun`   | no       | Script filename in the job directory |
| `prerun_timeout` | no | Max seconds for the prerun (default 120) |
| `notify`   | no       | Telegram user ID or Slack channel/DM for failure alerts |
| `timeout`  | no       | Max seconds for the run (default 900) |

`provider` and `model` are validated against the configured providers and
their model lists — a job naming an unknown provider or model is rejected at
creation and fails with a clear error instead of running.

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

**Most jobs should have a prerun script.** It runs before the LLM is
invoked, avoiding wasted tokens when there is nothing to do while keeping
real failures visible.

- **stdout** is captured and injected into the prompt wherever
  `{{prerun_output}}` appears
- **exit 0** = proceed with the job
- **exit 1** = intentional no-work result; skip silently
- **exit 2 or greater** = failure; skip the provider, record the error, and notify
- **timeout, missing script, or launch failure** = failure with the same behavior

Only use exit `1` deliberately. Shell wrappers must map command failures to exit
`2`, and Python entrypoints must catch unexpected exceptions rather than allowing
Python's default exit `1` to collide with the no-work sentinel.

For a useful alert without leaking source data, write one safe summary line to
stderr as `ENSO_ERROR: <summary>`. Enso never copies prerun stdout or arbitrary
stderr into notifications. Repeated identical failures are suppressed for 24 hours;
a changed failure alerts immediately, and the next healthy prerun sends one recovery.

### When to use prerun

- **Checking for new data**: unprocessed items, new emails, calendar changes
- **Gathering context**: fetching today's events, pulling API data
- **Gating on conditions**: skip if weekend, skip if no meetings, etc.

### When to skip prerun

- Jobs that should always run unconditionally (e.g. morning overview,
  daily journal prompt)

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

Enso invokes the file through `bash`, so it does not require an executable bit.

## Examples

### Daily overview (no prerun — always runs)

```markdown
---
name: Daily Overview
schedule: "30 6 * * *"
provider: claude
model: sonnet
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

- Use `haiku` or `sonnet` for frequent/simple jobs to save cost
- Use `opus` for jobs that need deep reasoning or complex output
- For Codex, use `sol` for frontier work, `terra` for balanced everyday work, or `luna` for fast, affordable runs
- Test with `enso job run <name>` before relying on the schedule
- Check logs with `enso service logs` if a job isn't firing
- Set `enabled: false` to pause a job without deleting it
