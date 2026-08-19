# Transport bindings and workspace policies

Slack gives each exact conversation route a workspace, and each workspace selects exactly one reusable policy. The model is intentionally small: Slack decides who belongs in a channel, Enso selects where the provider CLI starts, the workspace selects which native policy it receives, and the installed CLI enforces that policy.

This document owns who may invoke a Slack route. [slack-triggers.md](slack-triggers.md) owns when a channel message engages Enso at all — per-channel mention requirements, thread following, and `!` command addressing. [slack-output.md](slack-output.md) owns how authorized replies render and how App Home or Canvas drafts are confirmed.

Telegram remains private, one-to-one, and authorized by exact numeric IDs in `transports.telegram.allowed_users`, but it uses the same execution catalog. `transports.telegram.workspace` is required and derives that workspace's single policy, including provider availability, default provider, Enso commands, native launch, cwd, uploads, and concurrency.

## Model

| Concept       | Purpose                                                                                                    |
| ------------- | ---------------------------------------------------------------------------------------------------------- |
| Route         | An exact Slack DM user ID or channel ID mapped to one workspace, with durable provider/model/effort choices |
| Workspace     | A shared content root and provider cwd, with one policy and a process-local concurrency limit               |
| Policy        | Available providers, default provider, allowed Enso chat commands, and native provider policy selection    |
| Native policy | Provider-specific settings interpreted and enforced by the installed Claude Code, Codex, or Grok CLI       |

A user does not carry a permission level into every room:

- An exact DM route authorizes that Slack user.
- An exact channel route authorizes every human member who can post in that channel.
- Everyone using a channel gets the same workspace, policy, and durable provider/model/effort settings, including administrators.
- Threads inherit their parent channel route — including its durable settings and response triggers ([slack-triggers.md](slack-triggers.md)) — but keep their own conversation session.
- An unlisted DM or explicit mention in an unlisted channel receives a fixed local access message. Ordinary messages in unlisted channels are ignored; `mention_required` and `channel_defaults` configure routed channels only and never make an unrouted channel responsive.
- There is no default route, wildcard route, group overlay, sender ranking, or Slack `allowed_users` mode. `transports.slack.channel_defaults` supplies default response-trigger settings to channel routes ([slack-triggers.md](slack-triggers.md)); it is settings inheritance, not authorization, and routes nothing by itself.

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

## Company and client workspaces

A practical convention for a small team is:

```text
~/.enso/workspaces/
├── company/
│   ├── AGENTS.md
│   ├── CLAUDE.md -> AGENTS.md
│   ├── skills/
│   ├── .agents/skills -> ../skills
│   ├── .claude/skills -> ../skills
│   ├── knowledge/
│   ├── drafts/
│   └── uploads/
├── acme/
│   ├── AGENTS.md
│   ├── CLAUDE.md -> AGENTS.md
│   ├── skills/
│   ├── .agents/skills -> ../skills
│   ├── .claude/skills -> ../skills
│   ├── knowledge/
│   ├── drafts/
│   └── uploads/
└── globex/
```

Workspace names are lowercase kebab-case and determine these roots exactly:
`~/.enso/workspaces/<name>`. The tree is flat; config cannot select an external path,
nested root, or workspace symlink. `~/.enso/workspaces` and every workspace root must be
physical directories, and a direct `.git` entry makes a workspace invalid (repositories
deeper inside ordinary content are allowed). `knowledge/` holds durable shared material,
`drafts/` holds ordinary writable output, and Enso stores downloaded attachments in
persistent `uploads/<random-id>/` directories. Inbound Telegram files are limited to 20
MiB and inbound Slack files to 100 MiB per file; Enso checks available metadata and the
received bytes, and skips unsafe or oversized downloads. Enso does not automatically
expire retained uploads; retention and cleanup belong to the operator.

