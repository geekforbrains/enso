# Enso

Text your AI agents from Telegram or Slack. They run on your machine.

Enso connects [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), xAI's Grok Build CLI (`grok`), and Google's Antigravity CLI (`agy`) to a Telegram bot or Slack workspace so you can chat with them from your phone. You get live status updates as they work, can switch between agents mid-conversation, and schedule background jobs on a cron.

## Documentation

Design docs live in [`docs/`](docs/) and are the source of truth for planned and in-progress work — read the one that owns what you're changing, and update it in the same commit.

| Doc                                                        | Owns                                                                                |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| [`docs/PRD.md`](docs/PRD.md)                               | **Web UI** — product requirements, shipped scope, and planned extensions            |
| [`docs/specs/architecture.md`](docs/specs/architecture.md) | Dashboard/bot process boundaries and shared storage                                 |
| [`docs/specs/data-model.md`](docs/specs/data-model.md)     | SQLite schemas, config, and the `~/.enso/` layout                                   |
| [`docs/specs/docs.md`](docs/specs/docs.md)                 | Operator-authored reference docs and their dashboard/CLI workflow                   |
| [`docs/specs/slack-output.md`](docs/specs/slack-output.md) | Rich Slack replies, typed blocks, confirmed App Home, and Canvas publication        |
| [`docs/specs/teams.md`](docs/specs/teams.md)               | Transport workspace bindings, exact Slack routes, reusable policies, and audit metadata |
| [`docs/specs/permissions.md`](docs/specs/permissions.md)   | Native Claude/Codex/Grok policy selection and invocation                            |
| [`docs/specs/tables.md`](docs/specs/tables.md)             | Registered SQLite data tables, discovery, and bounded read-only views               |
| [`docs/specs/web.md`](docs/specs/web.md)                   | The web UI: routes, pages, read/write flows                                         |
| [`docs/migrations/unified-workspace-policies.md`](docs/migrations/unified-workspace-policies.md) | Manual breaking migration from `working_dir`, `access`, and legacy Slack routes |
| [`docs/migrations/v1.3-managed-workspaces.md`](docs/migrations/v1.3-managed-workspaces.md) | Manual migration from configurable/external workspace paths to the canonical workspace tree |
| [`CHANGELOG.md`](CHANGELOG.md)                             | What has actually shipped, per version                                              |

> The dashboard and run history ship today. The Web UI docs distinguish current
> behaviour from planned CRUD extensions.

## Requirements

- Python 3.10+
- Git 2.28+ (required for the managed local content repository)
- At least one of `claude`, `codex`, `grok`, or `agy` installed and on your PATH
  - Codex CLI 0.144.0 or newer is required for the Sol, Terra, and Luna models
  - Grok Build CLI 1.0.4 or newer is required for the `grok` provider's headless launches
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

The setup wizard detects your agent CLIs, connects your chosen transport, and optionally installs a background service (launchd on macOS, systemd on Linux) so Enso starts on boot. Every fresh install creates workspace `default` at `~/.enso/workspaces/default`, bound to an unrestricted `admin` policy with full authority. This setup-only bootstrap is the sole automatic policy creation; later policies and workspace bindings are explicit. Telegram captures one exact numeric user ID and binds its private one-to-one conversations to that workspace. Slack creates one exact owner DM route with the same binding; add channel routes deliberately in `config.json`.

Fresh setup seeds shared operational instructions at `~/.enso/AGENTS.md`, global skills at
`~/.enso/skills/`, three starter references under `~/.enso/docs/`, and a complete default
workspace. Root discovery uses relative links: `CLAUDE.md -> AGENTS.md`,
`.agents/skills -> ../skills`, and `.claude/skills -> ../skills`. Each workspace lives
exactly at `~/.enso/workspaces/<lowercase-kebab-name>` and contains its own focused
`AGENTS.md`, relative `CLAUDE.md` and skill-discovery links, an initially empty `skills/`,
`knowledge/README.md`, and empty `drafts/` and `uploads/`. Claude and Codex discover the
global and workspace instruction layers natively from this single Git tree; Grok and Agy
receive the freshly validated shared text once through their explicit provider adapters.
Every launch revalidates the physical workspace, exact Git boundary, discovery links, and
skill-name uniqueness before the provider starts.

