# Customizing

Customize Enso through `AGENTS.md`, skills, and the knowledge guide. Configuration controls
runtime behaviour; see [Wiring](#wiring). Provider CLIs may also load user-level instructions
and skills outside Enso, which Enso never changes.

## Instructions: `AGENTS.md`

| File | Purpose |
| --- | --- |
| `~/.enso/AGENTS.md` | Shared behaviour, preferences, and environment context |
| `~/.enso/workspaces/<name>/AGENTS.md` | This workspace's purpose and rules |

`CLAUDE.md` beside each is a symlink to `AGENTS.md`; keep it as a link. Enso initializes its
home as an empty Git repository so provider discovery includes home instructions. It never
commits there. [Workspaces](workspaces.md#how-they-reach-the-agent) owns provider-specific
instruction and skill discovery, including external user scopes.

Enso supplies the current platform, sender, location, and thread in each chat's
[origin block](concepts.md#chat-origin). Instructions only need platform-specific guidance
when it affects a procedure or response format.

### The home-level file

Keep guidance needed on every turn: voice, authorization, input trust, workspace context,
knowledge conventions, skill discovery, and reply delivery. Procedures belong in skills;
`enso --help` provides command discovery. Keep confirmed shared preferences here and detailed
background in knowledge, referenced by path. Never include secrets.

Setup preserves an existing file. Managed updates refresh only an untouched copy matching
its recorded bundled baseline; local edits remain yours.

### The workspace file

A new workspace starts with:

```markdown
## Workspace

<!-- What is this workspace for? One or two sentences. -->
```

The required `default` workspace receives its operator purpose and a reminder to preserve
it. Ordinary workspaces with untouched instructions receive an audit warning. Existing
workspace instructions are never refreshed.

Add only workspace-specific rules and references. Link to detailed facts or procedures
where they are maintained. Workspace filing rules can override the shared default; see
[Knowledge](knowledge.md#where-new-notes-go).

## Knowledge formatting

`$ENSO_HOME/shared/knowledge/Meta/Guide.md` owns editable organization and writing conventions.
Fresh installations receive it in the [starter collection](knowledge.md#starter-collection).
The bundled `enso-knowledge` skill owns CLI procedures; its `references/formatting.md` is the
fallback for homes without a guide.

When changing a convention, update the guide (or existing fallback) and affected checks in
`enso-knowledge/scripts/lint.py` together. Do not weaken a check just to pass a note. The
viewer never runs user scripts. Metadata, identity, safe paths, and link validation remain
application contracts.

## Skills

A skill is a directory containing a `SKILL.md` with YAML frontmatter and instructions:

```markdown
---
name: research
description: Research a topic and write a brief into shared knowledge. Use when asked to investigate a subject.
---

# Research

Steps, commands, conventions, and what a good result looks like.
```

The directory and `name` must match. Descriptions explain what the skill does and when to
load it. Follow the [Agent Skills specification](https://agentskills.io/specification): names
use 1–64 lowercase letters, digits, and hyphens, with no leading/trailing hyphen or `--`;
descriptions use 1–1024 characters. Keep instructions focused, with detail in `references/`,
helpers in `scripts/`, and reusable material in `assets/` when useful. `enso-skills` guides
authoring, validation, and extraction.

Frontmatter must be a valid YAML mapping without duplicate keys. Quote descriptions
containing `: ` to avoid an unintended mapping. Enso reads `name` and `description` and
ignores other fields. The workspace audit and doctor report malformed frontmatter with its
file, line, and column.

### Where to put one

| Location | Use |
| --- | --- |
| `<workspace>/skills/` | Specific to one workspace |
| `~/.enso/skills/` | Available throughout Enso |
| Your CLI's user directory, such as `~/.claude/skills/` | Also available outside Enso |

Names must be unique across workspace and home scopes: workspace skills cannot override
home skills. Run `enso workspace audit` after changes to check metadata and collisions.
No restart is needed; the next turn can load changed files through the existing links.
[Workspaces](workspaces.md) owns those links.

For bundled-skill changes in the source checkout, use the opt-in
[skill evaluation workflow](development.md#skill-evaluations) to compare correctness,
tokens, and tool calls through installed provider CLIs.

### The bundled skills

Enso installs these into `~/.enso/skills/`:

| Skill | Covers |
| --- | --- |
| `enso-browser` | Playwright CLI sessions, profiles, login, and browser actions; see [Browser](browser.md) |
| `enso-config` | Configuration, agents/providers, diagnostics, and restarts |
| `enso-heartbeat` | Future actions and temporary watches, gates, history, and completion |
| `enso-jobs` | Scheduled and stage jobs, gates, postrun checks, and concurrency |
| `enso-knowledge` | Markdown notes, links, imports, and user-defined formatting |
| `enso-messages` | Messages and attachments with workspace and destination context |
| `enso-projects` | Projects, tasks, and workflows |
| `enso-security` | Installation trust, permissions, credentials, and untrusted input |
| `enso-skills` | Official optional skills and manual skill authoring |
| `enso-slack` | People, channels, history, messages, tables, and charts |
| `enso-tables` | Registered structured data in `enso.db` |
| `enso-update` | Release checks, authorized updates, and recovery |
| `enso-workspace` | Workspace layout, audit, and skill scope |

`enso init` and `enso setup` seed missing home instructions and skill files. Setup and config
apply also install missing [bundled jobs](jobs.md#bundled-jobs) in the default workspace;
existing job directories are left alone, including missing scripts.

Managed upgrades use hashes in `.bundles.json`:

- Refresh a tracked file only while it matches its recorded baseline.
- Preserve edits, symlinks, untracked content, and previously tracked deletions, including
  a deleted whole bundle. Files without receipts are user-owned.
- Add new files to tracked bundles and install entirely new bundles.
- Remove unchanged retired files and their empty directories. Keep baseline receipts so a
  later release does not restore a deliberately deleted file.

Workspace instructions are never refreshed. Customized job prompts, schedules, and enabled
flags remain unchanged; agent jobs retain their agent selection. Explicit setup/config apply
may seed a missing bundle again. See [Upgrading](install.md#upgrading).

The generic `enso` skill is retired. Managed upgrades remove untouched recorded copies and
preserve customized ones. When adopting the focused skills above, update references in
customized instructions while preserving local rules. `enso --help` remains the command
entrypoint.

### Official optional skills

The [geekforbrains/enso-skills](https://github.com/geekforbrains/enso-skills) catalog is the
only source accepted by the optional installer:

```bash
enso skill list                     # installed home skills; offline
enso skill list --available         # official catalog and installation status
enso skill show enso-linkedin       # metadata, requirements, files, source commit
enso skill install enso-linkedin    # install into $ENSO_HOME/skills/
```

These commands need neither transport configuration nor a database. Use the workspace audit
or viewer for workspace and external scopes; [CLI](cli.md#skills) owns JSON results.

Each remote operation pins `main` to one commit. Installation validates and atomically
publishes only explicitly listed regular files. Arbitrary repositories, URLs, branches,
symlinks, and install hooks are unsupported. Failure leaves no partial skill. Required
bundled skills must already exist; other tools, accounts, and prerequisites need separate
setup.

Installation never overwrites an existing path. `.enso-skill.json` records source, commit,
and original hashes; retain it with the skill. It establishes provenance and permits the
reserved name, not approval of subsequent edits. Do not fabricate receipts.

Optional skills do not refresh with Enso. There are no update, force, or remove commands.
To reinstall, review and preserve local edits, then explicitly move the old copy aside.
Agents need the user's agreement to replace a colliding copy. Manual skills remain supported
under non-reserved names.

Review skills before use: they inherit provider access and existing action approvals.
Never publish credentials, browser profiles, personal configuration, or private material in
the catalog. Site-specific skills reuse `enso-browser` for profiles and login. The catalog
README owns contribution and validation instructions.

### The reserved prefix

`enso` and names beginning `enso-` are reserved for skills and jobs installed by Enso.
Choose other names for your own; the audit reports unrecognized use of the prefix.

## Wiring

| What | Where |
| --- | --- |
| Chat location → workspace | `bindings` in [`config.json`](configuration.md) |
| Workspace/job agent | `defaults`, workspace `workspace.json`, `JOB.md` |
| Provider permissions | [Provider controls](configuration.md#provider-permissions-and-installation-trust) and `providers.<name>.args` |
| Credentials | [Encrypted secrets](configuration.md#secrets), supplied by CLI or job declarations |
| Scheduled work | [`JOB.md`](jobs.md), prompt/command, and gate/postrun scripts |
| Projects and stages | `enso workflow init` and [`PROJECT.md`](configuration.md#projects); checks in stages, agent instructions in the bound job |
