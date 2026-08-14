# Slack routes and workspace policies

Slack gives each exact conversation route a workspace, and each workspace selects exactly one reusable policy. The model is intentionally small: Slack decides who belongs in a channel, Enso selects where the provider CLI starts, the workspace selects which native policy it receives, and the installed CLI enforces that policy.

This document owns who may invoke a Slack route. [slack-triggers.md](slack-triggers.md) owns when a channel message engages Enso at all — per-channel mention requirements, thread following, and `!` command addressing. [slack-output.md](slack-output.md) owns how authorized replies render and how App Home or Canvas drafts are confirmed.

Telegram is separate. It remains private, one-to-one, and authorized by exact numeric IDs in `transports.telegram.allowed_users`; interactive Telegram work uses the global `working_dir`.

## Model

| Concept       | Purpose                                                                                                 |
| ------------- | ------------------------------------------------------------------------------------------------------- |
| Route         | An exact Slack DM user ID or channel ID mapped to one workspace                                         |
| Workspace     | A shared content root and provider cwd, with one policy and a process-local concurrency limit            |
| Policy        | Available providers, default provider, allowed Enso chat commands, and native provider policy selection |
| Native policy | Provider-specific settings interpreted and enforced by the installed Claude Code or Codex CLI           |

A user does not carry a permission level into every room:

- An exact DM route authorizes that Slack user.
- An exact channel route authorizes every human member who can post in that channel.
- Everyone using a channel gets the same workspace and therefore the same policy, including administrators.
- Threads inherit their parent channel route — including its response-trigger settings ([slack-triggers.md](slack-triggers.md)) — but keep their own conversation session.
- An unlisted DM or explicit mention in an unlisted channel receives a fixed local access message. Ordinary messages in unlisted channels are ignored; `mention_required` and `channel_defaults` configure routed channels only and never make an unrouted channel responsive.
- There is no default route, wildcard route, group overlay, sender ranking, or Slack `allowed_users` mode. `routes.slack.channel_defaults` supplies default response-trigger settings to channel routes ([slack-triggers.md](slack-triggers.md)); it is settings inheritance, not authorization, and routes nothing by itself.

The workspace is not itself a security boundary. It is shared content and cwd. Authority comes from the workspace's policy and the selected CLI's native policy, plus any outer operating-system isolation the operator chooses to add.

## Example: one client, two trust levels

`#client-acme` and `#client-acme-internal` use distinct workspaces when they need different policies:

```text
#client-acme          -> workspace acme          -> policy client-readonly
#client-acme-internal -> workspace acme-internal -> policy staff
owner DM              -> workspace default       -> policy admin
#company              -> workspace company       -> policy staff
```

The client channel may answer questions from project knowledge but deny edits and administrative commands. The internal workspace can hold or deliberately reference the material needed by trusted staff while using broader authority. Sessions remain separate because they belong to their Slack channel or thread.

The client native policy should prevent writes to control files such as `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.agents/`, and skill definitions. If clients need to create material, grant write access only to an ordinary data directory such as `drafts/`.

## Company and client directories

A practical convention for a small team is:

```text
~/.enso/workspaces/
├── company/
│   ├── AGENTS.md
│   ├── CLAUDE.md -> AGENTS.md
│   ├── knowledge/
│   ├── drafts/
│   ├── uploads/
│   ├── .agents/skills/
│   └── .claude/skills/
└── clients/
    ├── acme/
    │   ├── AGENTS.md
    │   ├── CLAUDE.md -> AGENTS.md
    │   ├── knowledge/
    │   ├── drafts/
    │   ├── uploads/
    │   ├── .agents/skills/
    │   └── .claude/skills/
    └── globex/
```

These directory names are conventions, not Enso policy syntax. `knowledge/` holds durable shared material, `drafts/` holds ordinary writable output, and Enso stores downloaded attachments in persistent `uploads/<random-id>/` directories. Enso does not automatically expire those uploads; retention and cleanup belong to the operator.

The staff native policy may grant the company route read or write access to `~/.enso/workspaces/clients/**`. This lets an operator normalize project storage with ordinary directories instead of teaching Enso about project types or mounting several workspaces into one request.

