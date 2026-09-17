# Heartbeat

Heartbeat follows one finite situation until it is resolved, or performs one future action.
Examples include following a refund until it arrives, coordinating one dinner, and sending a
thank-you email tomorrow morning. A **beat** is one such instruction.

[Jobs](jobs.md) own standing responsibilities: daily backups, reviewing every new email, or a
regular digest. Repeated checking does not make a temporary watch a job, and a recurring job
with an end date is still a job. Work that can be finished now belongs in the conversation or
the [task board](tasks.md).

## Definitions and storage

A beat has a title, current instructions, a completion condition, allowed actions, a workspace,
an agent, a schedule, and a notification destination. Its agent is saved at creation: the
workspace agent or the configured default, unless the definition supplies a complete
`provider` / `model` / `effort` triple. Later default-agent changes do not change that beat.

SQLite in `$ENSO_HOME/enso.db` owns definitions and current state. The internal tables
`_enso_beats`, `_enso_beat_events`, and `_enso_beat_runs` are managed through
`enso heartbeat`, not registered as user tables or edited with SQL. There is no parallel
`BEAT.md` or status file. References such as `HB-001` remain unique after pruning.

A beat's script files live under `$ENSO_HOME/workspaces/<workspace>/heartbeat/<ref>/`.
The optional entry point is `gate.sh`; it can call Python or other helpers beside it.
The provider runs in the recorded workspace, while the gate runs in the beat's directory.
Create the script directory only when adding a gate. Enso rejects linked workspace or
gate directories and never falls back to scripts in another location.

The saved workspace owns the beat for its lifetime: definition updates cannot transfer it.
Restarting Enso or changing a binding, the caller's `ENSO_WORKSPACE`, or the default agent
does not reassign existing definitions, runs, or action receipts. Commands using `HB-001`
operate on that recorded beat. Per-beat execution locks live at the installation level in
`$ENSO_HOME/heartbeat/.locks/`, outside the script directories, and are removed with the beat.

`enso heartbeat list` uses `--workspace` or `ENSO_WORKSPACE`; `--all-workspaces` explicitly
includes the installation. `--all` separately includes closed beats. Runner notifications
keep the beat's workspace in the [message outbox](cli.md#messages), even if a chat's binding
has changed.

## Creation and management

The user asks in ordinary language. The agent loads the bundled `enso-heartbeat` skill and
creates the arrangement through the CLI. The home-level `AGENTS.md` teaches the distinction
between finite follow-through and standing jobs; the full skill is loaded when needed.

Definitions and updates are JSON objects read from `--file FILE`; `--file -` reads stdin.
Every command accepts `--json`. A failed command emits one `{"ok": false, "error": "…"}`
object and exits 1. Declarative validation reports independent problems together.
CLI syntax errors follow the usual exit-2 convention.

Creation selects an existing workspace from `--workspace`, or otherwise `ENSO_WORKSPACE`.
Missing or invalid context is an error, even when the command runs inside a workspace.
There is no implicit `default`. JSON definitions and updates reject `workspace`; Enso
saves the resolved owner in the database. For example, use
`enso heartbeat create --file beat.json --workspace team` to save a follow-up in `team`.

For example, a definition for a temporary watch is:

```json
{
  "title": "Follow the refund",
  "instructions": "Watch this refund thread and report meaningful progress.",
  "completion": "The refund has arrived and the user has been notified.",
  "allowed_actions": "Read the thread and notify me; ask before contacting the seller.",
  "schedule": "0 * * * *",
  "timezone": "America/Vancouver",
  "gate": "gate.sh"
}
```

`agent`, `notify`, and the notification thread are saved from the current context or
configured defaults unless supplied explicitly. Optional `expires_at` and `followup_at`
timestamps bound the situation or request another assessment. `timeout` and `gate_timeout`
are positive seconds, defaulting to 900 and 120. Updates merge supplied definition fields;
`--if-revision` rejects an edit based on an older definition. A running beat uses `note` and
`wait` to record context and follow-up timing; creation and definition edits happen from the
conversation so an old run cannot overwrite newer instructions.

Creation starts **paused**. This lets the agent write and validate the gate and confirm source
access before activating the beat. Validation must not execute the future action early. The
agent confirms the arrangement to the user only after activation succeeds, stating when it
will check or act and when it will stop.

A definition names exactly one time source:

- `at`: an ISO timestamp with an explicit UTC offset, for one future action.
- `schedule`: a five-field cron expression and an IANA `timezone`, for repeated checks.

A named timezone keeps a recurring wall-clock time across offset changes. The shared
scheduler works at minute resolution. A missing spring time moves forward by the clock gap;
a repeated autumn time runs on its first occurrence. Explicit follow-up times and deadlines
can also require attention when a source has no new messages.

A recurring beat without a gate requires explicit `llm_checks: true`. The creating agent must
explain that each scheduled check will involve the LLM. An unavailable or failing gate never
silently switches to this mode.

A one-shot time is consumed by its check. If it cannot proceed, the beat needs attention;
Enso does not keep replaying a stale send. An unfinished one-shot run must set a future
`followup_at` when it waits. Pausing, resuming, or editing unrelated instructions does not
restore an already-used time; changing `at` deliberately schedules a new assessment.
Before the first assessment, a one-shot follow-up cannot be earlier than `at`.

