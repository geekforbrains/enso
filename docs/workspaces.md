# Workspaces

A workspace is the provider's working directory and the context for its work. It owns its
instructions, jobs, projects, and task files. Workspaces share one installation and do not
isolate files or credentials; see the [trust model](concepts.md#installation-trust-model).

`enso workspace create NAME` scaffolds the layout. `enso workspace audit` checks it;
`--fix` repairs required infrastructure without replacing your content.

## Layout

Jobs and Heartbeat scripts use their owning workspace, as detailed in the [ownership layout](#ownership-in-020).

```text
~/.enso/workspaces/<name>/
├── AGENTS.md          # purpose, scope, terms, approval rules for this workspace
├── workspace.json    # optional agent triple and provider arguments
├── CLAUDE.md          # symlink -> AGENTS.md
├── skills/            # skills unique to this workspace
├── jobs/              # scheduled and stage jobs
├── projects/          # workspace project definitions and scripts
├── heartbeat/<REF>/   # optional; a beat's gate.sh and helpers
├── work/              # task files and retained output, grouped by task
├── uploads/           # chat attachments, one directory per turn
├── .claude/skills     # symlink -> ../skills, read by Claude Code and Grok
├── .agents/skills     # symlink -> ../skills, read by Codex, Grok, Antigravity, and OpenCode
└── .claude/settings.json, .codex/config.toml, .grok/config.toml, opencode.json
                       # optional: each CLI's own policy file
```

Names are lowercase kebab-case (`meteor`, `blog-research`), at most 64 characters. The home,
`workspaces/` container, and workspace must be real directories, not symbolic links.
Scaffolding creates `skills/`, `jobs/`, `projects/`, `work/`, and `uploads/`, but neither
`workspace.json` nor `heartbeat/`. Add those when needed; no restart is required.

Durable notes go in `$ENSO_HOME/shared/knowledge/`. Keep task files under `work/<task>/`
unless the task has an established repository or destination. Enso alone writes chat
attachments under `uploads/`. [Configuration](configuration.md#workspacejson) owns workspace
settings; provider policy files use the provider's format and enforcement.

The [home layout](concepts.md#home) holds shared instructions, knowledge, skills, and runtime
state. Write brief workspace guidance in `AGENTS.md`: purpose, terms, approval rules, and
references to detailed notes. `CLAUDE.md` links to that file. The home instructions and
shared `Meta/Guide.md` supply the common knowledge workflow; see [Customizing](customizing.md).

`enso init` prepares `default`, filling missing instructions and links while preserving
existing files and reporting conflicting paths. It is safe to rerun after an interrupted
scaffold; see [Non-interactive setup](install.md#non-interactive-setup).

### Consolidating knowledge and work files

New installations keep notes in `shared/knowledge/` and use workspace `work/` for other
output. Existing workspace `knowledge/` and `drafts/` directories remain supported but are
no longer scaffolded, including by `enso init`. There is no automatic content migration.
To consolidate an older installation, move notes through
`enso knowledge move REF DEST --workspace NAME --to-shared` to preserve IDs and repair
links. Before removing empty workspace knowledge roots or renaming `drafts/`, update local
instructions, jobs, scripts, and references that use their paths. Knowledge commands now
default to shared; scripts that still use workspace notes must pass `--workspace NAME`.
Keep repositories, source attachments and operating records in their existing homes.

`knowledge/`, `drafts/`, and `work/` are optional content roots. Audits validate those that
exist, but neither report missing optional roots nor recreate them with `--fix`. The viewer
shows present content roots. New workspace creation and explicit initialization seed `work/`;
they never rename, move, or remove existing content. Keep temporary and retained task files
inside their task's folder rather than adding loose files to the workspace root.

<a id="ownership-in-020"></a>

## Ownership

The containing directory owns workspace files. Moving a directory does not transfer existing
records, sessions, or worktrees; missing or ambiguous ownership is an error.

| Resource | Source of truth | Scope |
| --- | --- | --- |
| Installation settings and bindings | Home `config.json` | Installation |
| Shared guidance, knowledge, and skills | Home `AGENTS.md`, `shared/knowledge/`, `skills/` | Installation |
| Workspace guidance, settings, files, and skills | Workspace `AGENTS.md`, `workspace.json`, `work/`, `uploads/`, `skills/` | Workspace |
| Jobs and scripts | Workspace `jobs/<job>/JOB.md` and adjacent scripts | Workspace |
| Project definitions and scripts | Workspace `projects/<KEY>/PROJECT.md` and adjacent scripts | Workspace |
| Heartbeat gates and helpers | Workspace `heartbeat/<REF>/` | Beat's recorded workspace |
| Sessions, tasks, runs, beats, and outbox | Home `enso.db` | Recorded workspace |
| Registered user tables | Home `enso.db` | Installation |
| Service, updates, health, and concurrency-group locks | Home operational state | Installation |

Job references are always `<workspace>:<job>`, including in commands, history, alerts,
`ENSO_JOB`, and viewer routes. `team:digest` and `personal:digest` are separate jobs.
Per-job locks and installation-wide concurrency-group locks live in `runtime/locks/`;
jobs in different workspaces using the same group still serialize.

Project keys are unique across the installation. Scripts run beside `PROJECT.md`; external
repositories stay at their configured paths. See [Configuration](configuration.md#projects)
for definitions and [Tasks](tasks.md#project-files-and-scripts-in-020) for script execution.

### Operator workspace

`workspaces/default/` is the required operator workspace. Use it to manage the installation
across workspaces and to keep jobs with installation-wide responsibilities. Setup creates it,
binds the paired operator's Slack DM or Telegram private chat to it, and uses that chat for
notifications. Bindings and notification destinations remain freely configurable afterward;
no operator identity or special permission role is enforced. Provider permissions follow the
same rules as every other workspace.

Bundled maintenance jobs start here as `default:enso-audit` and `default:enso-update`.
Their instructions, settings, and schedules remain editable. Workspace-scoped commands still
require explicit or inherited context. [Update notifications](cli.md#updates) and
[doctor notifications](cli.md#operating) fall back to `default` without it.

Do not rename or retire `default`, even if no chat or job uses it. `config check`, `doctor`,
and the workspace audit report a missing or linked directory as an error; configuration
writes refuse it before saving. Restore a removed directory from backup, or run
`enso workspace create default` to create an empty replacement, then apply the existing
configuration to install missing bundled jobs. Creating a replacement does not restore old
content or transfer records. Audit `--fix` does not recreate the workspace.

<a id="context-selection-in-020"></a>

## Context selection

Chat bindings select a workspace. Queued turns keep their original workspace, subject to
[connection admission checks](connections.md#access-in-020).

Workspace-scoped CLI commands use `--workspace NAME`, then `ENSO_WORKSPACE`. Enso sets the
variable for chat agents, jobs, and Heartbeat runs. Commands never infer context from the
current directory. Missing or invalid context fails; an invalid explicit selection never
falls back. Operations on existing records retain their recorded ownership.

For example, with `ENSO_WORKSPACE=team`, a command selects `team` unless it passes
`--workspace personal`. The next command without the flag still uses `team`. This is context
selection within one trusted installation, not an identity or permission boundary.

These operations have different defaults:

- Knowledge defaults to shared and ignores `ENSO_WORKSPACE`; pass `--workspace NAME` for
  workspace notes. See [Knowledge](knowledge.md).
- Installation-wide updates and doctor checks can run without workspace context. Their
  notifications use `--workspace`, then `ENSO_WORKSPACE`, then `default`; an invalid
  notification owner fails.

[CLI](cli.md#workspace-context-in-020) owns command syntax and broader list/search options.

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

| Scope | Location | Owner |
| --- | --- | --- |
| Workspace | `<workspace>/skills/<name>/SKILL.md` | You, for this workspace |
| Enso | `$ENSO_HOME/skills/<name>/SKILL.md` | You and Enso-installed skills |
| User | The provider CLI's own user directories | You, outside Enso |

Names must be unique across workspace and Enso scopes; a collision is an audit error, not
an override. A collision with a user-level skill is a warning. In the first two scopes,
`SKILL.md` must have a `name` matching its directory. `enso` and `enso-` names are reserved
for Enso-installed skills and jobs.

[Customizing](customizing.md#the-bundled-skills) owns bundled skills, optional catalog skills,
and update behavior.

### How they reach the agent

Enso makes its home a Git root and links both skill scopes into the provider discovery paths:

```text
$ENSO_HOME/.claude/skills                       -> ../skills
$ENSO_HOME/.agents/skills                       -> ../skills
$ENSO_HOME/workspaces/<name>/.claude/skills      -> ../skills
$ENSO_HOME/workspaces/<name>/.agents/skills      -> ../skills
```

Providers discover these links and the home/workspace instructions when launched. Claude
reads `CLAUDE.md`; the other providers read `AGENTS.md`. A workspace with its own Git root
can hide home guidance and is an audit error. Keep repositories outside the workspace.
Edits are available on the next turn, without an Enso restart or link regeneration.
`enso workspace audit --fix` repairs missing or incorrectly targeted links.

Antigravity needs a registered project; Enso handles this on first use. OpenCode uses the
existing `.agents/skills` links without registration. See
[provider configuration](configuration.md#providers).

For audit and viewer listings, Enso scans these user-level skill directories read-only:

- `~/.claude/skills/`, `~/.agents/skills/`, `~/.codex/skills/`, `~/.grok/skills/`, and
  `~/.gemini/config/skills/`.
- OpenCode's `skill/` and `skills/` under `$XDG_CONFIG_HOME/opencode` (default
  `~/.config/opencode`), plus the same directories under `OPENCODE_CONFIG_DIR` when set.
  The additional root does not replace the global one; identical resolved roots are scanned once.

A user-level directory counts as a skill when it has `SKILL.md`; Enso does not validate its
contents and lists duplicate user-level names once. Providers load their own user-level
instructions independently. Enso neither reads those instruction files nor modifies any
user-level skills or instructions.

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
| The required `default` operator workspace is a real directory | `directory` | error | Reports only; restore it or use `enso workspace create default` |
| Required directories exist inside an existing workspace | `directory` | error | Creates them |
| `CLAUDE.md` is a symlink to `AGENTS.md` | `link` | error | Creates or repoints the link |
| `.claude/skills` and `.agents/skills` are symlinks to `../skills` | `link` | error | Creates or repoints the link |
| The home is a Git root, and no workspace is one | `git-root` | error | Runs `git init` in the home |
| The home has `AGENTS.md`, `skills/`, `shared/knowledge/`, `workspaces/`, and the same three links | `agents-md`, `directory`, `link` | error | Creates missing directories and the links |
| `AGENTS.md` exists | `agents-md` | error | Reports only |
| `AGENTS.md` is not still the untouched template | `agents-md` | warning | Reports only |
| Every skill directory has a `SKILL.md` whose `name` matches the directory | `skill` | error | Reports only |
| No skill name appears in both the workspace and enso scope | `skill-collision` | error | Reports only |
| No skill name collides with a user-level skill | `skill-collision` | warning | Reports only |
| No `enso-*` skill or job exists that Enso did not install | `reserved` | warning | Reports only |
| A workspace other than `default` is bound, or named by a job | `orphan` | warning | Reports only |
| A project command that is one `./script` beside `PROJECT.md` finds it present and executable | `script` | warning | Reports only |
| Unexpected entries at the home's or a workspace's top level, in `shared/`, or under `workspaces/` | `unexpected` | warning | Reports only |
| A dangling optional link, or a link or file in place of a real `runtime/` or `cache/` directory | `link`, `directory` | warning | Reports only |
| `config.json`, `.migrations.json`, and `runtime/` have no group or other access | `permissions` | warning | Removes group and other access; preserves owner access |
| SQLite sidecars left behind by a removed `enso.db` | `stale` | warning | Reports only |
| `uploads/` size | — | — | Reported as a number |

The audit checks layout, skill discovery, and project script references. Optional
provider policy files are allowed in the layout and preserved, with no policy or trust
checks. An audit does not establish provider permissions or prove that access is confined.

### What belongs where

The layout in [`src/enso/layout.py`](../src/enso/layout.py) is shared by setup, scaffolding,
and audit. Each present top-level entry has a category:

| Category | Meaning | Examples |
| --- | --- | --- |
| `required` | Enso's, and missing it is an error | `AGENTS.md`, `skills/`, `shared/`, `workspaces/`, `.git` |
| `managed` | Enso's, written when needed | `enso.db`, `cache/`, `runtime/`, `.bundles.json` |
| `user` | Enso may create the root; what is inside is yours | `.gitignore`, `workspace.json`, a workspace `heartbeat/` |
| `extension` | A provider or tool's own file, preserved and never read | `.codex/`, `.grok/`, `opencode.json` |
| `unexpected` | Nothing in the table claims this name | whatever you left there |

Layout classification covers top-level home/workspace entries and `shared/`, which accepts
only `knowledge/`. It does not inspect content below declared roots. `.DS_Store` is ignored;
provider-created files inside `.claude/` are allowed. Present `runtime/` and `cache/` must
be real directories. [Knowledge checks](knowledge.md) validate notes separately.

`permissions` covers only the private paths listed above. Repairs remove group/other bits,
preserve owner bits, and never follow symlinks. The [secret store](configuration.md#secrets)
validates its external key separately. `stale` identifies only `enso.db-wal` and
`enso.db-shm` without an `enso.db`; it never deletes them.

`script` checks project commands only when they are a single bare `./path`, warning if the
file is absent or not executable. Other shell expressions are checked when they execute.

`--fix` repairs required directories within existing workspaces, discovery links, the
home Git root, and private-path permissions. It creates missing `shared/knowledge/` but
never recreates a missing workspace or optional content root. It does not delete files,
edit instructions/settings, or replace a real file/directory with a link. Conflicting
paths remain for the operator to resolve. The report shows findings left after repairs.

The command exits 1 while any error remains, otherwise 0. Without readable configuration,
it skips orphan/script checks and lists only jobs that can be parsed independently.
`enso doctor` reports job parsing problems; the layout audit does not.

A root's `attention` is true for any error or an actionable warning. Orphan workspaces and
untouched instruction templates are warnings without attention; the
[nightly health audit](jobs.md#nightly-health-audit) does not alert on those alone.

`--json` prints one object: `ok`, the `home`, and one entry per workspace.

```json
{
  "ok": false,
  "attention": true,
  "home": {
    "path": "/Users/you/.enso",
    "status": "ok",
    "attention": false,
    "layout": {"AGENTS.md": "required", "runtime": "managed", ".gitignore": "user"},
    "findings": [],
    "fixed": []
  },
  "workspaces": [
    {
      "name": "meteor",
      "path": "/Users/you/.enso/workspaces/meteor",
      "status": "error",
      "attention": true,
      "bindings": ["slack:C0BP5BQF6UF"],
      "jobs": ["meteor:forum-watch"],
      "uploads_bytes": 1048576,
      "layout": {"AGENTS.md": "required", "notes.txt": "unexpected"},
      "findings": [
        {"check": "directory", "severity": "error", "message": "uploads/ is missing", "fixable": true, "attention": false}
      ],
      "fixed": []
    }
  ]
}
```

`status` is the worst severity present (`error`, `warning`, or `ok`), `fixed` lists what
`--fix` did on this run, and `fixable` says whether `--fix` would repair a finding.
`layout` maps each top-level entry that is actually there to its category, so a consumer
reads ownership from the report instead of guessing from a name. `attention` on a root is
true when it holds an error or a marked warning. The
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

The required [operator workspace](#operator-workspace), `default`, cannot be retired or renamed.
To retire another workspace, remove or repoint its bindings, disable its jobs, and resolve
open project tasks. Inspect active and paused beats with `enso heartbeat list --workspace NAME`, paging
through results if needed; close work only when the retirement request authorizes ending it.
Pausing a beat keeps its workspace reference. Beats cannot transfer workspaces, and moving
job or project files does not reassign existing records. There is no automated workspace
rename or transfer; any reference repair needs a deliberate, case-specific plan. Keep the
directory while its work or retained history still needs it.

Let running turns, jobs, and beats finish before moving files. Run `enso config check`
before and after archiving or deleting the directory; deletion needs the user's
authorization. The check validates bindings and discovered project definitions, but does
not inspect heartbeat state or transfer stored work. These changes need no Enso restart.
Enso does not delete a workspace for you.

## When a workspace is malformed

Layout audit errors, such as missing `uploads/`, are logged at service start and shown in
the viewer without stopping chat. `enso serve` names each failing root and points to
`enso workspace audit`.

Missing `default` or bound workspace directories are configuration errors and prevent
startup. Invalid settings use the
[configuration validation and reload rules](configuration.md#while-the-service-runs).
If a bound directory disappears during service operation, Enso retains its last valid
configuration but drops turns for that directory before provider startup. Other
conversations can continue.
