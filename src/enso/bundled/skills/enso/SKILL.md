---
name: enso
description: 'What Enso is and how it is laid out: the home directory, workspaces, skills, jobs, heartbeat, memory, and the enso CLI. Use when asked what Enso is, where something lives, which skill or command covers a task, or where to read more.'
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
├── knowledge/           # shared Markdown knowledge across workspaces
├── browser/             # private browser profiles, output, state, and optional tooling
├── .claude/skills -> ../skills
├── .agents/skills -> ../skills
├── workspaces/<name>/   # one directory per workspace: the agent's cwd and context
├── jobs/<name>/JOB.md   # scheduled and stage jobs, with their prerun and postrun scripts
├── heartbeat/<REF>/    # a beat's optional gate.sh and helpers
├── worktrees/<KEY>/<REF>/  # one Git worktree per task of a repo project
├── secrets/*.env        # KEY=value files exported into the service environment
├── enso.db             # runs, messages, sessions, jobs, tasks, beats, memory, registered tables
├── enso.log            # rotating log
├── runtime/             # managed releases, current link, installation receipt, update recovery
└── cache/
    ├── models.json      # models.dev catalog for enso models
    └── slack.json       # Slack directory cache for name lookups
```

The release installer writes a stable command into its selected bin directory. `enso service install` and `enso web install` write the operating system's user service units, and Antigravity may register a workspace in its own project catalog. `enso models` may reuse `$XDG_CACHE_HOME/opencode/models.json` (or `~/.cache/opencode/models.json` when that variable is unset or empty), but it only reads that external OpenCode cache and writes refreshes to Enso's own `cache/models.json`. Enso never modifies user-level instruction or skill files.

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
| `enso-memory` | Recent conversations, decisions, reported outcomes, and unfinished work by workspace and time |
| `enso-knowledge` | Shared and workspace Markdown notes, links, imports, and consistent user-defined formatting |
| `enso-browser` | Persistent Chrome profiles, human login, and attaching browser tools |
| `enso-skills` | Finding and installing official optional skills; authoring manual skills and choosing scope |
| `enso-security` | Restricting a workspace: each CLI's policy file, the provider args it needs, the `restricted` flag, what that guarantees |
| `enso-heartbeat` | Finite future actions and temporary watches: gates, event history, action receipts, explicit completion |
| `enso-jobs` | Scheduled and stage jobs: `JOB.md`, no-work prerun gates, postrun checks and same-session follow-ups, run history |
| `enso-tasks` | The task board: the Task block a stage job opens with, the moves, evidence, worktrees, landing a branch |
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
enso table list|register|schema      # see enso-tables
enso job list|show|run|create        # see enso-jobs
enso heartbeat status|create|list|show|history|update|pause|resume|wait|complete  # see enso-heartbeat
enso runs list|show
enso task add|list|show|advance|return|block|resume|release|edit|note|ref|land|sweep   # see enso-tasks
enso project list|add
enso workspace list|create|audit     # see enso-workspace
enso memory list|search|show|status|forget  # see enso-memory
enso knowledge roots|list|search|show|audit|create|adopt|update|move  # see enso-knowledge
enso skill list [--available]        # installed home skills, or the official catalog
enso skill show|install <name>       # see enso-skills; only geekforbrains/enso-skills
enso config show|check|set|unset     # set PATH VALUE or unset PATH edits one key; see reload rules below
enso models [--all] [--json]         # copy-ready OpenRouter model ids for OpenCode
enso doctor                          # config, home, workspaces, providers, transports, services, jobs, heartbeat
enso update check|apply|status|recover [--json]  # see enso-update before requesting an upgrade
enso logs [-f] [--turn ID] [--job NAME]
```

`enso setup`, `enso serve`, `enso service …`, and viewer lifecycle commands (`enso web install|uninstall|start|stop`) are the operator's; do not run them from a turn. `enso web status` is read-only. The viewer has an optional user service, independent of the agent; see [Viewer installation](https://github.com/geekforbrains/enso/blob/main/docs/install.md#the-viewer).

Change configuration with `enso config set PATH VALUE` or `enso config unset PATH`; use `enso config apply --file FILE` for a complete document. Bindings, agents, providers, workspaces, projects, run retention, memory, and heartbeat settings take effect on the next turn or scheduler tick without restarting Enso. A running turn or job keeps the configuration it started with.

The exceptions are `transports` and `logging`, which need a service restart, and `web`, which needs a viewer restart. Apply, set, and unset report `restart_required` in JSON and explain the needed restart in text output. Report the specific restart indicated by the result; adding a skill or changing a model does not need one. See [Configuration](https://github.com/geekforbrains/enso/blob/main/docs/configuration.md#while-the-service-runs) for the restart commands and separate service environment requirements, such as adding a provider directory to the service's `PATH`.

Every turn and job gets `ENSO_HOME` and `ENSO_WORKSPACE`. A chat turn opens with the `[Chat origin …]` block naming the platform, sender, location, and thread, and carries the same values as `ENSO_ORIGIN_*` for the commands you run; jobs add `ENSO_JOB` and `ENSO_RUN_ID`, and a stage job adds `ENSO_TASK` (with `ENSO_TASK_DIR` for a repo project). The home-level `AGENTS.md` says how to use them.

A beat gets the same home/workspace plus `ENSO_BEAT` and `ENSO_BEAT_RUN_ID`, with no new chat origin. It starts with compact current instructions and history pointers. Native sends use its saved notification destination/thread and require a stable `--action-key`; read `enso-heartbeat` before acting or settling the run.
