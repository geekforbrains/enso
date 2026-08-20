# Configuration

Enso stores machine configuration in `~/.enso/config.json`. Use focused CLI commands
for workspace and policy lifecycle, then edit route, model, notification, timeout, and
transport options where no CLI exists. Run `enso config check` and restart Enso after a
binding or configuration change.

The complete schema and filesystem contracts live in the
[data-model specification](specs/data-model.md). A worked multi-workspace example is in
[`examples/teams-config.jsonc`](examples/teams-config.jsonc).

## Execution model

```text
route or job  →  workspace  →  policy  →  provider CLI
identity          context/cwd      authority      session and native tools
```

- A **route** is one Telegram private-chat binding, exact Slack DM, or exact Slack
  channel. Scheduled jobs bind through the same model.
- A **workspace** is a name-derived shared-content root and provider working directory.
  It has one policy and a process-local concurrency limit.
- A **policy** lists available providers, a default provider, allowed Enso chat commands,
  and exactly one authority source: unrestricted execution or a protected native-policy
  directory.
- The **provider CLI** owns the session and enforces its own settings. Enso does not
  translate policies into a cross-provider permission language.

Routes select a workspace; jobs select a provider, model, and workspace. Neither can
replace or widen the workspace's policy. A workspace is context and a working directory,
not a security boundary; authority comes from the policy, the provider's native
enforcement, and any outer OS isolation.

## Read and validate configuration

```bash
enso config check
enso route explain slack U012ABC C0ACME
enso audit tail
```

Configuration reads are strict and non-mutating. A missing file, malformed JSON,
non-object root, unsafe link, or read race is reported instead of being replaced with
defaults. Supported in-memory defaults may make new provider/web/run fields available,
but an ordinary read never persists a migration. Enso's own writers use restricted
atomic replacement under a cross-process lock.

`enso config check` is the complete configuration and launch-plumbing validator: it
checks the catalog, canonical workspaces and links, shared instructions and skills,
route/job bindings, native policy sources, and constructible launches. It does not prove
that provider-native permission rules are semantically safe. Test every restricted
policy with the installed provider CLI, including forbidden reads/writes, shell and
network access, secret access, control-file changes, and approval attempts. See
[Permissions](specs/permissions.md) for provider-specific requirements.

## Workspaces

```bash
enso workspace list
enso workspace show company
enso workspace create company --policy staff
enso workspace create automation --policy automation --concurrency 2
enso workspace repair company
```

Workspace names are lowercase kebab-case, at most 64 characters. The name determines
the only valid root: `~/.enso/workspaces/<name>`. There is no path option. Roots must be
physical, flat children of `workspaces/`; external, nested, symlinked roots and a direct
`.git` entry at a workspace root are invalid.

`create` requires an existing policy, defaults concurrency to `1`, validates the whole
candidate catalog, builds a complete scaffold in a temporary sibling, atomically
publishes it, atomically saves config, and runs the installation check. It never chooses
`admin` or unrestricted authority automatically; genuinely fresh setup's `default`
workspace is the sole exception.

After reviewing a new scaffold, record its user-owned content in Enso's local history:

```bash
git -C ~/.enso add workspaces/company
git -C ~/.enso commit -m "docs: add company workspace"
```

Do not broadly stage `~/.enso`; runtime paths and config are intentionally ignored.
If saving or post-save validation fails after publication, Enso preserves the visible
directory and reports whether it is unused or configured. `create` refuses an existing
destination. Inspect a partial root, fix its binding if needed, then use `repair`.

`repair` is conservative: it creates only missing structural directories and known
relative discovery links. It never recreates `AGENTS.md`, skills, docs, or
`knowledge/README.md`, and reports missing launch content rather than overwriting it.
There is intentionally no automatic deletion, retirement, or rebinding workflow.

## Policies

Inspect policy capabilities and consumers without exposing native file contents or
secret values:

```bash
enso policy list
enso policy show <name>
```

Every post-setup policy creation chooses exactly one authority source, one or more
providers, and a default from that set:

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

Repeatable `--chat-command` and `--all-chat-commands` are mutually exclusive. Passing
neither grants no Enso chat commands. Environment passthrough is restricted-policy only
and stores names, never values.

Unrestricted execution uses each provider's bypass mode and belongs only on trusted
administrative workspaces. For `--policy-dir`, author or deliberately copy the complete
provider-native files into an explicit physical directory outside every writable
workspace. Make the source owner-protected and test it before registration. Enso
validates and registers existing content but never creates, copies, changes permissions,
rewrites, upgrades, repairs, or certifies it. Files under [`examples/`](examples/) are
explanatory starting points, not safe presets.

