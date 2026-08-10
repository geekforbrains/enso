# Slack teams mode

Teams mode gives each exact Slack conversation a workspace and an access profile. It is intentionally small: Slack decides who is in a channel, Enso chooses where the CLI starts and which native policy it uses, and the CLI enforces that policy.

Telegram is unchanged. It remains private, one-to-one, and authorized by `transports.telegram.allowed_users`.

## Model

| Concept        | Purpose                                                                                                              |
| -------------- | -------------------------------------------------------------------------------------------------------------------- |
| Workspace      | A directory containing shared project knowledge, instructions, and native workspace skills, plus a concurrency limit |
| Access profile | The providers, default provider, chat commands, and native provider policy available to a route                      |
| Route          | An exact Slack DM user ID or channel ID bound to one workspace and one access profile                                |

A user does not carry a permission level into every room. The route is the security boundary:

- An exact DM route authorizes that Slack user.
- An exact channel route authorizes every human member posting in that channel.
- Everyone using a channel gets the same workspace and access profile, including administrators.
- Threads inherit their parent channel route but keep their own conversation session.
- An unlisted DM or explicit mention in an unlisted channel receives a fixed local access message. Ordinary messages in unlisted channels are ignored. There is no default or wildcard route.

This keeps Slack behavior understandable: if a room is safe for a client, the Enso agent in that room is client-safe too. Staff use a separate internal channel when they need broader authority.

## Example: one client, two trust levels

`#client-acme` and `#client-acme-internal` can share the same files while using different native policies:

```text
#client-acme          -> workspace acme -> access client-readonly
#client-acme-internal -> workspace acme -> access staff
owner DM              -> workspace company -> access admin
#company              -> workspace company -> access staff
```

The client channel may answer questions from the project knowledge but deny edits and administrative commands. The internal channel starts in the same directory, so it sees the same project instructions and skills, but the staff profile may write documentation or use additional tools. Sessions remain separate because they belong to their Slack channel or thread, not merely to the filesystem directory.

The client profile should prevent writes to control files such as `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.agents/`, and skill definitions. If clients need to create material, grant write access only to an ordinary data directory such as `drafts/` or `uploads/`. Otherwise a client could alter instructions later trusted by the more privileged internal route.

## Company and client directories

A practical layout for a small team is:

```text
~/.enso/workspaces/
├── company/
│   ├── AGENTS.md
│   ├── .agents/skills/
│   └── .claude/skills/
└── clients/
    ├── acme/
    │   ├── AGENTS.md
    │   ├── README.md
    │   ├── .agents/skills/
    │   └── .claude/skills/
    └── globex/
```

The staff native policy may grant the company route read or write access to `~/.enso/workspaces/clients/**`. This lets an operator normalize project storage with ordinary directories instead of teaching Enso about project types or mounting several workspaces into one request.

Starting the CLI in `company/` does not reliably make every provider discover instructions or skills in a sibling client directory. The company `AGENTS.md` should therefore tell the agent where client workspaces live and require it to read the selected client's protected `AGENTS.md` and project overview before working there. This is explicit and provider-independent; Enso does not synthesize an instruction chain.

For work that should automatically begin with one client's project instructions and skills, use an internal client channel whose route starts directly in that client workspace.

## Configuration

