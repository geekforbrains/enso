---
name: enso
description: 'What Enso is and how it is laid out: the home directory, workspaces, skills, jobs, heartbeat, and the enso CLI. Use when asked what Enso is, where something lives, which skill or command covers a task, or where to read more.'
---

# Enso

Enso is a personal agent host: one service on this machine that gives the agent CLIs already installed here a chat front door in Slack or Telegram, shared scheduling for jobs and heartbeat, and a fixed workspace layout. You are running under it now. A chat turn, job, or beat started your process inside a workspace. Chat output goes back to the conversation; background output stays in run history unless the agent sends a message.

Choose from the user's intent: finite future actions and specific situations followed until resolved use heartbeat; standing responsibilities use jobs. Finish immediate work in the conversation or its tracked task. Users do not need to know these names before asking.

Source and docs: <https://github.com/geekforbrains/enso>, with the reference under [`docs/`](https://github.com/geekforbrains/enso/tree/main/docs). This skill is the map; the `enso-*` skills hold the detail.

## How Enso sets it up

Enso's normal runtime state lives under one directory, `~/.enso` (or `$ENSO_HOME`):

```text
~/.enso/
├── config.json          # transports, bindings, the default agent, providers
├── .bundles.json        # hashes used to preserve customized bundled files during updates
├── AGENTS.md            # instructions for every turn and job (CLAUDE.md links to it)
├── CLAUDE.md -> AGENTS.md
├── skills/              # enso-wide skills: enso, enso-*, and the user's own
├── shared/knowledge/    # shared Markdown knowledge across workspaces
├── browser/             # private browser profiles, output, state, and optional tooling
├── .claude/skills -> ../skills
├── .agents/skills -> ../skills
├── workspaces/<name>/   # one directory per workspace: the agent's cwd and context
│   ├── WORKSPACE.md     # optional agent and provider-argument overrides
│   ├── work/           # task files and retained output
│   ├── projects/<KEY>/PROJECT.md # project definition and sibling scripts
│   ├── jobs/<job>/JOB.md # workspace jobs, referenced as <workspace>:<job>
│   └── heartbeat/<REF>/ # a beat's optional gate.sh and helpers
├── enso.db             # runs, messages, sessions, tasks, beats, secrets, tables
├── enso.log            # rotating log
├── runtime/             # managed releases, current link, installation receipt, update recovery
└── cache/
    ├── models.json      # models.dev catalog for enso models
    └── slack.json       # Slack directory cache for name lookups
```

The release installer writes a stable command into its selected bin directory. `enso service install` and `enso web install` write the operating system's user service units, and Antigravity may register a workspace in its own project catalog. `enso models` may reuse `$XDG_CACHE_HOME/opencode/models.json` (or `~/.cache/opencode/models.json` when that variable is unset or empty), but it only reads that external OpenCode cache and writes refreshes to Enso's own `cache/models.json`. Enso never modifies user-level instruction or skill files.

New repository tasks use `<repo>/.worktrees/<REF>` unless their `PROJECT.md` selects another
`worktree_root`; existing tasks retain their recorded paths. Read `enso-projects` before preparing
or managing task worktrees.

The installed Enso release version is package metadata, also recorded in `runtime/install.json` for managed installs. It is separate from config and database schema versions. `runtime/current` selects the managed release; `runtime/update.json`, `runtime/operations/`, and the maintenance gate belong to the updater and must not be edited by hand.

`enso setup` creates it once. The home is a Git root that Enso never commits to: the provider CLIs walk from the working directory up to the nearest Git root for `AGENTS.md` and skills, which is how the home-level instructions and `skills/` reach every workspace. A `.git` inside a workspace would hide them. `enso workspace audit --fix` repairs the layout and never deletes.

## Skills

Three scopes:

| Scope | Location | Owner |
| --- | --- | --- |
| workspace | `<workspace>/skills/<name>/` | the user, for that workspace only |
| enso | `~/.enso/skills/<name>/` | the user, bundled skills, and installed official optional skills |
| user | the CLI's own directory, such as `~/.claude/skills/` | the user, outside Enso |

`enso` and names beginning `enso-` are reserved for what Enso installs. Setup writes missing bundled skills and preserves existing copies. Managed upgrades refresh only files that still match their recorded baseline; edits, disabled jobs, and tracked deletions survive. Historical bundles without baselines stay user-owned. Workspace instructions never refresh automatically. The audit warns about a `enso-*` skill or job Enso did not install. A name must not appear in both the workspace and enso scope.

Adding or editing a skill needs no Enso restart. The provider discovers skills through the existing directory links when the next turn starts; nothing needs regenerating. Read a changed skill again when it is needed instead of relying on an earlier copy in the conversation.

| Skill | Use it for |
| --- | --- |
| `enso-workspace` | The workspace layout, where files and skills go, bindings, the audit |
| `enso-knowledge` | Markdown notes in shared knowledge by default; links, imports, and consistent user-defined formatting |
| `enso-browser` | Persistent Chrome profiles, human login, and attaching browser tools |
| `enso-skills` | Finding and installing official optional skills; authoring manual skills and choosing scope |
| `enso-security` | Installation trust, provider permissions, credential handling, and untrusted input |
| `enso-heartbeat` | Finite future actions and temporary watches: gates, event history, action receipts, explicit completion |
| `enso-jobs` | Scheduled and stage jobs: `JOB.md`, no-work prerun gates, postrun checks and same-session follow-ups, run history |
| `enso-projects` | Projects, task boards and handoffs, workflows, checks, lifecycle scripts, and worktrees |
| `enso-slack` | Looking people and channels up, reading Slack, sending messages, files, tables, and charts |
| `enso-tables` | Durable structured data in `enso.db` |
| `enso-update` | Release checks, user-authorized self-updates, status, and interrupted-update recovery |

