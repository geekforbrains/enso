# Slack routes and team access

Slack gives each exact conversation route a workspace and an access profile. The model is intentionally small: Slack decides who belongs in a channel, Enso selects where the provider CLI starts and which native policy it receives, and the installed CLI enforces that policy.

Telegram is separate. It remains private, one-to-one, and authorized by exact numeric IDs in `transports.telegram.allowed_users`; interactive Telegram work uses the global `working_dir`.

## Model

| Concept        | Purpose                                                                                                 |
| -------------- | ------------------------------------------------------------------------------------------------------- |
| Route          | An exact Slack DM user ID or channel ID mapped to one workspace and one access profile                  |
| Workspace      | A shared content root and provider cwd, with a process-local concurrency limit                          |
| Access profile | Available providers, default provider, allowed Enso chat commands, and native provider policy selection |
| Native policy  | Provider-specific settings interpreted and enforced by the installed Claude Code or Codex CLI           |

A user does not carry a permission level into every room:

- An exact DM route authorizes that Slack user.
- An exact channel route authorizes every human member who can post in that channel.
- Everyone using a channel gets the same workspace and access profile, including administrators.
- Threads inherit their parent channel route but keep their own conversation session.
- An unlisted DM or explicit mention in an unlisted channel receives a fixed local access message. Ordinary messages in unlisted channels are ignored.
- There is no default route, wildcard route, group overlay, sender ranking, or Slack `allowed_users` mode.

The workspace is not a security boundary. It is shared content and cwd. Authority comes from the route's access profile and the selected CLI's native policy, plus any outer operating-system isolation the operator chooses to add.

## Example: one client, two trust levels

`#client-acme` and `#client-acme-internal` can share the same files while using different access profiles:

```text
#client-acme          -> workspace acme -> access client-readonly
#client-acme-internal -> workspace acme -> access staff
owner DM              -> workspace company -> access admin
#company              -> workspace company -> access staff
```

The client channel may answer questions from project knowledge but deny edits and administrative commands. The internal channel starts in the same directory, so it sees the same project material, but the staff profile may write documentation or use additional tools. Sessions remain separate because they belong to their Slack channel or thread, not merely to the filesystem directory.

The client native policy should prevent writes to control files such as `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.agents/`, and skill definitions. If clients need to create material, grant write access only to an ordinary data directory such as `drafts/`. Otherwise a client could alter instructions later trusted by the more privileged internal route.

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

