# Enso

Text your AI agents from Telegram or Slack. They run on your machine.

Enso connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), and Google's Antigravity CLI (`agy`) to a Telegram bot or Slack workspace so you can chat with them from your phone. You get live status updates as they work, can switch between agents mid-conversation, and schedule background jobs on a cron.

## Documentation

Design docs live in [`docs/`](docs/) and are the source of truth for planned and in-progress work — read the one that owns what you're changing, and update it in the same commit.

| Doc                                                        | Owns                                                                                |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| [`docs/PRD.md`](docs/PRD.md)                               | **Web UI** — product requirements, shipped scope, and planned extensions            |
| [`docs/specs/architecture.md`](docs/specs/architecture.md) | Dashboard/bot process boundaries and shared storage                                 |
| [`docs/specs/data-model.md`](docs/specs/data-model.md)     | SQLite schemas, config, and the `~/.enso/` layout                                   |
| [`docs/specs/docs.md`](docs/specs/docs.md)                 | Operator-authored reference docs and their dashboard/CLI workflow                   |
| [`docs/specs/teams.md`](docs/specs/teams.md)               | Exact Slack routes, shared workspaces, access profiles, and optional audit metadata |
| [`docs/specs/permissions.md`](docs/specs/permissions.md)   | Native Claude/Codex policy selection and invocation                                 |
| [`docs/specs/tables.md`](docs/specs/tables.md)             | Registered SQLite data tables, discovery, and bounded read-only views               |
| [`docs/specs/web.md`](docs/specs/web.md)                   | The web UI: routes, pages, read/write flows                                         |
| [`CHANGELOG.md`](CHANGELOG.md)                             | What has actually shipped, per version                                              |

> The dashboard and run history ship today. The Web UI docs distinguish current
> behaviour from planned CRUD extensions.

## Requirements

- Python 3.10+
- At least one of `claude`, `codex`, or `agy` installed and on your PATH
  - Codex CLI 0.144.0 or newer is required for the Sol, Terra, and Luna models
  - Agy CLI 1.1.5 or newer is required for stable model slugs and project-aware sessions
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

