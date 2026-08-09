# Teams

Teams mode applies to Slack only: it defines who may address Enso, which workspace handles
a Slack DM or channel mention, and what gets recorded. Telegram keeps its private,
one-to-one authorization and default workspace behavior; there is no `routes.telegram`.
The Telegram transport rejects every non-private chat type so authorized IDs cannot invoke
Enso from a Telegram group.

Sibling specs own the implementation details: [permissions.md](permissions.md) owns how
Enso invokes each agent CLI with its native policy, [architecture.md](architecture.md)
owns process and state boundaries, and [data-model.md](data-model.md) owns the canonical
config blocks and audit schema.

This document never invents a cross-provider permission language. Enso remains a thin
proxy over the installed agent CLIs; the operator authors and tests their native policy
files. See [permissions.md](permissions.md).

## Why the single-allowlist model does not scale

Without teams mode, Enso has one Slack allowlist (`transports.slack.allowed_users`), one
`working_dir`, and one implicit trust level: every permitted user gets an agent running
unsandboxed in the same workspace. That is appropriate for one operator and unsafe once
coworkers, clients, DMs, and shared channels have different access needs.

The direct extension — make groups and give each group a workspace — creates ambiguity:

- A person may belong to several groups.
- A channel may contain people from several groups.
- DM access, channel access, execution policy, and auditing are separate decisions.

## Groups, routes, and workspaces

The three concepts stay separate:

| Concept | Question | Carries |
| --- | --- | --- |
| **Group** | Who is this? | Slack user IDs only |
| **Route** | Where did this happen? | Allowed groups, workspace, audit, and context policy |
| **Workspace** | Where and how may work run? | Cwd, providers, native policies, skills, and chat commands |

**The route selects the workspace; the person never does so directly.** A request in
`#client-acme` runs in the acme workspace under its policy even when the operator sent it.
Privilege belongs to the room, not the sender's rank.

Enso retains every group a Slack user belongs to. Channel authorization uses the
intersection of that membership set and the route's `allow` list. Object declaration
order is never an authorization control.

DM routes also match groups, but a DM must resolve to exactly one route. If one user
matches several DM routes, the configuration is ambiguous and Slack teams dispatch is
disabled until the operator fixes it. Enso never guesses which workspace is more
privileged.

## Config shape