For a genuinely fresh install, setup first persists `setup.completed_at: null`, then
creates the initial content, records it in one baseline Git commit, and finally replaces
`null` with an ISO 8601 timestamp that includes a timezone. A failed seed or commit
leaves setup incomplete so an explicit rerun can finish missing work without overwriting
files already created. If the baseline committed but saving the timestamp failed, the
rerun recognizes the existing history and completes the timestamp without a second
commit.

Seeded content becomes user-owned immediately and may be edited or deleted. A completed
setup, a pre-feature installation with no `setup` field, startup, `enso web`, and
`enso config check` never seed it. For a pre-feature or completed installation, a later
explicit `enso setup` is structural-only: it validates the existing execution catalog,
then repairs only missing structural directories and known discovery links while
preserving missing or customized content and reporting conflicts. It does not reconfigure
providers, workspaces, transports, messaging, or the background service, and it does not
rewrite `config.json` or synthesize a `setup` marker. Startup and upgrades likewise never
restore or advance seeded copies.

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

The dashboard's **Configuration** section makes the execution model traceable: every
workspace and its one policy, exact Slack DM/channel routes, Telegram and job bindings,
shared instructions, and workspace-local `AGENTS.md` files. Policy and Slack pages are
read-only and never render secret values or native policy contents. Shared instructions
and every valid canonical workspace-root instruction file have revision-checked editors;
nested workspace instruction files remain read-only. Alternate, external, nested, and
symlinked workspace roots are invalid; their instruction content is never inspected or
rendered.

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

Telegram autocompletes these when you type `/`. On Slack, use `!` instead (e.g. `!status`). Slack commands follow the same response triggers as ordinary messages: a mention is required when the route's effective `mention_required` or `thread_mention_required` says so, while a responsive top level or joined thread accepts `!` commands without one. Every DM message is already admitted. A bare `!` remains ordinary prompt text.

| Command    | What it does                                                                          |
| ---------- | ------------------------------------------------------------------------------------- |
| `/use`     | Switch provider (or use `default` to follow the policy default again)                 |
| `/model`   | Switch model (or use `default` to follow the provider default again)                  |
| `/effort`  | Set the active provider's reasoning effort (or `default` to use the CLI default)      |
| `/status`  | Effective agent, model, and effort, including where each value came from              |
| `/stop`    | Stop process & clear queue                                                            |
| `/queue`   | View & manage the in-memory message queue (Telegram only)                             |
| `/clear`   | New session (shows current/all buttons)                                               |
| `/compact` | Summarise the current session and reseed a fresh one — keeps the thread, trims tokens |
| `/update`  | Validate and install the latest stable Enso source, then restart the service          |
| `/restart` | Restart the service (Telegram only)                                                   |
| `/logs`    | Last 25 log entries                                                                   |
| `/help`    | Show all commands                                                                     |

The bound workspace policy decides which providers and commands are available. Telegram's command menu and Slack's `!help` expose only that policy's allowed commands. `/use` and `!use` narrow the policy's authorized providers further to those whose native launch is currently usable; a transport cannot override the workspace policy.

Provider, model, and effort choices are durable route settings, separate from conversation sessions. One Slack DM or channel shares them across every top-level message and thread, while each Telegram private chat has its own settings; Slack accepts the settings commands inside threads and names their whole-channel or whole-DM scope in the reply. Model choices are kept per route and provider, and effort choices per route, provider, and model. `default` clears the corresponding explicit choice, and `status` labels the effective provider as a route selection or policy default, the model as a route selection or provider default, and effort as a route selection or CLI default. Route settings survive session retention. Per-conversation incoming-message queues are process-memory only and disappear on restart; Slack has no `!queue` command, although `!stop` still clears that conversation's queue.

If the current policy stops authorizing a stored provider choice, its default becomes effective without erasing that preference. A provider that remains policy-allowed but has an unusable native policy does not silently fall back: provider work reports the configuration error, while non-launch commands remain available to inspect status or select another authorized provider.

