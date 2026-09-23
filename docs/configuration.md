# Configuration

Enso's configuration and normal runtime state live under one home directory, `~/.enso`
unless `ENSO_HOME` is set. See [Concepts](concepts.md#home) for the layout and the
integration-specific user-level state Enso may inspect or create.

`enso config check` validates installation, workspace, and project settings and lists every
problem at once; `enso serve` refuses to start on a problem. `enso config show` prints `config.json`
with tokens redacted.
The required [`default` operator workspace](workspaces.md#operator-workspace) must exist,
even when no binding selects it. Configuration writes validate this before saving.

<a id="configuration-ownership-in-020"></a>

## Configuration ownership

| Setting | Owning file |
| --- | --- |
| Transports, bindings, defaults, providers, and service options | Home `config.json` |
| Optional workspace agent and provider arguments | `workspaces/<name>/workspace.json` |
| Project definition and workflow | `workspaces/<name>/projects/<KEY>/PROJECT.md` |
| Job definition | `workspaces/<name>/jobs/<job>/JOB.md` |

`config.json` uses schema `version: 2`. Configuration objects accept only documented keys;
unknown fields are errors. [Migration](migration.md) covers unsupported older homes and
managed upgrades. [Workspaces](workspaces.md#ownership-in-020) owns resource locations and
CLI context selection.

### workspace.json

The optional file contains a JSON object with only these fields:

| Field | Contract |
| --- | --- |
| `agent` | Optional complete object with nonempty `provider`, `model`, and `effort` strings; replaces the installation's whole default triple. |
| `providers` | Optional map from provider name to an object containing only `args`, a list of strings; replaces that provider's global arguments. |

Omitting `agent` inherits `defaults`. Omitting a provider override inherits its global
arguments; an explicit `args: []` replaces them with an empty list. Provider, model, and
effort must pass the same validation as installation defaults.
The directory supplies the workspace name. Working guidance belongs in `AGENTS.md`;
`workspace.json` contains only configuration, without comments or prose.

For example, `workspaces/team/workspace.json` can contain:

```json
{
  "agent": {"provider": "claude", "model": "opus", "effort": "xhigh"},
  "providers": {
    "claude": {"args": ["--permission-mode", "dontAsk"]}
  }
}
```

This changes `team`'s chat agent and its Claude arguments. Jobs retain their own saved
agent triple, while workspace provider-argument overrides apply to chat, jobs, and
Heartbeat. A missing file or an empty object (`{}`) supplies no overrides; malformed
settings are reported with the file path, never silently treated as an absent file. An
empty document, non-object JSON, duplicate keys, and non-finite numbers are invalid.
Workspace directories and this file must be real directories/files, not symbolic links;
setup and audit preserve conflicting paths.

Edit this file directly, preserving unrelated settings.
`enso config set` and `unset` edit only `config.json`; they do not edit `workspace.json`.
Configuration checks read settings from every discovered workspace, including unbound ones.

Existing `WORKSPACE.md` files require the [workspace settings migration](migration.md#workspace-settings-migration).
It preserves their frontmatter settings as JSON, deletes the Markdown files, and discards
their explanatory text. Normal configuration loading rejects leftover `WORKSPACE.md` files
instead of silently losing their overrides.

<a id="projectmd-in-020"></a>

### PROJECT.md

YAML frontmatter holds the [project fields](#projects); `name` and `stages` are required.
The key comes from `projects/<KEY>/` and the workspace from its parent directory. Keys are
unique across the installation; duplicates report both paths. For example,
`workspaces/team/projects/APP/PROJECT.md` can contain:

```yaml
---
name: Application
repo: ~/Projects/app
base: develop
setup: ./setup.sh
stages:
  - name: work
    checks:
      - name: tests
        command: ./test.sh
        timeout: 600
hooks:
  after:done: ./done.sh
---
```

Markdown after the frontmatter may describe the project; it does not configure workflow.
Commands start beside `PROJECT.md`; scripts explicitly enter `ENSO_TASK_DIR` to work on task
code. See [Tasks](tasks.md#project-files-and-scripts-in-020) for a script example. `repo`
names the external repository; `worktree_root` and `copy` remain repository-relative.
Moving the definition does not move or retarget existing task worktrees.

## Applying configuration

`enso config apply --file FILE` validates and atomically replaces the complete document;
`--file -` reads it from stdin. Input is strict UTF-8 JSON, at most 1 MiB. This is full
replacement, with no merging of omitted fields. Validation problems are reported together
and leave the current file unchanged. Successful writes use mode `0600` (owner read/write).
Transport tokens stay literal in this file; environment placeholders are not expanded.

Pass `--expected-hash HASH` with the exact-byte SHA256 from `enso config check --json` or
the previous apply to prevent overwriting intervening edits. Use `missing` to require an
absent config. A stale hash, busy writer lock, or symbolic-link `config.json` refuses the
write. Writers share `$ENSO_HOME/runtime/locks/config.lock`; use apply with revision checks
for scripted edits, and never delete the lock while Enso commands are running.

Apply installs missing bundled command jobs in `default`.
Existing job directories, edits, and deleted scripts are preserved. New jobs appear as
complete directories, so interrupted installation is retryable. If job installation fails
after saving config, JSON reports
`applied: true`, `jobs_complete: false`, and `ok: false`; fix the filesystem problem and
repeat apply with the returned revision.

Apply works while the service runs and does not start services or send messages. Active
pairing, unreadable pairing state, and [update maintenance](install.md#upgrading) block writes.

### Patching one value

`enso config set PATH VALUE` stores one value; `enso config unset PATH` removes one key.
Each dotted `PATH` segment is an object key. `set` creates missing parent objects, parses
`VALUE` as JSON, and treats non-JSON text as a string:

```bash
enso config set defaults.model opus
enso config set providers.claude.args '[]'
```

Quote strings that would parse as JSON (`'"123"'`), and put `--` after options before a
value starting with `-`. Unsetting an absent key fails. Both commands use apply's validation,
lock, revision check, atomic write, and job installation, and return the same fields without
printing values. They require a readable existing `config.json`; use apply to create or repair it.

### While the service runs

`enso serve` reloads changed configuration, workspace settings, and project definitions
before chat turns, scheduler ticks, and binding resolution. Creation or removal of
`workspace.json` and `PROJECT.md` is included. Turns and jobs keep their starting snapshot;
queued messages follow the [connection admission rules](connections.md#access-in-020).

Most settings take effect without a restart. Exceptions are:

| Changed setting | Required action |
| --- | --- |
| `transports`, `logging` | `enso service restart` or chat `restart` |
| `web` | `enso web stop`, then `enso web start` |
| Provider executable in a directory absent from the service PATH | `enso service install`; see [The service](install.md#the-service) |

Apply, set, and unset report changed `transports`, `logging`, or `web` as
`restart_required: true` in JSON and identify the restart in text. The flag is false on a
fresh home or when nothing was applied.

An invalid configuration, workspace, or project file is logged once per observed revision, and chat
turns and jobs keep the last valid combined snapshot until all settings are valid again.
Removing `workspace.json` is valid and restores inheritance on the next snapshot.
[Heartbeat](heartbeat.md) is stricter: it stops admitting assessments and cancels running
ones while the file is invalid.

`enso init` prepares the home before the first apply; see
[Non-interactive setup](install.md#non-interactive-setup). CLI JSON contracts are documented in
[Onboarding contracts](cli.md#onboarding-contracts).

## `config.json`

The example uses comments to explain the fields; remove them when saving `config.json`,
which accepts strict JSON.

```jsonc
{
  "version": 2,
  "transports": {
    "slack": {
      "bot_token": "xoxb-…", "app_token": "xapp-…",
      "notify": "C0AEWRPJ9LM",              // default target for untargeted sends and job alerts
      "mention_required": false,            // channel top-level messages need @bot
      "thread_mention_required": false      // replies inside a thread need @bot
    },
    "telegram": {
      "bot_token": "…",
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
  "agent":   { "timeout": 3600 },           // seconds per interactive turn
  "logging": { "level": "INFO", "max_bytes": 10485760, "backups": 5 },
  "runs":    { "keep": 500, "max_age_days": 30 },
  "heartbeat": { "enabled": true, "retention_days": 30 },
  "web":     { "host": "127.0.0.1", "port": 8787, "hosts": [],
                 "knowledge": { "recent_limit": 5, "page_size": 50 } }
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

**Telegram** supports private chats only. Each human sender needs an explicit
`telegram:<user id>` binding.

`notify` is where job failure alerts and untargeted `enso message send` calls go. It is a
conversation id (`C…`, `G…`, or `D…`; use `enso slack open-dm U…` for a person) or a
Telegram user id.

## Bindings

A binding grants access and maps a conversation to an existing workspace. Keys are:

| Key | Means |
| --- | --- |
| `slack:C…` or `slack:G…` | A channel. Every top-level message starts its own thread and conversation. |
| `slack:dm:U…` or `slack:dm:W…` | A user's DM, as one continuous conversation. `W…` ids are Enterprise Grid org-wide user ids. |
| `telegram:<user id>` | A Telegram private chat |

[Connections](connections.md#access-in-020) owns the access and unbound-notice rules,
pairing, and queued-turn behavior. Mention/thread settings control when Enso responds,
independently of access.
[Applying configuration](#while-the-service-runs) describes live binding reads.

The named workspace directory must exist. `config check` treats a binding pointing at a
missing directory as a problem. Outbound message commands omit that stale binding from their
operational snapshot because bindings neither authorize nor select their destination; the
selected send workspace must still exist. Incoming messages at the stale binding remain
unavailable as described in [Connections](connections.md#access-in-020).

## Defaults, workspaces, and agents

`defaults` names the provider, model, and effort every conversation uses unless the
workspace or a temporary chat selection overrides it, and all three keys are required.
Each agent job supplies its own triple in `JOB.md.agent`; changing chat defaults or a
workspace's `agent` does not change an existing job. Command and integration stages omit
the triple because they do not invoke a provider.

A workspace may replace the whole triple in `workspace.json` with an `agent` block
(also all three keys), or replace one provider's flags with `providers.<name>.args`. Providers with an ordered
reasoning ladder clamp effort down to the model's maximum, with a log line. Antigravity and
OpenCode have the different semantics described below.

Provider-argument overrides apply to chat turns, jobs, and heartbeat assessments; they
replace the global argument list rather than appending to it.

Only workspaces with overrides need a `workspace.json`. The directory
`~/.enso/workspaces/<name>` must exist for every binding and job that names it;
`enso workspace create NAME` scaffolds it. See [Workspaces](workspaces.md).

### Provider permissions and installation trust

Workspaces organize context and ownership within one trusted installation; they do not
isolate agents, credentials, or files. See the [trust model](concepts.md#installation-trust-model).

Chat, jobs, and Heartbeat launch providers in their workspace with the configured arguments.
Workspace arguments replace the global list, including an explicit empty list. Configure
permissions through the provider's flags or workspace policy files, such as
`.claude/settings.json`, `.codex/config.toml`, `.grok/config.toml`, or `opencode.json`.
Enso does not inspect or enforce those files or provider trust settings.

Job commands, gate/postrun scripts, heartbeat gates, workflow checks, and lifecycle scripts
run with the service account's access, outside provider policies. Transport authentication,
pairing, input validation, safe file handling, and action authorization still apply.
`ENSO_WORKSPACE` selects context; it does not restrict configuration writes or authenticate
an agent. Processes with filesystem access can write configuration directly.

The bundled `enso-security` skill guides agents through these responsibilities. The
[public security guide](https://ensobot.ai/docs/security/) is maintained separately; this
page owns the current Enso behavior.

## Providers

`path` is the executable — `~` is expanded, and a bare name is found on `PATH`. `models` is
the list a config or job may name. `args` are appended to every invocation verbatim.

Enso passes these flags through without workspace permission gates. Choose the intended
permission mode for unattended execution using the provider's own controls; see
[Provider permissions](#provider-permissions-and-installation-trust).

Model ids remain canonical everywhere except their compact chat label. Slack's live run
header and `!status`, and Telegram's live run header and `/status`, share that presentation;
it does not change provider arguments, configuration, logs, or CLI output. See
[Agent](concepts.md#agent) for the exact display rule.

Chat session ids are stored per conversation and provider in `enso.db`, and a session is only
resumable in the workspace it was created in. Changing the chat selection keeps these
sessions; `clear` forgets them. Sessions idle for 30 days are pruned at start. Jobs with
postrun capture a separate session per run and can resume it for bounded follow-ups; later
triggers start fresh. Job session IDs live in run history and are not chat sessions. See
[Jobs](jobs.md#postrun-scripts).

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

OpenCode models use full `<provider>/<model>` ids, such as
`openrouter/deepseek/deepseek-v4-flash`. The Enso provider remains `opencode`.
`providers.opencode.models` may contain any full id OpenCode accepts.
[`enso models`](cli.md#models) lists OpenRouter ids and their supported efforts from
models.dev; it never edits configuration. That command's reference owns catalog caching,
fallbacks, and output.

`effort` is passed unchanged as `--variant`. Enso accepts `none`, `minimal`, `low`, `medium`,
`high`, `xhigh`, and `max`, but model support can be sparse or absent. Choose from the model's
`EFFORTS` column; OpenCode may silently ignore unsupported variants. The catalog's derivation
tracks the OpenCode release Enso was tested against.

The catalog uses a model's published effort list (`null` becomes `none`), `high` and `max`
for a thinking-token budget, or OpenCode's family defaults otherwise. Some families have
no selectable variant.

Setup supplies `--auto`, which approves permissions not explicitly denied by OpenCode's
configuration. It does not override `deny`. Use OpenCode's permission rules and workspace
argument overrides to choose the intended behavior.

Enso always passes `--dir` to select the workspace, avoiding an inherited `PWD`. OpenCode
assigns session IDs; subsequent turns and job postrun follow-ups resume with `-s`.
Each job trigger starts fresh. Chat `clear` runs `opencode session delete <id>` from the
workspace and forgets Enso's copy.

### Antigravity

Antigravity (`agy`) works in a registered project, not the shell's current directory.
Enso's first launch passes `--new-project` to register the workspace; later launches use
`--project <id>` from `~/.gemini/config/projects/`. Enso reads that catalog; `agy` writes it.
Only a project whose first folder matches the workspace is reused. Resumed conversations
remain pinned to their original project and never register a replacement.

Antigravity embeds reasoning effort in model ids such as `gemini-3.8-flash-high`; Enso never
passes `--effort`. A `-low`, `-medium`, or `-high` suffix determines reported effort, with a
log message if it differs from configuration. Models without a suffix keep the configured
`low`, `medium`, or `high` for reporting only. `agy models` lists accepted model ids.

Antigravity interprets prompts beginning with `/` as slash commands. Adding
`--disable-slash-commands` to `args` disables this and skill expansion; Enso does not add it.

## Projects

Projects are discovered as `workspaces/<workspace>/projects/<KEY>/PROJECT.md` and keyed by
installation-unique prefixes (`EN` gives `EN-001`). `enso project add` creates a definition;
`enso workflow init` edits it and creates disabled stage jobs. Both validate before writing
atomically under the shared configuration writer lock. Direct edits use the same schema.
Neither command rewrites `config.json`; unknown fields, unreadable files, unsafe paths, and
duplicate keys are reported with their source paths by `config check` and `doctor`.
Project edits and additions are reloaded with workspace settings for the next turn or tick.

A minimal non-Git `PROJECT.md` is:

```yaml
---
name: Example
stages: [work]
---
```

See [PROJECT.md](#projectmd-in-020) for a checked repository example. The development preset
requires explicit lint and test commands. All commands start beside `PROJECT.md`; scripts
that inspect repository code must explicitly enter `ENSO_TASK_DIR`.

| Project field | Contract |
| --- | --- |
| Directory key | 2–10 uppercase letters/digits, starting with a letter; unique across workspaces |
| `name` | Nonempty display name |
| `repo` | Optional Git repository directory; `~` expands |
| `stages` | Nonempty ordered list of unique stage names or objects |
| `base` | Optional target branch; recorded for each task worktree, never silently retargeted |
| `worktree_root` | Default `<repo>/.worktrees`; repository-relative, absolute, `~`, or a sibling path |
| `setup` | Optional bash command run while preparing a new worktree |
| `copy` | Optional relative repository paths; no absolute paths or `..`; see [copy safety](tasks.md#worktrees) |
| `max_concurrency` | Positive integer limiting simultaneous project task executions; default 1 |
| `hooks` | Command strings keyed by `after_transition`, `after:STAGE`, or `teardown` |
| `script_timeout` | Positive seconds per setup/lifecycle script; default 600 |

A string stage is `"work"` or `"approve:human"`. An object stage accepts:

| Stage field | Contract |
| --- | --- |
| `name` | 2–24 lowercase letters/digits/hyphens, starting with a letter; never `backlog`, `blocked`, `done`, or `cancelled` |
| `human` | Boolean, default false; waits for operator action |
| `command` | Nonempty command; the stage runs without a provider |
| `integrate` | Boolean, default false; the engine owns landing and requires a repo/worktree |
| `worktree` | Boolean; true uses a task worktree and requires a repo, false skips it; omitted follows whether the project has a repo |
| `checks` | Optional list of required check objects; empty means no executable acceptance checks |
| `max_repairs` | Nonnegative additional repair opportunities, default 2; zero disables repair |
| `return_to` | Optional earlier stage name; otherwise `return` uses the previous stage |
| `max_returns` | Nonnegative return-loop limit, default 2; zero disables returns |

Choose at most one of `human`, `command`, and `integrate`. Other stages use an agent job.
Each check requires unique nonempty `name` and `command`, accepts positive `timeout`
seconds (default 600), and optional `protect` repository-relative path patterns. Enso
executes checks with bash beside `PROJECT.md` and records bounded output and
exit status. Checks do not consume model tokens or require a report format. Existing
validation inputs are protected from candidate changes; intentional rule changes require
operator review. [Tasks](tasks.md#stage-transactions-and-checks) owns acceptance, evidence,
repair, and the trust boundary.

Unknown fields, invalid types, duplicate names, invalid return destinations, or contradictory
execution kinds are reported with their `PROJECT.md` paths. Stage instructions belong in
agent jobs' prompts; execution/check rules belong here. Worktree creation, stable metadata,
copy semantics and safe cleanup are defined in [Tasks](tasks.md#worktrees).

## Secrets

Secrets are installation-wide named values, managed in the web **Secrets** page or through
[`enso secret`](cli.md#secrets). Names match `[A-Z][A-Z0-9_]*`; duplicate creation fails.
Values are UTF-8 text, including empty values, whitespace and multiline text, up to 64 KiB.
The CLI's `--stdin` stores exact bytes; the web form stores line breaks as LF because browsers
submit every textarea line break as CRLF.
NUL is rejected because process environments cannot contain it. Replacement uses delete/create.

Values are encrypted with authenticated encryption before reaching `enso.db` or its WAL.
Names are visible metadata. The versioned ciphertext binds each value to its name, and a
permanent encrypted verifier detects a missing or incorrect key even after all secrets
have been deleted. Encryption uses the maintained `cryptography` Fernet implementation.
The store does not insert values into prompts, logs, or web responses. A command given a
secret can still print or transmit it; command output retained in run history is ordinary
run data, separate from encrypted secret storage.

The default key is `~/.config/enso/master.key`, outside the Enso home. An optional setting
selects a different absolute path, also outside the home:

```json
"secrets": {"key_file": "/absolute/private-directory/master.key"}
```

The first secret created through either interface generates the key when it is absent.
Its directory must be owned by the Enso account with mode `0700`; the key must be a regular
file owned by that account with mode `0600`. Existing keys are never replaced. Once the
store is initialized, a missing or wrong key fails clearly; restore the original key.
The key is read automatically as needed, so service restarts and supported login/boot startup
require no vault unlock. Configuration holds only the key's path.

### Backup and restore

A backup of `enso.db` contains encrypted secret records; those values cannot be recovered
without the external master key. Back up that key separately and privately. Use a SQLite
backup operation for a running database, or stop its writers and checkpoint it before copying;
copying only the main file while writes are active can omit committed WAL data.

To restore on another machine, restore the Enso home/database and the same key separately.
Set `secrets.key_file` if its location changed, and give the new service account ownership
with the permissions above. The CLI and web UI can access the store without starting chat.
Losing the key loses access to the stored values. Enso never replaces a missing key on its
own, so an initialized store then refuses listing, creation, and deletion. The explicit way
past it is `enso secret reset`: after a warning and confirmation it permanently deletes
every saved secret and the key binding, leaving the key file untouched. The next creation
reuses a key that exists or generates one. Recreate the secrets afterwards.

### Upgrading from environment files

Enso no longer creates, reads, or imports `secrets/*.env`. Existing files remain untouched
and are no longer part of the managed home layout. Manually create the secrets you need,
add names to jobs, and update other scripts to use `enso secret run`. There is no secret
migration or automatic conversion. The database schema upgrade preserves unrelated data.

## Web

`web.host` and `web.port` are defaults for `enso web start`; command-line flags win. `port`
is an integer from 1 through 65535 and `host` a non-empty string. `web.knowledge.recent_limit`
defaults to 5 and is a non-negative integer; it limits the recently updated notes shown at the
Knowledge home. `web.knowledge.page_size` defaults to 50 and is a positive integer; it limits
each page of Knowledge listings. When `config.json` is missing or invalid the viewer still
starts, on `127.0.0.1:8787` unless flags say otherwise, because its Health page is where you
read the problem. The web UI edits instructions and secrets and has no authentication, so leave
`host` at `127.0.0.1` unless something else is handling access. See [Web viewer](web.md).

`web.hosts` lists the extra names a private tunnel or reverse proxy presents in the `Host`
header, such as `["enso.example.ts.net"]`: host names only, without a scheme, port, or path.
It defaults to empty and is read when the viewer starts. Requests for any other name are
refused; [Access](web.md#access) explains why. A missing or invalid config serves only
`localhost`, address literals, and the bind host.

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

Slack uses `!`, Telegram `/`. Slack commands follow the channel and thread mention settings:
use `@Enso !use …` when a mention is required, or `!use …` when it is not.

| Command | Effect |
| --- | --- |
| `stop` | Kill the running process for this conversation and drop its queue |
| `clear` | Reset the chat selection, forget the conversation's sessions, and attempt to delete their local provider data; refuses while a message is running, so stop it or wait first |
| `status` | Workspace, agent in effect and where it came from, session age, what is running, queue depth |
| `use` | Show the current selection and command help in Slack; open the selection picker in Telegram |
| `help` | List these |
| `restart` | Restart the service (or re-exec `enso serve`) after replying |

In Slack, `!use provider:model:effort` selects all three values. `!use model:MODEL` or
`!use effort:EFFORT` changes just that field of the current agent; `!use default` returns
to the workspace agent, or installation default if the workspace has none. For example,
`!use codex:sol:medium`, `!use model:astra`, and `!use effort:max`. A provider change
requires the complete triple: `!use provider:codex` and `!use codex:sol` are incomplete.
The model may contain colons; the first and last colons delimit the provider and effort.

Enso offers only providers and models registered in `config.json`. An unknown provider,
unconfigured model, or effort Enso would clamp or rewrite leaves the selection unchanged
and shows valid choices. A model-only change must also keep the
inherited effort unchanged. Telegram's `/use` picker has Provider, Model, Effort, and
Default buttons. It shows configured providers and models, then efforts compatible under
Enso's provider rules; the final choice uses the same validation as Slack. OpenCode's
model-specific variant support can still differ from Enso's offered efforts (see
[OpenCode](#opencode)). An outdated picker asks you to run `/use` again.

The selection stays in memory for that conversation and workspace until another selection,
`use default`, `clear`, or a service restart (including a software update that restarts it).
It has no idle expiry and is discarded if its binding moves to another workspace or a
configuration edit makes it invalid.
`use default` keeps provider sessions; `clear` removes them. Jobs and Heartbeat keep their
separately configured agents.
