---
name: enso-jobs
description: Create, schedule, test, pause, or troubleshoot recurring Enso jobs and task-stage workers. Use enso-heartbeat for one future action or a specific situation followed until resolved.
---

# Jobs

Use jobs for standing responsibilities, such as backups or recurring digests, even when
they have an end date. Use `enso-heartbeat` for a finite follow-up. Finish immediate work
in the conversation or its task.

**Prefer command jobs:** when code can do the work, run it without an LLM to keep Enso
fast and inexpensive. Use an agent only for judgment. Add a deterministic gate when it
can cheaply avoid unnecessary execution, and an independent postrun check when completion
can be verified. A command that cheaply handles no work needs no extra gate. Never perform
the whole job in a gate and exit `1`: that records completed work as `no_work`.

## Create and validate

1. Inspect `enso job list` and the existing `JOB.md`. Lists use `ENSO_WORKSPACE` or
   `--workspace NAME`; `--all-workspaces` broadens scope deliberately.
2. Scaffold a disabled job in an existing workspace. Installation-wide maintenance belongs
   in `default`; `enso-` names are reserved. Use current command help for optional flags.

   ```bash
   enso job create --name "Refresh data" --command 'bash refresh.sh' \
     --schedule '*/15 * * * *' --workspace default
   enso job create --name "Review feedback" --provider claude --model sonnet --effort high \
     --schedule '0 9 * * *' --workspace default
   ```

   Choose the agent triple from `enso config show`; jobs never inherit chat defaults.
3. Edit `$ENSO_HOME/workspaces/<workspace>/jobs/<job>/JOB.md` and its scripts. Standalone
   jobs require `name`, `enabled`, a quoted five-field cron in machine local time, and
   exactly one of `command` or `agent`. Agent jobs need a prompt body; command bodies are
   descriptions. Keep booleans and positive integer timeouts unquoted. Unknown fields,
   null placeholders, duplicate keys, cron aliases and seconds fields are rejected.
4. Inspect `enso job show WORKSPACE:JOB`; check shell syntax with `bash -n` and exercise
   scripts against fixtures. `enso job run WORKSPACE:JOB` performs real work even when
   disabled. Run it only when immediate effects are authorized; a scheduled send does
   not authorize sending early. Report live execution left untested.
5. Set `enabled: true` after validation. Definitions reload each minute. Set it false to
   pause; deleting the directory removes the job but preserves history.

Read [execution.md](references/execution.md) before adding or changing gates, postrun,
shared-resource concurrency, or follow-ups, and when diagnosing their outcomes.

## Scripts and delivery

Commands and hooks run through `bash -c` beside `JOB.md`; agents start in the workspace.
Invoke scripts explicitly, such as `bash run.sh`, or make them executable. Use installed
tools and the script's own dependencies, never Enso's private interpreter or `enso.*`
imports. Commands run with the service account's access, outside provider policies.
Keep subprocesses in the foreground; detached services need bounded teardown verified
for timeout and cancellation.

Declare credential names in `secrets: [NAME]`, populated through `enso secret`; never put
values in job files or prompts. All run steps receive one resolved snapshot.

`notify` controls runner failure/recovery alerts, not successful results. Send requested
results explicitly with `enso message send --to TARGET "text"`; untargeted scheduled sends
use the configured notify destination. Manual runs suppress runner alerts, but scripts
and agents can still send messages. Inspect `enso runs list --job WORKSPACE:JOB` and
`enso runs show ID --json` for output, attempts, and postrun failures.

Newly discovered jobs wait for a future slot. `catch_up: true` runs once after missed
slots; otherwise slots beyond `misfire_grace_seconds` (default 300) are skipped. Jobs have
no expiry field: enforce an end date in the gate and arrange to disable the job afterward.

## Stage workers

Use `enso-projects` for task and workflow design. Scaffold with `--project KEY --stage NAME`
in the project's workspace. Agent stages declare `agent`; command/integration stages omit
both `agent` and `command` because `PROJECT.md` owns execution. Human stages cannot have jobs.

Without `schedule`, a worker runs when a task is ready; a schedule restricts that trigger.
The gate runs before claiming, so it has no newly assigned task. The Task block supplies
the claimed scope and working directory. Project commands start beside `PROJECT.md` and
must enter `ENSO_TASK_DIR` themselves. An agent's advance/return submits a handoff;
execution, postrun, and workflow checks must finish before Enso accepts the transition.
