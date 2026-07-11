# Enso

Text your AI agents from Telegram or Slack. They run on your machine.

Enso connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli) to a Telegram bot or Slack workspace so you can chat with them from your phone. You get live status updates as they work, can switch between agents mid-conversation, and schedule background jobs on a cron.

## Documentation

Design docs live in [`docs/`](docs/) and are the source of truth for planned and in-progress work — read the one that owns what you're changing, and update it in the same commit.

| Doc | Owns |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | **Web UI** — product requirements, shipped scope, and planned extensions |
| [`docs/specs/architecture.md`](docs/specs/architecture.md) | Dashboard/bot process boundaries and shared run storage |
| [`docs/specs/data-model.md`](docs/specs/data-model.md) | The runs SQLite schema, config, `~/.enso/` layout |
| [`docs/specs/web.md`](docs/specs/web.md) | The web UI: routes, pages, read/write flows |
| [`CHANGELOG.md`](CHANGELOG.md) | What has actually shipped, per version |

> The dashboard and run history ship today. The Web UI docs distinguish current
> behaviour from planned CRUD extensions.

## Requirements

- Python 3.10+
- At least one of: `claude`, `codex`, or `gemini` installed and on your PATH
  - Codex CLI 0.144.0 or newer is required for the Sol, Terra, and Luna models
