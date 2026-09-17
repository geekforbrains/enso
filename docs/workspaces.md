# Workspaces

A workspace is where an agent works. It is the provider's working directory, the context it
starts with, and the default home for files that belong to that workspace. It is not a
sandbox: an explicit task or workspace rule may name another destination.

Enso is opinionated about this. The layout below is fixed, Enso creates it, `enso
workspace audit` proves it is still intact, and `--fix` repairs it. The point is that every
path the agent is told about in `AGENTS.md` actually exists, and that skills are wired up
rather than assembled by hand.

## Layout

This layout is implemented on the 0.2.0 development branch, including workspace jobs.
Heartbeat scripts, Markdown memory, live captures, and memory harvesting also use their
owning workspace, as detailed in the [ownership layout](#ownership-in-020).

```text
~/.enso/workspaces/<name>/
├── AGENTS.md          # purpose, scope, terms, approval rules for this workspace
├── WORKSPACE.md       # optional agent triple and provider arguments
├── CLAUDE.md          # symlink -> AGENTS.md
├── skills/            # skills unique to this workspace
├── knowledge/         # durable reference material the agent should keep
├── memory/            # dated Markdown memories
├── jobs/              # scheduled and stage jobs
├── projects/          # workspace project definitions and scripts
├── heartbeat/<REF>/   # optional; a beat's gate.sh and helpers
├── drafts/            # generated or editable output
├── uploads/           # chat attachments, one directory per turn
├── .claude/skills     # symlink -> ../skills, read by Claude Code and Grok
├── .agents/skills     # symlink -> ../skills, read by Codex, Grok, Antigravity, and OpenCode
└── .claude/settings.json, .codex/config.toml, .grok/config.toml, opencode.json
                       # optional: each CLI's own policy file
```

| Directory | What belongs there |
| --- | --- |
| `knowledge/` | Durable reference owned by this workspace: notes, facts, and research worth finding again. |
| `jobs/` | Scheduled and stage jobs, identified as `<workspace>:<job>`. |
| `memory/` | Dated Markdown conversations and experiences, maintained with `enso memory`. |
| `projects/` | Project definitions and scripts, each under `<KEY>/`; see [Tasks](tasks.md#projects-and-stages). |
| `heartbeat/` | Optional root, created only when workspace gate scripts are used. |
| `WORKSPACE.md` | Optional settings; [Configuration](configuration.md#workspacemd-in-020) owns its format and reload behavior. |
| `drafts/` | Ordinary work product. Posts, reports, scratch analysis. Safe to delete. |
| `uploads/` | Chat attachments. Enso writes here; nothing else should. |
| `skills/` | Skills only this workspace needs. |
| Policy files | Each CLI's own project-level permission file, in its own format. Optional; Enso neither checks nor enforces it. See [Provider permissions](configuration.md#provider-permissions-and-installation-trust). |

Names are lowercase kebab-case (`meteor`, `blog-research`), at most 64 characters. The name
is the directory name, and there is no other valid location. The workspace and its
`workspaces/` container must be real directories; a symbolic link cannot give a workspace
a second identity. The scaffold creates `knowledge/`, `memory/`, `jobs/`, `projects/`,
`drafts/`, `uploads/`, and `skills/`. The empty skills directory keeps the provider discovery
links valid even before you add a workspace skill. It creates neither `WORKSPACE.md` nor
`heartbeat/`; adding either later requires no restart.

The home also has `~/.enso/knowledge/` (or `$ENSO_HOME/knowledge/`) for shared reference that
belongs across workspaces. The viewer discovers existing workspace knowledge roots from
the directories themselves; no extra registration is needed. Both roots support nested
folders with notes and subfolders together. Keep one owning note and link across scopes
instead of copying shared facts into every workspace. [Knowledge](knowledge.md) owns the
note format, links, searching, and import behavior; `enso-knowledge` guides agent maintenance.

`AGENTS.md` is the file you write; `CLAUDE.md` is always a symlink to it so both CLI
families read one document. Skills follow the same rule: `skills/` is the directory you
write, and the two dot-directories are symlinks to it that the provider CLIs discover. One
source of truth, generated links everywhere else. Keep `AGENTS.md` short — what the
workspace is for, what ambiguous terms mean, and any rule that must be visible on every
single turn. Detail belongs in `knowledge/`, referenced by path. See
[Customizing](customizing.md).
The template directs current reference requests to `enso-knowledge` and `enso knowledge`,
and earlier events to `enso-memory` and `enso memory`. Workspace memory is shared context,
including when several separate DM bindings select it; it is not a confidentiality boundary.

`enso init` prepares this layout for `default`, filling missing instructions and links
without changing existing files. It reports conflicting files, directories, and links
instead of replacing them. This also makes an interrupted initial scaffold safe to rerun;
see [Non-interactive setup](install.md#non-interactive-setup). The ordinary workspace audit
retains its separately documented repair behavior.

## Ownership in 0.2.0

Workspace-owned files live under their workspace; the containing directory determines
ownership. Installation settings remain in `config.json`, as described
in [Configuration](configuration.md#configuration-ownership-in-020).

```text
$ENSO_HOME/
├── config.json
├── AGENTS.md
├── enso.db
├── knowledge/                    # shared current reference material
├── skills/                       # shared skills
└── workspaces/<name>/
    ├── AGENTS.md                 # purpose and working conventions for the agent
    ├── WORKSPACE.md              # optional agent triple and provider arguments
    ├── knowledge/                # current reference owned by this workspace
    ├── memory/                   # dated conversations and experiences
    ├── jobs/<job>/
    │   ├── JOB.md
    │   ├── prerun.sh             # optional
    │   └── postrun.sh            # optional
    ├── projects/<KEY>/
    │   ├── PROJECT.md
    │   └── *.sh                  # optional project scripts
    ├── heartbeat/                # optional follow-up gate scripts and helpers
    ├── drafts/
    ├── uploads/
    └── skills/                   # scaffolded; workspace-specific skills are optional
```

This is the content layout. Instruction links, provider discovery, private configuration,
locks, logs, caches, and managed runtime support still have the documented homes in
[Concepts](concepts.md#home) and [Skills](#skills). Provider authentication remains with the
provider CLI on the machine. A provider's optional workspace policy files remain its own;
Enso's restriction mode is removed without silently changing provider arguments.

| Resource | Source of truth | Owner |
| --- | --- | --- |
| Installation settings and bindings | Home `config.json` | Installation |
| Shared guidance, knowledge, and skills | Home `AGENTS.md`, `knowledge/`, and `skills/` | Installation |
| Workspace guidance, knowledge, memory, drafts, uploads, and skills | Files in the workspace | Containing workspace |
| Workspace agent and provider arguments | `WORKSPACE.md` | Containing workspace |
| Jobs and their supporting scripts | `jobs/<job>/` in the workspace | Containing workspace |
| Project definitions and scripts | `projects/<KEY>/` in the workspace | Containing workspace |
| Heartbeat gate scripts and helpers | `heartbeat/` in the workspace | The follow-up's recorded workspace |
| Captures, sessions, tasks, runs, follow-ups, and outbox records | One home `enso.db` | Workspace recorded or unambiguously linked in the record |
| Registered user tables | The same home `enso.db` | Installation |
| Service, scheduling, updates, health, and concurrency-group locks | Host runtime and home operational state | Installation |

Every job is referenced as `<workspace>:<job>`, including commands, scheduling, history,
alerts, task-claim actors, `ENSO_JOB`, and viewer routes. For example, `team:digest` and
`personal:digest` identify separate jobs in their respective workspace directories. A job's
frontmatter does not repeat its workspace. Per-job locks live with the job; concurrency
groups remain installation-wide, with locks under `runtime/.concurrency/` in the home. Jobs in
different workspaces using the same group still serialize.

A project's key and workspace come from `projects/<KEY>/PROJECT.md`'s location, rather than
repeating them as ownership fields. Project scripts live alongside the definition and run
from that directory. External source repositories can remain at their configured paths.
Internal records still retain the ownership needed to query and recover work correctly;
moving a directory must not silently reassign recorded work. Missing or ambiguous ownership
is an error. There is no required privileged workspace type.

Fresh setup keeps the name `default` and places installation-maintenance jobs there:
`workspaces/default/jobs/enso-audit/JOB.md` and
`workspaces/default/jobs/enso-update/JOB.md`, referenced as `default:enso-audit` and
`default:enso-update`. `default` is an ordinary workspace, not a privileged role or an
implicit fallback for missing CLI context. Each workspace also owns its own
[memory harvesting job](jobs.md#workspace-memory-job-in-020), including `default:enso-memory`
and `team:enso-memory`; those jobs process only their containing workspace.

## Context selection in 0.2.0

A chat binding selects an existing workspace for a turn, which keeps that selection
through preparation and queueing. Removing
its binding or losing its workspace drops it before execution; the
[connection access contract](connections.md#access-in-020) owns admission and notices. The
shared resolver is used by task, project, workflow, job creation, Heartbeat creation,
message sends, operational list commands, knowledge, and memory. Knowledge adds `--shared`
for explicit home-level reference; memory belongs only to a workspace.
[CLI](cli.md#workspace-context-in-020) owns command syntax. Enso sets `ENSO_WORKSPACE`
for its chat agents, jobs, and Heartbeat runs, and CLI calls they launch inherit it.
Workspace-scoped CLI operations default to that value; an optional
`--workspace` explicitly overrides it for that operation. This includes Heartbeat creation,
which saves the resolved workspace for later scheduling and execution.

For example, an agent running with `ENSO_WORKSPACE=team` operates on `team` by default.
Adding `--workspace personal` deliberately selects `personal` for that CLI operation. A
later command without the flag still uses `team`. The CLI does not infer context from its
current directory or fall back to `default`. Missing context, an invalid selected workspace,
or ambiguous ownership is an error; an invalid explicit selection never falls back to the
environment. An operation on an existing record respects that record's stored ownership;
selecting another context does not transfer it.

Environment variables are context hints, not authenticated identities. Cross-workspace
selection is a deliberate context choice, available within the installation's trust model;
it introduces no admin role or privilege boundary. Installation-wide operations keep their
installation scope. Relevant lists and searches start in the selected workspace, with
intentional broader lookup available where supported. [CLI](cli.md#workspace-context-in-020)
shows examples; [Knowledge](knowledge.md#knowledge-and-memory-in-020) and [Memory](memory.md)
own recall and maintenance guidance.

## Uploads

Chat attachments download into `<workspace>/uploads/<id>/`, one directory per turn, for
both Slack and Telegram. The agent is given the local paths in its prompt.

A Slack attachment is named by Enso: an opaque token, then a short readable tail taken from
the original name, so nothing a sender chooses decides a path. Slack file metadata is
otherwise untrusted too, so a file is fetched only from Slack's own file-download endpoint
and one that names anywhere else is skipped without a request. A Telegram attachment keeps
its own filename, sanitized.

These are retained on purpose — an agent may need to come back to a file the next day — and
Enso never deletes them. `enso workspace audit` reports the total size so you can decide
when to clear it out.

## Skills

Skills resolve across three scopes.

| Scope | Location | Who owns it |
| --- | --- | --- |
| **Workspace** | `<workspace>/skills/<name>/SKILL.md` | You, for this workspace only |
| **Enso** | `~/.enso/skills/<name>/SKILL.md` | You, bundled skills, and installed official optional skills |
| **User** | Your CLI's own user directory, see below | You, entirely outside Enso |

Enso ships its core skills into `~/.enso/skills/`; see the
[bundled skill list](customizing.md#the-bundled-skills). `enso init` and `enso setup` write a
missing bundled skill and preserve existing copies. Managed upgrades refresh only bundled
files that match their recorded baseline; edits and tracked deletions are preserved.
`enso` and the `enso-` prefix are reserved for what Enso installs, skills and jobs alike,
so the audit warns about a name in that namespace it did not put there. See
[Customizing](customizing.md#the-bundled-skills) for the owning rules.

Skill names must be unique across the workspace and enso scopes. A workspace does not
override an enso-wide skill by reusing its name; the audit reports that as an error, because
the provider CLIs disagree about which copy would win (see the table below). A name that
collides with one of your user-level skills is a warning.

### How they reach the agent

Every provider CLI discovers project skills by walking from its working directory up to the
nearest Git root, following symlinks on the way. The working directory is the workspace and
the Git root is the Enso home (`enso init` and `enso setup` prepare it), so two symlinks
in each place put both Enso scopes in reach:

```text
~/.enso/.claude/skills                    -> ../skills
~/.enso/.agents/skills                    -> ../skills
~/.enso/workspaces/<name>/.claude/skills  -> ../skills
~/.enso/workspaces/<name>/.agents/skills  -> ../skills
```

These links are static. Add or edit a skill in `<workspace>/skills/` or `~/.enso/skills/`
and the next turn can load it; no Enso restart or link regeneration is needed. The provider
CLI discovers the files when Enso launches it for a turn. `enso workspace audit --fix`
creates a missing link and repairs one that points elsewhere.

The home-level `AGENTS.md` reaches the agent the same way: Codex, Grok, Antigravity, and
OpenCode walk up to the Git root for `AGENTS.md`, and Claude Code walks up for `CLAUDE.md`.
That is why the home must be a Git root, and the audit checks that it still is.

What each CLI does, verified on a fresh home on 2026-09-02:

| CLI | Version | Reads from a workspace | On a name collision | Lists skills without a model call |
| --- | --- | --- | --- | --- |
| Claude Code | 2.1.258 | `.claude/skills/` here and in every parent up to the Git root; `~/.claude/skills/` | Your user-level copy wins. Between workspace and enso: undocumented, one is shown | No. The `skills` field of the `init` event under `--output-format stream-json` costs one model turn |
| Codex | 0.152.1 | `.agents/skills/` from the Git root down to here; `~/.agents/skills/`, `~/.codex/skills/` | Both are shown, no override | `codex debug prompt-input` |
| Grok | 1.0.13 | `.grok/`, `.agents/`, and `.claude/` skill directories at every level up to the Git root; `~/.grok/skills/`, `~/.agents/skills/`, `~/.claude/skills/` | Nearest wins: workspace, then enso, then user | `grok inspect --json` |
| Antigravity | 1.1.24 | `.agents/skills/` from the project folder up to the Git root; `~/.gemini/config/skills/` | The project walk beats your user-level copy. Between workspace and enso: undocumented, one is shown | `agy -p /skills --output-format json`, only inside a registered project |
| OpenCode | 1.18.26 | `.agents/skills/` and `.claude/skills/` here and in each parent up to the Git root; `skill/` and `skills/` below each of the configuration roots described under the table; `~/.agents/skills/`, `~/.claude/skills/` | Undocumented; one copy is shown | `opencode debug skill` |

Antigravity is the odd one out: its working directory is the folder registered for a
project in `~/.gemini/config/projects/`, not the shell's cwd. Started without a project it
discovers nothing — not the skills above, not any `AGENTS.md`. So on the first turn Enso
launches `agy` with `--new-project`, which registers the workspace, and pins every later
launch to that project id. Nothing is asked of you, and nothing in the workspace changes; see
[Configuration](configuration.md#antigravity).

OpenCode needs no registration and no extra workspace link: it reads the existing
`.agents/skills` links directly. Its native user-level skills are the `skill/` and `skills/`
directories below each of its configuration roots, and both names below both roots are
scanned by Enso, alongside the Claude-compatible user directories OpenCode also loads:

| Root | Where it is |
| --- | --- |
| Global configuration | `$XDG_CONFIG_HOME/opencode` when that variable is set and non-empty, otherwise `~/.config/opencode` |
| Additional configuration | The directory `OPENCODE_CONFIG_DIR` names, when it is set and non-empty |

`OPENCODE_CONFIG_DIR` adds a root rather than moving the global one, and it does not move
OpenCode's global `AGENTS.md`, which stays at the global root. Two roots that resolve to the
same directory are scanned once.

User-level skills are found by each CLI on its own, and so are your user-level
instructions (`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/AGENTS.md`, and
`AGENTS.md` at OpenCode's global root above, so `~/.config/opencode/AGENTS.md` by
default). They apply inside Enso too. Enso scans the user-level
skill directories so the audit and the [web viewer](web.md) can show the full skill picture,
but it does not read user-level instruction files or write either kind of file. If another
tool manages them, Enso stays out of its way. In a listing a user-level directory counts as
a skill when it has a `SKILL.md`; Enso does not validate its contents, and a name present in
more than one of those directories is shown once.

None of the CLIs require the frontmatter `name` to match the directory name; all of them
use the directory name as the skill's identity. Enso requires the match anyway, so that
what you see in a listing is the directory you edit.

## Auditing

```bash
enso workspace audit                 # the home and every workspace
enso workspace audit meteor          # the home and one workspace
enso workspace audit --fix           # repair what can be repaired, then report the rest
enso workspace audit --json          # machine-readable, for scripts and the viewer
```

The audit checks, each finding carrying the check id shown:

| Check | Id | Severity | `--fix` behaviour |
| --- | --- | --- | --- |
| Required directories exist inside an existing workspace | `directory` | error | Creates them |
| `CLAUDE.md` is a symlink to `AGENTS.md` | `link` | error | Creates or repoints the link |
| `.claude/skills` and `.agents/skills` are symlinks to `../skills` | `link` | error | Creates or repoints the link |
| The home is a Git root, and no workspace is one | `git-root` | error | Runs `git init` in the home |
| The home has `AGENTS.md`, `skills/`, `knowledge/`, and the same three links | `agents-md`, `directory`, `link` | error | Creates missing directories and the links |
| `AGENTS.md` exists | `agents-md` | error | Reports only |
| `AGENTS.md` is not still the untouched template | `agents-md` | warning | Reports only |
| Every skill directory has a `SKILL.md` whose `name` matches the directory | `skill` | error | Reports only |
| No skill name appears in both the workspace and enso scope | `skill-collision` | error | Reports only |
| No skill name collides with a user-level skill | `skill-collision` | warning | Reports only |
| No `enso-*` skill or job exists that Enso did not install | `reserved` | warning | Reports only |
| The workspace is bound, or named by a job | `orphan` | warning | Reports only |
| Unexpected entries at a workspace's top level, or under `workspaces/` | `unexpected` | warning | Reports only |
| `uploads/` size | — | — | Reported as a number |

The audit checks layout and skill discovery. Optional provider policy files are allowed
in the layout and preserved, with no policy or trust checks. An audit does not establish
provider permissions or prove that access is confined.

`--fix` only ever creates and repairs directories and discovery links. It never deletes a
file, edits `AGENTS.md` or `WORKSPACE.md`, or changes content inside workspace directories.
A real file or directory sitting where a link belongs, or a dangling symbolic link sitting where a
directory belongs, is reported and left for you to move aside. Fixes run first and the
report shows what remains, so a second `--fix` finds nothing to do.

The shared `knowledge/` directory is created when missing, by setup, managed upgrades, or
the fixing audit, without changing its contents.
A file or symbolic link occupying a knowledge root is reported and preserved, including
a dangling link. Note metadata, links, and style use the separate
[knowledge checks](knowledge.md), not the workspace layout audit.

The command exits 1 while any error remains and 0 otherwise; warnings never fail an
audit. An orphan workspace — one nothing is bound to and no job names — is a warning, not
an error. So is an unexpected top-level entry: Enso tells you it is there and leaves it
alone. Files the CLIs themselves drop inside `.claude/`, such as Claude Code's
`.cc-writes/`, are expected and not reported, and neither is `.DS_Store`.

`--fix` repairs directories inside an existing workspace; it does not recreate an entire
missing workspace. Use `enso workspace create NAME` to scaffold one.

A workspace with its own `.git` is an error that `--fix` does not touch. The CLIs stop
their walk at the nearest Git root, so a repository inside a workspace hides the home's
`AGENTS.md` and skills from the agent. Keep the repository elsewhere.

`--json` prints one object: `ok`, the `home`, and one entry per workspace.

```json
{
  "ok": false,
  "home": {"path": "/Users/you/.enso", "status": "ok", "findings": [], "fixed": []},
  "workspaces": [
    {
      "name": "meteor",
      "path": "/Users/you/.enso/workspaces/meteor",
      "status": "error",
      "bindings": ["slack:C0BP5BQF6UF"],
      "jobs": ["meteor-forum-watch"],
      "uploads_bytes": 1048576,
      "findings": [
        {"check": "directory", "severity": "error", "message": "drafts/ is missing", "fixable": true}
      ],
      "fixed": []
    }
  ]
}
```

`status` is the worst severity present (`error`, `warning`, or `ok`), `fixed` lists what
`--fix` did on this run, and `fixable` says whether `--fix` would repair a finding. The
same report backs `enso workspace list`'s audit column, the warning `enso serve` logs at
start, and the [web viewer](web.md).

`enso doctor` runs the workspace audit alongside a config check, the provider paths, and
the service status, for one strict answer to "is this machine healthy". It exits 1 on an
audit error even when `serve` can continue as described below; see [CLI](cli.md#operating).

## Creating and retiring

```bash
enso workspace create meteor
```

Scaffolds the full layout, seeds `AGENTS.md` from the template, and creates the skill
links. Then bind a conversation to it in `config.json`; the next message in that
conversation lands in the new workspace, with no restart. See
[Configuration](configuration.md).

To retire one, remove or repoint its bindings, disable its jobs, and resolve open project
tasks. Inspect active and paused beats with `enso heartbeat list --workspace NAME`, paging
through results if needed; close work only when the retirement request authorizes ending it.
Pausing a beat keeps its workspace reference. Beats cannot transfer workspaces, and moving
job or project files does not reassign existing records. There is no automated workspace
rename or transfer; any reference repair needs a deliberate, case-specific plan. Keep the
directory while its work or retained history still needs it.

Let running turns, jobs, and beats finish before moving files. Workspace overrides live
with the directory in `WORKSPACE.md`, so there is no configuration override block to remove.
Run `enso config check` before and after archiving or deleting the directory; deletion
needs the user's authorization. These changes
need no Enso restart. Configuration validation catches missing binding and project
directories, but it does not inspect heartbeat state, so the beat review is a separate step.
Enso does not delete a workspace for you.

## When a workspace is malformed

A bound workspace that fails its audit is a warning at service start, not a fatal error. It
is logged, the viewer shows it, and turns still run. A missing `drafts/` should not take
your chat bridge down. The same goes for a workspace a job names, and for the home itself:
`enso serve` logs one line per failing root, naming the errors, and points at
`enso workspace audit`.

Malformed `WORKSPACE.md` settings use the separate
[configuration validation and reload rules](configuration.md#while-the-service-runs).

The other exception is a bound workspace directory that does not exist at all: `config check`
treats that as a problem and `enso serve` refuses to start. If a bound directory disappears
while the service is running, the next read of `config.json` fails the same check: the
service logs that once and keeps the last valid configuration, a turn bound to the missing
directory is dropped before provider startup, and other conversations and jobs
continue. A missing directory named only
by a job is a job validation problem: that job cannot run, while the service and other jobs
can continue.