The setup wizard detects your agent CLIs, connects your chosen transport, and optionally installs a background service (launchd on macOS, systemd on Linux) so Enso starts on boot. Telegram captures one exact numeric user ID and remains private one-to-one. Slack creates one exact owner DM route backed by a default workspace and administrative access profile; add channel routes deliberately in `config.json`.

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
    "token_1password": {
      "item": "Enso - Web - Dashboard",
      "field": "WEB_TOKEN"
    }
  }
}
```

The host allowlist prevents DNS-rebinding requests; it is not authentication. With
neither `web.token` nor `web.token_1password`, authentication is disabled entirely.
Any remotely reachable dashboard should use a strong token or sit behind trusted
tailnet/reverse-proxy access controls.

## Chat Commands

Telegram autocompletes these when you type `/`. On Slack, use `!` instead (e.g. `!status`).

| Command    | What it does                                                                          |
| ---------- | ------------------------------------------------------------------------------------- |
| `/use`     | Switch agent (shows buttons, or `/use claude` / `/use agy`)                           |
| `/model`   | Switch model (shows buttons, or `/model sonnet` / `/model gemini-3.6-flash-high`)     |
| `/effort`  | Set the active provider's reasoning effort (or `default` to clear)                    |
| `/status`  | Active agent, model, and effort                                                       |
| `/stop`    | Stop process & clear queue                                                            |
| `/queue`   | View & manage queued messages (Telegram only)                                         |
| `/clear`   | New session (shows current/all buttons)                                               |
| `/compact` | Summarise the current session and reseed a fresh one — keeps the thread, trims tokens |
| `/update`  | Validate and install the latest stable Enso source, then restart the service          |
| `/restart` | Restart the service (Telegram only)                                                   |
| `/logs`    | Last 25 log entries                                                                   |
| `/help`    | Show all commands                                                                     |

You can also send files — they're downloaded and passed to the active agent. Responses render with per-transport formatting (Telegram HTML; Slack mrkdwn). While a request runs, Enso keeps one transient status message showing which provider, model, and effort are handling it, how long it has been running, and what the agent is doing right now (`Reading core.py`, `Running pytest`, `Writing report.md`) — including for Antigravity, whose headless mode prints only a final answer. The elapsed counter updates every second through 30 seconds, then every five seconds to stay within transport limits; each edit includes the latest activity. The final response contains only the agent's answer. Interactive turns stop after `agent.timeout` seconds (900 by default; set it to `0` to disable). A timeout leaves a conversation-scoped background notice for the next turn so the active provider knows partial work may remain.

Effort is stored separately for each conversation, provider, and model. Claude supports its existing model-dependent range through `max`. Codex Sol and Terra support `low` through `ultra`; Luna supports `low` through `max`. Antigravity's concrete model names already encode effort (for example, `gemini-3.6-flash-low`), so choose the desired variant with `/model`. Enso clamps an unsupported higher Claude/Codex choice to the active model's maximum and reports the effective level.

**Slack specifics.** Every authorized Slack location is an exact route. Configured DMs dispatch every ordinary message. In channels, Enso only responds when mentioned (`@bot help me`); once a thread starts, it stays attentive to that thread only if you keep mentioning it. For configured routes, the bot fetches the last few thread/channel messages as context so it knows what's going on. Explicit contact at an unconfigured location gets a fixed local response as described below.

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

enso slack search "deploy failed"         # search.messages (public channels)
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
1. Choose **From an app manifest**
1. Paste the contents of `~/.enso/slack-app-manifest.yaml` (or the
   bundled `src/enso/slack_manifest.yaml`)
1. **Install to workspace** — gives you the xoxb- bot token
1. Under **Basic Information → App-Level Tokens**, generate a token
   with scope `connections:write` — that's the xapp- token
1. `enso setup` and paste both tokens when prompted

The manifest is a reasonable default; prune scopes or events if you
don't need a feature. Without the directory-cache events the cache
still works, it just refreshes lazily instead of in real time.

## Sending messages from the CLI

Enso can send one-off messages or file attachments from the command line:

```bash
enso message send "Deploy finished"
enso message attach report.pdf "Weekly summary"
```

Pass `--to` to target a single destination. Without it, an interactive agent call returns to `ENSO_ORIGIN_CHANNEL`; otherwise Enso uses that transport's `notify_channel` and errors when neither exists:

| Transport | With `--to`                     | Without `--to`                                    |
| --------- | ------------------------------- | ------------------------------------------------- |
| Telegram  | send to that numeric chat ID    | use the interactive origin, then `notify_channel` |
| Slack     | send to that channel/DM/user ID | use the interactive origin, then `notify_channel` |

Neither transport auto-broadcasts. Always pass `--to`, call from an interactive turn with an origin, or configure `notify_channel`. Slack file uploads accept any type up to 1 GB.

Telegram accepts private chats only and authorizes exact numeric strings in `transports.telegram.allowed_users`. The `"*"` wildcard and old `allowed_user_ids` spelling are not supported.

## Slack routes, workspaces, and access

Slack binds each exact DM user or channel route to two named objects: a workspace containing shared project knowledge and an access profile containing providers, Enso chat commands, and either unrestricted execution or protected native CLI policies. Slack has no default route, wildcard, or `allowed_users` mode.

Channel membership is the authorization boundary. Everyone in a configured channel uses its route's same access profile, including administrators. A client channel and an internal staff channel can point at the same project workspace while using read-only and broader profiles respectively. Exact DMs are keyed by Slack user ID, so an owner DM can use an unrestricted administrative profile without granting that authority in shared channels.

An unlisted DM receives a fixed access message, and an explicit mention in an unlisted channel receives a fixed thread reply; neither response invokes an LLM, resolves a workspace, fetches context, or creates a route audit record. Ordinary messages in unlisted channels remain ignored. A broken configured route or native policy reports a configuration error and never falls back to another workspace or unrestricted execution.

```bash
enso config check                          # check routes, jobs, and native policy plumbing
enso route explain slack U012ABC C0ACME    # dry-run how a sender/channel resolves
enso audit tail                            # inspect recorded Slack audit turns
```

The examples in [`docs/examples/`](docs/examples/) are starting points, not policy certification. [`docs/specs/teams.md`](docs/specs/teams.md) covers routing and client projects; [`docs/specs/permissions.md`](docs/specs/permissions.md) covers native policy invocation. Config changes require restarting Enso.

## Background Jobs

Enso can run agents on a schedule. Jobs live in `~/.enso/jobs/` and run inside `enso serve` on a 60-second tick.

```bash
enso job create --name "Daily Review" --provider claude --model sonnet --schedule "0 9 * * *" --workspace company --access automation
enso job create --name "Fast Triage" --provider codex --model luna --schedule "*/30 * * * *" --workspace company --access automation
enso job create --name "Agy Review" --provider agy --model gemini-3.6-flash-high --schedule "0 10 * * *" --workspace company --access admin
enso job list
enso job run daily-review    # test it manually
```

Each job has a `JOB.md` with a cron schedule, provider, model, named `workspace`, named `access`, and prompt. The workspace is the provider CLI's cwd; the access profile must allow the job's provider and supplies its native policy. Every job requires both names. `enso config check` reports a missing required field and the loader skips that file; a parsed job with an invalid binding creates an error run before prerun or provider execution. Neither case falls back to the global working directory or unrestricted execution. Workspace/access configuration is top-level and works independently of Slack routes.

Schedules are validated at creation; if a hand-edited schedule later becomes invalid, the scheduler skips that job (with a log warning) instead of running it. By default a job that misses its scheduled time by more than `misfire_grace_seconds` (300) — say the machine was asleep — is skipped rather than run late; set `catch_up: true` in the frontmatter to run missed schedules on the next tick. A persistent per-job `.run.lock` file coordinates the scheduler, CLI, and dashboard so the same job does not run concurrently. Jobs sharing a workspace also use that workspace's process-local concurrency limit, but separate Enso processes do not share that workspace semaphore.

Jobs can include a prerun script that gates execution — `exit 0` to proceed, `exit 1` to skip silently, and any other exit to fail. The prerun is trusted host-side code run with the job directory as cwd; it is not constrained by the provider's native policy. Prerun timeouts, missing scripts, and exit `2+` are recorded in run history and notify through the job's configured destination. Identical alerts are suppressed for 24 hours and one recovery is sent when the prerun becomes healthy. Prerun stdout gets injected into the prompt via `{{prerun_output}}`; only an explicit, sanitized `ENSO_ERROR:` stderr summary can appear in an alert. The bundled `jobs` skill teaches your agents how to create and manage jobs themselves.

Successful jobs are silent unless their prompt explicitly sends a message. Scheduled failures and prerun recovery use the job's `notify` destination or the transport's `notify_channel`. `enso job run <name>` exercises the same prerun and provider pipeline while suppressing Enso's automatic job alerts; it cannot suppress a message explicitly sent by the provider process. Intentional no-work exits successfully with a clear message; prerun and provider failures return a nonzero CLI status.

Codex models use the short names `sol`, `terra`, and `luna` in chat commands and job files. Enso translates them to the CLI model IDs `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` when spawning Codex. Full or custom model IDs remain supported.

A job's `provider` and `model` are validated against your configured providers — jobs naming unknown values fail with a clear error instead of running. Each job's whole lifecycle — dispatch, prerun gate, spawn, completion or timeout — is logged under a `[job:<name>]` tag for easy tracing.

## Reference Docs

Standing knowledge about your setup — how a machine is wired, a deploy runbook, account
conventions — lives as Markdown under `~/.enso/docs/`, nested to any depth. Nothing loads
docs automatically; the agent consults them when a task calls for one.

```bash
enso doc list                       # path, name, and description for every doc
enso doc create stuff/homelab.md    # creates parent dirs, scaffolds frontmatter
```

Each doc carries `name` and `description` frontmatter, and identity is the path relative
to `~/.enso/docs/` rather than a slug. `enso doc list` *is* the index — it is computed
from frontmatter on every call, so it can never drift from what is on disk, which is why
there is no `INDEX.md` to maintain. The bundled `docs` skill teaches agents to check docs
before answering from memory about your setup. Browse, create, edit, and delete them under
**Docs** in the dashboard. See [`docs/specs/docs.md`](docs/specs/docs.md) for the full
design.

## Data Tables

Enso uses the existing `~/.enso/enso.db` for user-owned, queryable records such as
measurements, inventories, and metrics. Agents create and update ordinary SQLite tables;
a bundled skill keeps schemas, units, timestamps, transactions, and destructive
changes consistent.

```bash
enso table list
enso table schema weight_entries
enso table register weight_entries \
  --name "Weight" \
  --description "One body-weight measurement per row, recorded in kilograms."