- One of:
  - A Telegram bot token ([create one with @BotFather](https://t.me/BotFather)), or
  - A Slack app with a bot token + app-level token (Socket Mode)

## Quick Start

```bash
git clone https://github.com/geekforbrains/enso.git
cd enso
pip install -e ".[telegram]"    # or ".[slack]", or ".[telegram,slack]"
enso setup
```

The setup wizard detects your agent CLIs, connects your chosen transport (Telegram or Slack), prompts for the user IDs allowed to message the bot (the bot is locked down by default and only responds to listed IDs), and optionally installs a background service (launchd on macOS, systemd on Linux) so Enso starts on boot.

Once setup is done, start chatting:

```bash
enso serve
```

Or if you installed the background service, it's already running.

The optional local dashboard runs as a separate process. Install the web extra and
start it at `http://127.0.0.1:1337`:

```bash
pip install -e ".[web]"
enso web
```

For remote or Tailscale access, bind the dashboard to the required interface. A
concrete `web.host` is allowed automatically. If you bind `0.0.0.0` or `::`, list each
hostname or IP that clients will use in `web.allowed_hosts`; a wildcard listen address
does not accept arbitrary `Host` headers. For example:

```json
{
  "web": {
    "host": "0.0.0.0",
    "allowed_hosts": ["enso.example.ts.net", "100.64.0.10"],
    "token": "replace-with-a-long-random-secret"
  }
}
```

The host allowlist prevents DNS-rebinding requests; it is not authentication. An empty
`web.token` disables authentication entirely. Any remotely reachable dashboard should
use a strong token or sit behind trusted tailnet/reverse-proxy access controls.

## Chat Commands

Telegram autocompletes these when you type `/`. On Slack, use `!` instead (e.g. `!status`).

| Command | What it does |
|---------|-------------|
| `/use` | Switch agent (shows buttons, or `/use claude`) |
| `/model` | Switch model (shows buttons, or `/model sonnet` / `/model sol`) |
| `/effort` | Set Claude/Codex reasoning effort (or `default` to clear) |
| `/kage` | Route Claude through kage instead of `claude -p` (`/kage jobs on` for background jobs) |
| `/status` | Active agent, model, runner, and effort |
| `/stop` | Stop process & clear queue |
| `/queue` | View & manage queued messages |
| `/clear` | New session (shows current/all buttons) |
| `/compact` | Summarise the current session and reseed a fresh one — keeps the thread, trims tokens |
| `/update` | Validate and install the latest stable Enso source, then restart services |
| `/restart` | Restart the service |
| `/logs` | Last 25 log entries |
| `/help` | Show all commands |

You can also send files — they're downloaded and passed to the active agent. Responses render with per-transport formatting (Telegram HTML; Slack mrkdwn).

Effort is stored separately for each conversation, provider, and model. Claude supports its existing model-dependent range through `max`. Codex Sol and Terra support `low` through `ultra`; Luna supports `low` through `max`. Enso clamps an unsupported higher choice to the active model's maximum and reports the effective level.

**Slack specifics.** DMs work like Telegram — every message dispatches. In channels, Enso only responds when mentioned (`@bot help me`); once a thread starts, it stays attentive to that thread only if you keep mentioning it. The bot fetches the last few thread/channel messages as context so it knows what's going on.

## Claude runner (kage)

Claude requests default to `claude -p`. You can instead route them through [kage](https://github.com/geekforbrains/kage), which drives Claude Code's interactive TUI in tmux — useful when you'd rather use your Claude subscription than `claude -p`'s API billing. kage must be installed and on your PATH.

Interactive chat and background jobs choose their runner **independently**:

| Toggle | Affects |
|--------|---------|
| `/kage on` · `/kage off` | Your interactive chat messages |
| `/kage jobs on` · `/kage jobs off` | Background jobs |

So chat can run through kage while jobs stay on `claude -p`, or any other mix. `/status` shows both. The settings live under `providers.claude` in `config.json` (`runner` for chat, `job_runner` for jobs); both default to `print` (i.e. `claude -p`).

## Slack directory (`enso slack`)

When an agent needs to mention a person or post to a channel, it has to
speak in Slack IDs (`<@U…>`, `<#C…>`). The `enso slack` subcommand is a
name↔ID directory backed by a local JSON cache at
`~/.enso/cache/slack.json`.

```bash
enso slack lookup-user "gavin"            # name / email / display → user
enso slack lookup-channel "daily"         # name → channel
enso slack whois U0AETSSDDEF              # reverse: ID → user
enso slack open-dm gavin                  # returns the DM channel ID
enso slack list [users|channels]          # dump cache (auto-refresh if empty)
enso slack refresh [--users|--channels]   # force refresh

enso slack search "deploy failed"         # search.messages (needs user token)
enso slack history C0AEWRPJ9LM            # channel history
enso slack thread C0AEWRPJ9LM <ts>        # full thread
```

Lookups refresh automatically on a miss (guarded to at most once every 60
seconds so a typo-happy agent can't hammer the API). The bundled `slack`
skill teaches agents when and how to use these commands.

### Slack app setup

Enso ships a Slack app manifest with every scope and event subscription
pre-configured. `enso setup` copies it to `~/.enso/slack-app-manifest.yaml`
and walks you through the one-paste flow. To do it manually:

1. Open https://api.slack.com/apps?new_app=1
2. Choose **From an app manifest**
3. Paste the contents of `~/.enso/slack-app-manifest.yaml` (or the
   bundled `src/enso/slack_manifest.yaml`)
4. **Install to workspace** — gives you the xoxb- bot token
5. Under **Basic Information → App-Level Tokens**, generate a token
   with scope `connections:write` — that's the xapp- token
6. `enso setup` and paste both tokens when prompted

The manifest is a reasonable default; prune scopes or events if you
don't need a feature. Without the directory-cache events the cache
still works, it just refreshes lazily instead of in real time.

## Sending messages from the CLI

Enso can send one-off messages or file attachments from the command line:

```bash
enso message send "Deploy finished"
enso message attach report.pdf "Weekly summary"
```

Pass `--to` to target a single destination:

| Transport | With `--to` | Without `--to` |
|-----------|-------------|----------------|
| Telegram  | send to that user ID | broadcast to all `allowed_users` |
| Slack     | send to that channel/DM/user ID | use `notify_channel` from config; error if not set |

Slack never auto-broadcasts — always pass `--to` or configure `notify_channel`. Slack file uploads accept any type up to 1 GB.

## Background Jobs

Enso can run agents on a schedule. Jobs live in `~/.enso/jobs/` and run inside `enso serve` on a 60-second tick.

```bash
enso job create --name "Daily Review" --provider claude --model sonnet --schedule "0 9 * * *"
enso job create --name "Fast Triage" --provider codex --model luna --schedule "*/30 * * * *"
enso job list
enso job run daily-review    # test it manually
```

Each job has a `JOB.md` with a cron schedule, provider, model, and prompt. Jobs can include a prerun script that gates execution — `exit 0` to proceed, `exit 1` to skip silently, and any other exit to fail. Prerun timeouts, missing scripts, and exit `2+` are recorded in run history and notify through the job's configured destination. Identical alerts are suppressed for 24 hours and one recovery is sent when the prerun becomes healthy. Prerun stdout gets injected into the prompt via `{{prerun_output}}`; only an explicit, sanitized `ENSO_ERROR:` stderr summary can appear in an alert. The bundled `jobs` skill teaches your agents how to create and manage jobs themselves.

`enso job run <name>` exercises the same prerun and provider pipeline without sending chat notifications. Intentional no-work exits successfully with a clear message; prerun and provider failures return a nonzero CLI status.

Codex models use the short names `sol`, `terra`, and `luna` in chat commands and job files. Enso translates them to the CLI model IDs `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` when spawning Codex. Full or custom model IDs remain supported.

Claude jobs run via `claude -p` by default; `/kage jobs on` routes them through kage independently of your chat runner (see [Claude runner](#claude-runner-kage)). Each job's whole lifecycle — dispatch, prerun gate, spawn, completion or timeout — is logged under a `[job:<name>]` tag for easy tracing.

## Service Management

```bash
enso service status
enso service install       # launchd on macOS, systemd on Linux
enso service uninstall
enso service logs -f
```

`/update` (or `!update` on Slack) is deterministic and never asks the active
model to modify the installation. It checks the fixed
`geekforbrains/enso` `main` branch, pins its exact Git commit, builds a wheel,
installs it in an isolated environment, runs that revision's test suite, and
only then installs the same wheel and restarts Enso. If the installed commit
already matches, it reports that there is nothing to update. Successful
updates are confirmed after the bot (and the dashboard service, when
installed) have restarted. Editable development checkouts that already
contain stable `main` are recognized as ahead and are never downgraded.

Update metadata lives in `~/.enso/update.json`, separate from user settings in
`config.json`. Enso tracks the commit SHA as well as the package version,
because multiple source revisions can legitimately share a version while
development is in progress.

## Config

Everything lives under `~/.enso/`. Config is at `~/.enso/config.json` — the setup wizard writes it for you, but you can edit it directly to add models or change the working directory. Set `notify_channel` to give `enso message send`, job alerts, and autocompact hooks a default destination (required for Slack; on Telegram it's optional — without it, sends broadcast to `allowed_users`).

## Development

```bash
pip install -e ".[dev]"
ruff check src/
pytest
```

### Branching & Releases

| Branch | Purpose |
|--------|---------|
| `main` | Latest stable release. Tagged with version numbers (e.g. `v0.10.0`). |
| `dev` | Pre-release work for the next version. All feature branches merge here first. |
| `feat/*`, `fix/*` | Short-lived branches off `main` or `dev` for individual changes. |

**Workflow:**

1. Create a feature branch off `main` (or `dev` if building on unreleased work)
2. Do the work, commit with [conventional commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, etc.)
3. Merge into `dev` — this is where changes accumulate before release
4. When ready to release: bump version in `pyproject.toml` (remove `.dev0`), finalize the `[Unreleased]` section in `CHANGELOG.md` with the date, merge `dev` → `main`, and tag

### Versioning

Version lives in `pyproject.toml`. When cutting a release: bump the version, change the `CHANGELOG.md` heading from `[Unreleased] (X.Y.Z)` to `[X.Y.Z] - YYYY-MM-DD`, commit as `chore: release vX.Y.Z`, merge `dev` → `main`, and tag `vX.Y.Z`.

### Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/). The `dev` branch always has an `[Unreleased]` section at the top. Add entries there as you merge features — don't wait until release time.