## Lifecycle and history

A beat is `active`, `paused`, `fulfilled`, `cancelled`, or `expired`. Attention is separate from
its lifecycle: waiting for a person keeps the situation open. Pause preserves it. Fulfillment
requires a reason and evidence that the requested outcome was reached. A successful provider
exit alone does not fulfill a beat.

The event timeline records observations, decisions, instruction changes, actions, failures,
and closure. Events are append-only until the closed beat is pruned. They identify who acted,
when something happened, when Enso recorded it, and the associated run when there is one.

Checking stores only the latest check's timestamp, status and diagnostic, and the last
successful check time. Quiet checks produce no timeline entries or agent-run rows. A first
failure and recovery are recorded; repeating the same failure does not fill the history.

Source checkpoints live in beat state. New observations are saved before starting a provider,
and remain pending until explicitly handled. Reading an event or putting it in a prompt does
not acknowledge it. A run acknowledges only the input it was given; newer events remain
pending for the next assessment.

Actions have stable keys and recorded outcomes. Record an attempt before the external action,
then its confirmed result and receipt. A succeeded action must not be repeated; an uncertain
outcome must be reconciled before retrying. This does not make arbitrary third-party tools
transactional: the agent must use available receipts and source history to establish what
happened.

Native Enso message sends and uploads use `--action-key` inside a beat and record their
attempt and receipt automatically. Other tools use `heartbeat action` before acting and
`heartbeat action-result` afterward. A known receipt can still be recorded after an edit or
cancellation; that preserves evidence without authorizing another action. If an outcome
cannot be established, notify the user and pause instead of clearing the uncertainty.

## Execution

The gate executes with the service account's access, outside provider policies. The
assessment uses its saved provider and the workspace's effective arguments, with no Enso
policy prerequisites. [Provider permissions](configuration.md#provider-permissions-and-installation-trust)
remain the provider's responsibility.

`enso serve` runs jobs and Heartbeat from one minute clock. Each system decides its own
readiness and uses the same bounded subprocess and provider execution code. A lock prevents
overlapping assessments of one beat, including across daemon processes.

A gate exits 0 for ready, 1 for quiet, and any other code for failure. Only its plain stdout
becomes fresh evidence. A safe `ENSO_ERROR: …` line on stderr may explain a failure; other
stderr stays out of user alerts. Gate output is bounded to 16 KiB. Gates cannot send native
Enso messages, and source checkpoints change only after explicit agent settlement.

Each assessment starts a fresh provider session with current instructions, timing and wake
reason, bounded gate evidence, the latest event, history counts, and CLI lookup commands.
Its structured output follows the shared [session identity rules](configuration.md#session-identity).
The agent retrieves other history as needed. Due follow-ups can wake it after a quiet gate;
gate failures never silently start a provider. New evidence arriving after the last run's
input cutoff can prompt another assessment without waiting for the ordinary schedule.
A future one-shot is still held until its scheduled time.
After a gate failure, pending evidence stays available, but retries wait for the next
scheduled check or explicit follow-up instead of triggering an early check every minute.

After downtime, Enso makes one current assessment rather than replaying every missed
check. A late one-shot includes its planned time and lateness so the agent can decide whether
the authorized action remains useful. An explicit expiry closes the beat without fulfillment.
Failures and recoveries notify the saved destination/thread; quiet checks and ordinary
successful runs do not send automatic updates. The agent sends useful results explicitly.

## Configuration and retention

```json
"heartbeat": {
  "enabled": true,
  "retention_days": 30
}
```

Heartbeat is enabled by default. `retention_days` must be a positive integer and defaults to
30. Disabling preserves definitions and history; agents must not silently replace a disabled
heartbeat with an equivalent job. The daemon rereads this setting while it runs and stops
active heartbeat work when it is disabled.

Only closed beats are eligible for automatic pruning. Retention starts at closure, not
creation. After it, the daemon removes everything the beat owns in a fixed order: the script
directory, then the record with its events and runs, then the lock file. Because the record
goes last, a pass interrupted by a filesystem error or a restart is simply repeated; a missing
workspace has no scripts to remove. A linked script path is never followed, and that beat is
kept. Lock files whose beat no longer exists are reclaimed on the same pass; nothing else in
the lock directory is touched. Active and paused beats retain their context, and references
are never reused. Permanent outputs, such as a signed agreement, belong in the user's
requested notes or document location.

A closed beat is kept past retention while it holds evidence the user has not received: an
action still `pending` or `uncertain`, or an undelivered notification for a beat with a saved
destination. The daemon logs each kept beat with its reason once, and again if the reason
changes. Record the outcome with `enso heartbeat action-result`, or restore the notification
route, and the next pass removes the beat. Nothing is pruned while Heartbeat is disabled;
retention resumes on the first tick after it is enabled again.

## Viewer

The existing [web viewer](web.md#heartbeats) shows current and previous beats. Open one to
read its instructions, latest check, audit history, and agent runs. Viewing a beat never
changes it; ask Enso to manage it from the conversation.
