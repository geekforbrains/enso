# Getting started

This guide installs Enso, explains what the first setup creates, and gets a trusted
private conversation running. Configure shared Slack channels only after reading the
[configuration](configuration.md) and [Slack](slack.md) guides.

## Requirements

- Python 3.10 or newer.
- Git 2.28 or newer. Enso uses it for the managed local content repository.
- At least one authenticated provider CLI on `PATH`: `claude`, `codex`, `grok`, or
  `agy`.
- Either a Telegram bot token from [BotFather](https://t.me/BotFather), or a Slack bot
  token plus app-level Socket Mode token.

Codex CLI 0.144.0 or newer is required for the Sol, Terra, and Luna aliases. Grok Build
CLI 1.0.4 or newer is required for headless Grok launches. Antigravity CLI 1.1.5 or newer
is required for stable model names and project-aware sessions.

## Install and configure

```bash
git clone https://github.com/geekforbrains/enso.git
cd enso
pip install -e ".[telegram]"    # or ".[slack]" / ".[telegram,slack]"
enso setup
```

The wizard detects installed provider CLIs, configures the selected transport, and can
install a launchd service on macOS or a systemd user service on Linux. Telegram records
one exact numeric user ID and binds its private chat to the initial workspace. Slack
creates one exact owner-DM route; adding channels is a deliberate later step.

> [!WARNING]
> Fresh setup creates policy `admin` with unrestricted authority and binds workspace
> `default` to it. A provider launched there has the authority of the local user running
> Enso. Keep this binding private and trusted. Before routing a shared channel, create a
> restricted policy, test its native provider settings, and bind a separate workspace.

Start Enso in the foreground if the wizard did not install its service:

```bash
enso serve
```

Send the bot a message. While the provider works, Enso edits one transient status with
the effective provider, model, effort, elapsed time, and current activity. The final
message contains only the provider's answer. The interactive timeout is controlled by
`agent.timeout` in `config.json`: it defaults to 1,800 seconds, and `0` disables it.

## What fresh setup owns

A genuinely fresh setup creates one managed tree rooted at `~/.enso/`:

```text
~/.enso/
├── AGENTS.md                    shared instructions
├── skills/                      shared agent skills
├── docs/                        operator reference notes
├── jobs/                        scheduled-agent definitions
└── workspaces/default/
    ├── AGENTS.md                focused workspace instructions
    ├── skills/                  workspace-only agent skills
    ├── knowledge/               durable shared material
    ├── drafts/                  ordinary generated output
    └── uploads/                 retained incoming attachments
```

Claude and Codex discover the root and workspace instruction/skill layers through
managed relative links. Grok and Antigravity receive the validated shared instructions
through their adapters. Every provider launch revalidates the exact Git root, physical
workspace, discovery links, and skill-name uniqueness before it starts.

Setup marks an in-progress fresh transaction with `setup.completed_at: null`, creates
the seed content, records one baseline local Git commit, then writes a timezone-bearing
completion time. An interrupted run can reuse matching pieces and finish without
overwriting them or creating a second baseline commit.

All seeded content becomes user-owned immediately. A completed setup rerun is
structural-only: it checks the current catalog and repairs missing managed directories or
known links, but does not reconfigure providers, transports, routes, messaging, the
service, or `config.json`. Startup, upgrades, repair, and config checks never restore or
advance customized prompts, skills, docs, or knowledge files.

## Chat commands

Telegram commands begin with `/`; Slack uses `!`. The workspace policy decides which
commands are available, so `/help` or `!help` is the authoritative list for the route.

| Command | Purpose |
| --- | --- |
| `use` | Select an allowed provider, or `default` to follow the policy default |
| `model` | Select a model for the provider, or clear it with `default` |
| `effort` | Select reasoning effort, or clear it with `default` |
| `status` | Show effective provider, model, effort, and the source of each choice |
| `stop` | Stop the current provider process and clear that conversation's queue |
| `queue` | Inspect the in-memory queue; Telegram only |
| `clear` | Start a new provider session for the current or all conversations |
| `compact` | Summarize a session and reseed a shorter one without changing the thread |
| `update` | Validate and install stable `main`, then restart the service |
| `restart` | Restart the service; Telegram only |
| `logs` | Show the latest 25 log entries |
| `help` | Show commands allowed by the current policy |

Provider, model, and effort are durable route settings, separate from sessions. A Slack
DM or channel shares them across all roots and threads; a Telegram private chat has its
own settings. Model choices are remembered per provider, and effort choices per provider
and model. Session retention does not erase them. If policy changes revoke a selected
provider, the policy default becomes effective without deleting the saved preference.

Slack commands follow that route's mention and thread triggers. Session commands still
act only on the current DM, root, or thread; provider/model/effort commands change the
whole Slack DM or channel. See [Slack response triggers](slack.md#response-triggers).

## Files, responses, and local content

Send attachments as part of an ordinary message. Enso retains them in a unique
`uploads/<random-id>/` directory inside the routed workspace and passes their paths to
the provider. Telegram accepts files up to 20 MiB and Slack up to 100 MiB per inbound
file; unsafe or oversized downloads are skipped. Enso does not expire retained uploads
automatically.

Telegram responses use HTML. Interactive Slack responses use standard Markdown and may
use validated tables, fields, or charts; persistent App Home or Canvas changes always
show an exact requester-bound preview before Slack is changed. See [Slack](slack.md).

Fresh setup installs six portable skills: `docs`, `jobs`, `policy`, `slack`, `tables`,
and `workspace`. Keep broadly useful skills at the root and focused ones in a workspace.
An installed skill is user-owned and is not silently upgraded or resurrected. A global
and workspace-local skill may not share the same directory name.

## Optional dashboard

```bash
pip install -e ".[web]"
enso web
```

Open `http://127.0.0.1:1337`. The dashboard runs separately from the bot and exposes
run history plus configuration, workspace instructions, docs, jobs, and registered
tables. Before making it remotely reachable, follow the authentication and host-header
guidance in [Operations](operations.md#dashboard).

## Next steps

- Create restricted policies and additional workspaces in [Configuration](configuration.md).
- Configure routes and response behavior in [Slack](slack.md).
- Install and monitor the daemon with [Operations](operations.md).
- Add recurring work with [Background jobs](jobs.md).
