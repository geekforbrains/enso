# Enso

Text your AI agents from Telegram. They run on your machine.

Enso connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli) to a Telegram bot so you can chat with them from your phone. You get live status updates as they work, can switch between agents mid-conversation, and schedule background jobs on a cron.

## Requirements

- Python 3.10+
- At least one of: `claude`, `codex`, or `gemini` installed and on your PATH
- A Telegram bot token ([create one with @BotFather](https://t.me/BotFather))

## Quick Start

```bash
git clone https://github.com/geekforbrains/enso.git
cd enso
pip install -e .
enso setup
```

The setup wizard detects your agent CLIs, connects your Telegram bot, and optionally installs a background service (launchd on macOS, systemd on Linux) so Enso starts on boot.

Once setup is done, start chatting:

```bash
enso serve
```

Or if you installed the background service, it's already running.

## Chat Commands

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

Version lives in `pyproject.toml`. The `dev` branch uses PEP 440 dev versions:

- **Released:** `0.10.0` (on `main`)
- **In development:** `0.11.0.dev0` (on `dev`)

When cutting a release, update `pyproject.toml` to `0.11.0`, update `CHANGELOG.md` heading from `[Unreleased] (0.11.0)` to `[0.11.0] - YYYY-MM-DD`, merge to `main`, and tag `v0.11.0`.

### Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/). The `dev` branch always has an `[Unreleased]` section at the top. Add entries there as you merge features — don't wait until release time.
