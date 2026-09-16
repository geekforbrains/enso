# Configuration

Enso's configuration and normal runtime state live under one home directory, `~/.enso`
unless `ENSO_HOME` is set. See [Concepts](concepts.md#home) for the layout and the
integration-specific user-level state Enso may inspect or create.

`enso config check` validates the file and lists every problem at once; `enso serve`
refuses to start on a problem. `enso config show` prints it with tokens redacted.

## Configuration ownership in 0.2.0

**Forthcoming in 0.2.0.** `config.json` contains installation settings only: transport
connections, bindings, default agent, global providers, and service/runtime options.
Workspace and project definitions move out of this credential-bearing file.

| Setting | Owning file in 0.2.0 |
| --- | --- |
| Transports, bindings, defaults, providers, and service options | Home `config.json` |
| Optional workspace agent triple and provider-argument overrides | `workspaces/<name>/WORKSPACE.md` |
| Project definition and workflow | `workspaces/<name>/projects/<KEY>/PROJECT.md` |
| Job definition | `workspaces/<name>/jobs/<job>/JOB.md` |

For example, `workspaces/team/WORKSPACE.md` holds `team`'s agent override, while
`"bindings": { "slack:C0123": "team" }` stays in `config.json`. The `workspaces` and `projects`
blocks are removed from config. `WORKSPACE.md` is optional: without it the workspace uses
the installation defaults. Its settings are read fresh like bindings. Exact workspace and
project frontmatter formats will be documented before their loaders are implemented.
The [workspace layout](workspaces.md#ownership-in-020) owns paths, project scripts, qualified
`<workspace>:<job>` references, and installation-wide concurrency groups.

Bindings become the sole chat access rule, including Telegram; `allowed_users` is removed.
The [connection access contract](connections.md#access-in-020) owns the audience trusted by
a binding, the canned unbound notice, and trusted pairing. Missing bound workspaces are
errors, with no fallback. [Workspace context](workspaces.md#context-selection-in-020) owns
CLI selection through `ENSO_WORKSPACE` and optional `--workspace`.

Enso's `restricted` workspace mode and its prerequisite gates are removed. Provider
argument configuration and native permission behavior remain: a workspace override replaces
that provider's global argument list, and Enso does not silently substitute bypass flags.
Provider CLIs still read their own policy files from their working directory; Enso neither
checks nor enforces those files in 0.2.0. Transport authentication, pairing, and input
validation remain part of the [installation trust model](concepts.md#installation-trust-model).

The config schema becomes `version: 2`, with no compatibility parsing of version 1 or
removed settings. The examples and restriction details below remain **current 0.1.x
behavior** until the corresponding implementation lands; they are not a 0.2.0 template.

## Applying configuration

`enso config apply --file FILE` validates and atomically replaces the complete document;
`--file -` reads it from stdin. Input is strict UTF-8 JSON, at most 1 MiB. This is full
replacement, with no merging of omitted fields. Validation problems are reported together
and leave the current file unchanged. Successful writes use mode `0600` (owner read/write).
Transport tokens stay literal in this file; environment placeholders are not expanded.

For an update based on an earlier read, pass `--expected-hash HASH`, using the exact-byte
SHA256 from `enso config check --json` or the previous apply. Use `missing` to require that
no config exists. A stale hash or busy writer lock refuses the change. Enso's writers share
`~/.enso/.config.lock`; do not delete that file while Enso commands are running. Programs
editing config themselves should use the apply command and revision check to participate in
that protection. A symbolic-link `config.json` is preserved and refused by apply.

Apply installs only missing bundled jobs after a valid default agent is known, using that
agent for their initial configuration. It preserves any existing job directory, including
edited jobs or deleted scripts. Each new job appears as a complete directory, so an interrupted
installation is retryable. If config was saved but job installation failed, JSON reports
`applied: true`, `jobs_complete: false`, and `ok: false`; fix the filesystem problem and
repeat apply with the returned revision. Apply does not start services or send messages.
It works while the service is running: the write is atomic, and the next chat turn or
scheduler tick reads the new document. Apply refuses while a pairing attempt is active,
hosted or in the `setup` wizard, and when the pairing state cannot be read. During an
[update](install.md#upgrading) the CLI itself refuses new commands, so a write never lands
in an update's snapshot.

### Patching one value

`enso config set PATH VALUE` stores one value in the current `config.json` and
`enso config unset PATH` removes one key. `PATH` is dotted: each segment is an object key,
so a workspace, provider, binding, or project name is just a segment, and `set` creates
the objects on the way when they are missing. `VALUE` is JSON; text that is not valid JSON
is stored as a string, so `enso config set defaults.model opus` and
`enso config set workspaces.meteor.restricted true` both do what they look like. Quote a
string that would parse as JSON, such as `'"123"'`, to keep it a string, and put `--`
before a value that starts with `-`, after any options. Removing a key that is not set is
a problem. The patched document takes exactly the path apply does — the same lock,
revision check, symlink refusal, validation, atomic write, and job installation — and
reports the same fields, so a result that fails validation leaves the file unchanged. A
missing or unreadable `config.json` is reported rather than created; apply is the repair
path. Neither command prints the document or a value.

### While the service runs

`enso serve` reads `config.json` again before each chat turn, before each job scheduler
tick, and whenever a transport or chat command resolves a binding, so `bindings`,
`defaults`, `workspaces`, `providers`, `projects`, `agent`, `runs`, and `heartbeat` take
effect on the next turn or tick without a restart, however the file was written. A turn or
job run keeps the snapshot it started with; a queued message runs in the workspace it was
bound to when it arrived, and is dropped with a notice if that binding is removed before it
runs. `transports` and `logging` are read when `enso serve` starts, so a change there needs
`!restart` in chat or `enso service restart`; `web` is read when the viewer starts, so a
change there needs `enso web stop`, then `enso web start`. Apply, set, and unset report any
of the three as `restart_required: true` in JSON and as a line in text output.
`restart_required` compares those three sections of the replaced and applied documents; it
is false on a fresh home and whenever nothing was applied. A provider whose directory is new
to the service unit also needs `enso service install`; see
[The service](install.md#the-service).

A file that fails validation when it is read is logged once per revision, and chat turns
and jobs keep the last valid configuration until the file is valid again.
[Heartbeat](heartbeat.md) is stricter: it stops admitting assessments and cancels running
ones while the file is invalid.

`enso init` prepares the home before the first apply; see
[Non-interactive setup](install.md#non-interactive-setup). CLI JSON contracts are documented in
[Onboarding contracts](cli.md#onboarding-contracts).

Every object below accepts only the keys documented for it. An unrecognized member — a typo
such as `logging.max_byte`, or a key from some other tool — is a problem reported by its full
path alongside the others, never a silently ignored extra that leaves the default in force.
The names you choose are not members: binding keys, workspace names, and provider names stay
free-form within their own rules, and only the objects stored under them are closed. `version`
names the schema itself, so a file declaring anything but `1` is reported as an unsupported
version and its members are left alone rather than measured against version 1.

## `config.json`

The example uses comments to explain the fields; remove them when saving `config.json`,
which accepts strict JSON.

```jsonc
{
  "version": 1,
  "transports": {
    "slack": {
      "bot_token": "xoxb-…", "app_token": "xapp-…",
      "notify": "C0AEWRPJ9LM",              // default target for untargeted sends and job alerts
      "mention_required": false,            // channel top-level messages need @bot
      "thread_mention_required": false      // replies inside a thread need @bot
    },
    "telegram": {
      "bot_token": "…",
      "allowed_users": ["123456"],          // exact numeric ids; anyone else is ignored
      "notify": "123456"
    }
  },
  "bindings": {                             // conversation -> workspace
    "slack:dm:U0AETSSDDEF": "default",
    "slack:C0AEWRPJ9LM":    "default",
    "slack:C0BP5BQF6UF":    "meteor",
    "telegram:123456":      "default"
  },
  "defaults": { "provider": "claude", "model": "opus", "effort": "xhigh" },
  "workspaces": {                           // optional overrides only
    "meteor":  { "agent": { "provider": "codex", "model": "sol", "effort": "xhigh" } },
    "testing": { "restricted": true,        // needs the provider's policy file; see below
                 "providers": { "claude": { "args": ["--permission-mode", "dontAsk"] } } }
  },
  "providers": {
    "claude": { "path": "/Users/x/.local/bin/claude", "models": ["opus", "sonnet", "haiku"],
                "args": ["--dangerously-skip-permissions"] },
    "codex":  { "path": "codex", "models": ["astra", "sol", "terra", "luna"],
                "args": ["--dangerously-bypass-approvals-and-sandbox"] },
    "grok":   { "path": "grok", "models": ["grok-4.6", "grok-4.5"], "args": ["--always-approve"] },
    "agy":    { "path": "agy", "models": ["gemini-3.8-flash-high", "claude-sonnet-4-6"],
                "args": ["--dangerously-skip-permissions"] },
    "opencode": { "path": "opencode", "models": ["openrouter/deepseek/deepseek-v4-flash"],
                  "args": ["--auto"] }
  },
  "projects": {                             // task board projects; see Tasks
    "EN": { "name": "Enso", "workspace": "dev", "repo": "~/Projects/enso",
            "stages": ["triage", "todo", "review"], "setup": ".dev/prepare", "copy": [".env"] }
  },
  "agent":   { "timeout": 3600 },           // seconds per interactive turn
  "logging": { "level": "INFO", "max_bytes": 10485760, "backups": 5 },
  "runs":    { "keep": 500, "max_age_days": 30 },
  "heartbeat": { "enabled": true, "retention_days": 30 },
  "web":     { "host": "127.0.0.1", "port": 8787 }
}
```

## Transports

Configure one or both; they run in the same process.

**Slack** needs a bot token and an app-level token for Socket Mode. Create the app at
<https://api.slack.com/apps?new_app=1> from the JSON printed by `enso slack manifest`;
it lists exactly the scopes and events Enso uses. Invite the bot to every channel you
bind. When you apply the manifest to an existing app with more scopes, revoke its bot token
first and then reinstall — Slack never removes scopes from a live token, and the reinstall
drops the bot from its channels, so invite it again.

**Telegram** is private chats only. `allowed_users` are the numeric ids the bot answers;
everyone else is ignored silently. An empty list admits nobody; a binding is also required.

`notify` is where job failure alerts and untargeted `enso message send` calls go. It is a
conversation id (`C…`, `G…`, or `D…`; use `enso slack open-dm U…` for a person) or a
Telegram user id.

## Bindings

A binding maps a place to a workspace. Keys are:

| Key | Means |
| --- | --- |
| `slack:C…` or `slack:G…` | A channel. Every top-level message starts its own thread and conversation. |
| `slack:dm:U…` or `slack:dm:W…` | A user's DM, as one continuous conversation. `W…` ids are Enterprise Grid org-wide user ids. |
| `telegram:<user id>` | A Telegram private chat |

**Forthcoming in 0.2.0:** these bindings both grant access and select workspace context,
without Telegram's separate allowlist. [Connections](connections.md#access-in-020) owns the
access and unbound-notice rules, pairing, and queued-turn behavior. Mention/thread settings
control when Enso responds, independently of access and eligible live message capture.

In 0.1.x, Slack already uses explicit channel and DM bindings, with an unbound notice when
mentioned or messaged directly; Telegram additionally requires `allowed_users`.
[Applying configuration](#while-the-service-runs) describes live binding reads.

The named workspace directory must exist. `config check` treats a binding pointing at a
missing directory as a problem.

## Defaults, workspaces, and agents

`defaults` names the provider, model, and effort every conversation uses unless the
workspace overrides it, and all three keys are required. Each provider-backed job supplies
its own triple in `JOB.md`; changing chat defaults or a workspace's `agent` does not change
an existing job. Command and integration stages omit the triple because they do not invoke
a provider.

A workspace may replace the whole triple with an `agent` block (also all three keys), or
replace one provider's flags with `providers.<name>.args`. Providers with an ordered
reasoning ladder clamp effort down to the model's maximum, with a log line. Antigravity and
OpenCode have the different semantics described below.

Provider-argument overrides apply to chat turns, jobs, and heartbeat assessments; they
replace the global argument list rather than appending to it.

Only workspaces with overrides need an entry in `workspaces`. The directory
`~/.enso/workspaces/<name>` must exist for every binding and job that names it;
`enso workspace create NAME` scaffolds it. See [Workspaces](workspaces.md).

### Restricted workspaces

`workspaces.<name>.restricted` defaults to `false`. When true,
[`policy.check`](../src/enso/policy.py) checks prerequisites before chat provider execution,
the job provider-turn sequence, and heartbeat assessments. A refusal becomes a chat error
or an error run without starting that provider. The check uses the provider and effective
workspace arguments for that execution, not necessarily the chat defaults.

| Provider | Required workspace-relative file | Additional Enso check |
| --- | --- | --- |
| `claude` | `.claude/settings.json` | None |
| `codex` | `.codex/config.toml` | Trusted home; reject exact argument tokens `--dangerously-bypass-approvals-and-sandbox` and `--yolo` |
| `grok` | `.grok/config.toml` | Trusted home or an exact `--trust` argument |
| `opencode` | `opencode.json` | None |
| `agy` | No supported workspace policy | Always refused when restricted |

The Codex trust file is `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`), with
`[projects."<home>"]` and `trust_level = "trusted"`. Grok uses
`$GROK_HOME/trusted_folders.toml` (default `~/.grok/trusted_folders.toml`), with
`[folders."<home>"]` and `trusted = true`; `--trust` delegates recording trust to Grok.
`<home>` is the resolved absolute Enso home, which is the Git root, not the workspace.
Codex requires the exact key; Grok also accepts a trailing slash. Missing, unreadable,
or malformed trust configuration does not satisfy the check. Enso never writes this trust.

The policy-file check is `Path.is_file()` (including a symlink to a regular file).
Enso does not parse the file, validate rules, or inspect effective provider permissions.
An empty or malformed policy can pass; other CLI flags, user settings, tools, and provider
versions can change enforcement. The rejected argument list is deliberately narrow, not
an exhaustive detector of configurations that weaken a policy. Provider follow-ups and
job repairs do not turn this gate into continuous tool-call enforcement.

Job prerun/postrun scripts, heartbeat gates, command/integration stages, workflow checks,
and lifecycle scripts run outside provider policies with the service account's access.
Preruns and gates may execute before a provider is refused. A workspace audit checks only
the resolved chat provider's prerequisites; it does not validate job or beat providers or
prove a rule is enforced. [Auditing](workspaces.md#auditing) owns the report contract.

The config-write guard in [`initialization.py`](../src/enso/initialization.py) prevents
accidental changes through `config apply`, `set`, and `unset`: when `ENSO_WORKSPACE`
names a currently restricted workspace, it refuses changes to that workspace's subtree
or global provider arguments. It uses the previous valid document; missing/invalid
configuration is repaired without this guard. A caller without that environment marker
is unguarded. This is not authentication, and neither this guard nor a command deny rule
protects against direct file writes by a process with access. Enso does not make policy
files immutable or isolate credentials between workspaces.

Provider-native setup examples, verification probes, and version caveats belong in the
[public security guide](https://ensobot.ai/docs/security/). The bundled `enso-security`
skill carries the agent procedure. Maintain these alongside this technical contract when
changing policy behavior; do not describe model instructions as enforced permissions.

## Providers

`path` is the executable — `~` is expanded, and a bare name is found on `PATH`. `models` is
the list a config or job may name. `args` are appended to every invocation verbatim.

Enso passes these flags through except for the explicitly rejected Codex arguments in a
[restricted workspace](#restricted-workspaces). Pick the permission mode you want an
unattended agent to run with, knowing it will run without anyone watching.

Model ids remain canonical everywhere except their compact chat label. Slack's live run
header and `!status`, and Telegram's live run header and `/status`, share that presentation;
it does not change provider arguments, configuration, logs, or CLI output. See
[Agent](concepts.md#agent) for the exact display rule.

Chat session ids are stored per conversation and provider in `enso.db`, and a session is only
resumable in the workspace it was created in. `clear` in chat forgets them. Sessions idle
for 30 days are pruned at start. Jobs with postrun capture a separate session per run and
can resume it for bounded follow-ups; later triggers start fresh. Job session IDs live in
run history and are not chat sessions. See [Jobs](jobs.md#postrun-scripts).

Every id is checked against the contract of the provider that owns it — one opaque token,
never a path — before it is stored, resumed, or deleted, and a `clear` proves the data it
removes really is inside that provider's own session store. A stored row that no longer
matches is not resumed and not deleted from: it is dropped, and the next message starts a
fresh session.

### Session identity

Chat turns, jobs with postrun, and Heartbeat assessments use the same structured-output
rules. The requested session ID is authoritative, whether Enso just assigned it or is
resuming it. When the provider chooses the ID, its first valid announcement establishes
the session. A later announcement must match. An invalid or conflicting ID fails the turn
with a protocol error; the original ID is retained and the conflicting ID is never saved
or used for a follow-up. Enso does not retry a session conflict or silently start over.

A newly assigned ID becomes resumable only after the adapter recognizes a provider event.
An empty stream, plain diagnostic text, or unknown JSON such as `{}` cannot establish a
session. A successful process exit with no recognized events still fails the turn, with
the available diagnostic. A session established before an error remains available to
resume or clear. Jobs without postrun use plain batch execution and capture no session.

### OpenCode

OpenCode models use their full `<provider>/<model>` ids. For example, the copy-ready id for
the default OpenRouter model is `openrouter/deepseek/deepseek-v4-flash`, not
`deepseek/deepseek-v4-flash`. Enso's provider is still named `opencode`; OpenRouter is the
model's route, not another Enso provider. `providers.opencode.models` may contain any full
id OpenCode accepts. [`enso models`](cli.md#models) lists full, copy-ready ids from
models.dev's OpenRouter catalog, restricted by default to models that support tool calls.
The lookup never edits this list or any other part of `config.json`.

For OpenCode, `effort` is the exact model variant passed as `--variant`. Enso accepts
`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`, but support is model-specific
and can be a sparse set: choose one shown in that model's `EFFORTS` column from
`enso models`. Enso passes the value unchanged, and OpenCode may silently ignore a variant
the model does not support.

That column is derived exactly as OpenCode derives its own variants: the model's published
effort list when models.dev has one, with a null entry spelled `none`; `high` and `max` when
the model publishes only a thinking-token budget; and OpenCode's per-family default
otherwise, which for families such as Qwen, Kimi and MiniMax is no variant at all. It tracks
the OpenCode release Enso is tested against, so upgrading OpenCode can change what a model
lists.

The lookup reuses a catalog for one hour. It considers Enso's `cache/models.json` and
OpenCode's `$XDG_CACHE_HOME/opencode/models.json` (or `~/.cache/opencode/models.json` when
that variable is unset or empty), choosing the newest valid fresh copy before fetching
models.dev. Enso only reads OpenCode's cache; refreshes are written to its own home. A
failed refresh falls back to the newest valid stale copy with a warning. See the
[command reference](cli.md#models) for its exact output and JSON schema.

Setup writes `--auto` in `args`. That flag approves only permissions that OpenCode's own
configuration has not explicitly denied; it does not override a `deny` rule or make a
restrictive permission profile unrestricted. Edit the OpenCode permission rules, or use a
workspace-specific `providers.opencode.args`, when a workspace needs a different boundary.

OpenCode reads a `PWD` it inherits in preference to the directory it was started in, so
Enso names the workspace explicitly with `--dir` on every run. A service launched from
somewhere else still works in the selected workspace. Nothing is asked of you.

OpenCode assigns the session id and reports it in its event stream. Enso stores that id for
the conversation and workspace, then resumes the next interactive turn with `-s`. Each job
trigger starts a fresh session; postrun follow-ups within that run resume it with `-s`.
`clear` in chat runs `opencode session delete <id>` from the workspace and forgets Enso's copy.

### Antigravity

Antigravity (`agy`) does not work in the directory it is started in. It works in the folder
registered for a *project* in `~/.gemini/config/projects/`, and launched without one it
reads no `AGENTS.md` and finds none of the workspace's skills. So the first launch in a
workspace passes `--new-project`, which tells Antigravity to register the directory it was
started in, and every later launch looks that entry up and passes `--project <id>`. Enso
itself only reads that catalog; Antigravity writes the registration. A lookup that misses
on a fresh turn registers the workspace again, and a project is adopted only when its first
folder is the workspace, so a turn can never land in another project's directory. A
conversation stays pinned to the project it was created in, so a resumed turn carries
`--conversation <id>` as well — and a resume whose lookup misses registers nothing,
because the conversation already carries its project.

Antigravity carries the reasoning effort in the model id (`gemini-3.8-flash-high`), and the
CLI rejects `--effort` alongside such an id and for the `claude-*` models entirely. Enso
never passes it. For a model id ending in `-low`, `-medium`, or `-high`, Enso reports that
embedded level in chat and job run metadata, raising or lowering the requested `effort`
with a log line when it differs. For example, `model: gemini-3.8-flash-high` with
`effort: low` reports `high`, and a `-low` model with `effort: high` reports `low`.
The selected model id and command arguments stay unchanged. Models without an effort
suffix, such as `claude-sonnet-4-6` and `claude-opus-4-6-thinking`, keep the requested
`low`, `medium`, or `high` value for reporting; it is not sent as a flag to Antigravity.
`agy models` lists the live catalog and `models` accepts any id from it.

Prompts beginning with `/` are read by Antigravity as slash commands. `--disable-slash-commands`
in `args` stops that, at the cost of skill expansion — which is most of the point of a
workspace — so it is yours to add, not Enso's.

## Projects

`projects` declares the [task board's projects](tasks.md), keyed by the prefixes used in
references (`EN` gives `EN-001`). The section is optional. `enso project add` creates a
project; `enso workflow init` configures a preset and jobs. Both validate configuration
and use its atomic writer. Direct file edits use the same schema.

A minimal non-Git project is:

```json
"projects": {
  "EX": { "name": "Example", "workspace": "default", "stages": ["work"] }
}
```

Checks and Git are optional. A development project can use:

```json
"projects": {
  "APP": {
    "name": "Application",
    "workspace": "dev",
    "repo": "~/Projects/app",
    "base": "main",
    "worktree_root": ".worktrees",
    "max_concurrency": 3,
    "setup": "npm ci",
    "stages": [
      { "name": "plan", "worktree": false },
      {
        "name": "implement",
        "max_repairs": 2,
        "checks": [
          { "name": "lint", "command": "npm run lint", "timeout": 600 },
          { "name": "tests", "command": "npm test", "timeout": 600 }
        ]
      },
      { "name": "review", "return_to": "implement", "max_returns": 2 },
      {
        "name": "integrate", "integrate": true,
        "checks": [
          { "name": "lint", "command": "npm run lint", "timeout": 600 },
          { "name": "tests", "command": "npm test", "timeout": 600 }
        ]
      }
    ],
    "hooks": { "after:done": "./scripts/task-done" },
    "script_timeout": 600
  }
}
```

Choose commands that the project actually provides; the example is not installed as an
active project. The development preset requires explicit lint and test commands.

| Project field | Contract |
| --- | --- |
| key | 2–10 uppercase letters/digits, starting with a letter |
| `name` | Nonempty display name |
| `workspace` | Existing lowercase kebab-case Enso workspace |
| `repo` | Optional Git repository directory; `~` expands |
| `stages` | Nonempty ordered list of unique stage names or objects |
| `base` | Optional target branch; recorded for each task worktree, never silently retargeted |
| `worktree_root` | Default `<repo>/.worktrees`; repository-relative, absolute, `~`, or a sibling path |
| `setup` | Optional bash command run while preparing a new worktree |
| `copy` | Optional relative repository paths; no absolute paths or `..`; see [copy safety](tasks.md#worktrees) |
| `max_concurrency` | Positive number of simultaneous project task executions; default 1 |
| `hooks` | Command strings keyed by `after_transition`, `after:STAGE`, or `teardown` |
| `script_timeout` | Positive seconds per setup/lifecycle script; default 600 |

A string stage is `"work"` or `"approve:human"`. An object stage accepts:

| Stage field | Contract |
| --- | --- |
| `name` | 2–24 lowercase letters/digits/hyphens, starting with a letter; never `backlog`, `blocked`, `done`, or `cancelled` |
| `human` | Boolean, default false; waits for operator action |
| `command` | Nonempty command; the stage runs without a provider |
| `integrate` | Boolean, default false; the engine owns landing and requires a repo/worktree |
| `worktree` | Boolean; false keeps work in the Enso workspace, true requires a repo; omitted follows whether the project has a repo |
| `checks` | Optional list of required check objects; empty means no executable acceptance checks |
| `max_repairs` | Nonnegative additional repair opportunities, default 2; zero disables repair |
| `return_to` | Optional earlier stage name; otherwise `return` uses the previous stage |
| `max_returns` | Nonnegative return-loop limit, default 2; zero disables returns |

Choose at most one of `human`, `command`, and `integrate`. Other stages use an agent job.
Each check requires unique nonempty `name` and `command`, accepts positive `timeout`
seconds (default 600), and optional `protect` repository-relative path patterns. Enso
executes checks with bash in the task execution directory and records bounded output and
exit status. Checks do not consume model tokens or require a report format. Existing
validation inputs are protected from candidate changes; intentional rule changes require
operator review. [Tasks](tasks.md#stage-transactions-and-checks) owns acceptance, evidence,
repair, and the trust boundary.

Unknown fields, invalid types, duplicate names, invalid return destinations, or contradictory
execution kinds are reported with their configuration paths. Stage instructions belong in
agent jobs' prompts; execution/check rules belong here. Worktree creation, stable metadata,
copy semantics and safe cleanup are defined in [Tasks](tasks.md#worktrees).

## Secrets

The service starts with almost no environment. Put `KEY=value` lines (an `export ` prefix is
fine) in `~/.enso/secrets/*.env` and `enso serve` exports them before anything starts; a
variable already in the environment wins.

Keyring passwords and service-account tokens that provider CLIs or job scripts read belong
here. These files are read by every agent Enso runs — treat the directory as one trust
boundary, not several.

## Web

`web.host` and `web.port` are defaults for `enso web start`; command-line flags win. `port`
is an integer from 1 through 65535 and `host` a non-empty string. When `config.json` is
missing or invalid the viewer still starts, on `127.0.0.1:8787` unless flags say otherwise,
because its Health page is where you read the problem. The viewer is read-only and has no
authentication, so leave `host` at `127.0.0.1` unless something else is handling access.
See [Web viewer](web.md).

## Heartbeat

`heartbeat.enabled` defaults to `true`; setting it to `false` stops beat execution and keeps
saved records. `heartbeat.retention_days` is a positive integer, defaulting to `30`, for
closed beats. Each beat keeps its own timing and saved agent. See [Heartbeat](heartbeat.md)
for its lifecycle, gate contract, and retention.

## Logging and runs

`logging.level` is one of `DEBUG`, `INFO`, `WARNING`, `ERROR`. The log rotates at
`max_bytes` and keeps `backups` files.

`runs.keep` and `runs.max_age_days` bound job run history: the newest `keep` finished rows
within `max_age_days` survive each prune. Heartbeat runs stay with their beat until its
closed records are pruned under `heartbeat.retention_days`.

## Chat commands

Slack uses `!`, Telegram `/`.

| Command | Effect |
| --- | --- |
| `stop` | Kill the running process for this conversation and drop its queue |
| `clear` | Forget the conversation's sessions and attempt to delete their local provider data; refuses while a message is running, so stop it or wait first |
| `status` | Workspace, agent in effect and where it came from, session age, what is running, queue depth |
| `help` | List these |
| `restart` | Restart the service (or re-exec `enso serve`) after replying |
