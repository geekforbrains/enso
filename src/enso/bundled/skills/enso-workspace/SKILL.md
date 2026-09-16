---
name: enso-workspace
description: Inspect, create, bind, or retire an Enso workspace; write focused workspace guidance; place global or workspace-local skills; or diagnose which workspace a conversation or job runs in.
---

# Workspace

## How Enso sets it up

A workspace is a directory under `~/.enso/workspaces/<name>`: the provider's working directory and the context it starts with. It is a content root, not a security boundary. Names are lowercase kebab-case, and the name is the only valid location. `enso workspace create` makes the whole layout, and `enso workspace audit --fix` repairs it without ever deleting.

Paths below show the default home; use `ENSO_HOME` instead of `~/.enso` when it is set.

```text
~/.enso/
├── AGENTS.md                 # shared instructions for every turn and job
├── CLAUDE.md -> AGENTS.md
├── skills/                   # enso-wide skills: enso, enso-*, and yours
├── knowledge/                # shared reference across workspaces
├── .claude/skills -> ../skills
├── .agents/skills -> ../skills
└── workspaces/<name>/
    ├── AGENTS.md             # purpose, scope, terms, approval rules for this workspace
    ├── WORKSPACE.md           # optional agent triple and provider arguments
    ├── CLAUDE.md -> AGENTS.md
    ├── skills/               # skills unique to this workspace
    ├── knowledge/            # durable reference owned by this workspace
    ├── memory/               # prepared for workspace memory (forthcoming)
    ├── jobs/                 # prepared for job relocation (forthcoming)
    ├── projects/             # prepared for project relocation (forthcoming)
    ├── drafts/               # generated or editable output
    ├── uploads/              # chat attachments, one directory per turn
    ├── .claude/skills -> ../skills
    └── .agents/skills -> ../skills
```

`AGENTS.md` and `skills/` are the files you write; the `CLAUDE.md` and `.claude`/`.agents` links are generated and are how the provider CLIs find them. Never replace a link with a copy, and never add skills under the links instead of `skills/`. A `.git` inside a workspace hides the home's `AGENTS.md` and skills from the agent, so keep repositories elsewhere.

Keep `AGENTS.md` small: what the workspace is for, what ambiguous terms mean, and any rule that must be visible every turn. Point to files in `knowledge/` for detail instead of inlining it.

Load `enso-knowledge` to find, write, import, organize, or link durable notes, or change their
formatting. It owns shared versus workspace placement and the common formatting rules;
the viewer discovers these knowledge roots automatically and stays read-only.

## Commands

```bash
enso workspace list              # names, bindings, jobs, and audit state
enso workspace create <name>     # scaffold the full layout above
enso workspace audit [<name>] [--fix]   # layout, links, skills; --fix creates and repairs, never deletes
enso config set bindings.slack:C… <name>   # bind a conversation; live on its next message
enso config unset bindings.slack:C…        # unbind it
enso config check                # validates bindings against existing directories
```

## Bind conversations and jobs

Bindings live in `~/.enso/config.json`. Keys are `slack:C…` (a channel), `slack:dm:U…` or `slack:dm:W…` (a user's DM; `W…` is an Enterprise Grid org-wide user id), or `telegram:<user id>`; values are workspace names. Workspace overrides live in the optional `WORKSPACE.md`, not in `config.json`:

```json
{ "bindings": {"slack:C0BP5BQF6UF": "meteor"} }
```

For example, `workspaces/meteor/WORKSPACE.md`:

```yaml
---
agent:
  provider: codex
  model: sol
  effort: xhigh
providers:
  claude:
    args: ["--permission-mode", "dontAsk"]
---
```

An `agent` block needs all three keys. Provider `args` replace the global list, including an empty list. Omitting the file inherits installation defaults. Edit it directly, preserving other settings and any explanatory Markdown; run `enso config check` afterward. Do not put credentials, executable paths, bindings, or a workspace-name field here. The format and validation belong to [Configuration](https://github.com/geekforbrains/enso/blob/develop/docs/configuration.md#workspacemd-in-020).

A job currently names its workspace in `JOB.md`; job and project relocation remain forthcoming. Bindings and workspace settings are read fresh for each operation, with no restart. A queued turn keeps its arrival workspace; removing its binding drops it before provider startup. Use `enso slack lookup-channel` for ids; never guess one.

The workspace scaffold keeps `skills/` and its provider links ready even when empty. `WORKSPACE.md` and the future workspace `heartbeat/` root are optional and are not created by scaffolding.

## Skills

Global skills go in `~/.enso/skills/<skill>/SKILL.md`; workspace-only ones in `<workspace>/skills/<skill>/SKILL.md`. Drop the directory in and the next turn sees it; nothing needs regenerating. The directory name and the frontmatter `name` match, and `description` says what the skill does and when to use it. A name must not appear in both places: a workspace skill does not override a global one, the CLIs disagree about which copy wins. Keep long reference material in `references/` beside `SKILL.md`.

`enso` and names beginning `enso-` are reserved for the skills Enso installs. Give your own skills other names; the audit warns about a `enso-*` skill Enso did not install.

Load `enso-skills` for the authoring format and validation, or to discover and install an
official optional skill from `geekforbrains/enso-skills`. Those installs are Enso-wide and
carry a source receipt; they never override an existing path. Load `enso-browser` for
browser profiles. A workspace may name its preferred browser profile in `AGENTS.md`, but
the profile itself is private data under the home, not content to put in `skills/`.

## Retiring a workspace

Remove or repoint its bindings and jobs, repoint any projects that name it while preserving their tasks, and review all active and paused beats with `enso heartbeat list --workspace NAME` (page through results if needed). Use `enso-heartbeat` to move ongoing beats to another workspace or close them when the retirement request includes ending that work; pausing alone leaves the workspace reference in place. Keep the directory while any of that work still needs it.

Let running turns, jobs, and beats finish before moving their files. Overrides live in its `WORKSPACE.md`; there is no workspace override entry in `config.json` to remove. Confirm with `enso config check`, then archive the directory or delete it with the user's authorization, and check configuration again. These changes need no Enso restart. See [Workspaces](https://github.com/geekforbrains/enso/blob/main/docs/workspaces.md#creating-and-retiring) for the lifecycle.
