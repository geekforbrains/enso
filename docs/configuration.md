# Configuration

Enso's configuration and normal runtime state live under one home directory, `~/.enso`
unless `ENSO_HOME` is set. See [Concepts](concepts.md#home) for the layout and the
integration-specific user-level state Enso may inspect or create.

`enso config check` validates installation, workspace, and project settings and lists every
problem at once; `enso serve` refuses to start on a problem. `enso config show` prints `config.json`
with tokens redacted.
The required [`default` operator workspace](workspaces.md#operator-workspace) must exist,
even when no binding selects it. Configuration writes validate this before saving.

## Configuration ownership in 0.2.0

Workspace settings live in `WORKSPACE.md`, projects in `PROJECT.md`, and jobs in `JOB.md`.
`config.json` contains installation settings only.

| Setting | Owning file in 0.2.0 |
| --- | --- |
| Transports, bindings, defaults, providers, and service options | Home `config.json` |
| Optional workspace agent triple and provider-argument overrides | `workspaces/<name>/WORKSPACE.md` |
| Project definition and workflow | `workspaces/<name>/projects/<KEY>/PROJECT.md` |
| Job definition | `workspaces/<name>/jobs/<job>/JOB.md` |

For example, `workspaces/team/WORKSPACE.md` holds `team`'s agent override, while
`"bindings": { "slack:C0123": "team" }` stays in `config.json`. The `workspaces` and `projects`
blocks are rejected in `config.json`. `WORKSPACE.md` is optional: without it the workspace uses the installation defaults. Its settings are read
fresh like bindings. The formats are [WORKSPACE.md](#workspacemd-in-020) and
[PROJECT.md](#projectmd-in-020) below.
The [workspace layout](workspaces.md#ownership-in-020) owns paths, project scripts, qualified
`<workspace>:<job>` references, and installation-wide concurrency groups.

Bindings are the sole chat access rule, including Telegram; `allowed_users` is removed.
The [connection access contract](connections.md#access-in-020) owns the audience trusted by
a binding, the canned unbound notice, and trusted pairing. Missing bound workspaces are
errors, with no fallback for incoming messages. Outbound sends validate their selected owner
and destination independently, so an unrelated binding to a missing workspace does not block
them.
[Workspace context](workspaces.md#context-selection-in-020) owns CLI selection through
`ENSO_WORKSPACE` and optional `--workspace`.

The config schema is `version: 2`, Enso's
workspace restriction mode is removed, and workspace settings load from `WORKSPACE.md`.
[Provider permissions](#provider-permissions-and-installation-trust) owns the launch and
trust contract. Version 1 is refused without parsing its fields, using
one message: "This Enso home predates 0.2.0; automatic migration is unsupported:" followed by
the [migration guide's repository URL](migration.md). Managed migrations start at 0.2.0;
changing the version number alone is not a migration. An old database or home-level
`jobs/` directory is also refused with that message.

### WORKSPACE.md in 0.2.0

The optional file contains YAML frontmatter with only these fields:

| Field | Contract |
| --- | --- |
| `agent` | Optional complete object with nonempty `provider`, `model`, and `effort` strings; replaces the installation's whole default triple. |
| `providers` | Optional map from provider name to an object containing only `args`, a list of strings; replaces that provider's global arguments. |

Omitting `agent` inherits `defaults`. Omitting a provider override inherits its global
arguments; an explicit `args: []` replaces them with an empty list. The existing provider,
model, and effort validation rules still apply. Partial triples, unknown fields, provider
executable paths, model catalogs, bindings, credentials, and `restricted` are not accepted
here. There is no workspace-name field: the directory supplies it. Markdown after the
frontmatter may explain the settings; agent working guidance belongs in `AGENTS.md`.

For example, `workspaces/team/WORKSPACE.md` can contain:

```yaml
---
agent:
  provider: claude
  model: opus
  effort: xhigh
providers:
  claude:
    args: ["--permission-mode", "dontAsk"]
---
```

This changes `team`'s chat agent and its Claude arguments. Jobs retain their own saved
agent triple, while workspace provider-argument overrides apply to chat, jobs, and
Heartbeat. A missing file or an empty frontmatter mapping supplies no overrides; malformed
settings are reported with the file path, never silently treated as an absent file. An
empty mapping is written as `---`, `{}`, `---` on three lines; an empty document or a
frontmatter block without a mapping is invalid. Duplicate keys and non-text keys are
rejected using the shared Markdown frontmatter rules. Workspace directories and this file
must be real directories/files, not symbolic links; setup and audit preserve conflicting
paths.

Edit this file directly, preserving unrelated settings and explanatory Markdown.
`enso config set` and `unset` edit only `config.json`; they do not edit `WORKSPACE.md`.
Configuration checks read settings from every discovered workspace, including unbound ones.

### PROJECT.md in 0.2.0

YAML frontmatter contains the [project fields](#projects): `name`, `repo`, `stages`, `base`,
`worktree_root`, `setup`, `copy`, `max_concurrency`, `hooks`, and `script_timeout`. `name` and `stages` are required; the
remaining fields retain their existing optionality and defaults. Stage and check objects
retain the fields, validation, and defaults in that section. There is no additional
`key`, `workspace`, or schema-version field. Unknown fields are errors.

The key comes from `projects/<KEY>/` and the workspace from its parent workspace. Keys
remain unique across the installation; a duplicate is reported with both paths rather
than selecting one. For example, `workspaces/team/projects/APP/PROJECT.md` can contain:

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

This defines project `APP` in `team`. A non-Git project needs only `name` and, for example,
`stages: [work]`. Markdown after the frontmatter may describe the project; it is not another
source of workflow settings. Setup, stage commands, checks, and lifecycle commands start
in the directory containing `PROJECT.md`. Supporting scripts live there and explicitly
enter `ENSO_TASK_DIR` when they need the task's code; see
[Tasks](tasks.md#project-files-and-scripts-in-020) for a complete script example. External
repositories remain at `repo`, and `worktree_root` and `copy` keep their repository-relative
meaning. Moving the definition does not move or retarget an existing task's worktree.

## Applying configuration

`enso config apply --file FILE` validates and atomically replaces the complete document;
`--file -` reads it from stdin. Input is strict UTF-8 JSON, at most 1 MiB. This is full
replacement, with no merging of omitted fields. Validation problems are reported together
and leave the current file unchanged. Successful writes use mode `0600` (owner read/write).
Transport tokens stay literal in this file; environment placeholders are not expanded.

For an update based on an earlier read, pass `--expected-hash HASH`, using the exact-byte
SHA256 from `enso config check --json` or the previous apply. Use `missing` to require that
no config exists. A stale hash or busy writer lock refuses the change. Enso's writers share
`~/.enso/runtime/locks/config.lock`; do not delete it while Enso commands are running. Programs
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
so a provider or binding name is just a segment, and `set` creates
the objects on the way when they are missing. `VALUE` is JSON; text that is not valid JSON
is stored as a string, so `enso config set defaults.model opus` and
`enso config set providers.claude.args '[]'` both do what they look like. Quote a
string that would parse as JSON, such as `'"123"'`, to keep it a string, and put `--`
before a value that starts with `-`, after any options. Removing a key that is not set is
a problem. The patched document takes exactly the path apply does — the same lock,
revision check, symlink refusal, validation, atomic write, and job installation — and
reports the same fields, so a result that fails validation leaves the file unchanged. A
missing or unreadable `config.json` is reported rather than created; apply is the repair
path. Neither command prints the document or a value.

### While the service runs

`enso serve` checks `config.json`, workspace settings, and project definitions before each chat turn, each job
scheduler tick, and whenever a transport or chat command resolves a binding. Files are
parsed again when they change, including creation, replacement, or removal of
`WORKSPACE.md` or `PROJECT.md`. Thus `bindings`, `defaults`, `providers`, `agent`, `runs`,
`heartbeat`, workspace overrides, and project definitions take effect on the next turn or tick without a
restart, however the file was written. A turn or
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

An invalid installation or workspace file is logged once per observed revision, and chat
turns and jobs keep the last valid combined snapshot until all settings are valid again.
Removing `WORKSPACE.md` is valid and restores inheritance on the next snapshot.
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
names the schema itself, so a file declaring anything but integer `2` is reported as an unsupported
version and its members are left alone rather than measured against version 2. Version 1
gets only the migration notice above.

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

**Telegram** is private chats only. Each human sender needs an explicit
`telegram:<user id>` binding. `allowed_users` is rejected with a diagnostic explaining how
to replace it with bindings; even an empty or null value is invalid.

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
Each provider-backed job supplies its own triple in `JOB.md`; changing chat defaults or a
workspace's `agent` does not change an existing job. Command and integration stages omit
the triple because they do not invoke a provider.

A workspace may replace the whole triple in `WORKSPACE.md` with an `agent` block
(also all three keys), or replace one provider's flags with `providers.<name>.args`. Providers with an ordered
reasoning ladder clamp effort down to the model's maximum, with a log line. Antigravity and
OpenCode have the different semantics described below.

Provider-argument overrides apply to chat turns, jobs, and heartbeat assessments; they
replace the global argument list rather than appending to it.

Only workspaces with overrides need a `WORKSPACE.md`. The directory
`~/.enso/workspaces/<name>` must exist for every binding and job that names it;
`enso workspace create NAME` scaffolds it. See [Workspaces](workspaces.md).

### Provider permissions and installation trust

One installation is one trusted environment for a person or a small team. Workspaces
organize context and ownership; they do not isolate agents, credentials, or files from
other workspaces. Teams needing separation run separate installations on separate machines
or VPSs. [Concepts](concepts.md#installation-trust-model) owns this trust model.

Enso has no workspace restriction mode. `restricted` is rejected in `WORKSPACE.md`,
including when its value is `false`; the old `workspaces` config block is also rejected.
Chat, jobs, and Heartbeat start their provider in the workspace directory with the configured arguments. An override replaces
the global list, including an explicit empty list; removing the old mode does not change
setup's provider defaults or insert bypass flags into existing argument lists.

Provider CLIs can still load their own workspace policy files, such as
`.claude/settings.json`, `.codex/config.toml`, `.grok/config.toml`, and `opencode.json`.
The provider controls loading, trust requirements, and enforcement. Enso neither requires
nor inspects these files or provider trust settings, and the workspace audit leaves them
alone. Configure and verify permissions with the provider itself.

Job prerun/postrun scripts, heartbeat gates, command/integration stages, workflow checks,
and lifecycle scripts run with the service account's access, outside provider policies.
Transport authentication, pairing, admission checks, input validation, safe file handling,
subprocess limits, and authorization for external actions remain in force.

`ENSO_WORKSPACE` selects context; it does not gate `enso config apply`, `set`, or `unset`.
These commands retain validation, conflict detection, locks, and atomic writes. A workspace
name is not an authenticated identity, and Enso does not protect configuration from direct
writes by a process with access to it.

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
read the problem. The web UI permits secret creation/deletion and has no authentication, so leave `host` at
`127.0.0.1` unless something else is handling access. See [Web viewer](web.md).

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