```

Registration is the visibility boundary: only catalogued user tables appear under
**Tables** in the dashboard. Internal `runs`, `_enso_*`, and `sqlite_*` names stay hidden
and reserved. The web UI shows schema plus a bounded, paginated row preview; table and
row edits remain standard SQLite operations rather than a custom Enso query language.
Enso uses short-lived connections and bounded lock waits; the dashboard distinguishes a
retryable **Database busy** state from a broader **Database unavailable** failure without
blocking its health endpoint.
See [`docs/specs/tables.md`](docs/specs/tables.md) for the full design.

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
updates are confirmed after the bot service has restarted. `enso service install` only manages the bot service, but if you run the dashboard as your
own service named `com.enso.web` (launchd) or `enso-web.service` (systemd),
`/update` restarts and health-checks it too; a dashboard started with a
foreground `enso web` must be restarted manually after an update. Editable
development checkouts that already contain stable `main` are recognized as
ahead and are never downgraded.

Update metadata lives in `~/.enso/update.json`, separate from user settings in
`config.json`. Enso tracks the commit SHA as well as the package version,
because multiple source revisions can legitimately share a version while
development is in progress.

## Config

Everything lives under `~/.enso/`. Config is at `~/.enso/config.json` — the setup wizard writes it for you, but you can edit it directly to add models, define workspaces/access profiles/routes, change Telegram's global working directory, or set the interactive timeout through `agent.timeout` (whole seconds). Upgrades backfill newly supported providers without replacing existing paths or custom model lists. Set `notify_channel` to give `enso message send`, job alerts, and autocompact hooks a default destination when no interactive origin or explicit destination exists. No transport broadcasts implicitly.

### Secrets

`enso serve` loads every `~/.enso/secrets/*.env` file into its own environment at startup,
and background jobs — prerun scripts and the provider process alike — inherit it. This
exists because a service manager hands the daemon a minimal environment, so a CLI that
reads a credential from the environment (a keyring password, a service-account token) has
no other way to receive one under launchd or systemd.

```bash
mkdir -p ~/.enso/secrets && chmod 700 ~/.enso/secrets
printf 'OP_SERVICE_ACCOUNT_TOKEN=ops_...\n' > ~/.enso/secrets/1password.env
```

Files are read in filename order. Blank lines and `#` comments are skipped, a leading
`export ` is tolerated, and surrounding quotes are stripped. **A variable already present
in the environment always wins**, so an explicit export still overrides the file. Loading
happens in `enso serve` only — the dashboard process does not read these files. Restart
the service after editing them.

Credentials can be supplied three ways, and Enso does not require any particular secret
manager: a literal value in `config.json` (simplest, fine for a personal install), an
environment projection through `secrets/*.env` as above, or a reference to a secret
manager. One reference implementation ships, for 1Password — it is entirely optional, and
with no reference key configured Enso never invokes the helper.

If you choose to keep `~/.enso` in git, a literal credential in `config.json` becomes a credential in your git history. Enso does not initialize that repository for you. Keep state and secrets ignored, or use one of the other credential options so tracked config holds a reference instead of a secret.

The 1Password integration resolves credentials at each process start or CLI invocation,
without copying the token into `config.json` or projecting it into the environment. It
expects `~/.enso/lib/1password.sh` to define `op_secret "<item>" "<field>"`; Enso calls
that helper and never invokes the 1Password CLI directly. Reconfiguring an existing
reference also requires the helper's `op_set_secret` function.

```json
{
  "transports": {
    "telegram": {
      "bot_token_1password": {
        "item": "Enso - Transport - Telegram",
        "field": "TELEGRAM_BOT_TOKEN"
      },
      "allowed_users": ["123456789"],
      "notify_channel": "123456789"
    },
    "slack": {
      "bot_token_1password": {
        "item": "Enso - Transport - Slack",
        "field": "SLACK_BOT_TOKEN"
      },
      "app_token_1password": {
        "item": "Enso - Transport - Slack",
        "field": "SLACK_APP_TOKEN"
      },
      "notify_channel": "C12345678"
    }
  },
  "web": {
    "token_1password": {
      "item": "Enso - Web - Dashboard",
      "field": "WEB_TOKEN"
    }
  }
}
```

That fragment demonstrates credential storage only. A valid Slack configuration also needs the top-level `workspaces`, `access`, and exact `routes.slack` blocks shown in [`docs/examples/teams-config.jsonc`](docs/examples/teams-config.jsonc).

The service-account credential needed by the helper may still use the bootstrap
`~/.enso/secrets/1password.env` file. Existing literal `bot_token` and `app_token`
keys and `web.token` remain supported. If a matching `*_1password` key is present, it
takes precedence and a malformed or unavailable reference fails closed instead of
falling back to a possibly stale literal. Legacy literal values must still be strings;
malformed values are rejected rather than treated as empty credentials. `enso setup`, `enso message`, `enso slack`,
both transport daemons, and the dashboard app factory all use the same resolution path.
When `enso setup` reconfigures a transport that already uses a reference, it validates
the replacement token, sends the new value to `op_set_secret` over stdin, and preserves
the reference in config. A helper failure aborts setup instead of adding a plaintext
fallback. Slack preloads both previous referenced values before writing either one and
restores an earlier write if the second update fails. Literal-only transport configs
keep the original setup behavior.

### Environment variables

A few advanced knobs are environment variables rather than config keys: `ENSO_SESSION_TTL_DAYS` (prune idle conversations, default 30), `ENSO_JOB_CONCURRENCY` (parallel scheduled jobs, default 2), `ENSO_PROCESS_TERMINATE_GRACE_SECS` (SIGTERM grace before SIGKILL, default 5), and `ENSO_JOB_FAILURE_RENOTIFY_SECS` (duplicate-alert cooldown, default 86400). `enso service install` snapshots any that are set into the service definition — re-run it after changing them.

## Development

```bash
pip install -e ".[dev,telegram,slack,web]"
ruff check src/
pytest
```

### Branching & Releases

| Branch            | Purpose                                                                       |
| ----------------- | ----------------------------------------------------------------------------- |
| `main`            | Latest stable release. Tagged with version numbers (e.g. `v0.10.0`).          |
| `dev`             | Pre-release work for the next version. All feature branches merge here first. |
| `feat/*`, `fix/*` | Short-lived branches off `main` or `dev` for individual changes.              |

**Workflow:**

1. Create a feature branch off `main` (or `dev` if building on unreleased work)
1. Do the work, commit with [conventional commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, etc.)
1. Merge into `dev` — this is where changes accumulate before release
1. When ready to release: bump the version in `pyproject.toml`, finalize the `[Unreleased]` section in `CHANGELOG.md` with the date, merge `dev` → `main`, and tag

### Versioning

Version lives in `pyproject.toml` and is the single source of truth — the package reads it back through `importlib.metadata`, so nothing else needs bumping. When cutting a release: bump the version, change the `CHANGELOG.md` heading from `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, commit as `chore(release): X.Y.Z`, merge `dev` → `main`, and tag `vX.Y.Z` (annotated).

### Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/). Add entries to the `[Unreleased]` section as you merge features — don't wait until release time. Cutting a release renames that heading to the version, so `dev` carries no `[Unreleased]` section until the next change adds one back.