You can also send files — they're downloaded and passed to the active agent. Responses render with per-transport formatting: Telegram uses HTML, while successful interactive Slack answers use standard Markdown by default, including headings, links, fenced code, task lists, and Markdown tables. Slack agents can also choose validated native tables, compact fields, and line, bar, area, or pie charts when those layouts are useful. While a request runs, Enso keeps one transient status message showing which provider, model, and effort are handling it, how long it has been running, and what the agent is doing right now (`Reading core.py`, `Running pytest`, `Writing report.md`) — including for Antigravity, whose headless mode prints only a final answer. The elapsed counter updates every second through 30 seconds, then every five seconds to stay within transport limits; each edit includes the latest activity. The final response contains only the agent's answer. Interactive turns stop after `agent.timeout` seconds (1,800 by default; set it to `0` to disable). A timeout leaves a conversation-scoped background notice for the next turn so the active provider knows partial work may remain.

Claude supports its existing model-dependent effort range through `max`. Codex Sol and Terra support `low` through `ultra`; Luna supports `low` through `max`. Grok supports `low` through `xhigh`. Antigravity's concrete model names already encode effort (for example, `gemini-3.6-flash-low`), so choose the desired variant with `/model`. Enso preserves a chosen Claude/Codex/Grok level and clamps its effective value to the active model's maximum.

**Slack specifics.** Every authorized Slack location is an exact route. Slack credentials, options, and routes live together in `transports.slack`. Configured DMs dispatch every ordinary message. In channels, the default is mention-gated: Enso responds only when mentioned (`@bot help me`), and thread replies need a mention too. Two per-channel booleans relax this — `mention_required: false` dispatches every top-level message, and `thread_mention_required: false` follows every reply in a thread Enso already participates in — one a prior dispatch joined, or one rooted by a message Enso posted itself, such as a job notification or `enso message send` (first contact in a thread someone else started still needs a mention). A `transports.slack.channel_defaults` block supplies defaults that individual channel routes override. Either way, replies to channel messages always land in that message's thread, and posts from other bots and apps never dispatch in any mode — only human members engage a route. For configured routes, the bot fetches the last few messages of a thread it is replying in as context so it knows what's going on. Channel history is not injected: an agent using an unrestricted policy is instead told, once per conversation, how to read the channel with `enso slack history` and `enso slack thread`, and pulls it only when the request calls for it — so a new top-level ask no longer arrives carrying unrelated earlier threads. An agent using a restricted policy, whose sandbox may have no route to Slack, keeps receiving the channel context it cannot fetch for itself. Explicit contact at an unconfigured location gets a fixed local response as described below. Rich output and persistent surfaces are enabled by default. A natural-language request for an App Home or Canvas produces an exact preview with requester-bound **Publish** and **Cancel** buttons; Slack is not mutated before confirmation. App Home requests are accepted only in a configured one-to-one DM. A channel Canvas request creates a tab when none exists or clearly proposes a full replacement of the one unambiguous existing Canvas. See [`docs/specs/slack-output.md`](docs/specs/slack-output.md) for limits, fallbacks, security, and opt-outs.

## Slack directory (`enso slack`)

When an agent needs to mention a person or post to a channel, it has to
speak in Slack IDs (`<@U…>`, `<#C…>`). The `enso slack` subcommand is a
name↔ID directory backed by a local JSON cache at
`~/.enso/cache/slack.json`.

```bash
enso slack lookup-user "alex"            # name / email / display → user
enso slack lookup-channel "general"         # name → channel
enso slack whois U0123456789              # reverse: ID → user
enso slack open-dm alex                  # returns the DM channel ID
enso slack list [users|channels]          # dump cache (auto-refresh if empty)
enso slack refresh [--users|--channels]   # force refresh

enso slack search "deploy failed"         # search.messages (public channels)
enso slack history C0123456789            # channel history (top-level messages)
enso slack history C0123456789 --since 24h   # bound the window
enso slack history C0123456789 --all      # keep joins, pins and other noise
enso slack thread C0123456789 <ts>        # full thread
enso slack thread C0123456789 <ts> -n 20  # root plus the 19 most recent
```

`history` and `thread` are also what an agent uses to read the channel it
was invoked from — its ID arrives in `ENSO_ORIGIN_CHANNEL`. Both resolve
display names, render mentions inert, and decode Slack's entity escaping.
Thread replies never appear in `history`; Slack keeps them out of channel
history, so reach them with `thread` and the parent's `ts`.