Starting the CLI in `company/` does not reliably make every provider discover instructions or skills in a sibling client directory. The company `AGENTS.md` should tell the agent where client workspaces live and require it to read the selected client's protected instructions and project overview before working there. Enso does not synthesize an instruction chain.

For work that should automatically begin with one client's project instructions and skills, use an internal client channel whose route starts directly in that client workspace.

## Configuration

The complete schema is in [data-model.md](data-model.md#execution-catalog-and-slack-routes). Workspace and policy names are portable identifiers matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; they cannot contain path separators or traversal segments. This example shows the relationships:

```jsonc
{
  "workspaces": {
    "default": {
      "path": "~/.enso/workspaces/default",
      "policy": "admin",
      "concurrency": 1
    },
    "company": {
      "path": "~/.enso/workspaces/company",
      "policy": "staff",
      "concurrency": 1
    },
    "acme": {
      "path": "~/.enso/workspaces/clients/acme",
      "policy": "client-readonly",
      "concurrency": 1
    },
    "acme-internal": {
      "path": "~/.enso/workspaces/clients/acme-internal",
      "policy": "staff",
      "concurrency": 1
    }
  },

  "policies": {
    "admin": {
      "unrestricted": true,
      "providers": ["claude", "codex", "agy"],
      "default_provider": "claude",
      "chat_commands": "*"
    },
    "staff": {
      "policy_dir": "~/.enso/policies/staff",
      "providers": ["claude", "codex"],
      "default_provider": "claude",
      "chat_commands": ["status", "clear", "stop", "help", "use", "model", "effort", "compact"]
    },
    "client-readonly": {
      "policy_dir": "~/.enso/policies/client-readonly",
      "providers": ["claude"],
      "default_provider": "claude",
      "chat_commands": ["status", "clear", "stop", "help"]
    },
    "automation": {
      "policy_dir": "~/.enso/policies/automation",
      "providers": ["claude", "codex"],
      "default_provider": "claude",
      "chat_commands": []
    }
  },

  "routes": {
    "slack": {
      "account_id": "T0YOURTEAM",
      "channel_defaults": {
        "mention_required": false,
        "thread_mention_required": false
      },
      "dms": {
        "U01OWNER": {
          "workspace": "default",
          "audit": false
        }
      },
      "channels": {
        "C0ACME": {
          "workspace": "acme",
          "audit": true,
          "mention_required": true,
          "thread_mention_required": true
        },
        "C0ACMEINTERNAL": {
          "workspace": "acme-internal",
          "audit": false
        },
        "C0COMPANY": {
          "workspace": "company",
          "audit": false
        }
      }
    }
  }
}
```

Slack requires `routes.slack`. The same top-level workspace and policy catalogs also serve jobs and are parsed independently of Slack, so a Telegram-only installation can still run policy-bound jobs.

Here `channel_defaults` makes routed channels fully responsive, and `C0ACME` opts back into mention-only; both settings, their defaults, and their validation rules are specified in [slack-triggers.md](slack-triggers.md). Neither key is valid on a DM route.

The same policy may be reused across many workspaces when its native policy is written in terms of the invocation workspace. For example, every external client channel can use `client-readonly`; each route still starts in its own client's directory.

## Resolution and lifecycle

At Slack startup Enso authenticates the account, loads the exact routes and execution catalog, and checks native policy plumbing for providers used by those routes. Jobs are checked separately by `enso config check` and revalidated before each execution. Changes to `config.json` require an Enso restart; Enso deliberately does not hot-reload route authorization while work is queued or running.

For each Slack event Enso:

1. Verifies the authenticated Slack account.
2. Accepts an ordinary DM or explicit channel mention always, and a non-mention channel message only when its channel's route settings allow it ([slack-triggers.md](slack-triggers.md)): effective `mention_required: false` for a top-level message, or effective `thread_mention_required: false` for a reply in a thread Enso already participates in (a thread a prior dispatch joined, or one rooted by a message Enso posted itself). Other channel messages are ignored.
3. Resolves the exact DM user ID or channel ID and claims its delivery ID for retry deduplication.
4. If the location is unlisted, returns the fixed local response described below and stops.
5. Resolves the configured workspace and its policy.
6. Checks that the selected provider and native policy can be launched.
7. Runs the provider directly in the workspace directory.

An invalid route or native policy never falls back to another workspace, policy, global `working_dir`, or unrestricted execution. Configuration errors for an otherwise authorized route are reported. A globally invalid configuration cannot establish usable Slack routing, and an event from the wrong Slack account remains silent and is logged rather than receiving an access response.

Slack DMs dispatch ordinary messages. Channels always dispatch explicit bot mentions, including inside threads; a routed channel additionally dispatches non-mention messages where its effective `mention_required` or `thread_mention_required` is `false` ([slack-triggers.md](slack-triggers.md)), and replies to channel messages always land in the message's thread. Unlisted locations respond only to explicit contact. An unlisted DM receives `I haven't been enabled for your DMs yet. Ask an Enso admin for access.` An explicit mention in an unlisted channel receives `I haven't been enabled in this channel yet. Ask an Enso admin to set me up.` as a thread reply. These are fixed transport responses: Enso does not resolve a workspace or policy, fetch context or attachments, invoke an LLM, or create an audit record. They pass through the delivery ledger so a retried Slack event receives at most one reply.

For configured routes, route resolution still occurs before surrounding context or attachments are fetched. Channel context is untrusted input even though every member is authorized to invoke the route.

Thread context is always pushed: a threaded turn receives that thread's messages since Enso last spoke, or the whole thread when the conversation has no provider session yet. Channel history is not. An unrestricted policy instead receives, on the first turn of a conversation, the `enso slack history` and `enso slack thread` commands for the channel it is replying in, and fetches history only when the request calls for it. A restricted policy cannot be assumed to reach the network, so it keeps receiving the pushed channel context it cannot fetch for itself. Pushing that history unconditionally meant a new top-level request arrived carrying the roots of unrelated earlier threads, and the agent answered them.

Rich output does not add route authority. A structured reply receives the same destination as its ordinary text fallback. A persistent-surface draft captures the exact authenticated account, route, requester, workspace, policy, audit setting, conversation, and confirmation message before it can be shown. Only that requester may use its controls. At click time Enso resolves the route again and requires those bindings to remain identical; a removed or changed route revokes the draft instead of falling back to another workspace or policy.

On an audited route, Publish or Cancel creates a separate `surface_confirmation` audit turn before the draft is consumed. The existing `audit.on_failure` policy applies before any Canvas or App Home mutation. The original provider turn retains the full exact confirmation preview that was delivered; the click records the same preview plus the human decision and terminal publication result.

`enso config check` inspects Enso's configuration and native-policy launch plumbing. `enso route explain slack <user-id> [channel-id]` explains the local routing decision. Neither command certifies that a native policy has the intended meaning; test policies with the installed provider CLI and disposable files.

## Providers and Enso commands

A policy declares available providers, a default provider, and allowed Enso chat commands. `!help` and `!use` show only capabilities offered by the workspace policy. Service-wide Enso commands such as update, restart, and logs normally belong only to an administrative policy.

A restricted policy can additionally grant named environment variables through `env_passthrough` (names, never values) and, for Claude, an exact MCP server allowlist through the conventional `<policy_dir>/claude/mcp.json`. Both default to off, and both are real grants: MCP servers are dialled by the provider process itself and bypass the sandbox's network rules, so grant only servers whose entire tool surface is acceptable, and a passthrough variable's value is readable by any policy that can run Bash — passthrough delivers a credential, it does not scope one. Neither applies to an unrestricted policy, which already inherits everything (`env_passthrough` there is a config error). See [permissions.md](permissions.md#granting-credentials-and-mcp-servers-to-a-restricted-policy) for how and when to use them.

`chat_commands` controls Enso's `!` command surface. It does not hide or authorize the provider CLI's own tools, slash commands, skills, plugins, hooks, or MCP servers. A `!` command is recognized only when explicitly addressed — a bot mention in a channel, whatever the route's response triggers, or any DM message; an unaddressed `!`-prefixed message in a responsive channel is ordinary prompt text, never a command. This is a fixed rule, not a setting ([slack-triggers.md](slack-triggers.md)), so making a channel responsive never widens its command surface. Commands such as `!status`, `!clear`, and `!stop` are handled by Enso; `!compact` launches the active provider and therefore also remains subject to the selected native policy.

Enso never combines user-level permissions with a channel's policy and never translates policies between providers. A route selects one workspace, and that workspace selects one complete policy for the active CLI.

## Skills and instructions

Project instructions and skills are ordinary provider-native files in the workspace: `AGENTS.md` and `.agents/skills/` for Codex, plus `CLAUDE.md` and `.claude/skills/` for Claude Code. The CLIs may also expose native user, managed, plugin, system, or bundled skill scopes; Enso does not suppress those scopes or maintain a skill allowlist. A project skill adds relevant behavior but is not proof that other skills are absent. Treat skill discovery as functionality rather than isolation, and rely on the selected native policy for actual authority.

Claude Code behavior changes independently of Enso. Operators should review the official [permissions](https://code.claude.com/docs/en/permissions), [settings](https://code.claude.com/docs/en/settings), [tools reference](https://code.claude.com/docs/en/tools-reference), and [skills](https://code.claude.com/docs/en/skills) documentation, then test their installed CLI. Enso supplies native settings; it does not certify their meaning.

Use project-specific skills in the relevant client workspace and company-wide skills in the company workspace. A staff route starting directly in a client workspace naturally sees that client's project material. A route starting in the company workspace must explicitly read a client's protected instructions before working across directories.

## Jobs

Jobs are defined under `~/.enso/jobs/`, not as Slack routes, but every `JOB.md` must select one named workspace:

```yaml
workspace: company
```

The job's existing `provider` and `model` remain authoritative. The provider must be allowed by the workspace policy, runs with the named workspace as cwd, and receives that policy's native configuration. Missing, unknown, incomplete, or unsafe bindings fail before prerun and provider execution; there is no global or unrestricted fallback.

An optional prerun script remains trusted host-side automation. Enso runs it through Bash with the job directory as cwd, outside the provider's native policy, then injects allowed stdout into the provider prompt. Keep prerun scripts protected and review them as executable operator code.

The job's `notify` destination remains independent of Slack routes. Scheduled successes are silent unless the prompt explicitly sends a message. Host-side failure and recovery alerts use `notify` or the transport's `notify_channel`; a manual `enso job run` suppresses those automatic alerts but cannot suppress a message explicitly sent by the provider process.

A persistent per-job `.run.lock` coordinates the scheduler, CLI, and dashboard across processes. Jobs also use the named workspace's process-local semaphore, but separate Enso processes do not provide cross-process workspace serialization.

## Audit

`audit` is optional route metadata and defaults to `false`. When enabled, Enso attempts to record the triggering message and outcome using its audit store. This is useful operational evidence, not a complete security transcript: Slack history, fetched context, attachments, status edits, reasoning, tool calls, native provider sessions, and out-of-band messages have their own retention and visibility.

The metadata-only Slack delivery ledger exists independently of route auditing and prevents retried Slack events from triggering duplicate work or duplicate canned no-route replies. Pending ledger claims left by a crash are closed at startup, and ledger rows are pruned on their own retention schedule.

Provider policy must keep restricted agents away from Enso's config, secrets, policies, database, jobs, and service-control commands whether route auditing is enabled or not.

## Migration

This release intentionally removes the old Slack allowlist path. A Slack transport without `routes.slack`, or with `transports.slack.allowed_users`, is invalid. Migrate each authorized DM user and channel to an exact route selecting a known workspace; that workspace selects its policy. Enso never synthesizes routes because doing so grants access.

Telegram still uses `transports.telegram.allowed_users`, but entries must be exact numeric user IDs. The old `allowed_user_ids` spelling and `"*"` wildcard are not supported. Telegram accepts only private chats.

Every existing job must declare `workspace`; job-level `access` or `policy` overrides are rejected. The catalogs are top-level configuration and do not depend on Slack being enabled.

Configurations using top-level `access`, route-level `access` or `policy`, or job-level `access` are rejected. Rename the catalog to `policies`, assign exactly one `policy` to every workspace, remove policy overrides from routes and jobs, and key each DM route by exact Slack user ID.