The complete schema is in [data-model.md](data-model.md#slack-teams-mode). This example shows the relationships:

```jsonc
{
  "workspaces": {
    "company": {
      "path": "~/.enso/workspaces/company",
      "concurrency": 1
    },
    "acme": {
      "path": "~/.enso/workspaces/clients/acme",
      "concurrency": 1
    }
  },

  "access": {
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
      "chat_commands": "*"
    },
    "client-readonly": {
      "policy_dir": "~/.enso/policies/client-readonly",
      "providers": ["claude"],
      "default_provider": "claude",
      "chat_commands": ["status", "clear", "stop", "help"]
    }
  },

  "routes": {
    "slack": {
      "account_id": "T0YOURTEAM",
      "dms": {
        "U01OWNER": {
          "workspace": "company",
          "access": "admin",
          "audit": false
        }
      },
      "channels": {
        "C0ACME": {
          "workspace": "acme",
          "access": "client-readonly",
          "audit": true
        },
        "C0ACMEINTERNAL": {
          "workspace": "acme",
          "access": "staff",
          "audit": false
        },
        "C0COMPANY": {
          "workspace": "company",
          "access": "staff",
          "audit": false
        }
      }
    }
  }
}
```

The same access profile may be reused across many workspaces when its native policy is written in terms of the invocation workspace. For example, every external client channel can use `client-readonly`; each route still starts in its own client's directory.

## Resolution and lifecycle

On startup Enso validates the Slack account ID, exact routes, workspaces, access profiles, enabled providers, and native policy plumbing. Configuration changes require an Enso restart. Enso deliberately does not hot-reload authorization while work is queued or running.

For each Slack event Enso:

1. Verifies the authenticated Slack account.
1. Accepts an ordinary DM or explicit channel mention. Other channel messages are ignored.
1. Resolves the exact DM user ID or channel ID and claims its delivery ID for retry deduplication.
1. If the location is unlisted, returns the fixed local response described below and stops.
1. Resolves a configured route's workspace and access profile.
1. Checks that the selected provider and native policy can be launched.
1. Runs the provider directly in the workspace directory.

An invalid route or policy never falls back to another workspace, profile, legacy `working_dir`, or unrestricted execution. Configuration errors for an otherwise authorized route are reported. A globally invalid configuration cannot establish usable teams routing, and an event from the wrong Slack account remains silent and is logged rather than receiving an access response.

Slack DMs dispatch ordinary messages. Channels dispatch only explicit bot mentions, including inside threads. An unlisted DM receives `I haven't been enabled for your DMs yet. Ask an Enso admin for access.` An explicit mention in an unlisted channel receives `I haven't been enabled in this channel yet. Ask an Enso admin to set me up.` as a thread reply. These are fixed transport responses: Enso does not resolve a workspace or access profile, fetch context or attachments, invoke an LLM, or create an audit record. They pass through the delivery ledger so a retried Slack event receives at most one reply.

For configured routes, route resolution still occurs before surrounding context or attachments are fetched. Channel context is untrusted input even though every member is authorized to invoke the route.

`enso policy check` inspects Enso's configuration and policy-file plumbing. `enso route explain slack <user-id> [channel-id]` explains the local routing decision. Neither command certifies that a native policy has the intended meaning; test policies with the provider's own tools and disposable files.

## Providers and commands

An access profile declares its available providers, default provider, and chat commands. `!help` and `!use` show only capabilities offered by the route's access profile. Service-wide commands such as update, restart, and logs normally belong only to an administrative profile.

Enso never combines a user policy with a channel policy and never translates policies between providers. A route selects one complete access profile, and that profile selects one native policy for the active CLI.

## Skills and instructions

Project instructions and skills are ordinary provider-native files in the workspace: `AGENTS.md` and `.agents/skills/` for Codex, plus `CLAUDE.md` and `.claude/skills/` for Claude Code. The CLIs may also expose their native user, managed, plugin, system, or bundled skill scopes; Enso does not suppress those scopes or maintain a skill allowlist. A project skill adds relevant behavior but is not proof that other skills are absent. Treat all skill discovery as functionality rather than isolation, and rely on the selected native policy for actual authority.

Use project-specific skills in the relevant client workspace and company-wide skills in the company workspace. A staff route starting directly in a client workspace naturally sees that client's project material. A route starting in the company workspace must explicitly read a client's protected instructions before working across directories.

## Jobs

Scheduled jobs are independent of Slack teams routing. They continue to use the existing global `working_dir`, provider configuration, locks, and run history. Enabling `routes.slack` does not require a `workspace:` field, change prerun behavior, or reschedule existing jobs.

If project-scoped jobs are needed later, that should be a separate explicit design rather than an implicit consequence of enabling Slack routes.

## Audit

`audit` is optional route metadata and defaults to `false`. When enabled, Enso attempts to record the triggering message and outcome using its audit store. This is useful operational evidence, not a complete security transcript: Slack history, fetched context, provider sessions, tool activity, status edits, and out-of-band messages have their own retention and visibility.

Provider policy must keep restricted agents away from Enso's config, secrets, policies, database, and service-control commands whether route auditing is enabled or not.

## Migration

The presence of `routes.slack` enables teams mode. It is mutually exclusive with the legacy Slack `allowed_users` key. With no teams block, legacy Slack continues to use `allowed_users` and `working_dir` exactly as before.

Earlier branch configurations using `groups`, route `allow`, route `context_from`, or permission fields inside `workspaces` are rejected with a migration error. Convert them explicitly:

- Move `unrestricted`, `policy_dir`, `providers`, `default_provider`, and `chat_commands` from each workspace into a named `access` profile.
- Add `access` to every route.
- Key DMs by exact Slack user ID.
- Remove groups and route allowlists; a configured channel authorizes its members uniformly.

No routes are synthesized during migration because creating a route grants access.
