# Enso

Chat with your AI agents from Telegram or Slack. They run on your machine.

Enso connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli) to a Telegram or Slack bot so you can chat with them from your phone or desktop. You get live status updates as they work, can switch between agents mid-conversation, and schedule background jobs on a cron.

## Requirements

- Python 3.10+
- At least one of: `claude`, `codex`, or `gemini` installed and on your PATH
- A Telegram bot token ([create one with @BotFather](https://t.me/BotFather)) **or** a Slack app (see [Slack Setup](#slack-setup))

## Quick Start

```bash
git clone https://github.com/geekforbrains/enso.git
cd enso
pip install -e .          # Telegram only
pip install -e ".[slack]" # adds Slack support
enso setup
```

The setup wizard detects your agent CLIs, connects your Telegram or Slack bot, and optionally installs a background service (launchd on macOS, systemd on Linux) so Enso starts on boot.

Once setup is done, start chatting:

```bash
enso serve
```

Or if you installed the background service, it's already running.

## Chat Commands

### Telegram

Commands show up in Telegram's autocomplete menu when you type `/`.

| Command | What it does |
|---------|-------------|
| `/use` | Switch agent (shows buttons, or `/use claude`) |
| `/model` | Switch model (shows buttons, or `/model sonnet`) |
| `/status` | Active agent and model |
| `/stop` | Kill whatever's running |
| `/clear` | New session (shows current/all buttons) |
| `/restart` | Restart the service |
| `/logs` | Last 25 log entries |
| `/help` | Show all commands |

You can also send files — they're downloaded and passed to the active agent. Responses render with full Telegram formatting (bold, italic, code blocks, links, blockquotes).

### Slack

In Slack, use `!command` syntax (no Slack app manifest changes needed):

| Command | What it does |
|---------|-------------|
| `!use` | Switch agent (`!use claude`) |
| `!model` | Switch model (`!model sonnet`) |
| `!status` | Active agent and model |
| `!stop` | Kill whatever's running |
| `!clear` | New session (`!clear all` for all providers) |
| `!help` | Show all commands |

**DMs**: All messages go directly to the agent.
**Channels**: @mention the bot to start a conversation. Replies are threaded.

## Background Jobs

Enso can run agents on a schedule. Jobs live in `~/.enso/jobs/` and run inside `enso serve` on a 60-second tick.

```bash
enso job create --name "Daily Review" --provider claude --model sonnet --schedule "0 9 * * *"
enso job list
enso job run daily-review    # test it manually
```

Each job has a `JOB.md` with a cron schedule, provider, model, and prompt. Jobs can include a prerun script that gates execution — `exit 0` to proceed, `exit 1` to skip silently, `exit 2+` for errors. Prerun stdout gets injected into the prompt via `{{prerun_output}}`. The bundled `jobs` skill teaches your agents how to create and manage jobs themselves.

## Service Management

```bash
enso service status
enso service install       # launchd on macOS, systemd on Linux
enso service uninstall
enso service logs -f
```

## Config

Everything lives under `~/.enso/`. Config is at `~/.enso/config.json` — the setup wizard writes it for you, but you can edit it directly to add models or change the working directory.

## Slack Setup

To use Enso with Slack, you need to create a Slack app. This takes about 5 minutes.

### 1. Create the App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From scratch**
3. Name it (e.g., "Enso Agent") and select your workspace

### 2. Enable Socket Mode

1. Go to **Settings → Socket Mode** in the left sidebar
2. Toggle **Enable Socket Mode** on
3. Create an app-level token with the `connections:write` scope
4. Copy the token — it starts with `xapp-`

### 3. Add Bot Scopes

Go to **OAuth & Permissions → Scopes → Bot Token Scopes** and add:

| Scope | Why |
|-------|-----|
| `chat:write` | Send messages |
| `channels:history` | Read messages in public channels |
| `groups:history` | Read messages in private channels |
| `im:history` | Read DMs |
| `im:write` | Send DM notifications |
| `app_mentions:read` | Respond to @mentions |

### 4. Subscribe to Events

Go to **Event Subscriptions** → toggle on → **Subscribe to bot events** and add:

| Event | Why |
|-------|-----|
| `message.im` | Receive DMs |
| `app_mention` | Receive @mentions in channels |

Optionally add `message.channels` and/or `message.groups` if you want the bot to see all messages in channels it's in (not just @mentions).

### 5. Install & Get Tokens

1. Go to **Install App** and install to your workspace
2. Copy the **Bot User OAuth Token** — it starts with `xoxb-`

### 6. Run Setup

```bash
pip install -e ".[slack]"
enso setup
```

Choose "slack" when prompted for transport, then paste your `xoxb-` bot token and `xapp-` app-level token. Optionally add your Slack user ID for access control (find it in your Slack profile → three dots → "Copy member ID").

### 7. Invite the Bot

Add the bot to any channels where you want to @mention it, or just DM it directly.

## Development

```bash
pip install -e ".[dev]"
ruff check src/
pytest
```
