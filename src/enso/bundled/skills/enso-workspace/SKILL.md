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
├── shared/knowledge/         # shared reference across workspaces
├── .claude/skills -> ../skills
├── .agents/skills -> ../skills
└── workspaces/<name>/
    ├── AGENTS.md             # purpose, scope, terms, approval rules for this workspace
    ├── WORKSPACE.md           # optional agent triple and provider arguments
    ├── CLAUDE.md -> AGENTS.md
    ├── skills/               # skills unique to this workspace
    ├── memory/               # dated Markdown memory, maintained with enso memory
    ├── jobs/                 # scheduled and stage jobs
    ├── projects/             # workspace project definitions
    ├── work/                 # task files and generated or editable output
    ├── uploads/              # chat attachments, one directory per turn
    ├── .claude/skills -> ../skills
    └── .agents/skills -> ../skills
```

`AGENTS.md` and `skills/` are the files you write; the `CLAUDE.md` and `.claude`/`.agents` links are generated and are how the provider CLIs find them. Never replace a link with a copy, and never add skills under the links instead of `skills/`. A `.git` inside a workspace hides the home's `AGENTS.md` and skills from the agent, so keep repositories elsewhere.

Keep `AGENTS.md` small: what the workspace is for, what ambiguous terms mean, and any rule that must be visible every turn. Point to files in `$ENSO_HOME/shared/knowledge/` for detail instead of inlining it. Keep work in its established repository or destination; otherwise group it by task under `work/` and keep the workspace root clear.

Load `enso-knowledge` to find, write, import, organize, or link durable notes, or change their
formatting. New notes go in shared knowledge by default. The skill owns the note workflow and
common formatting rules; the viewer discovers knowledge roots automatically and stays
read-only.
Knowledge is current reference; dated conversations and experiences belong in `memory/`.
For earlier work, load `enso-memory`, search with `enso memory search`, and read relevant
notes and sources before answering. People sharing this workspace share its maintained memory.

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

Bindings grant access as well as selecting context. Binding a channel trusts all its human
participants, including guests and later additions, and captures eligible live discussion.
Binding a DM trusts that person; people sharing its workspace share maintained memory,
without a confidentiality boundary. Only trusted configuration or operator-initiated pairing
may add bindings. Unknown senders cannot authorize themselves; an unbound conversation gets
only the fixed notice, with no provider work, attachment download, or capture. In an unbound
channel, only mentioning the bot triggers that notice. See the [connection rules](https://github.com/geekforbrains/enso/blob/develop/docs/connections.md#access-in-020).

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

A job belongs to the workspace containing `jobs/<job>/JOB.md` and uses a `<workspace>:<job>` reference. Projects live in `projects/<KEY>/PROJECT.md`; neither file repeats its workspace. Bindings, workspace settings, and project definitions are read fresh for each operation, with no restart. A queued turn keeps its arrival workspace; removing its binding drops it before provider startup. Use `enso slack lookup-channel` for ids; never guess one.

The workspace scaffold keeps `skills/` and its provider links ready even when empty. `WORKSPACE.md` and the workspace `heartbeat/` root are optional and are not created by scaffolding. Existing `knowledge/` and `drafts/` directories remain supported, but new workspaces do not create them. Audits preserve absent optional content folders; initialization and audits never move existing notes or work files.

## Skills

Global skills go in `~/.enso/skills/<skill>/SKILL.md`; workspace-only ones in `<workspace>/skills/<skill>/SKILL.md`. Drop the directory in and the next turn sees it; nothing needs regenerating. The directory name and the frontmatter `name` match, and `description` says what the skill does and when to use it. A name must not appear in both places: a workspace skill does not override a global one, the CLIs disagree about which copy wins. Keep long reference material in `references/` beside `SKILL.md`.

`enso` and names beginning `enso-` are reserved for the skills Enso installs. Give your own skills other names; the audit warns about a `enso-*` skill Enso did not install.

Load `enso-skills` for the authoring format and validation, or to discover and install an
official optional skill from `geekforbrains/enso-skills`. Those installs are Enso-wide and
carry a source receipt; they never override an existing path. Load `enso-browser` for
browser profiles. A workspace may name its preferred browser profile in `AGENTS.md`, but
the profile itself is private data under the home, not content to put in `skills/`.

## Retiring a workspace

Remove or repoint its bindings, disable its jobs, resolve open project tasks, and review active and paused beats with `enso heartbeat list --workspace NAME` (page through results if needed). Close beats only when the retirement request includes ending that work; pausing alone leaves the workspace reference in place. Beats cannot transfer workspaces, and moving job or project files does not reassign existing records. There is no automated workspace rename or transfer: plan any reference repair for the specific case and keep the directory while its work or retained history needs it.

Let running turns, jobs, and beats finish before moving their files. Overrides live in its `WORKSPACE.md`; there is no workspace override entry in `config.json` to remove. Confirm with `enso config check`, then archive the directory or delete it with the user's authorization, and check configuration again. These changes need no Enso restart. See [Workspaces](https://github.com/geekforbrains/enso/blob/main/docs/workspaces.md#creating-and-retiring) for the lifecycle.