The canonical schema and defaults live in
[data-model.md § Teams config](data-model.md#teams-config). This illustrative example
shows the relationships:

```jsonc
{
  "groups": {
    "admin":  { "slack": ["U01ADMIN"] },
    "team":   { "slack": ["U02DEV", "U03PM"] },
    "client": { "slack": ["U04CLIENT"] }
  },

  "workspaces": {
    "ops": {
      "path": "~/.enso/workspaces/ops",
      "unrestricted": true,
      "providers": ["claude", "codex", "agy"],
      "default_provider": "claude",
      "skills": "*",
      "chat_commands": "*"
    },
    "acme": {
      "path": "~/.enso/workspaces/acme",
      "policy_dir": "~/.enso/policies/acme",
      "providers": ["claude", "codex"],
      "default_provider": "claude",
      "skills": ["docs"],
      "chat_commands": ["status", "clear", "stop", "help"]
    }
  },

  "routes": {
    "slack": {
      "account_id": "T0ENSO",
      "dms": {
        "owner": {
          "allow": ["admin"],
          "workspace": "ops",
          "audit": false
        },
        "project-team": {
          "allow": ["team"],
          "workspace": "acme",
          "audit": true
        }
      },
      "channels": {
        "C0ACME": {
          "allow": ["team", "admin"],
          "workspace": "acme",
          "audit": true,
          "context_from": "allowed"
        },
        "C0FINANCE": {
          "allow": ["admin"],
          "workspace": "ops",
          "audit": false
        }
      }
    }
  },

  "audit": {
    "on_failure": "block",
    "max_age_days": 365
  }
}
```

Fail-closed defaults are part of the schema, not conventions:

- There is no catch-all Slack route. An absent DM or channel route cannot dispatch.
- `routes.slack.account_id` must exactly match the Slack team/workspace authenticated by
  the configured token. A mismatch disables Slack teams dispatch.
- Missing `allow` means nobody. Missing `audit` means `false`. Missing `context_from`
  means `"allowed"`.
- Missing `providers`, `skills`, or `chat_commands` means none; `"*"` must be explicit.
- A route with an unknown group or workspace, no workspace, or an unusable selected
  provider is disabled and reported. It never falls back to another workspace. An
  unusable non-selected provider blocks only that provider.
- Any ambiguous DM match disables all Slack teams dispatch until the configuration is
  corrected.
- A workspace runs in today's yolo mode only when `unrestricted: true` is explicit. That
  flag does not implicitly grant providers, skills, or commands.
- Unrestricted and policy-controlled modes are mutually exclusive. A workspace with
  `unrestricted: true` plus an explicit or discovered native policy source is invalid.
- Otherwise the active provider's native policy must exist and be accepted by its CLI.
  Missing or rejected policy blocks the turn. Enso does not generate or grade it.

## Opt-in and migration

`routes.slack` is the teams-mode switch.

- When it is absent, the presence of the legacy
  `transports.slack.allowed_users` keeps that behavior and its existing `working_dir`.
  The key itself is the explicit legacy opt-in; absence of both configurations blocks
  Slack.
- When it is present, Slack authorization comes only from `groups` and `routes.slack`.
  Configuring both teams mode and the legacy Slack allowlist is an error rather than an
  undocumented precedence rule.
- Migration does not synthesize a workspace, move the existing `working_dir`, or leave a
  symlink. Legacy execution continues to use `working_dir`; the operator explicitly
  declares any unrestricted teams-mode workspace that should reuse that path. A
  policy-controlled workspace may not overlap it.
- Setup never synthesizes channel routes from the old allowlist: doing so would create an
  unsafe catch-all. New Slack setup writes the authenticated `routes.slack.account_id`
  plus empty `dms` and `channels` maps, with no legacy allowlist, so access remains
  blocked until the operator adds exact entries.
- Telegram does not participate in this migration. It retains its user-ID allowlist and
  default workspace, with an explicit private-chat-type check.

## Resolution

One immutable `Resolution` is computed before commands, context fetches, attachment
downloads, or provider work:

```
Resolution {
  transport, account_id, route_id, groups, workspace_id, workspace_path,
  provider, binding_revision, policy_revision, audit, context_from
}
```

For Slack teams mode, ingress runs the full pipeline below. Immediately before execution,
the shared route resolver repeats steps 3 through 7 against the original delivery claim:

1. **Identify.** Extract Slack team/account ID, user ID, channel ID, canonical source
   message timestamp, channel type, parent channel, and thread timestamp; derive the
   stable `delivery_id` defined in [data-model.md](data-model.md#slack-delivery-ledger).
   Reject an account ID that does not match `routes.slack.account_id`. Only `im` is a DM;
   `mpim`, public, private, and Slack Connect conversations are exact-ID channel routes. A
   thread inherits its channel's route.
2. **Deduplicate.** Atomically claim the delivery ID in the metadata-only Slack event
   ledger. A retry never executes a command or provider twice, even when audit is off.
3. **Resolve memberships.** Collect every configured Slack group containing the user ID.
   No membership means the sender is unknown.
4. **Resolve the route.** For a channel, look up the exact channel ID. For a DM, collect
   DM routes whose `allow` list intersects the membership set. Zero matches means no
   route; more than one means invalid configuration.
5. **Authorize.** Authorization succeeds when the memberships intersect the route's
   `allow` list. An unknown, unmatched, or disallowed sender is silently ignored.
6. **Configure.** Resolve the execution-scoped provider from the workspace default or a
   prior permitted `!use` selection, then validate the workspace, provider allowlist, and
   native policy.
   An authorized sender whose route is unusable receives an explicit configuration error;
   no provider process starts.
7. **Bind.** Freeze the route, workspace, provider, authorization/config revision, native
   policy revision, audit state, and context policy into the `Resolution` carried through
   dispatch.

Queued turns retain that snapshot, but it is never an authorization lease. Immediately
before a command or provider spawn, Enso fully resolves the current config and sender
memberships again. The current resolution must be authorized and match the queued route,
workspace, selected provider, `binding_revision`, and `policy_revision`; otherwise the
stale turn is refused rather than rerouted. If audit became enabled while the turn waited,
the terminal refusal is recorded before Enso replies. A sender whose access was revoked
receives silence.

The delivery claim happens only at ingress; revalidation does not mistake the active turn
for a retry. Only the shared route resolver performs mandatory revalidation. Commands,
providers, and other downstream consumers never derive authorization ad hoc or read a
global working directory.

### Silence and errors are different controls

| Situation | Response |
| --- | --- |
| Unknown user | Silence |
| Known user with no matching DM or exact channel route | Silence |
| Known user not allowed by the matched channel route | Silence |
| Authorized user, but route/workspace/provider/policy is unusable | Explicit error; no spawn |
| Authorized audited turn whose initial audit write fails | Explicit error; no spawn |

An unauthorized user stays silent even if audit storage or config diagnostics fail. Enso
must not disclose that the bot is listening or that an allowlist exists.

`enso route explain slack <user-id> [channel-id]` may report memberships, route,
workspace, audit state, and configuration errors to the local operator. It explains Enso
routing only; it does not certify a provider policy.

## Slack triggers and untrusted context

Slack DMs dispatch ordinary messages. Channels dispatch only an explicit bot mention,
including inside threads. Authorization and route validation occur before Enso fetches
surrounding context or downloads attachments.

Ignored users can still place text in an allowed channel that reaches the next authorized
request. Being ignored is not the same as not being processed, so surrounding context is
always untrusted input.

- `context_from: "allowed"` includes only messages authored by users whose memberships
  intersect the route's `allow` list, plus Enso's own messages. This is the default.
- `context_from: "everyone"` includes the full available channel/thread context when the
  operator deliberately needs it.
- Every injected message carries its author identity and an explicit untrusted-content
  marker.
- Fetched context is not part of the plain-text audit trail. The trail records the
  triggering request and Enso's user-visible result.

## Chat commands and providers

Commands are capabilities. They resolve through the same route before executing, and a
workspace's `chat_commands` allowlist controls which are offered and accepted. `!help`
lists only permitted commands.

This is especially important for `!update`, `!restart`, and `!logs`, which affect or
expose the shared service, and `!use`, which changes the execution-scoped provider
selection. `!use` lists only the workspace's permitted providers and refuses a provider
whose native policy is absent or unusable. It never changes another route's selection.

## Jobs

Scheduled jobs have no Slack route and therefore require an explicit `workspace:` in
`JOB.md` once teams mode is enabled. There is no workspace fallback. The job uses the same
provider allowlist, native-policy invocation, environment handling, workspace lock, and
policy revision as chat work.

Scheduling captures the job-file digest, workspace binding revision, selected provider,
and provider policy revision. After acquiring both the per-job lock and workspace
semaphore, Enso reloads the job and workspace and requires every value to match immediately
before `prerun` or provider spawn. A changed or deleted job, workspace, provider, or policy
cancels that snapshot instead of running old authorization against new files. An enabled
job missing `workspace:` is a visible startup/load error and is never scheduled.

A job `prerun` executes outside the provider CLI, so a provider policy cannot govern it;
Enso therefore permits `prerun` only for an explicitly unrestricted workspace.

Job execution remains run history, not a chat audit turn.

## Audit

The audit trail is an opt-in, plain-text safety record of the triggering human message and
Enso's final user-visible reply or terminal outcome. It excludes fetched context, status
ticker edits, tool calls, reasoning, and raw provider prompts. The canonical turn schema
is in [data-model.md § Audit log](data-model.md#audit-log).

- `audit` defaults to `false` on every route. An unmatched route has no audit policy.
- For an audited route, Enso creates the turn record before any command, context fetch,
  attachment download, or provider spawn. Failure blocks an authorized turn by default.
- Enso stores the final response before delivery and then records whether Slack delivery
  succeeded.
- A matched audited route may record an ignored or denied trigger with no response. An
  unauthorized sender still receives silence if that write fails.
- `audit: false` means no `_enso_audit` turn. Teams-mode operational logs are
  metadata-only and never contain prompt previews, but Slack retention, native provider
  session history, and uploads remain separate retention surfaces.
- To keep the one-row turn contract complete, Enso refuses `enso message send`, job
  alerts, and other out-of-band sends whose destination is an audited Slack route. Those
  paths would need a separate outbound-event audit schema to target one.
- `!status` reports the route's audit state, and startup logs enumerate audited routes.

The operator's native provider policy or outer isolation must deny access to
`~/.enso/enso.db`. Enso does not translate that requirement into a generic provider rule.

## Escalation surface

Routing decides where a request runs, not what code in that workspace can reach. The
operator's native policies and any outer sandbox must account for these shared surfaces:

- The `enso` CLI can create jobs, send messages, and access shared data.
- `~/.enso/secrets/` contains credentials loaded by the service.
- `~/.enso/skills/` can influence every workspace.
- `~/.enso/enso.db` contains runs, user tables, and audit turns.
- `~/.enso/config.json` contains authorization and route definitions.
- Native provider homes may contain credentials, config, and conversation history.

Enso still owns the process boundary: it passes only the required environment, exposes
only allowlisted skills, and does not link jobs, docs, config, or the database into a
policy-controlled workspace. These controls complement rather than replace the provider's
native policy.

A Slack route is dispatchable only when its workspace explicitly selects
`unrestricted: true` or the active provider's native policy can be loaded by the CLI.
Missing, ambiguous, or stale configuration blocks.

## Decisions

- Teams mode is Slack-only; Telegram uses its existing allowlist and remains private and
  one-to-one.
- Routes are transport-qualified and exact; there is no dispatchable default route.
- Group declaration order has no security meaning.
- Enso does not define or compile a generic permission policy.
- The operator authors and tests native provider policies; Enso selects them and fails
  closed when it cannot apply them.
- An unrestricted workspace may still enable audit.
- Audit failures block authorized audited turns by default.