Lookups refresh automatically on a miss (guarded to at most once every 60
seconds so a typo-happy agent can't hammer the API). The bundled `slack`
skill teaches agents when and how to use these commands.

### Slack app setup

Enso ships a Slack app manifest with its scopes, events, App Home, interactivity, and
Socket Mode preconfigured. During a fresh or incomplete setup that selects Slack, the
wizard copies it to `~/.enso/slack-app-manifest.yaml` and walks you through the one-paste
flow. Structural-only setup for a pre-feature or completed installation does not refresh
`~/.enso/slack-app-manifest.yaml` or reconfigure Slack. Existing installations should
apply the current bundled [`src/enso/slack_manifest.yaml`](src/enso/slack_manifest.yaml)
deliberately; copy it to the local manifest path manually only after preserving any local
edits. To create a new Slack app manually:

1. Open https://api.slack.com/apps?new_app=1
1. Choose **From an app manifest**
1. Paste the contents of `~/.enso/slack-app-manifest.yaml` (or the
   bundled `src/enso/slack_manifest.yaml`)
1. **Install to workspace** — gives you the xoxb- bot token
1. Under **Basic Information → App-Level Tokens**, generate a token
   with scope `connections:write` — that's the xapp- token
1. During fresh or incomplete setup, choose Slack and paste both tokens when prompted

For an existing Enso Slack app, apply the current manifest to that app before upgrading: enable its Home tab and interactivity, add `canvases:write` and `files:read` if missing, reinstall or reauthorize when Slack requests consent, then restart Enso. Block actions continue over Socket Mode; no public interactivity URL or `block_actions` event subscription is required. `chat:write` covers replies and confirmation-card updates, while App Home publication adds no special bot scope. The manifest is a reasonable default; prune features only when you also disable their Enso configuration. Without the directory-cache events the cache still works, but refreshes lazily instead of in real time.

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

Neither transport auto-broadcasts. Always pass `--to`, call from an interactive turn with an origin, or configure `notify_channel`. Slack file uploads accept any type up to 1 GB. CLI messages, file captions, scheduled-job notifications, and other direct notifications are intentionally text-only in this release; rich structured rendering applies to interactive Slack final answers.

Telegram accepts private chats only and authorizes exact numeric strings in `transports.telegram.allowed_users`. Its required `workspace` selects the conversation cwd and derives provider availability, commands, concurrency, and native policy. The `"*"` wildcard and old `allowed_user_ids` spelling are not supported. Telegram attachments use the same persistent `uploads/<random-id>/` layout as other workspace-bound turns. Enso retains inbound Telegram files up to 20 MiB and inbound Slack files up to 100 MiB per file; oversized or unsafe downloads are skipped rather than exposed to the agent.

## Workspaces, policies, and Slack routes

Every execution begins in one named workspace containing focused project knowledge, and every workspace names exactly one reusable policy controlling providers, Enso chat commands, and either unrestricted execution or protected native CLI settings. Telegram names one workspace in `transports.telegram.workspace`; every Slack DM or channel route names one workspace; every job does the same in `JOB.md`. None can override the workspace's policy.

Slack credentials, transport-wide options, and route maps coexist in `transports.slack`. Channel routes may carry the optional response-trigger settings `mention_required` and `thread_mention_required`, with fallback defaults in `transports.slack.channel_defaults`. Slack has no default route, wildcard, or `allowed_users` mode: `channel_defaults` is settings inheritance for channels that are already routed, not authorization — unrouted channels stay unrouted.

Channel membership is the authorization boundary. Everyone in a configured channel uses its workspace's policy, including administrators. Channels that need different context or authority use different workspaces; multiple workspaces may reuse the same policy. Exact DMs are keyed by Slack user ID, so an owner DM can use a workspace with an unrestricted administrative policy without granting that authority in shared channels.

An unlisted DM receives a fixed access message, and an explicit mention in an unlisted channel receives a fixed thread reply; neither response invokes an LLM, resolves a workspace, fetches context, or creates a route audit record. Ordinary messages in unlisted channels remain ignored — only a configured channel route can dispatch an un-mentioned message, and only when its effective `mention_required` is `false`. A broken configured route or native policy reports a configuration error and never falls back to another workspace or unrestricted execution.

```bash
enso policy list                          # inspect policy capabilities and consumers
enso policy show staff                    # inspect safe native validation metadata
enso config check                          # check routes, jobs, and native policy plumbing
enso route explain slack U012ABC C0ACME    # dry-run how a sender/channel resolves
enso audit tail                            # inspect recorded Slack audit turns
```

The examples in [`docs/examples/`](docs/examples/) are explanatory starting points, not trusted or certified policy presets. A copy becomes user-owned native policy content. [`docs/specs/teams.md`](docs/specs/teams.md) covers routing and client projects; [`docs/specs/permissions.md`](docs/specs/permissions.md) covers native policy invocation. Existing installations must apply the [unified-policy migration](docs/migrations/unified-workspace-policies.md) where needed and then the [v1.3 managed-workspace migration](docs/migrations/v1.3-managed-workspaces.md); there is no `enso migrate` command. Config changes require restarting Enso.

## Policies

Inspect the policy catalog without exposing secret values or native file contents:

```bash
enso policy list
enso policy show <name>
```

Every post-setup policy creation names exactly one authority source, at least one
provider, and a default from that provider set:

```bash
enso policy create <name> --unrestricted \
  --provider <provider> [--provider <provider>...] \
  --default-provider <provider> \
  [--chat-command <command>...] [--all-chat-commands]

enso policy create <name> --policy-dir <path> \
  --provider <provider> [--provider <provider>...] \
  --default-provider <provider> \
  [--chat-command <command>...] [--all-chat-commands] \
  [--env-passthrough <name>...]
```

The named options are repeatable. `--chat-command` and `--all-chat-commands` are mutually
exclusive. Passing neither form grants no Enso chat commands; the latter explicitly
grants all of them. Environment passthrough is available only for restricted policies
and records names, never values.

For `--policy-dir`, first author or deliberately copy the complete provider-native files
into a physical, owner-protected directory outside every writable workspace, then test them
with the installed provider CLIs. The path is always explicit; there is no implicit
policy directory. Enso validates and registers that existing source but never generates,
copies, changes permissions on, rewrites, upgrades, or repairs canonical restricted
policy content. The files remain user-owned.

Use `enso config check` as the one complete validator after creation. It checks plumbing,
not the meaning or safety of native rules. Deletion, consumer rebinding, repair, and
trusted presets are intentionally absent from this policy lifecycle. Restart Enso after
policy creation or a later binding change.

## Bundled skills

Enso seeds six portable [Agent Skills](https://agentskills.io/specification) under `~/.enso/skills/`:

| Skill       | Use it for                                                    |
| ----------- | ------------------------------------------------------------- |
| `docs`      | Installation-specific reference notes and durable setup facts |
| `jobs`      | Scheduled and recurring work                                  |
| `policy`    | Explicit policy authority, native authoring, and validation   |
| `slack`     | Slack lookup, history, rich replies, and persistent surfaces   |
| `tables`    | Durable structured data in Enso's SQLite database              |
| `workspace` | Workspace layout, instructions, skills, and existing bindings  |

Fresh setup copies these skills once into the canonical global source and creates the relative provider views `~/.enso/.agents/skills -> ../skills` and `~/.enso/.claude/skills -> ../skills`. A new workspace starts with an empty canonical `<workspace>/skills/` plus matching relative provider views. Installed skills are user-owned: upgrades, startup, setup repair, and dashboard deletion do not copy new bundle versions, restore deleted skills, create tombstones, or clean up tool copies. Keep shared skills global; use the `workspace` skill for focused project guidance and workspace-only skills. A workspace is invalid if the same skill directory name exists at both global and local scope.

## Workspaces

Inspect and manage the canonical workspace catalog through the CLI:

```bash
enso workspace list
enso workspace show company
enso workspace create company --policy staff
enso workspace create automation --policy automation --concurrency 2
enso workspace repair company
```

`list` and `show` are read-only. `create` accepts a lowercase kebab-case name, derives
the only possible root as `~/.enso/workspaces/<name>`, and defaults concurrency to `1`.
It has no path option and requires an explicit policy that already exists; it never
silently grants `admin` or unrestricted execution. The sole automatic unrestricted
exception is genuinely fresh `enso setup`, which creates `admin` and binds the initial
`default` workspace to it.

Creation takes the strict current config under the cross-process config lock, validates
the complete candidate catalog, builds the workspace in a temporary sibling, and
atomically publishes the finished directory before atomically saving `config.json`.
It then performs the same installation checks used by `enso config check`. Record the
new scaffold in local history afterwards with one scoped commit
(`git -C ~/.enso add workspaces/<name>` and `git -C ~/.enso commit`). The runtime-facing
`drafts/` and `uploads/` directories are not Git content, and configuration stays
ignored, so local history is content history rather than a configuration backup.

If config persistence or post-save validation fails after publication, Enso preserves
the user-visible directory and reports the partial state instead of deleting it. A
config-write failure leaves an unused directory; a later failure may leave a configured
workspace that still needs repair.
`create` refuses any existing destination, including one left by a migration or partial
attempt. `repair` is conservative: it creates only missing structural directories and
known relative discovery links, never `AGENTS.md`, skill definitions, docs, or
`knowledge/README.md`, and reports missing content that prevents launch. Restart Enso
after a successful workspace creation or any binding change; running processes do not
hot-reload routing.

## Local content history

`~/.enso` is a local-only Git repository. After finishing one coherent change to Enso
content, record exactly the reviewed paths with an ordinary scoped commit:

```bash
git -C ~/.enso add workspaces/company/AGENTS.md workspaces/company/knowledge/onboarding.md
git -C ~/.enso commit -m "docs: update onboarding"
```

The managed `.gitignore` block keeps configuration, credentials, databases, uploads,
drafts, native policies, and runtime state out of history; never use broad staging such
as `git add -A`, and never `--force`-add an ignored path. History is local only: Enso
never creates or contacts a remote, and agents are instructed never to push, pull,
fetch, or run destructive history or worktree commands. `enso config check` reports any
tracked file that the protective ignore rules would exclude, because tracking removes a
file from `.gitignore`'s protection.

`enso doc create` and `enso job create` intentionally produce incomplete placeholders;
finish the doc or disabled job first, then record one scoped commit.

## Background Jobs

Enso can run agents on a schedule. Jobs live in `~/.enso/jobs/` and run inside `enso serve` on a 60-second tick.

```bash
enso job create --name "Daily Review" --provider claude --model sonnet --schedule "0 9 * * *" --workspace company
enso job create --name "Fast Triage" --provider codex --model luna --schedule "*/30 * * * *" --workspace company
enso job create --name "Grok Review" --provider grok --model grok-4.6 --schedule "0 11 * * *" --workspace company
enso job create --name "Agy Review" --provider agy --model gemini-3.6-flash-high --schedule "0 10 * * *" --workspace company
enso job list
enso job run daily-review    # test it manually
```

Each job has a `JOB.md` with a cron schedule, provider, model, named `workspace`, and prompt. The workspace is the provider CLI's cwd, and its policy must allow the job's provider and supply that provider's native settings. `enso config check` reports a missing required field and the loader skips that file; a parsed job with an invalid workspace-policy binding creates an error run before prerun or provider execution. Neither case falls back to an implicit cwd, another workspace, or unrestricted execution. Workspace and policy configuration is transport-independent.

Schedules are validated at creation; if a hand-edited schedule later becomes invalid, the scheduler skips that job (with a log warning) instead of running it. By default a job that misses its scheduled time by more than `misfire_grace_seconds` (300) — say the machine was asleep — is skipped rather than run late; set `catch_up: true` in the frontmatter to run missed schedules on the next tick. A persistent per-job `.run.lock` file coordinates the scheduler, CLI, and dashboard so the same job does not run concurrently. Jobs sharing a workspace also use that workspace's process-local concurrency limit, but separate Enso processes do not share that workspace semaphore.

Jobs can include a prerun script that gates execution — `exit 0` to proceed, `exit 1` to skip silently, and any other exit to fail. The prerun is trusted host-side code run with the job directory as cwd; it is not constrained by the provider's native policy. Prerun timeouts, missing scripts, and exit `2+` are recorded in run history and notify through the job's configured destination. Identical alerts are suppressed for 24 hours and one recovery is sent when the prerun becomes healthy. Prerun stdout gets injected into the prompt via `{{prerun_output}}`; only an explicit, sanitized `ENSO_ERROR:` stderr summary can appear in an alert. The bundled `jobs` skill teaches your agents how to create and manage jobs themselves.

Successful jobs are silent unless their prompt explicitly sends a message. Scheduled failures and prerun recovery use the job's `notify` destination or the transport's `notify_channel`. `enso job run <name>` exercises the same prerun and provider pipeline while suppressing Enso's automatic job alerts; it cannot suppress a message explicitly sent by the provider process. Intentional no-work exits successfully with a clear message; prerun and provider failures return a nonzero CLI status.

Codex models use the short names `sol`, `terra`, and `luna` in chat commands and job files. Enso translates them to the CLI model IDs `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` when spawning Codex. Full or custom model IDs remain supported.

A job's `provider` and `model` are validated against your configured providers — jobs naming unknown values fail with a clear error instead of running. Each job's whole lifecycle — dispatch, prerun gate, spawn, completion or timeout — is logged under a `[job:<name>]` tag for easy tracing.

## Reference Docs

Standing knowledge about your setup — how a machine is wired, a deploy runbook, account
conventions — lives as Markdown under `~/.enso/docs/`, with paths capped at eight segments
including the filename. Nothing loads docs automatically; the agent consults them when a
task calls for one.

A fresh install starts with exactly three references: `enso/content_model.md` explains
where durable context belongs, `enso/layout.md` describes the managed filesystem and
local-history boundaries, and `operator.md` is an editable template for confirmed
operator context. Enso does not create empty account, browser, network, service, project,
or business notes.

```bash
enso doc list                       # path, name, and description for every doc
enso doc create stuff/homelab.md    # creates parent dirs, scaffolds frontmatter
```

Each doc carries `name` and `description` frontmatter, and identity is the path relative
to `~/.enso/docs/` rather than a slug. `enso doc list` *is* the index — it is computed
from frontmatter on every call, so it can never drift from what is on disk, which is why
there is no `INDEX.md` to maintain. The bundled `docs` skill teaches agents to check docs
before answering from memory about your setup. The three installed starters are ordinary
user-owned docs: edit or delete them as needed, and the dynamic list immediately reflects
that choice. Completed setup reruns, repair, startup, and upgrades never recreate them.
Browse, create, edit, and delete docs under **Docs** in the dashboard. See
[`docs/specs/docs.md`](docs/specs/docs.md) for the full design.

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

The service itself has no configured process working directory. Each provider subprocess receives its resolved workspace as cwd. After upgrading from a release whose launchd or systemd definition contained `WorkingDirectory`, run `enso service install` again rather than only restarting it.

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

Everything lives under `~/.enso/`. Config is at `~/.enso/config.json`; the setup wizard
writes it for you. Use `enso workspace create` and `enso workspace repair` for workspace
lifecycle changes instead of hand-editing workspace JSON or constructing discovery links,
and use `enso policy create` for every post-setup policy registration. Route, model,
Telegram binding, notification, and agent timeout settings still live in config and may
be edited directly where no focused CLI exists. Workspace
names use lowercase kebab-case and derive their only valid roots as
`~/.enso/workspaces/<name>`; entries contain `policy` and optional `concurrency`, never
`path`. External, nested, and symlinked workspace roots are rejected, as is a `.git`
entry directly at a workspace root. Every provider launch also requires `~/.enso` itself
to remain the exact, non-corrupt Git worktree root; there is no fallback
instruction-delivery mode for a partial layout. There is no top-level `working_dir`, and
`enso serve` has no `--working-dir` override. Upgrades backfill newly supported providers
without replacing custom provider paths or model lists. Set `notify_channel` to give
`enso message send` and job alerts a default destination when no interactive origin or
explicit destination exists. No transport broadcasts implicitly. Config and binding
changes require an Enso restart.

Slack's `rich_messages` and `persistent_surfaces` settings both default to `true`. Set `persistent_surfaces` to the JSON boolean `false` to keep standard Markdown and structured message blocks while disabling App Home and Canvas drafts; set `rich_messages` to `false` to restore legacy text delivery and implicitly disable surfaces too. Non-boolean values fail closed as disabled. Restart Enso after changing either setting.

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

Enso initializes `~/.enso` as a local Git repository with protective ignore rules before
any content is staged. `config.json`, secrets, databases, messages, logs, uploads,
drafts, native policies, and other runtime state stay out of history through the managed
`.gitignore` block, and `enso config check` reports any tracked file those rules would
exclude. Enso never creates or contacts a remote: this history is a local content
journal, not a configuration backup. Literal credentials in `config.json` remain
untracked, but a secret-manager reference is still preferable to plaintext at rest.

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
      "notify_channel": "123456789",
      "workspace": "default"
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
      "notify_channel": "C12345678",
      "rich_messages": true,
      "persistent_surfaces": true
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

That fragment demonstrates credential storage, Telegram's required workspace binding, and the default Slack output settings. Create policies and workspaces through their focused CLIs, then add `account_id`, `channel_defaults`, `dms`, and `channels` to that same `transports.slack` object where no routing command exists. [`docs/examples/teams-config.jsonc`](docs/examples/teams-config.jsonc) shows the resulting relationship. Removed configuration fields are rejected rather than interpreted; upgrading operators should follow the [unified-policy guide](docs/migrations/unified-workspace-policies.md) and [v1.3 workspace guide](docs/migrations/v1.3-managed-workspaces.md).

The service-account credential needed by the helper may still use the bootstrap
`~/.enso/secrets/1password.env` file. Existing literal `bot_token` and `app_token`
keys and `web.token` remain supported. If a matching `*_1password` key is present, it
takes precedence and a malformed or unavailable reference fails closed instead of
falling back to a possibly stale literal. Legacy literal values must still be strings;
malformed values are rejected rather than treated as empty credentials. `enso setup`, `enso message`, `enso slack`,
both transport daemons, and the dashboard app factory all use the same resolution path.
When the fresh or incomplete setup wizard reconfigures a transport that already uses a
reference, it validates the replacement token, sends the new value to `op_set_secret`
over stdin, and preserves the reference in config. A helper failure aborts setup instead
of adding a plaintext fallback. Slack preloads both previous referenced values before
writing either one and restores an earlier write if the second update fails. Literal-only
transport configs keep the original setup behavior.

### Environment variables

A few advanced knobs are environment variables rather than config keys: `ENSO_SESSION_TTL_DAYS` (prune idle provider sessions, compact seeds, and conversation-participation activity — never route settings; default 30), `ENSO_JOB_CONCURRENCY` (parallel scheduled jobs, default 2), `ENSO_PROCESS_TERMINATE_GRACE_SECS` (SIGTERM grace before SIGKILL, default 5), and `ENSO_JOB_FAILURE_RENOTIFY_SECS` (duplicate-alert cooldown, default 86400). `enso service install` snapshots any that are set into the service definition — re-run it after changing them.

## Development

```bash
pip install -e ".[dev,telegram,slack,web]"
ruff check src/
mypy
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
1. When ready to release: follow the release checklist under [Versioning](#versioning) — it covers every place a version bump has to land

### Versioning

`pyproject.toml` holds the version and is the only place the number is written by hand. Everything else derives from it: `__version__` reads the *installed* distribution metadata through `importlib.metadata`, and that is what `enso --version`, the `Starting Enso vX.Y.Z` startup log line, and the post-update confirmation message all report.

That indirection is the trap. Bumping `pyproject.toml` does not change what a checkout reports, because the metadata is written at install time — an editable install keeps announcing the previous version until it is reinstalled. Re-run the install to resync it:

```bash
pip install -e . --no-deps   # refresh dist-info after a version bump
```

`--no-deps` restricts the reinstall to metadata, so it cannot upgrade or reshuffle an environment that already has the transport extras resolved.

**Release checklist.** A version bump has to touch every one of these or the number drifts:

1. `pyproject.toml` — bump `version`
1. `CHANGELOG.md` — rename the `[Unreleased]` heading to `[X.Y.Z] - YYYY-MM-DD`
1. Commit as `chore(release): X.Y.Z`
1. Merge `dev` → `main`, then tag `vX.Y.Z` (annotated)
1. `pip install -e . --no-deps` on every editable checkout — dev machines, and any host running `enso serve` or `enso web` from source
1. Restart long-running services (`enso service restart`) so the new version reaches the logs

Only the installed metadata drifts. The updater's up-to-date check compares git revisions rather than version strings, and for an editable install it reads the checkout's own revision, so a stale number misreports but never causes a spurious update.

### Changelog

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/). Add entries to the `[Unreleased]` section as you merge features — don't wait until release time. Cutting a release renames that heading to the version, so `dev` carries no `[Unreleased]` section until the next change adds one back.