The complete schema is in [data-model.md](data-model.md#execution-catalog-and-slack-routes). This example shows the relationships:

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

Slack requires `routes.slack`. The same top-level workspace and access catalogs also serve jobs and are parsed independently of Slack, so a Telegram-only installation can still run policy-bound jobs.

The same access profile may be reused across many workspaces when its native policy is written in terms of the invocation workspace. For example, every external client channel can use `client-readonly`; each route still starts in its own client's directory.

## Resolution and lifecycle

At Slack startup Enso authenticates the account, loads the exact routes and execution catalog, and checks native policy plumbing for providers used by those routes. Jobs are checked separately by `enso config check` and revalidated before each execution. Changes to `config.json` require an Enso restart; Enso deliberately does not hot-reload route authorization while work is queued or running.

For each Slack event Enso:

1. Verifies the authenticated Slack account.
1. Accepts an ordinary DM or explicit channel mention. Other channel messages are ignored.
1. Resolves the exact DM user ID or channel ID and claims its delivery ID for retry deduplication.
1. If the location is unlisted, returns the fixed local response described below and stops.
1. Resolves a configured route's workspace and access profile.
1. Checks that the selected provider and native policy can be launched.
1. Runs the provider directly in the workspace directory.

An invalid route or native policy never falls back to another workspace, access profile, global `working_dir`, or unrestricted execution. Configuration errors for an otherwise authorized route are reported. A globally invalid configuration cannot establish usable Slack routing, and an event from the wrong Slack account remains silent and is logged rather than receiving an access response.

Slack DMs dispatch ordinary messages. Channels dispatch only explicit bot mentions, including inside threads. An unlisted DM receives `I haven't been enabled for your DMs yet. Ask an Enso admin for access.` An explicit mention in an unlisted channel receives `I haven't been enabled in this channel yet. Ask an Enso admin to set me up.` as a thread reply. These are fixed transport responses: Enso does not resolve a workspace or access profile, fetch context or attachments, invoke an LLM, or create an audit record. They pass through the delivery ledger so a retried Slack event receives at most one reply.

For configured routes, route resolution still occurs before surrounding context or attachments are fetched. Channel context is untrusted input even though every member is authorized to invoke the route.

`enso config check` inspects Enso's configuration and native-policy launch plumbing. `enso route explain slack <user-id> [channel-id]` explains the local routing decision. Neither command certifies that a native policy has the intended meaning; test policies with the installed provider CLI and disposable files.

## Providers and Enso commands

An access profile declares available providers, a default provider, and allowed Enso chat commands. `!help` and `!use` show only capabilities offered by the route's access profile. Service-wide Enso commands such as update, restart, and logs normally belong only to an administrative profile.

`chat_commands` controls Enso's `!` command surface. It does not hide or authorize the provider CLI's own tools, slash commands, skills, plugins, hooks, or MCP servers. Commands such as `!status`, `!clear`, and `!stop` are handled by Enso; `!compact` launches the active provider and therefore also remains subject to the selected native policy.

Enso never combines user-level permissions with a channel's access profile and never translates policies between providers. A route selects one complete access profile, and that profile selects one native policy for the active CLI.

## Skills and instructions

Project instructions and skills are ordinary provider-native files in the workspace: `AGENTS.md` and `.agents/skills/` for Codex, plus `CLAUDE.md` and `.claude/skills/` for Claude Code. The CLIs may also expose native user, managed, plugin, system, or bundled skill scopes; Enso does not suppress those scopes or maintain a skill allowlist. A project skill adds relevant behavior but is not proof that other skills are absent. Treat skill discovery as functionality rather than isolation, and rely on the selected native policy for actual authority.

Claude Code behavior changes independently of Enso. Operators should review the official [permissions](https://code.claude.com/docs/en/permissions), [settings](https://code.claude.com/docs/en/settings), [tools reference](https://code.claude.com/docs/en/tools-reference), and [skills](https://code.claude.com/docs/en/skills) documentation, then test their installed CLI. Enso supplies native settings; it does not certify their meaning.

Use project-specific skills in the relevant client workspace and company-wide skills in the company workspace. A staff route starting directly in a client workspace naturally sees that client's project material. A route starting in the company workspace must explicitly read a client's protected instructions before working across directories.

## Jobs

Jobs are defined under `~/.enso/jobs/`, not as Slack routes, but every `JOB.md` must select the same two named execution objects:

```yaml
workspace: company
access: automation
```

The job's existing `provider` and `model` remain authoritative. The provider must be allowed by the selected access profile, runs with the named workspace as cwd, and receives that profile's native policy. Missing, unknown, incomplete, or unsafe bindings fail before prerun and provider execution; there is no global or unrestricted fallback.

An optional prerun script remains trusted host-side automation. Enso runs it through Bash with the job directory as cwd, outside the provider's native policy, then injects allowed stdout into the provider prompt. Keep prerun scripts protected and review them as executable operator code.

The job's `notify` destination remains independent of Slack routes. Scheduled successes are silent unless the prompt explicitly sends a message. Host-side failure and recovery alerts use `notify` or the transport's `notify_channel`; a manual `enso job run` suppresses those automatic alerts but cannot suppress a message explicitly sent by the provider process.

A persistent per-job `.run.lock` coordinates the scheduler, CLI, and dashboard across processes. Jobs also use the named workspace's process-local semaphore, but separate Enso processes do not provide cross-process workspace serialization.

## Audit

`audit` is optional route metadata and defaults to `false`. When enabled, Enso attempts to record the triggering message and outcome using its audit store. This is useful operational evidence, not a complete security transcript: Slack history, fetched context, attachments, status edits, reasoning, tool calls, native provider sessions, and out-of-band messages have their own retention and visibility.

The metadata-only Slack delivery ledger exists independently of route auditing and prevents retried Slack events from triggering duplicate work or duplicate canned no-route replies. Pending ledger claims left by a crash are closed at startup, and ledger rows are pruned on their own retention schedule.

Provider policy must keep restricted agents away from Enso's config, secrets, policies, database, jobs, and service-control commands whether route auditing is enabled or not.

## Migration

This release intentionally removes the old Slack allowlist path. A Slack transport without `routes.slack`, or with `transports.slack.allowed_users`, is invalid. Migrate each authorized DM user and channel to an exact route selecting a known workspace and access profile. Enso never synthesizes routes because doing so grants access.

Telegram still uses `transports.telegram.allowed_users`, but entries must be exact numeric user IDs. The old `allowed_user_ids` spelling and `"*"` wildcard are not supported. Telegram accepts only private chats.

Every existing job must add both `workspace` and `access`. The catalogs are top-level configuration and do not depend on Slack being enabled.

Earlier branch configurations using `groups`, route `allow`, route `context_from`, or permission fields inside `workspaces` are also rejected. Move `unrestricted`, `policy_dir`, `providers`, `default_provider`, and `chat_commands` into named access profiles; add `access` to every route and job; and key each DM route by exact Slack user ID.
