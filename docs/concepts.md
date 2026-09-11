# Concepts

Enso is a personal AI assistant running as one long-running service on your machine.
You talk to Enso through chat, give it context in workspaces, and ask it to work on a
schedule or follow up later. Claude Code, Codex, Grok, Antigravity, and OpenCode are the
provider CLIs that can power it; each execution runs inside a workspace.

This document defines the primitives and traces how conversations and background work move
through the system. Everything else in the docs assumes these words.

## The primitives

| Primitive | What it is |
| --- | --- |
| **Home** | `~/.enso` (or `ENSO_HOME`) — config, database, log, workspaces, jobs, skills, secrets |
| **Transport** | A chat platform connection: Slack or Telegram. Both run in one process. |
| **Binding** | A map from a chat location to a workspace. Unbound places are ignored. |
| **Workspace** | A directory with a fixed layout. The agent's working directory and its context. |
| **Agent** | An explicit `provider` + `model` + `effort` triple |
| **Conversation** | A serialized queue of turns with a resumable provider session |
| **Job** | `JOB.md`: a cron schedule or a stage to serve, an agent, a workspace, and a prompt |
| **Beat** | One future action or finite situation to follow until resolved, managed by Heartbeat |
| **Run** | One agent execution for a job or beat: status, exit code, duration, output |
| **Project** | A key, a workspace, an ordered list of stages, and optionally a Git repository |
| **Task** | One unit of work in a project: a reference, a spec, a stage, and its timeline |
| **Skill** | Instructions the agent can load, resolved across three scopes |
| **Table** | A registered SQLite table in `enso.db` holding your structured data |
| **Message** | An out-of-band send, recorded so the next turn hears about it |
| **Viewer** | The optional read-only web UI over all of the above |

## Home

Enso's normal runtime state lives under one directory:

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
├── browser/             # optional private Chrome profiles, output, state, and tooling
├── .claude/skills       # symlink -> ../skills, discovered by the provider CLIs
├── .agents/skills       # symlink -> ../skills
├── workspaces/<name>/   # one directory per workspace
├── jobs/<name>/JOB.md   # scheduled and stage jobs
├── jobs/.concurrency/   # advisory locks for job concurrency groups
├── heartbeat/<HB-ref>/  # optional gate.sh and helpers for one beat
├── worktrees/<KEY>/<REF>/  # one Git worktree per task of a repo project
├── secrets/*.env        # KEY=value files exported into the service environment
├── enso.db             # runs, messages, sessions, jobs, tasks, beats, registered tables
├── enso.log            # rotating log
├── web.log, web.pid     # the web viewer's output and lock, while it runs
├── launchd-web.log      # stdout/stderr when the optional viewer service runs
├── runtime/
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
for managed installs, `runtime/install.json`. Enso migrates the database forward and
refuses an unsupported newer schema. An incomplete managed update can restore its
pre-migration snapshot together with the previous code; see [Upgrading](install.md#upgrading).

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
home-level instructions and any user-level instructions the provider loads; see
[Customizing](customizing.md#instructions-agentsmd).

## Agent

An agent is three values, always stated together:

```json
{ "provider": "claude", "model": "opus", "effort": "xhigh" }
```

There is no partial agent and no inferred model. `defaults` in `config.json` gives the
triple every conversation uses; a workspace may replace the whole triple. Every job must
state its own triple in `JOB.md`, independent of those chat defaults, while still using
the workspace's provider-argument overrides. Providers with an ordered reasoning ladder
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

An unbound location is silent. Enso answers once to say it is unbound only if you mention
the bot or DM it directly.

A **conversation** is finer-grained than a binding. In a Slack channel each top-level
message starts its own thread and its own conversation; a DM or a Telegram chat is one
continuous conversation. Each conversation is serialized — one provider process at a time,
later messages queued behind it — and holds a resumable provider **session** per provider,
valid only in the workspace it was created in.

## Job and run

A job is a directory under `~/.enso/jobs/` containing a `JOB.md`: YAML frontmatter naming
the schedule, agent, and workspace, plus a prompt body. The scheduler wakes once a minute
and fires the jobs whose cron slot has passed.

A job may have a **prerun** script that gates it (nothing is spent when there is nothing to
do) and a **postrun** script that checks or reacts to the outcome, optionally sending a
follow-up message into the same provider session. Every trigger that passes the
per-job lock creates a **run** row recording status, exit code, duration, and the output
tail. See [Jobs](jobs.md).

A **stage job** names a project and one of its agent stages instead of, or as well as, a
schedule. It fires when a task is ready in that stage, and Enso claims the task for the run
before the provider starts; see [Stage jobs](jobs.md#stage-jobs).

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
Git repository, declared in `config.json`. A task belongs to one project, sits in one stage
(one of the project's own, or the built-in `backlog`, `blocked`, `done`, or `cancelled`),
and carries an append-only timeline. Moves between stages are derived from the list —
`advance`, `return`, `block`, `resume`, `drop` — and every move records a handoff message,
so a finished task is a labelled trace of how it got there. Tasks of a repo project are
worked in a worktree of their own under `worktrees/`. [Tasks](tasks.md) owns all of it.

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
12. The session id is stored against the conversation and workspace, once it matches the
    identifier contract of the provider that produced it. An id that does not is a
    provider protocol error: nothing is stored and the turn reports it. Where Enso
    assigned the id, that one is authoritative and a CLI may only confirm it.

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

A stage job follows the job run above, with a task threaded through it:

1. On each tick, a stage job fires when an unclaimed task waits in its stage (trigger
   `ready`), or at a cron slot that has passed when it also has a `schedule` and a task is
   waiting. Nothing is recorded when nothing waits; a manual `enso job run` records
   `no_work` instead.
2. The per-job lock, the `running` row, and the prerun are as above. A stage job's
   `concurrency_group` defaults to `project:<KEY>`, so one project's stages serialise and
   different projects run in parallel.
3. Enso claims the ready task with the highest priority, then the oldest, for this run.
   None left: `no_work`.
4. For a repo project, the task's worktree is prepared under `worktrees/<KEY>/<REF>`. A
   failure here releases the claim with the fault as the message and fails the run.
5. The prompt is assembled: the [Task block](tasks.md#the-task-block), the main checkout's
   `AGENTS.md` (else `CLAUDE.md`) as project instructions, then the job prompt with
   `{{prerun_output}}` substituted. The provider runs in the workspace with `ENSO_TASK` and,
   for a repo project, `ENSO_TASK_DIR` set.
6. Provider turns and postrun follow-ups proceed exactly as above.
7. The agent hands off with a move, which clears the claim; the run then holds nothing and
   may not move the task again, so a postrun follow-up cannot walk it on through the next
   stage. If the run ends still holding the claim, Enso releases it and records that the
   run ended without a handoff; a second such run in a row blocks the task with the
   attention flag.
8. After a run finishes a task, and once per tick for every repo project, the worktrees of
   finished tasks are swept.

## What Enso is not

- **Not a sandbox.** Permissions belong to the provider CLI. Enso passes your flags through.
- **Not a content system.** No docs, notes, or wiki features. Use a directory and a skill.
- **Not multi-user.** One operator and their machine.
- **Not a control plane.** The web viewer is read-only by design; the board moves from
  chat and the CLI.
