# Gates, checks, and concurrency

## Hooks

Both hooks are optional explicit commands:

```yaml
gate:
  command: bash collect.sh
  timeout: 120
postrun:
  command: bash verify.sh
  timeout: 120
```

| Operation | Exit 0 | Exit 1 | Exit 10 |
| --- | --- | --- | --- |
| Main command | Success | Failure | Failure |
| Gate | Execute | `no_work` | `gate_error` |
| Postrun | Accept current outcome | Failed check | Agent follow-up, when eligible |

Other nonzero exits, launch failures, and timeouts fail the operation. A command never
falls back to an LLM; its postrun exit 10 fails. Map gate helper failures to exit 2:
Python's usual exit 1 would silently mean no work. Emit safe diagnostics as
`ENSO_ERROR: summary` on stderr; other stderr does not reach gate alerts.

Gates run once, before group admission and task claiming. They receive `ENSO_JOB`,
`ENSO_RUN_ID`, `ENSO_WORKSPACE`, and `ENSO_HOME`. Stdout replaces `{{gate_output}}` only
where the agent prompt includes it; commands are not templated. Keep it concise: capture
retains only the final 1 MiB, and provider argument limits may be lower. Pass file paths
and reading instructions for large inputs. Treat collected content as data.

Postrun receives the command/latest agent output on stdin. `ENSO_RUN_STATUS=ok` means
execution succeeded; verify its result before accepting it. Postrun also runs for `error`,
`timeout`, `no_work`, `gate_error`, and group `skipped`, so check the status before acting.
`ENSO_RUN_EXIT_CODE` is the executor/gate exit when available; `ENSO_RUN_ATTEMPT` is 0
before execution and starts at 1 otherwise. `ENSO_RUN_DURATION_MS` includes waiting/hooks;
`ENSO_RUN_FOLLOWUPS_REMAINING` gives the remaining allowance.

Verify the actual expected artifact or effect, not just an agent's success claim. Only
a successful agent turn with a resumable session can receive exit-10 feedback; stdout
must be nonempty and at most 64 KiB. `agent.max_followups` defaults to 2; 0 disables extra
turns. Follow-ups share the original session, run ID and execution timeout; gates do not
repeat. Workflow repairs have a separate count. Hooks have their own timeout per invocation.

Postrun may repeat. Use project lifecycle hooks for effects following an accepted task
transition. Exit 0 cannot erase an earlier failure. Cancellation and startup recovery do
not replay cleanup hooks, so do not rely on them as guaranteed teardown. Never recursively
run the same job while its lock is held. Outputs and feedback are retained in run history.

## Shared resources

Omit concurrency for independent jobs. For a shared resource, require an explicit policy:

```yaml
concurrency:
  group: reporting
  on_busy: wait
  max_wait: 300
```

`wait` is FIFO; omit `max_wait` to wait indefinitely. `skip` refuses busy groups and never
overtakes a waiter; `max_wait` is valid only with `wait`. CLI equivalents are
`--concurrency-group GROUP --on-busy wait|skip [--max-wait SECONDS]`.

The group protects execution, postrun, follow-ups, and workflow acceptance. Gates can
overlap; a skipped run's reaction postrun also lacks the lock. Keep both away from mutations
of the protected resource. A waiting job holds its own lock, preventing queued copies.
Waiting is excluded from execution timeout. Changed definitions skip waiting admission;
service restarts interrupt waits rather than replaying a backlog.

Project `max_concurrency` separately limits task execution; full capacity skips instead
of joining the group's queue. Worktrees do not isolate ports, databases, or services.