Fresh setup seeds the global prompt and skills plus the default workspace. Every later
workspace creation atomically publishes the complete structure shown above, a short local
prompt, and `knowledge/README.md`; local `skills/` starts empty. Those files become
user-owned immediately. Startup and configuration checks validate without changing
content, while explicit setup repair creates only missing structural directories and
known relative discovery links. It preserves and reports missing content or conflicting
paths instead of overwriting them.

The staff native policy may grant the company route read or write access to selected
siblings such as `~/.enso/workspaces/acme/**`. This does not mount another workspace or
change the company route's own cwd.

Starting the CLI in `company/` does not reliably make every provider discover instructions or skills in a sibling client directory. The company `AGENTS.md` should tell the agent where client workspaces live and require it to read the selected client's protected instructions and project overview before working there. Enso does not synthesize an instruction chain.

For work that should automatically begin with one client's project instructions and skills, use an internal client channel whose route starts directly in that client workspace.

## Configuration

The complete schema is in [data-model.md](data-model.md#execution-catalog-and-transport-bindings). Workspace names are at most 64 characters of lowercase letters and numbers separated by single hyphens; policy names retain their broader portable identifier syntax. This example shows the relationships:

```jsonc
{
  "transports": {
    "telegram": {
      "bot_token": "...",
      "allowed_users": ["123456789"],
      "notify_channel": "123456789",
      "workspace": "default"
    },
    "slack": {
      "bot_token": "xoxb-...",
      "app_token": "xapp-...",
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
  },

  "workspaces": {
    "default": {
      "policy": "admin",
      "concurrency": 1
    },
    "company": {
      "policy": "staff",
      "concurrency": 1
    },
    "acme": {
      "policy": "client-readonly",
      "concurrency": 1
    },
    "acme-internal": {
      "policy": "staff",
      "concurrency": 1
    }
  },

  "policies": {
    "admin": {
      "unrestricted": true,
      "providers": ["claude", "codex", "grok", "agy"],
      "default_provider": "claude",
      "chat_commands": "*"
    },
    "staff": {
      "policy_dir": "~/.enso/policies/staff",
      "providers": ["claude", "codex", "grok"],
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
  }
}
```

Slack credentials, transport-wide options, and exact routes coexist in `transports.slack`. Slack requires `account_id` there; any DM and channel routes are declared in its `dms` and `channels` maps. The same top-level workspace and policy catalogs serve Telegram and jobs and are parsed independently of Slack.

Here `channel_defaults` makes routed channels fully responsive, and `C0ACME` opts back into mention-only; both settings, their defaults, and their validation rules are specified in [slack-triggers.md](slack-triggers.md). Neither key is valid on a DM route.

The same policy may be reused across many workspaces when its native policy is written in terms of the invocation workspace. For example, every client channel can use `client-readonly`; each route still starts in its own name-derived directory.

## Resolution and lifecycle

At Slack startup Enso authenticates the account, loads the exact routes and execution catalog, and checks the repository, canonical workspace scaffold, unique global/workspace skill names, and native policy plumbing without seeding or repair. Jobs are checked separately by the read-only `enso config check` and revalidated before each execution. Changes to `config.json` require an Enso restart; Enso deliberately does not hot-reload route authorization while work is queued or running.

For each Slack event Enso:

1. Verifies the authenticated Slack account.
2. Accepts an ordinary DM or explicit channel mention always, and a non-mention channel message only when its channel's route settings allow it ([slack-triggers.md](slack-triggers.md)): effective `mention_required: false` for a top-level message, or effective `thread_mention_required: false` for a reply in a thread Enso already participates in (a thread a prior dispatch joined, or one rooted by a message Enso posted itself). Other channel messages are ignored.
3. Resolves the exact DM user ID or channel ID and claims its delivery ID for retry deduplication.
4. If the location is unlisted, returns the fixed local response described below and stops.
5. Resolves the configured workspace and its policy.
6. Resolves the route's durable provider/model/effort choices through the current policy.
7. Handles an admitted `!` command, or checks the effective provider's native policy and runs it directly in the workspace directory.

An invalid route or native policy never falls back to another workspace, policy, implicit cwd, or unrestricted execution. A stored provider choice that the current policy no longer allows is ignored in favor of that policy's declared default without erasing the stored preference. By contrast, a policy-allowed but native-unusable effective provider reports the existing configuration error for provider work; non-launch commands remain available so the user can inspect status or repair the choice. A globally invalid configuration cannot establish usable Slack routing, and an event from the wrong Slack account remains silent and is logged rather than receiving an access response.

Slack DMs dispatch ordinary messages. Channels always dispatch explicit bot mentions, including inside threads; a routed channel additionally dispatches non-mention messages where its effective `mention_required` or `thread_mention_required` is `false` ([slack-triggers.md](slack-triggers.md)), and replies to channel messages always land in the message's thread. Unlisted locations respond only to explicit contact. An unlisted DM receives `I haven't been enabled for your DMs yet. Ask an Enso admin for access.` An explicit mention in an unlisted channel receives `I haven't been enabled in this channel yet. Ask an Enso admin to set me up.` as a thread reply. These are fixed transport responses: Enso does not resolve a workspace or policy, fetch context or attachments, invoke an LLM, or create an audit record. They pass through the delivery ledger so a retried Slack event receives at most one reply.

For configured routes, route resolution still occurs before surrounding context or attachments are fetched. Channel context is untrusted input even though every member is authorized to invoke the route.

Thread context is always pushed: a threaded turn receives that thread's messages since Enso last spoke, or the whole thread when the conversation has no provider session yet. Channel history is not. An unrestricted policy instead receives, on the first turn of a conversation, the `enso slack history` and `enso slack thread` commands for the channel it is replying in, and fetches history only when the request calls for it. A restricted policy cannot be assumed to reach the network, so it keeps receiving the pushed channel context it cannot fetch for itself. Pushing that history unconditionally meant a new top-level request arrived carrying the roots of unrelated earlier threads, and the agent answered them.

Rich output does not add route authority. A structured reply receives the same destination as its ordinary text fallback. A persistent-surface draft captures the exact authenticated account, route, requester, workspace, policy, audit setting, conversation, and confirmation message before it can be shown. Only that requester may use its controls. At click time Enso resolves the route again and requires those bindings to remain identical; a removed or changed route revokes the draft instead of falling back to another workspace or policy.

On an audited route, Publish or Cancel creates a separate `surface_confirmation` audit turn before the draft is consumed. The existing `audit.on_failure` policy applies before any Canvas or App Home mutation. The original provider turn retains the full exact confirmation preview that was delivered; the click records the same preview plus the human decision and terminal publication result.

`enso config check` inspects Enso's configuration, validates the canonical shared instruction source, and checks native-policy launch plumbing. `enso route explain slack <user-id> [channel-id]` explains the local routing decision. Neither command certifies that a native policy has the intended meaning; test policies with the installed provider CLI and disposable files.

### Telegram resolution

Telegram rejects non-private chats and authorizes the sender by exact numeric ID before resolving execution. A usable `transports.telegram.workspace` then supplies the workspace and its single policy. If the binding or selected provider's native policy is invalid, Enso returns a fixed configuration error and launches no provider.

Telegram's command menu contains only commands allowed by the bound policy, `/use` lists the subset of policy-authorized providers whose native launch is usable, and every command callback reauthorizes the same current binding. Durable provider/model/effort settings are keyed only by private chat, while conversation state is keyed by chat, workspace, and policy. Changing the binding therefore keeps the chat's settings but cannot silently resume a session created under different authority. Telegram receives global background messages by explicit transport choice; Slack routes and jobs do not.

Messages, compaction, session clearing, and unique `uploads/<random-id>/` attachment directories all use that resolved workspace. Telegram has no global or unrestricted execution path and cannot override the workspace policy.

## Providers and Enso commands

A policy declares authorized providers, a default provider, and allowed Enso chat commands. Slack's `!help` and Telegram's menu and `/help` show only commands offered by the workspace policy. `!use` and `/use` show the narrower subset of authorized providers whose current native launch is usable. Service-wide Enso commands such as update, restart, and logs normally belong only to an administrative policy.

`!use`, `!model`, and `!effort` update the whole Slack route, not one root or thread; they are valid inside a thread and explicitly say that their result applies to the entire channel or DM. Telegram applies the equivalent commands to that private chat. Passing `default` clears the explicit provider, model, or effort choice so resolution follows the policy, provider configuration, or CLI default again. `!status` and `/status` report the effective values and label each as a route selection, policy default, provider default, or CLI default. These settings are durable and do not expire with conversation retention.

A restricted policy can additionally grant named environment variables through `env_passthrough` (names, never values) and, for Claude, an exact MCP server allowlist through the conventional `<policy_dir>/claude/mcp.json`. Grok has no policy-declared MCP channel wired today: the untrusted workspace contributes no MCP servers, but Grok's home-scope vendor-compat discovery still reaches the operator's own MCP configuration through `$HOME` ([permissions.md's known limitation](permissions.md#grok)), so a restricted Grok policy that must not reach ambient MCP tools carries a bare `MCPTool` deny rule. Both grants default to off, and both are real grants: MCP servers are dialled by the provider process itself and bypass the sandbox's network rules, so grant only servers whose entire tool surface is acceptable, and a passthrough variable's value is readable by any policy that can run Bash — passthrough delivers a credential, it does not scope one. Neither applies to an unrestricted policy, which already inherits everything (`env_passthrough` there is a config error). See [permissions.md](permissions.md#granting-credentials-and-mcp-servers-to-a-restricted-policy) for how and when to use them.

`chat_commands` controls Enso's `!` command surface. It does not hide or authorize the provider CLI's own tools, slash commands, skills, plugins, hooks, or MCP servers. Slack first applies the route's normal response trigger; once the message is admitted, any non-bare `!` prefix is parsed as a command whether or not it used a mention. A responsive top level or joined thread therefore accepts mention-free commands, while mention-gated and unjoined threads remain gated; a bare `!` stays prompt text ([slack-triggers.md](slack-triggers.md)). Commands such as `!status`, `!clear`, and `!stop` are handled without launching a provider; `!compact` launches the effective provider and therefore also remains subject to its native policy.

Enso never combines user-level permissions with a channel's policy and never translates policies between providers. A route selects one workspace, and that workspace selects one complete policy for the active CLI.

## Skills and instructions

Enso has two instruction layers. Canonical shared operational instructions live at
`~/.enso/AGENTS.md`, with `~/.enso/CLAUDE.md -> AGENTS.md`; each workspace carries focused
project instructions in its own `AGENTS.md` and `CLAUDE.md` view. Claude and Codex discover
both layers natively because every provider starts at the exact name-derived workspace
inside the `~/.enso` Git root. Enso does not also inject those bytes. Grok receives the
freshly validated shared content once through `--rules`, and unrestricted Agy receives it
once through an Enso prompt envelope. Immediately before each spawn, Enso revalidates the
physical root/workspace topology, exact Git root, discovery links, duplicate skill names,
and current shared source; invalid or partial discovery never falls back to another
delivery mode. The CLIs may also expose native user, managed, plugin, system, or bundled
skill scopes; Enso does not suppress those scopes or maintain a skill allowlist. A project
skill adds relevant behavior but is not proof that other skills are absent. Treat
instruction and skill discovery as functionality rather than isolation, and rely on the
selected native policy for actual authority.

The shared template states that the active policy is authoritative and that quoted, forwarded, fetched, attached, or otherwise untrusted transport content is data rather than higher-priority instructions. Workspace files supplement that shared layer; they cannot widen the policy.

Claude Code behavior changes independently of Enso. Operators should review the official [permissions](https://code.claude.com/docs/en/permissions), [settings](https://code.claude.com/docs/en/settings), [tools reference](https://code.claude.com/docs/en/tools-reference), and [skills](https://code.claude.com/docs/en/skills) documentation, then test their installed CLI. Enso supplies native settings; it does not certify their meaning.

Enso-wide skills live canonically under `~/.enso/skills/`, with the exact relative views
`~/.enso/.agents/skills -> ../skills` and `~/.enso/.claude/skills -> ../skills`. Each
workspace has its own canonical `<workspace>/skills/`, with `.agents/skills -> ../skills`
and `.claude/skills -> ../skills`. Claude uses the latter view, Codex and Agy use the
former, and Grok reads Claude Code skills. Fresh setup copies the bundled global set once;
workspace skills start empty, and no startup installer changes either source later.

Put a genuinely project-specific skill's canonical copy under
`<workspace>/skills/<name>/SKILL.md`. Root and workspace skill directory names must be
unique for that workspace; duplicates fail validation rather than relying on a provider's
precedence. Every skill follows the [Agent Skills specification](https://agentskills.io/specification), and the bundled `workspace` skill carries the operational workflow.

After one coherent edit to instructions, canonical skills, or workspace knowledge, an
agent should create one local snapshot with explicit paths:

```bash
enso snapshot create --message "docs: update client onboarding" -- \
  AGENTS.md knowledge/onboarding.md
```

Relative paths resolve from the provider's workspace cwd. The command accepts only
versionable content below `~/.enso`; policies, configuration, credentials, uploads,
drafts, and runtime state remain outside its allowlist. A native policy still decides
whether the provider may execute Enso and modify those files. See
[snapshots.md](snapshots.md) for the complete boundary.

A staff route starting directly in a client workspace naturally sees that client's project material. A route starting in the company workspace must explicitly read a client's protected instructions before working across directories.

## Jobs

Jobs are defined under `~/.enso/jobs/`, not as Slack routes, but every `JOB.md` must select one named workspace:

```yaml
workspace: company
```

The job's existing `provider` and `model` remain authoritative. The provider must be allowed by the workspace policy, runs with the named workspace as cwd, and receives that policy's native configuration plus shared and workspace-local instructions. Missing, unknown, incomplete, or unsafe bindings fail before prerun and provider execution; there is no implicit or unrestricted fallback.

An optional prerun script remains trusted host-side automation. Enso runs it through Bash with the job directory as cwd, outside the provider's native policy, then injects allowed stdout into the provider prompt. Keep prerun scripts protected and review them as executable operator code.

The job's `notify` destination remains independent of Slack routes. Scheduled successes are silent unless the prompt explicitly sends a message. Host-side failure and recovery alerts use `notify` or the transport's `notify_channel`; a manual `enso job run` suppresses those automatic alerts but cannot suppress a message explicitly sent by the provider process.

A persistent per-job `.run.lock` coordinates the scheduler, CLI, and dashboard across processes. Jobs also use the named workspace's process-local semaphore, but separate Enso processes do not provide cross-process workspace serialization.

## Audit

`audit` is optional route metadata and defaults to `false`. When enabled, Enso attempts to record the triggering message and outcome using its audit store. This is useful operational evidence, not a complete security transcript: Slack history, fetched context, attachments, status edits, reasoning, tool calls, native provider sessions, and out-of-band messages have their own retention and visibility.

The metadata-only Slack delivery ledger exists independently of route auditing and prevents retried Slack events from triggering duplicate work or duplicate canned no-route replies. Pending ledger claims left by a crash are closed at startup, and ledger rows are pruned on their own retention schedule.

Provider policy must keep restricted agents away from Enso's config, secrets, policies, database, jobs, and service-control commands whether route auditing is enabled or not.

## Migration

Legacy `working_dir`, workspace `path`, top-level `routes` and `access`, route/job policy overrides, and Telegram without a workspace are rejected. This migration cannot be inferred safely when one old workspace carried several access profiles, lived outside the canonical tree, or mixed shared and local instructions. First follow the [manual unified-workspace migration](../migrations/unified-workspace-policies.md) for binding and policy changes, then the [v1.3 managed-workspace migration](../migrations/v1.3-managed-workspaces.md) for names, file moves, links, and the removed `path` field. Enso provides no `enso migrate` command or legacy-path fallback.