See [Permissions](specs/permissions.md) for exact Claude, Codex, Grok, and Antigravity
launch contracts, environment filtering, staged homes, MCP behavior, and native-policy
testing guidance.

## Transport bindings and route settings

Telegram accepts private chats only. `transports.telegram.allowed_users` contains exact
numeric strings, and its required `workspace` supplies cwd, providers, commands,
concurrency, uploads, and policy. The old `allowed_user_ids` spelling and `"*"` wildcard
are invalid.

Slack credentials, options, exact `dms`, exact `channels`, and optional
`channel_defaults` all live under `transports.slack`. A Slack route contains a workspace,
optional audit settings, and channel-only response triggers; it has no policy override.
There is no top-level `routes`, Slack wildcard/default route, or `allowed_users`. See
[Slack](slack.md) for app setup, authorization, mentions, threads, and rich output.

Provider, model, and effort selections are durable runtime route settings stored
separately from sessions. Config supplies the provider catalog and defaults; route
commands may narrow them only within the workspace policy. A broken binding fails closed
instead of falling back to another workspace, policy, or unrestricted execution.

Set a transport's `notify_channel` to give CLI messages and job alerts a default when
there is no interactive origin or explicit `--to`. No transport broadcasts implicitly.
Set `agent.timeout` in seconds (`1800` by default, `0` to disable). Config and binding
changes are not hot-reloaded.

## Credentials and secrets

Enso accepts transport/dashboard credentials in three forms:

1. A literal value in `config.json` for a simple personal installation.
2. Environment projection from `~/.enso/secrets/*.env` for credentials consumed by
   provider CLIs or helpers.
3. An optional secret-manager reference. The included reference implementation uses
   1Password, but Enso does not require it.

`enso serve` loads every `~/.enso/secrets/*.env` file in filename order. Blank lines and
`#` comments are ignored, leading `export ` is accepted, and surrounding quotes are
stripped. A variable already present in the process environment always wins. Jobs and
their trusted prerun scripts inherit this service environment; the separate dashboard
process does not load these files. Restrict the directory and restart after changes:

```bash
mkdir -p ~/.enso/secrets
chmod 700 ~/.enso/secrets
# Add KEY=value lines to a mode-restricted .env file.
```

A 1Password reference names an item and field without copying the resolved value into
config or projecting it into the environment:

```json
{
  "transports": {
    "telegram": {
      "bot_token_1password": {
        "item": "Enso - Transport - Telegram",
        "field": "TELEGRAM_BOT_TOKEN"
      }
    },
    "slack": {
      "bot_token_1password": {
        "item": "Enso - Transport - Slack",
        "field": "SLACK_BOT_TOKEN"
      },
      "app_token_1password": {
        "item": "Enso - Transport - Slack",
        "field": "SLACK_APP_TOKEN"
      }
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

The machine-local `~/.enso/lib/1password.sh` must provide
`op_secret "<item>" "<field>"`; reconfiguration of an existing reference also needs
`op_set_secret`. Enso calls the helper rather than `op` directly. A matching reference
takes precedence over a matching literal, and malformed/unavailable references fail closed
instead of falling back. Setup validates replacement values, preserves references, and
aborts on helper failure rather than writing plaintext. Slack pair updates preload both
old values and attempt to restore the first if the second write fails.

The service-account credential that lets the helper reach 1Password may itself live in
`~/.enso/secrets/1password.env`. Keep secrets out of shell startup files: commands a
provider spawns can re-source shell configuration and regain variables filtered from the
initial provider environment.

## Advanced environment settings

| Variable | Default | Purpose |
| --- | ---: | --- |
| `ENSO_SESSION_TTL_DAYS` | `30` | Prune idle sessions, compact seeds, and conversation activity; never route settings |
| `ENSO_JOB_CONCURRENCY` | `2` | Maximum parallel scheduled jobs in one service process |
| `ENSO_PROCESS_TERMINATE_GRACE_SECS` | `5` | Grace after SIGTERM before SIGKILL |
| `ENSO_JOB_FAILURE_RENOTIFY_SECS` | `86400` | Duplicate job-alert cooldown |

`enso service install` snapshots values that are set into its service definition. Run it
again after changing them.

## Upgrading from 1.2

The 2.0 execution catalog is a manual breaking migration. Back up and stop Enso, follow
the [unified workspace-policy guide](migrations/unified-workspace-policies.md), then the
[managed-workspace guide](migrations/v2.0-managed-workspaces.md). Removed fields are
rejected rather than interpreted, and there is no `enso migrate` command. Validate every
route and representative job before restarting or removing the backup.
