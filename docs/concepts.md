# Concepts

Enso is a personal AI assistant running as one long-running service on your machine.
You talk to Enso through chat, give it context in workspaces, and ask it to work on a
schedule or follow up later. Claude Code, Codex, Grok, Antigravity, and OpenCode are the
provider CLIs that can power it; each execution runs inside a workspace.

This document defines the primitives and traces how conversations and background work move
through the system. Everything else in the docs assumes these words.

Sections marked **Forthcoming in 0.2.0** describe agreed contracts awaiting implementation.
Other runtime descriptions reflect the currently implemented behavior.

## Installation trust model

One Enso installation is one trusted environment for a person or
a small team, with one service and one operational database. Workspaces select relevant
context and own work; they do not keep data confidential from agents elsewhere in the same
installation. Separate personal workspace names or home directories under the same
unrestricted account do not provide that separation.

Teams needing separation use separate installations on separate machines or VPSs, with
their own credentials, provider logins, data, and chat connections. Provider permissions
remain configurable. Enso still authenticates transports, validates input, protects
credentials, preserves user data, and requires authorization for external actions. Removing
Enso's workspace restriction mode does not silently replace provider arguments with bypass
flags; [Configuration](configuration.md#provider-permissions-and-installation-trust) owns that contract.

For example, two team channels can select different workspaces in one installation while
sharing its credentials and capabilities. A finance team needing separation from development
runs another installation on another machine. [Connections](connections.md#access-in-020)
owns who a binding admits; [Workspaces](workspaces.md#ownership-in-020) owns resource locations
and [context selection](workspaces.md#context-selection-in-020).

## The primitives

| Primitive | What it is |
| --- | --- |
| **Home** | `~/.enso` (or `ENSO_HOME`) — config, database, log, workspaces, knowledge, jobs, skills, secrets |
| **Transport** | A chat platform connection: Slack or Telegram. Both run in one process. |
| **Binding** | A map from a chat location to a workspace. Unbound places are ignored. |
| **Workspace** | A directory with a fixed layout. The agent's working directory and its context. |
| **Knowledge** | Durable Markdown notes in a shared home root or a workspace, written with the agent and browsed read-only |
| **Agent** | An explicit `provider` + `model` + `effort` triple |
| **Conversation** | A serialized queue of turns with a resumable provider session |
| **Job** | `JOB.md`: a cron schedule or executable stage, a workspace, and agent instructions when a model is used |
| **Beat** | One future action or finite situation to follow until resolved, managed by Heartbeat |
| **Run** | One agent execution for a job or beat: status, exit code, duration, output |
| **Project** | A key, a workspace, an ordered list of stages, and optionally a Git repository |
| **Task** | One unit of work in a project: a reference, a spec, a stage, and its timeline |
| **Workflow transaction** | One stage execution and submitted candidate, accepted by Enso with any required check evidence |
| **Skill** | Instructions the agent can load, resolved across three scopes |
| **Table** | A registered SQLite table in `enso.db` holding your structured data |
| **Message** | An out-of-band send, recorded so the next turn hears about it |
| **Viewer** | The optional read-only web UI over all of the above |

## Home

Enso's runtime state lives under one directory:

```text
~/.enso/
├── config.json          # transports, bindings, agents, providers
├── config.example.json  # editable, incomplete template prepared by init
├── .config.lock         # shared advisory lock for configuration writers
├── .bundles.json        # hashes of the bundled files Enso installed
├── slack/manifest.json  # initial copy of the packaged Slack app manifest
├── AGENTS.md            # instructions for every turn and job (CLAUDE.md links to it)
├── CLAUDE.md -> AGENTS.md
├── skills/              # enso-wide skills, available in every workspace
├── knowledge/           # shared Markdown knowledge, visible across workspaces
├── browser/             # optional private Chrome profiles, output, state, and tooling
├── .claude/skills       # symlink -> ../skills, discovered by the provider CLIs
├── .agents/skills       # symlink -> ../skills
├── workspaces/<name>/   # one directory per workspace
├── heartbeat/<HB-ref>/  # optional gate.sh and helpers for one beat
├── worktrees/           # legacy task worktrees retained during migration; new defaults live by the repo
├── secrets/*.env        # KEY=value files exported into the service environment
├── enso.db             # runs, messages, sessions, jobs, tasks, beats, registered tables
├── enso.log            # rotating log
├── web.log, web.pid     # the web viewer's output and lock, while it runs
├── launchd-web.log      # stdout/stderr when the optional viewer service runs
├── runtime/
│   ├── .concurrency/    # installation-wide job concurrency-group locks
│   ├── releases/<version>-<hash>/  # immutable managed Python environments
│   ├── current -> releases/...    # selected release behind the stable launcher
│   ├── install.json     # installed package version, feed, extras, integration settings
│   ├── update.json      # latest update operation and outcome
│   ├── maintenance.json # durable gate while an update pauses new work
│   ├── daemon.json      # current daemon version, readiness, and accepted work
│   ├── operations/<id>/ # pinned manifest, journal, backups, failed state, helper files
│   └── tools/, python/, cache/    # installer tools and uv-managed runtime support
└── cache/
    ├── connect/         # private pairing state, receive lock, and first-reply receipt
    ├── models.json      # models.dev catalog for enso models
    └── slack.json       # Slack directory cache for name lookups
```

[Connections](connections.md) owns the pairing lifecycle and private state. Prepared home,
paired chat, valid configuration, and a successful provider reply are separate milestones.

**Partly implemented for 0.2.0:** workspace settings now use `WORKSPACE.md`, and the new
workspace directories are scaffolded. Jobs and projects now use the workspace paths; memory
and Heartbeat scripts will use the [workspace ownership layout](workspaces.md#ownership-in-020).
Installation support
files shown here keep their documented purpose and locations unless that layout explicitly
changes them. Operational records stay in one home database; maintained knowledge and memory
stay in Markdown.

Enso keeps its runtime state inside its home. The release installer writes a stable
launcher in the selected bin directory; `enso service install` and `enso web install`
write their operating-system user service units, and a first
Antigravity turn can ask `agy` to register the workspace in its own project catalog. The
`enso models` lookup may reuse OpenCode's `$XDG_CACHE_HOME/opencode/models.json` (or
`~/.cache/opencode/models.json` when that variable is unset or empty), but treats that
external cache as read-only and writes refreshes only to Enso's own `cache/models.json`.
See [Install](install.md#the-service), [Configuration](configuration.md#antigravity), and
[Models](cli.md#models) for those paths and behaviours. Enso scans user-level skill files,
including OpenCode's native `skill/` and `skills/` directories, which follow
`XDG_CONFIG_HOME` and `OPENCODE_CONFIG_DIR` as
[Workspaces](workspaces.md#how-they-reach-the-agent) sets out, to show you what the agent
can see, but never modifies them.
Provider CLIs load their own user-level instruction files; Enso does not read or modify
those.

`enso.db` carries its schema version, and `config.json`'s `version` identifies its config
format. Neither is the application release version: that comes from package metadata and,
for managed installs, `runtime/install.json`. The 0.2.0 database starts a new schema line
at `user_version = 1`, identified by SQLite `application_id = 0x454E534F` (`ENSO`). It refuses
0.1.x databases and unsupported newer schemas without altering them; it never migrates
an old home. An incomplete managed update can restore its pre-update snapshot together
with the previous code; see [Upgrading](install.md#upgrading).

Managed updates stage verified releases before pausing new work. An independent helper
waits for accepted turns, jobs, and ordinary CLI operations, then snapshots affected state,
switches code, and verifies readiness before admitting work again. An interrupted or failed
update retains its journal and recovery gate. These files are private runtime state, never
configuration to edit by hand. [Install](install.md#upgrading) owns recovery and adoption;
[Releases](releasing.md) owns the published bundle and feed.

Home is also a Git repository, initialised empty and never committed to. Every provider
CLI walks up to the nearest Git root for instructions and skills, and this is what puts the
home-level `AGENTS.md` and `skills/` in reach from a workspace. Enso does not commit,
push, or read history in the home repository. A repo project's separate worktrees have the
explicit Git operations described in [Tasks](tasks.md#worktrees).

## Workspace

A workspace is where an agent actually works. It is a content root, not a security
boundary — the agent has whatever access the provider CLI's flags give it, and that is the
whole machine unless you say otherwise.

Names are lowercase kebab-case, and a workspace lives at `~/.enso/workspaces/<name>`. The
layout is fixed and Enso keeps it that way; see [Workspaces](workspaces.md).

Every conversation, job, and beat names exactly one workspace. That workspace becomes the
provider's working directory. Its `AGENTS.md` adds workspace-specific context to Enso's
home-level instructions and any user-level instructions the provider loads. Optional
`WORKSPACE.md` supplies settings as defined in
[Configuration](configuration.md#workspacemd-in-020); see also
[Customizing](customizing.md#instructions-agentsmd).

## Knowledge

Knowledge is ordinary Markdown under the home's shared `knowledge/` directory or a
workspace's `knowledge/`. The agent maintains notes through chat and the CLI; the viewer
provides folder navigation, search, and clickable note links. Files remain the source of
truth. Folders help the agent select context, but they do not isolate access.

Shared material has one home across workspaces; workspace-specific facts stay with their
workspace and link across roots when needed. See [Knowledge](knowledge.md) for metadata,
links, imports, and the user-editable formatting convention.

## Memory

**Forthcoming in 0.2.0.** Memory is dated conversation and experience maintained as Markdown
in one workspace's `memory/`. Knowledge holds current facts and reference material. When a
user asks about an earlier discussion, the agent uses the memory CLI and `enso-memory`
skill, starting in the selected workspace. People sharing a workspace share its maintained
memory, while their DM conversations and live provider sessions remain distinct.
[Memory](memory.md) owns capture, recall, and removal.

## Agent

An agent is three values, always stated together:

```json
{ "provider": "claude", "model": "opus", "effort": "xhigh" }
```

There is no partial agent and no inferred model. `defaults` in `config.json` gives the
triple every conversation uses; a workspace may replace the whole triple. Each provider-backed
job must state its own triple in `JOB.md`, independent of those chat defaults, while still
using the workspace's provider-argument overrides. Command and integration stages omit the
triple because they do not invoke a provider. Providers with an ordered reasoning ladder
clamp effort *down* to what the model supports, with a log line saying so. Antigravity
reports the effort embedded in its model id, even when the request is lower; OpenCode
instead treats it as an exact, model-specific variant. See
[Configuration](configuration.md#providers).

Slack and Telegram use a compact model label in chat so long ids do not crowd the
live run header or the `status` response. Enso takes the final slash-separated segment;
segments of at most 24 characters stay intact, while longer ones become their first 15
characters, `…`, and final 8 characters. This is presentation only: config, provider
invocations, logs, CLI output, and every other surface retain the full canonical model id.

## Binding and conversation

A **binding** maps a place to a workspace:

```json
{ "slack:C0BP5BQF6UF": "meteor", "slack:dm:U0AETSSDDEF": "default", "telegram:123456": "default" }
```

A binding also grants access to the installation. Bound channel
participants can use Enso there; each Slack DM or Telegram user needs an explicit binding.
Unknown senders receive only the canned unbound notice in the circumstances defined by
[Connections](connections.md#access-in-020). Mention/thread settings decide when Enso replies;
eligible live human messages in bound conversations will be captured even when Enso only
observes, once the forthcoming [capture pipeline](memory.md#conversation-capture) is implemented.

A **conversation** is finer-grained than a binding. In a Slack channel each top-level
message starts its own thread and its own conversation; a DM or a Telegram chat is one
continuous conversation. Each conversation is serialized — one provider process at a time,
later messages queued behind it — and holds a resumable provider **session** per provider,
valid only in the workspace it was created in.

## Job and run

A job is a workspace directory `jobs/<job>/` containing a `JOB.md`: YAML frontmatter
naming the schedule and agent, plus a prompt body. Its workspace comes from its location;
its reference is `<workspace>:<job>`, as defined in
[Workspaces](workspaces.md#ownership-in-020). The scheduler wakes once a minute and fires
the jobs whose cron slot has passed.

A job may have a **prerun** script that gates it (nothing is spent when there is nothing to
do) and a **postrun** script that checks or reacts to the outcome, optionally sending a
follow-up message into the same provider session. Every trigger that passes the
per-job lock creates a **run** row recording status, exit code, duration, and the output
tail. See [Jobs](jobs.md).

A **stage job** names a project and one of its executable stages instead of, or as well as, a
schedule. It fires when a task is ready in that stage, and Enso claims the task for the run
before execution starts. Agent stages use a provider; command and integration stages run
without one. See [Stage jobs](jobs.md#stage-jobs).

## Heartbeat and beat

Heartbeat follows finite situations or performs future one-shot actions. Waiting for one
refund, coordinating one dinner, and sending one email tomorrow are beats. An ongoing inbox
policy or a nightly backup is a job. The agent chooses from the user's intent and explains
what it has arranged; users do not need to know these names beforehand.

Each beat has its own timing, instructions, completion condition, allowed actions, workspace,
and saved agent. Its script gate can skip the LLM when there is nothing to handle. SQLite
keeps the current state, meaningful event history, and agent runs. A quiet check updates only
the latest check fields. The agent explicitly records when the situation is resolved.
See [Heartbeat](heartbeat.md) for the lifecycle, gate contract, and retention rules.

## Project and task

A project is a key such as `EN`, a workspace, an ordered list of stages, and optionally a
Git repository, defined in the containing workspace's `projects/<KEY>/PROJECT.md`; see
[Configuration](configuration.md#configuration-ownership-in-020).

A task belongs to one project, sits in one stage
(one of the project's own, or the built-in `backlog`, `blocked`, `done`, or `cancelled`),
and carries an append-only timeline. Moves follow the ordered stages — `advance`, `return`,
`block`, `resume`, `drop`. An agent's forward/return handoff is submitted to a transaction;
it does not immediately advance the task. Enso checks the stable candidate after the model
stops, repairs within configured limits, and commits acceptance with its evidence.

Required checks are optional. `work → done` with no Git or checks remains valid. The dev
preset adds planning, implementation checks, review, and explicit engine integration.
Human checkpoints and command-only stages fit the same ordered flow. Workflow/check/spec
versions and candidates bind the evidence; the viewer shows submission separately from
acceptance and retains the audit when provider runs are pruned.

Repo projects create a worktree lazily for stages that need one, by default at
`<repo>/.worktrees/<REF>`, with a configurable root and a recorded target branch. Tasks can
execute concurrently within project limits while each worktree has one owner and repository
landing is serialized. Lifecycle scripts react to accepted transitions separately from
worktree setup/cleanup. [Tasks](tasks.md) owns these contracts and the trust limitations.

## Skill

A skill is a directory containing a `SKILL.md` — a name, a description of when to use it,
and the instructions themselves. Enso resolves skills across three scopes:

| Scope | Location | Owned by |
| --- | --- | --- |
| **Workspace** | `<workspace>/skills/` | You, per workspace |
| **Enso** | `~/.enso/skills/` | You, bundled skills, and installed official optional skills |
| **User** | Your CLI's own user directory, such as `~/.claude/skills/` or `~/.config/opencode/skills/` | You, outside Enso entirely |

Names must be unique across the workspace and enso scopes; the audit reports a collision.
`enso` and names beginning `enso-` are reserved for what Enso installs.
Enso links `skills/` into the dot-directories the provider CLIs discover, so a workspace
agent actually sees its workspace and enso-wide skills. It never modifies user-level
skills or instruction files. See [Customizing](customizing.md).

`enso skill list` shows installed home skills offline; `enso skill list --available` and
`enso skill install <name>` use only the official `geekforbrains/enso-skills` catalog.
Optional skills carry their installation receipt inside their directory and do not update
with the application. [Customizing](customizing.md#official-optional-skills) owns that flow;
[Browser](browser.md) owns persistent profile setup and its private home data.

## Message

Anything Enso sends outside the normal reply — a job alert, a `enso message send` from a
running agent — is recorded in an outbox. At the start of the next turn in that
conversation, unread rows are shown to the agent as `[Background messages]` and marked
consumed. A turn's own sends are retired when it ends, because the agent already knows what
it said.

This is what lets a long job report progress into a chat and have the next human message
land in a conversation that knows what happened.

## Table

Registered tables are ordinary SQLite tables in `enso.db`, catalogued so an agent can
discover them. Enso's own state tables are reserved. The catalog holds only metadata —
schemas are always read back from SQLite itself.

## How a chat turn flows

1. A message arrives on a transport.
2. `config.json` is read for this turn. A file that fails validation is logged once, and
   the last valid configuration stays in force.
3. Its location resolves to a binding key. No binding, no work.
4. The location and thread resolve to a conversation key.
5. Commands (`!stop`, `!clear`, `!status`, `!help`, `!restart`) are handled here and stop.
6. Attachments download into `<workspace>/uploads/<id>/` under [names the transport
   decides](workspaces.md#uploads); Slack fetches only from its own file-download endpoint,
   so metadata naming anywhere else is refused without a request. A message whose
   attachments all fail says so in the prompt. A message that cannot be prepared is answered
   with the error and dropped; the ones queued behind it still run.
7. The turn takes the conversation lock, or queues behind whatever holds it.
8. The agent triple resolves: workspace override, else `defaults`. Effort is normalized for
   that provider.
9. The prompt is assembled: the [chat-origin block](#chat-origin), background messages,
   transport context, attachment paths, the user's text, and — on Slack — the rich-format
   contract.
10. The provider CLI runs in the workspace directory, resuming its session if there is one.
11. Its stdout stream drives a live status message, whose header uses the same compact model
    label as the chat `status` command; the final text is delivered, split to the transport's
    limit. When Enso translates ordinary Markdown for Slack, it keeps HTTP, HTTPS, email, and
    Slack deep links clickable; a link to anything else is shown as its inline-code target
    instead. A reply whose fenced code block names a language Slack highlights goes out as a
    native Slack Markdown block, so the code is highlighted instead of showing the language as
    its first line; an unlabelled fence stays a plain code block, and a block Slack refuses
    falls back to the translated text. The Slack prompt asks the agent to name local files with
    workspace-relative inline-code paths such as `drafts/report.md`.
12. The validated session ID is stored against the conversation and workspace once a
    recognized provider event establishes it. Invalid or conflicting IDs fail the turn
    without replacing its original session. Chat, jobs with postrun, and Heartbeat share the
    [session identity rules](configuration.md#session-identity).

## Chat origin

Every chat turn opens with one block Enso writes itself, stating where the turn came from.
It is the agent's authoritative answer to which platform it is on, who is talking, and
where, so workspace instructions never have to guess it and the agent never has to read its
own environment to find out.

```text
[Chat origin — written by Enso for this turn; the sender cannot change it]
Platform: slack
Sender: "Gavin Vickery" (U0AETSSDDEF)
Location: "#general" (C0BP5BQF6UF)
Thread: 1788497764.626909
```

The fields always appear in that order, one per line. `Platform` is the transport name,
`slack` or `telegram`. `Sender` and `Location` are a quoted display name followed by the
platform id in parentheses. `Thread` is the thread a reply lands in, and is omitted when
there is none. The four state the same origin as `ENSO_ORIGIN_TRANSPORT`, `_USER_NAME` and
`_USER_ID`, `_CHANNEL_NAME` and `_CHANNEL`, and `_THREAD_TS`, rendered for reading: ids
verbatim, names escaped, and a `_CHANNEL_NAME` of `dm` written out as `direct message`. No
origin value one of those variables carries is dropped from the block.

| Chat shape | `Location` | `Thread` |
| --- | --- | --- |
| Slack channel, top-level message | `"#general" (C0BP5BQF6UF)` | the message's own timestamp, which is the thread it starts |
| Slack channel, reply in a thread | `"#general" (C0BP5BQF6UF)` | the thread's timestamp |
| Slack direct message, unthreaded | `direct message (D0AETSSDDEF)` | omitted |
| Slack direct message, reply in a thread | `direct message (D0AETSSDDEF)` | the thread's timestamp |
| Telegram private chat | `direct message (123456)` | omitted |

A direct message renders as the words `direct message` and its id on both transports,
because a DM has no channel name to show. Slack allows threads inside a DM, and a reply in
one is threaded like any other: the whole DM stays a single conversation with a single
session, but the reply posts into that thread, so the timestamp is a real origin value and
the block states it. Telegram has no threads, so its `Thread` line never appears.

### Untrusted names, trusted ids

A display name is chosen by whoever holds the account, so Enso treats it as hostile data
and never interpolates it raw:

- Every control character, and each of `<`, `>`, `[`, `]`, and `"`, becomes a space. Angle
  brackets would revive Slack's live mention syntax; the rest could forge a block header or
  close the quotes early.
- Runs of whitespace collapse to one space and the ends are trimmed, so a name can never add
  a line to the block.
- What remains is cut to 64 characters, with a trailing `…` when it was longer.
- A name that comes out empty is dropped and the field is the bare id: `Sender: U0AETSSDDEF`.

Ids are generated by the platform, not typed by the sender. They are rendered verbatim and
never truncated, because they are what the agent hands back to `enso slack history C…`,
`enso message send --to`, and the rest of the CLI. A field with an id but no usable name is
the id alone; one with neither is `unknown`.

### Where the block sits

The origin block is the first thing in the prompt, ahead of every value Enso did not write
itself: `[Background messages]`, the transport's own context (Slack's thread history or
channel-access pointer, Telegram's quoted reply), `Attached files:`, the user's text, and
Slack's rich-format contract. The facts come first, then the data they describe.

It is written on every chat turn, including the ones that resume an existing provider
session, because the platform, sender, location, and thread are true of *this* message and
of nothing earlier. A Slack DM makes that concrete: one conversation carries every thread in
it, so restating `Thread` each turn is the only thing telling the agent which thread this
message arrived in. A scheduled job never gets one: nobody sent it, and a fabricated origin
is a fact the agent would act on.

The same values remain available as `ENSO_ORIGIN_*` environment variables; see
[CLI § Environment for agents](cli.md#environment-for-agents). The variables are the
programmatic interface a script or a `enso message send` reads. The block is what the model
reads.

## How a job run flows

1. The scheduler ticks on the minute and reads `config.json` and every `JOB.md` again.
2. A job whose cron slot has passed fires, unless it is disabled, has problems, is already
   running, or missed its slot by more than its grace period with `catch_up` disabled.
3. It takes the per-job lock. An overlapping trigger is skipped, never queued.
4. A run row opens as `running`.
5. The **prerun** runs from the job directory. Exit 0 opens the gate and its stdout is
   substituted into the prompt; exit 1 means no work; anything else is a failure.
6. If the job names a `concurrency_group`, it tries that group's lock after the prerun
   opens; a collision produces `skipped` without starting a provider.
7. The provider runs in the workspace. Jobs with postrun capture a resumable session from
   structured output; jobs without postrun use batch execution.
8. The **postrun** receives the latest output on stdin and current outcome in its
   environment. Exit 0 finishes. Exit 10 with a message on stdout requests another turn in
   the same session, followed by another check. The default `max_followups` is 2 and each
   `JOB.md` can override it; prerun never repeats within this loop.
9. The row stays `running`, and the acquired job/group locks stay held, until checking
   finishes. Attempts and hook diagnostics are retained; failed checks fail the run. The
   provider turns share the job's timeout allowance, while each hook has its own timeout.
10. Non-provider outcomes and provider failures still run a reaction hook but cannot start
    follow-ups. Cancellation or an unexpected runner exception closes the row as `error`
    and bypasses further hooks. A new trigger always starts a fresh session.
11. Scheduled and ready-triggered runs send final failure alerts and prerun recovery notices
    as described in [Jobs](jobs.md#alerts); prompts and scripts can send messages during any run.

## How a stage job run flows

A stage job follows the job flow above, with a task transaction around execution:

1. On each tick, a valid enabled stage job fires when work is ready, or at its configured
   cron slot when a task waits. Idle polling creates no run; a manual run records `no_work`.
2. The per-job lock and optional shared-resource group apply. Project `max_concurrency`
   separately limits simultaneous task executions, so distinct stages can work on distinct
   tasks. One task/worktree has one owner through execution and checks.
3. Enso claims the highest-priority ready task, oldest first on ties, and lazily prepares its
   worktree if the stage needs one. Existing path/base metadata is reused. Setup failure
   stops execution and records the reason.
4. Enso snapshots the spec, workflow, starting revision, and execution directory in a
   transaction record. Agent stages receive the Task block, main repository instructions,
   and job prompt; the provider still starts in the Enso workspace. Command and integration
   stages use the engine without starting a model.
5. The agent works, submits a handoff with `advance` or `return`, and finishes its turn.
   Submission retains the stage and claim; it cannot walk through another stage. A return
   names the configured earlier stage and is constrained by a finite return budget.
6. After the provider and postrun stop writing, Enso checks the candidate and selected
   inputs. Required commands run outside the model and outside database write transactions.
   Actual failures can request bounded repair in the held task; a changed candidate needs
   fresh evidence. Budgets survive restart/re-entry. Unrecoverable work stays visible for
   attention rather than being accepted from a successful provider exit.
7. Integration, when configured, serializes against the repository target, updates the
   candidate against that target, rechecks, and lands. An ordinary last stage does not imply
   integration. Blocking/cancellation remain possible when forward checks cannot pass.
8. Enso atomically records evidence, accepts the transition, and enqueues lifecycle events.
   Events retain stable IDs and retry outcomes; an after-transition failure does not undo
   the stage. Execution ownership and worktree-using hooks prevent an overlapping writer.
9. Finished worktrees become cleanup candidates only after their users finish. Safe cleanup
   preserves dirty or unmerged work, runs teardown, and records failures. Blocked and human
   checkpoints retain their worktrees. Startup recovery and subsequent ticks reconcile
   interrupted work and pending events; they never infer a pass from incomplete evidence.

[Tasks](tasks.md) owns submission, evidence, lifecycle and recovery. [Jobs](jobs.md#stage-jobs)
owns trigger/execution behavior. These mechanisms enforce workflow through supported Enso
paths; worktrees and environment actor variables are not an OS sandbox.

## What Enso is not

- **Not a sandbox.** Permissions belong to the provider CLI. Enso passes your flags through.
- **Not a publishing or editing application.** Knowledge stays in Markdown files, maintained
  through the agent and CLI. The viewer browses notes read-only; there is no web editor.
- **Not a control plane.** The web viewer is read-only by design; the board moves from
  chat and the CLI.
