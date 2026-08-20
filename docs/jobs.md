# Background jobs

Enso runs scheduled agents inside `enso serve`. Each job chooses a provider, model, and
named workspace; the workspace supplies cwd, instructions, concurrency, and exactly one
policy. Jobs cannot override that policy or fall back to unrestricted execution.

The bundled `jobs` skill gives agents the same creation workflow, so you can ask a
trusted Enso conversation to build and test a job for you.

## Create, test, and enable

```bash
enso job create \
  --name "Daily Review" \
  --provider claude \
  --model sonnet \
  --schedule "0 9 * * *" \
  --workspace company

enso job list
enso job run daily-review
```

Creation makes `~/.enso/jobs/daily-review/JOB.md` with `enabled: false`. Finish its
prompt, add a prerun script if useful, run it manually, then set `enabled: true`. The
scheduler loads jobs on its 60-second tick.

After one coherent change, record the exact durable paths in Enso's local content
history. Do not commit partial placeholders, runtime output, or broad staging:

```bash
git -C ~/.enso add jobs/daily-review/JOB.md jobs/daily-review/prerun.sh
git -C ~/.enso commit -m "feat: add daily review job"
```

## Job format

```markdown
---
name: Daily Review
schedule: "0 9 * * *"
provider: claude
model: sonnet
workspace: company
enabled: true
timeout: 900
prerun: prerun.sh
prerun_timeout: 120
notify: C0123456789
catch_up: false
misfire_grace_seconds: 300
---

Review today's inputs and send a concise summary.

Data collected by the gate:

{{prerun_output}}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Human-readable name used in the UI and notifications |
| `schedule` | yes | Five-field cron schedule in the machine's local timezone |
| `provider` | yes | Provider from `config.json` |
| `model` | yes | Configured model for that provider |
| `workspace` | yes | Lowercase kebab-case workspace name |
| `enabled` | yes | Whether the scheduler considers the job |
| `timeout` | no | Provider timeout in seconds; default `900` |
| `prerun` | no | Script filename inside the job directory |
| `prerun_timeout` | no | Prerun limit in seconds; default `120` |
| `notify` | no | Explicit transport destination for failure/recovery alerts |
| `catch_up` | no | Run an otherwise-missed schedule late; default `false` |
| `misfire_grace_seconds` | no | Maximum ordinary schedule lateness; default `300` |

The provider and model stay explicit in the job. Enso verifies that both exist and that
the workspace policy allows the provider. The policy supplies provider-native settings;
the provider starts at `~/.enso/workspaces/<workspace>` with shared and workspace-local
instructions. Invalid, incomplete, or unsafe bindings fail before prerun and provider
execution, with no implicit cwd, alternate workspace, or authority fallback.

Codex job/model commands accept `sol`, `terra`, and `luna`; Enso translates them to
`gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`. Full or custom configured model IDs
remain supported.

## Schedules and missed runs

Cron uses five local-time fields:

```text
┌───── minute (0–59)
│ ┌──── hour (0–23)
│ │ ┌─── day of month (1–31)
│ │ │ ┌── month (1–12)
│ │ │ │ ┌─ day of week (0–6, Sunday=0)
* * * * *
```

| Schedule | Runs |
| --- | --- |
| `0 9 * * *` | Daily at 09:00 |
| `30 6 * * 1-5` | Weekdays at 06:30 |
| `*/15 * * * *` | Every 15 minutes |
| `0 9 * * 1` | Mondays at 09:00 |

Creation validates the schedule. If a later hand edit makes it invalid, the scheduler
logs and skips it instead of guessing. A run delayed less than
`misfire_grace_seconds` may still start. One delayed longer than that is skipped, such as
after a sleeping laptop wakes, unless `catch_up: true` requests the next tick to run it.

Each job has a persistent `.run.lock` shared by the scheduler, CLI, and dashboard, so two
processes do not run the same job concurrently. Jobs in the same workspace also share
its process-local concurrency semaphore; distinct Enso processes do not share that
semaphore. Global scheduled-job parallelism defaults to two and can be changed with
`ENSO_JOB_CONCURRENCY` before reinstalling the service definition.

## Prerun gates

A prerun is trusted host-side Bash executed from the job directory before the provider:

- Exit `0` to continue.
- Exit exactly `1` for intentional no-work, which skips silently.
- Exit `2` or greater to fail, record the error, and notify.
- A timeout, missing script, or launch failure also fails and notifies.
- Standard output replaces `{{prerun_output}}` in the provider prompt.

Only the provider launch receives the workspace's native policy. The prerun is outside
that sandbox and inherits the Enso service environment, so review it as privileged local
automation and treat its output as untrusted provider input.

Map ordinary command errors to exit `2`; many tools and Python exceptions use exit `1`,
which Enso intentionally reserves for no work. Notifications never copy arbitrary stdout
or stderr. To add a safe reason, emit one sanitized stderr line:

```bash
#!/usr/bin/env bash
set -uo pipefail

if ! RESULT=$(some-command); then
  echo "ENSO_ERROR: data check failed" >&2
  exit 2
fi

if [[ -z "$RESULT" ]]; then
  exit 1
fi

printf '%s\n' "$RESULT"
```

Enso invokes the script through Bash, so it does not need an executable bit. Identical
failure alerts are suppressed for 24 hours by default; a changed failure alerts
immediately, and the next healthy prerun sends one recovery. Change the cooldown with
`ENSO_JOB_FAILURE_RENOTIFY_SECS` and reinstall the service definition.

## Results and notifications

Scheduled successes are silent unless the prompt explicitly calls `enso message send`.
Failures and prerun recovery use the job's `notify` destination, then the active
transport's `notify_channel`. They never infer a destination from an inbound Slack route
or broadcast.

`enso job run <name>` uses the same binding, discovery, prerun, provider, timeout, and
recording pipeline. It prints the result and suppresses Enso's automatic job alerts, but
cannot suppress a message the provider explicitly sends. Intentional no-work exits
successfully; prerun/provider failures return a nonzero CLI status.

Parsed jobs with invalid execution bindings create terminal error runs before prerun.
Files that cannot be parsed or lack required frontmatter are reported by
`enso config check`, skipped by the loader, and create no run. Intentional no-work and
invalid/late/disabled schedule skips also create no run.

The whole dispatch lifecycle logs under `[job:<name>]`. Use `enso service logs -f`, the
dashboard's job and run pages, and a manual run to troubleshoot. Job alerts and direct
messages are text-only; Slack's interactive structured-response envelopes are not parsed
on this path.