## The CLI

The reference docs define behaviour; `enso --help` and `enso <command> --help` define accepted syntax, so run them instead of guessing flags. From inside a turn or a job you will mostly want:

```bash
enso message send "text"             # to the conversation that asked, else the notify target
enso message attach FILE [CAPTION]
enso slack …                         # see enso-slack
enso telegram send|attach …
enso browser create|list|status|open|stop|mcp  # see enso-browser
enso table list|register|schema      # see enso-tables
enso job list|show|run|create        # see enso-jobs
enso heartbeat status|create|list|show|history|update|pause|resume|wait|complete  # see enso-heartbeat
enso runs list|show
enso secret list|add|delete|get       # encrypted credentials; list shows names only
enso secret run --secret NAME -- COMMAND  # supply credentials without reading them
enso task add|list|show|advance|return|block|resume|release|edit|note|ref|land|sweep   # see enso-projects
enso project list|add
enso workspace list|create|audit     # see enso-workspace
enso knowledge roots|list|search|show|audit|create|adopt|update|move  # see enso-knowledge
enso skill list [--available]        # installed home skills, or the official catalog
enso skill show|install <name>       # see enso-skills; only geekforbrains/enso-skills
enso config show|check|set|unset     # set PATH VALUE or unset PATH edits one key; see reload rules below
enso models [--all] [--json]         # copy-ready OpenRouter model ids for OpenCode
enso doctor                          # installation health, including knowledge audits
enso update check|apply|status|recover [--json]  # see enso-update before requesting an upgrade
enso logs [-f] [--turn ID] [--job WORKSPACE:JOB]
```

For credentials, use `enso secret list` to discover names and prefer
`enso secret run --secret NAME -- COMMAND` when only the command needs the value. Repeat
`--secret` for multiple names. Use `enso secret get NAME` only when the task requires reading
the value itself; never copy it into prompts, notes or diagnostics. Jobs declare names in
`JOB.md` with `secrets: [NAME]` and receive them automatically for the run. Operators can add
secrets in the web UI or with `enso secret add NAME` (hidden prompt or `--stdin`). Existing
names require delete/create to replace. Missing credentials need operator setup; there is
no environment-file loader. See [Secrets](https://github.com/geekforbrains/enso/blob/main/docs/configuration.md#secrets).

Use the CLI for Enso-owned operations. Jobs and skill scripts run other tools directly,
with their own runtime and dependencies; do not depend on Enso's internal Python path or
import private `enso.*` modules. Installed releases and the live development instance share
the same `enso` command.

Message sends and job, run, message, Heartbeat, task, and project lists default to
`ENSO_WORKSPACE`; use `--workspace NAME` to select another existing workspace. Missing
context is an error. These lists accept `--all-workspaces` for an installation-wide view.
For sends, the selected workspace owns the outbox message; `--to` or `--channel` chooses
its destination. A chat loads background messages only for the workspace it is using.

`enso setup`, `enso serve`, `enso service …`, and viewer lifecycle commands (`enso web install|uninstall|start|stop`) are the operator's; do not run them from a turn. `enso web status` is read-only. The viewer has an optional user service, independent of the agent; see [Viewer installation](https://github.com/geekforbrains/enso/blob/main/docs/install.md#the-viewer).

Change configuration with `enso config set PATH VALUE` or `enso config unset PATH`; use `enso config apply --file FILE` for a complete document. Edit workspace overrides in `WORKSPACE.md` and projects in workspace `projects/<KEY>/PROJECT.md`; they are not config.json blocks. Bindings, agents, providers, workspace/project files, run retention, and heartbeat settings take effect on the next turn or scheduler tick without restarting Enso. A running turn or job keeps the configuration it started with.

The exceptions are `transports` and `logging`, which need a service restart, and `web`, which needs a viewer restart. Apply, set, and unset report `restart_required` in JSON and explain the needed restart in text output. Report the specific restart indicated by the result; adding a skill or changing a model does not need one. See [Configuration](https://github.com/geekforbrains/enso/blob/main/docs/configuration.md#while-the-service-runs) for the restart commands and separate service environment requirements, such as adding a provider directory to the service's `PATH`.

Every turn and job gets `ENSO_HOME` and `ENSO_WORKSPACE`. A chat turn opens with the `[Chat origin …]` block naming the platform, sender, location, and thread, and carries the same values as `ENSO_ORIGIN_*` for the commands you run; jobs add `ENSO_JOB` and `ENSO_RUN_ID`, and a stage job adds `ENSO_TASK` (with `ENSO_TASK_DIR` for a repo project). The home-level `AGENTS.md` says how to use them.

A beat gets the same home/workspace plus `ENSO_BEAT` and `ENSO_BEAT_RUN_ID`, with no new chat origin. It starts with compact current instructions and history pointers. Native sends use its saved notification destination/thread and require a stable `--action-key`; read `enso-heartbeat` before acting or settling the run.
