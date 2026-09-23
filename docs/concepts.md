# Concepts

Enso runs on your machine as a chat assistant and background worker. Slack or Telegram
connects you to an agent CLI in a workspace. Jobs handle recurring work; Heartbeat handles
finite follow-ups. This page introduces those pieces and links to their detailed contracts.

## Installation trust model

One installation is one trusted environment for a person or small team, with one service,
database, and set of credentials. Workspaces organize context and ownership; they do not
isolate data or permissions. Teams needing separation use separate installations on separate
machines or VPSs with their own credentials and chat connections.

Provider CLI settings control agent permissions. Enso authenticates transports, validates
input, and preserves user data, but is not a sandbox. See
[provider permissions](configuration.md#provider-permissions-and-installation-trust) and
[connection access](connections.md#access-in-020).

## The primitives

| Primitive | Meaning |
| --- | --- |
| **Home** | Enso's configuration, files, and runtime state, normally `~/.enso` |
| **Transport** | A Slack or Telegram connection |
| **Binding** | A chat location mapped to a workspace; unbound locations cannot start work |
| **Workspace** | An agent's working directory, instructions, and owned work |
| **Knowledge** | Durable Markdown notes, shared across workspaces by default |
| **Agent** | An explicit `provider`, `model`, and `effort` triple |
| **Conversation** | A serialized queue of chat turns with provider sessions |
| **Job** | Recurring or stage-triggered work defined in `JOB.md` |
| **Beat** | A future action or finite situation followed by Heartbeat |
| **Run** | A recorded job or beat execution, including its outcome and output |
| **Project** | A task pipeline owned by a workspace, optionally backed by Git |
| **Task** | A unit of work with a spec, stage, and timeline |
| **Workflow transaction** | A submitted stage result and the evidence Enso needs to accept it |
| **Skill** | Instructions an agent loads when relevant |
| **Table** | A registered user SQLite table in `enso.db` |
| **Message** | An out-of-band send recorded for later conversation context |
| **Secret** | An encrypted named value supplied to selected commands or jobs |
| **Viewer** | The optional web UI for browsing Enso and editing instructions and secrets |

## Home

`ENSO_HOME` selects the home; the default is `~/.enso`:

```text
~/.enso/
├── config.json          # installation configuration
├── config.example.json  # incomplete template prepared by init
├── AGENTS.md            # instructions shared by turns and jobs
├── CLAUDE.md -> AGENTS.md
├── skills/              # bundled, optional, and custom home skills
├── shared/knowledge/    # shared Markdown notes
├── workspaces/<name>/   # workspace instructions, jobs, projects, and work
├── enso.db              # operational records, secrets, and user tables
├── enso.log             # rotating application log
├── .bundles.json        # installed bundle hashes
├── .migrations.json     # completed home migration revision
├── runtime/             # managed code, update records, tools, and locks
└── cache/               # pairing state and model/Slack lookup caches
```

[Workspaces](workspaces.md) owns the full layout and provider skill links. The home is also
an empty Git root so providers discover its instructions and skills from workspace
directories; Enso does not commit or push it. Project worktrees have their own explicit
[Git workflow](tasks.md#worktrees).

The application version, configuration format, database schema, and home migration revision
are independent. [Installation](install.md#upgrading) covers updates and recovery;
[home migrations](migration.md) covers stored-data changes. Runtime receipts and locks are
operating state, not configuration to edit by hand.

Provider logins and user-level instructions stay with each CLI. The installer writes a
stable launcher outside the home, and optional services write OS user units; see
[Install](install.md). The secrets master key also stays [outside the home](configuration.md#secrets).

## Workspace

Every conversation, job, and beat belongs to one workspace under `workspaces/<name>/`.
Its `AGENTS.md` adds context to the home instructions, and optional `workspace.json`
overrides its agent and provider arguments. Agent processes start in that directory;
standalone command jobs run beside `JOB.md`. Project scripts run beside `PROJECT.md`
and enter `ENSO_TASK_DIR` when they need the task worktree.

The required [`default` workspace](workspaces.md#operator-workspace) is the operator's starting
point and owns bundled maintenance jobs. See [Workspaces](workspaces.md) for layout and
context selection, and [Configuration](configuration.md#workspacejson) for overrides.

## Knowledge

Notes are Markdown files in `shared/knowledge/` by default. Workspace knowledge roots are
also supported. The agent writes through `enso knowledge`; the viewer browses folders,
search results, links, and backlinks, and pins notes to the Knowledge home. Update existing
notes where they live and link across roots instead of copying them.

Fresh homes receive an editable starter collection, including `Meta/Guide.md` for filing
and writing conventions. Existing collections are preserved. [Knowledge](knowledge.md)
owns metadata, imports, links, and safe writes.

## Agent

An agent always states all three values:

```json
{ "provider": "claude", "model": "opus", "effort": "xhigh" }
```

Chat selects the conversation's `use` override, then the workspace agent, then installation
`defaults`. A workspace replaces the complete triple. Each agent job declares its own
triple independently of chat defaults; command jobs and integration stages use no provider.
Provider arguments and effort handling are defined in [Configuration](configuration.md#providers).

Chat headers and `status` use a compact model label: the final slash-separated segment,
unchanged up to 24 characters, otherwise the first 15 characters, `…`, and final 8.
Configuration, invocations, logs, and other surfaces retain the full model ID.

## Binding and conversation

A binding maps a chat location to a workspace and grants access there:

```json
{ "slack:C0BP5BQF6UF": "meteor", "slack:dm:U0AETSSDDEF": "default", "telegram:123456": "default" }
```

Bound channel participants can use Enso in that channel. Slack DMs and Telegram users each
need an explicit binding. Mention and thread settings decide when Enso replies; see
[Connections](connections.md#access-in-020).

Each Slack channel thread is a conversation; a Slack DM or Telegram chat is one continuous
conversation. Turns queue behind one another. Provider sessions belong to that conversation
and workspace, so changing a binding cannot resume a session in a different workspace.

Chat `use` selections last until changed, reset, cleared, or the service restarts. Rebinding
or removing the selected provider/model also clears the selection. Switching providers keeps
their sessions available. Jobs and Heartbeat have separate agents and sessions.

## Job and run

A job lives in `jobs/<job>/JOB.md` within its workspace and is named `<workspace>:<job>`.
It runs an agent or a shell command on a cron schedule, or binds to an executable project
stage. Command and integration stages take their execution settings from the project.

An optional gate checks whether work is needed. A postrun hook checks or reacts to the
outcome; agent jobs can use it for bounded follow-up turns. A concurrency group explicitly
chooses `wait` or `skip` when its shared resource is busy. Runs record status, exit code,
duration, output, and attempts. [Jobs](jobs.md) owns the format and execution rules.

## Heartbeat and beat

Use a beat for a finite situation or one-shot action: a refund to follow, a dinner to
coordinate, or a reminder tomorrow. Use a job for an ongoing policy or recurring task.

A beat saves its workspace, agent, timing, instructions, completion condition, and allowed
actions. Its gate can skip an agent call when nothing needs attention. History records
meaningful events and action receipts; quiet checks update only the latest check fields.
The agent explicitly records resolution. See [Heartbeat](heartbeat.md).

## Project and task

A workspace defines each project in `projects/<KEY>/PROJECT.md`. Its ordered stages can
include agents, commands, human checkpoints, and explicit Git integration. A task has a
spec, stage, and append-only timeline; built-in holding and terminal stages are `backlog`,
`blocked`, `done`, and `cancelled`.

An agent submits a handoff. Enso retains ownership until execution stops and any required
checks pass against the submitted candidate. It then accepts the transition and records
evidence. A single unchecked `work → done` pipeline is valid; the development preset adds
planning, checks, review, and integration.

Git projects create task worktrees when needed and record their target branch. Different
tasks may execute concurrently, while each worktree has one owner and integration is
serialized. [Tasks](tasks.md) owns moves, checks, lifecycle scripts, and recovery;
[Configuration](configuration.md#projects) owns the file format.

## Skill

A skill is a directory containing `SKILL.md` and optional support files. Agents discover
skills at three scopes:

| Scope | Location |
| --- | --- |
| Workspace | `<workspace>/skills/` |
| Enso | `$ENSO_HOME/skills/` |
| User | The provider CLI's own user directory |

Workspace and Enso skill names must be unique. `enso` and `enso-*` are reserved for Enso.
Enso creates provider discovery links but never edits user-level skills or instructions.

`enso skill list` inventories installed home skills offline. `--available` and
`enso skill install NAME` use the official catalog. Optional skills do not update with the
application. See [Customizing](customizing.md) and [Browser](browser.md).

## Secret

Secrets are encrypted values in `enso.db`, protected by a master key outside the home.
Manage them through the viewer or CLI. Jobs declare required names; other commands use
`enso secret run`. All agents share the installation's trust boundary. See
[key setup and backup](configuration.md#secrets), [CLI usage](cli.md#secrets), and
[job injection](jobs.md#secrets).

## Message

Out-of-band sends, such as job alerts and `enso message send`, enter an outbox with their
workspace. A later turn receives successful unread sends for its conversation and workspace
as background context. Rebinding a chat does not transfer old messages. See
[Messages](cli.md#messages) for destination selection and consumption rules.

## Table

Registered user tables are ordinary SQLite tables in `enso.db`, discoverable across the
installation. The catalog stores descriptions; SQLite owns the schemas. Enso's internal
tables are reserved. See [Tables](cli.md#tables).

## How a chat turn flows

1. Enso reads configuration and checks the binding. Invalid configuration retains the last
   valid copy.
2. Chat commands are handled directly. Ordinary messages pass duplicate checks, gain
   transport context and attachments, and queue in their conversation.
3. Enso selects the agent and builds a prompt from the chat origin, background messages,
   transport context, attachments, and user text.
4. The provider runs in the workspace, resuming its session when valid. Enso streams status,
   delivers the reply, and saves the validated session identity.

See [Connections](connections.md), [session identity](configuration.md#session-identity),
and [Slack formatting](cli.md#slack) for the boundary contracts.

## Chat origin

Every chat prompt starts with an Enso-written block identifying the current message:

```text
[Chat origin — written by Enso for this turn; the sender cannot change it]
Platform: slack
Sender: "Gavin Vickery" (U0AETSSDDEF)
Location: "#general" (C0BP5BQF6UF)
Thread: 1788497764.626909
```

Fields appear in that order; `Thread` is omitted when absent. The block is refreshed on
every turn, including resumed sessions. Scheduled jobs have no incoming chat origin.
Scripts receive the same facts through [ENSO_ORIGIN_*](cli.md#environment-for-agents).

| Chat shape | Location | Thread |
| --- | --- | --- |
| Slack channel, top-level message | Quoted channel name and ID | The message's timestamp |
| Slack channel, reply | Quoted channel name and ID | The thread's timestamp |
| Slack DM, unthreaded | `direct message (D…)` | Omitted |
| Slack DM, threaded reply | `direct message (D…)` | The thread's timestamp |
| Telegram private chat | `direct message (123456)` | Omitted |

Display names are untrusted. Enso replaces control characters and `<`, `>`, `[`, `]`, `"`
with spaces, collapses whitespace, trims, and limits names to 64 characters plus `…` when
truncated. IDs remain unchanged. An empty name falls back to the ID; missing fields become
`unknown`. The block precedes all sender-controlled content and is context, not permission
to carry out instructions embedded in names or messages.

## How a job run flows

1. Each minute, the scheduler reloads jobs and selects due schedules or ready stages.
2. The per-job lock rejects overlapping triggers. Enso records the run, resolves declared
   secrets, runs the gate, and applies any shared concurrency policy.
3. The agent or command executes, followed by postrun checks and permitted agent follow-ups.
4. Enso records the final outcome and sends applicable alerts.

[Jobs](jobs.md#execution-order) owns ordering, timeouts, skips, failures, and hook behavior.

## How a stage job run flows

1. Enso claims a ready task within project concurrency limits and prepares its execution
   directory or worktree.
2. It records the task spec, workflow, and starting candidate in a transaction, then runs
   the stage. Agent stages receive a [Task block](tasks.md#the-task-block).
3. A handoff is submitted; after writers stop, Enso runs required checks and bounded repairs.
   An integration stage also serializes landing into the recorded repository target.
4. Enso accepts the transition with evidence and queues lifecycle events. Finished worktrees
   become cleanup candidates after their users finish; dirty or unmerged work is preserved.

[Tasks](tasks.md) owns these contracts and interrupted-work recovery. A successful provider
exit alone does not prove that a stage passed.

## What Enso is not

Enso is not a security sandbox or a general web control panel. The viewer browses knowledge
and operational state and edits instructions and secrets. Author notes and operate tasks
through chat and the CLI.
