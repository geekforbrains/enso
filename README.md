# Enso

**Text your AI agents from Telegram or Slack. They run on your machine.**

Enso connects [Claude Code](https://code.claude.com/docs),
[Codex](https://github.com/openai/codex), xAI's Grok Build CLI, and Google's
Antigravity CLI to chat. Ask for work from your phone, follow live progress, switch
provider or model, send files, and receive the final answer where the conversation
started.

Enso is a self-hosted bridge, not another agent runtime. The provider CLI still owns
the session and enforces its native permissions; Enso supplies routing, workspace
context, scheduling, transport formatting, and a local dashboard around it.
Everything stays inspectable on the host, from prompts and jobs to run history and logs.

## Key features

- **Telegram and Slack:** private Telegram chats plus exact Slack DM and channel routes.
- **Four agent CLIs:** Claude, Codex, Grok, and Antigravity, with durable provider,
  model, and reasoning-effort choices per route.
- **Visible progress:** one live status message while work runs, followed by a clean
  final response with transport-appropriate formatting.
- **Controlled execution:** each workspace selects one reusable policy, which either
  grants trusted unrestricted access or supplies provider-native restricted settings.
- **Scheduled work:** cron jobs use the same workspace and policy model as chat, with
  optional host-side prerun gates and targeted notifications.
- **Local operations:** a web dashboard, run history, reference docs, structured data
  tables, managed content history, and service management for macOS and Linux.

## The execution model

```text
route  →  workspace  →  policy  →  provider CLI
who       context/cwd      authority      session and tools
```

A Telegram account, Slack DM, Slack channel, or job selects a named workspace. The
workspace supplies focused instructions and files, sets the provider's working
directory, and names exactly one policy. The policy decides which providers and Enso
commands are available and which native CLI settings protect the launch. Routes and
jobs cannot override that authority. See [Configuration](docs/configuration.md) for the
full operator model.

## Requirements

- Python 3.10 or newer
- Git 2.28 or newer
- At least one installed `claude`, `codex`, `grok`, or `agy` CLI
- A Telegram bot token or a Slack app configured for Socket Mode

Current minimums for newer provider features are Codex CLI 0.144.0, Grok Build CLI
1.0.4, and Antigravity CLI 1.1.5. Authentication for each provider CLI should already
work on the machine that will run Enso.

## Quick start

```bash
git clone https://github.com/geekforbrains/enso.git
cd enso
pip install -e ".[telegram]"    # or ".[slack]" / ".[telegram,slack]"
enso setup
enso serve
```

The setup wizard detects installed providers, connects one transport, creates the
managed `~/.enso/` tree, and can install a launchd or systemd service. Slack setup uses
the bundled app manifest; Telegram setup walks through the BotFather token and exact
numeric user ID. Continue with the [Getting started guide](docs/getting-started.md), or
go directly to the [Slack guide](docs/slack.md).

> [!WARNING]
> A fresh setup creates `~/.enso/workspaces/default` and binds it to an unrestricted
> `admin` policy. That workspace has the authority of your local user account. Keep it
> limited to trusted private routes, and create restricted policies before connecting
> shared Slack channels.

If setup installed the background service, Enso is already running. Otherwise use
`enso serve`. The optional local dashboard is a separate process:

```bash
pip install -e ".[web]"
enso web                         # http://127.0.0.1:1337
```

In Telegram, type `/help`; in Slack, use `!help`. The available command list reflects
the route's workspace policy. Common commands switch provider/model/effort, show status,
stop work, clear or compact a session, inspect logs, and install a validated update.

## Documentation

Start at the [documentation index](docs/README.md), or jump to the task at hand:

| Goal | Guide |
| --- | --- |
| Install Enso and send the first message | [Getting started](docs/getting-started.md) |
| Configure a Slack app, routes, triggers, and output | [Slack](docs/slack.md) |
| Create workspaces and policies or manage secrets | [Configuration](docs/configuration.md) |
| Run the dashboard, service, updater, docs, and tables | [Operations](docs/operations.md) |
| Create and troubleshoot scheduled agent work | [Background jobs](docs/jobs.md) |
| Develop Enso or cut a release | [Contributing](docs/contributing.md) |

The [changelog](CHANGELOG.md) records shipped behavior. Operators upgrading from 1.2
must perform the breaking [policy/binding migration](docs/migrations/unified-workspace-policies.md)
and then the [managed-workspace migration](docs/migrations/v2.0-managed-workspaces.md);
there is no automatic migration command.

## Security and ownership

Enso validates that routes, workspaces, policies, and native settings fit together, but
`enso config check` is a plumbing check—not proof that a provider sandbox is safe. Test
restricted native policies with the installed CLI, protect policy files and credentials
outside writable workspaces, and treat channel membership as authorization for a routed
Slack channel.

Your operational content remains local under `~/.enso/`. Enso initializes a local-only
Git history for user-owned instructions, skills, docs, jobs, and workspace knowledge;
configuration, credentials, databases, uploads, drafts, logs, and other runtime state
stay ignored. Enso never creates or contacts a Git remote for that content.

## Development

See [Contributing](docs/contributing.md) for the development environment, checks,
branching model, changelog rules, and release checklist.

Enso is available under the [MIT License](LICENSE).
