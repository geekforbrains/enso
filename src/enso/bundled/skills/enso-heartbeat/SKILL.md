---
name: enso-heartbeat
description: Arrange a future action, reminder, temporary watch, or follow-up until a specific situation resolves; manage or assess Enso beats. Recurring responsibilities belong to enso-jobs.
---

# Heartbeat

Use a beat for one future action or finite situation: sending tomorrow's email or watching
a refund until it arrives. Daily digests and handling every new email belong to `enso-jobs`,
even with an end date. Finish immediate work in the conversation or its task.

Use `enso heartbeat` and subcommand help. Definitions, events, and checkpoints belong to
the CLI; do not keep parallel status files or edit internal tables. **When `ENSO_BEAT` is
set, or reconciling an action from a prior run, read
[assessment.md](references/assessment.md) before acting.** It covers input cutoffs, action
receipts, and required settlement. A successful agent exit alone never fulfills a beat.

## Arrange and manage

1. Check `enso heartbeat status --json` and `enso heartbeat list` for availability and
   duplicates. Respect disabled heartbeat; do not replace it with a job. Lists select
   `ENSO_WORKSPACE` or `--workspace`; `--all-workspaces` broadens scope, while `--all`
   includes closed beats.
2. Capture the objective, exact source/thread identifiers, authorized actions, constraints,
   and completion condition. Resolve dates in the user's timezone. Create with
   `enso heartbeat create --file FILE --json` (`--file -` reads stdin).

```json
{
  "title": "Follow the refund",
  "instructions": "Watch refund thread <source-id>; report meaningful progress.",
  "completion": "Refund received and user notified.",
  "allowed_actions": "Read the thread and notify the user.",
  "schedule": "0 * * * *",
  "timezone": "Etc/UTC",
  "gate": "gate.sh"
}
```

Replace example references and timezone. Require `title`, `instructions`, `completion`,
and `allowed_actions`. Choose either `at` with an explicit UTC offset, or five-field
`schedule` plus IANA `timezone`. Optional `followup_at` and `expires_at` need offset
timestamps; set expiry when a late action would be inappropriate. A one-shot follow-up
cannot precede its first `at`.

3. Select an existing workspace through `ENSO_WORKSPACE` or `--workspace`, never JSON.
   Agent and notification destination/thread are saved from context or defaults. Override
   `agent` only with a complete configured provider/model/effort triple.
4. Creation is paused. Add any gate in the returned directory and verify source access
   with a read-only probe. `enso heartbeat resume REF --json` validates the definition
   and shell syntax, not account access. Do not test by performing the future action early.
5. Confirm what happens, when it checks or acts, and when it stops after activation succeeds.

Manage with `show`, `update REF --file FILE [--if-revision N]`, `pause`, `resume`, `cancel`,
or `expire`. Updates merge supplied fields and cannot transfer workspace ownership.
Resuming does not replay a consumed one-shot; changing `at` deliberately schedules another
assessment. A running beat uses `note` and `wait`, not creation or definition updates.

## Gate readiness

Use a gate when code can decide readiness. Recurring beats without one require
`llm_checks: true`; explain that each check invokes the model. A failed gate never switches
to model checks. One-shots can rely on their due time.

Create `$ENSO_HOME/workspaces/<workspace>/heartbeat/<REF>/` only for scripts/helpers.
`gate.sh` runs with Bash there; it observes without performing the intended action.

- Exit **0** to assess; stdout supplies evidence with source identifiers and timestamps.
- Exit **1** for nothing to assess; other exits, missing scripts, and timeouts fail.
- Remap helper failures: Python's exit 1 otherwise means quiet. Emit a safe
  `ENSO_ERROR: explanation` on stderr.
- Read `ENSO_BEAT_CHECKPOINT` JSON for progress; never advance it from the gate.
