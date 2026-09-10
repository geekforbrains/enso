---
name: enso-workspace
description: Inspect, create, bind, or retire an Enso workspace; write focused workspace guidance; place global or workspace-local skills; or diagnose which workspace a conversation or job runs in.
---

# Workspace

## How Enso sets it up

A workspace is a directory under `~/.enso/workspaces/<name>`: the provider's working directory and the context it starts with. It is a content root, not a security boundary. Names are lowercase kebab-case, and the name is the only valid location. `enso workspace create` makes the whole layout, and `enso workspace audit --fix` repairs it without ever deleting.

```text
~/.enso/
├── AGENTS.md                 # shared instructions for every turn and job
├── CLAUDE.md -> AGENTS.md
├── skills/                   # enso-wide skills: enso, enso-*, and yours
├── .claude/skills -> ../skills
├── .agents/skills -> ../skills
└── workspaces/<name>/
    ├── AGENTS.md             # purpose, scope, terms, approval rules for this workspace
    ├── CLAUDE.md -> AGENTS.md
    ├── skills/               # skills unique to this workspace
    ├── knowledge/            # durable shared material
    ├── drafts/               # generated or editable output
    ├── uploads/              # chat attachments, one directory per turn
    ├── .claude/skills -> ../skills
    └── .agents/skills -> ../skills
```

`AGENTS.md` and `skills/` are the files you write; the `CLAUDE.md` and `.claude`/`.agents` links are generated and are how the provider CLIs find them. Never replace a link with a copy, and never add skills under the links instead of `skills/`. A `.git` inside a workspace hides the home's `AGENTS.md` and skills from the agent, so keep repositories elsewhere.

Keep `AGENTS.md` small: what the workspace is for, what ambiguous terms mean, and any rule that must be visible every turn. Point to files in `knowledge/` for detail instead of inlining it.

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

Bindings live in `~/.enso/config.json`. Keys are `slack:C…` (a channel), `slack:dm:U…` or `slack:dm:W…` (a user's DM; `W…` is an Enterprise Grid org-wide user id), or `telegram:<user id>`; values are workspace names. A workspace may override the agent or a provider's permission flags:

```json
{
  "bindings": {"slack:C0BP5BQF6UF": "meteor"},
  "workspaces": {
    "meteor": {"agent": {"provider": "codex", "model": "sol", "effort": "xhigh"}},
    "testing": {"providers": {"claude": {"args": ["--permission-mode", "dontAsk"]}}}
  }
}
```

An `agent` block needs all three keys. A job names its workspace in `JOB.md`. Bindings are read again for every message: `enso config set bindings.slack:C… <name>`, or an edit to the file, takes effect on the next message in that conversation, with no restart. Use `enso slack lookup-channel` for ids; never guess one.

## Skills

Global skills go in `~/.enso/skills/<skill>/SKILL.md`; workspace-only ones in `<workspace>/skills/<skill>/SKILL.md`. Drop the directory in and the next turn sees it; nothing needs regenerating. The directory name and the frontmatter `name` match, and `description` says what the skill does and when to use it. A name must not appear in both places: a workspace skill does not override a global one, the CLIs disagree about which copy wins. Keep long reference material in `references/` beside `SKILL.md`.

`enso` and names beginning `enso-` are reserved for the skills Enso installs. Give your own skills other names; the audit warns about a `enso-*` skill Enso did not install.

Load `enso-skills` for the authoring format and validation, or to discover and install an
official optional skill from `geekforbrains/enso-skills`. Those installs are Enso-wide and
carry a source receipt; they never override an existing path. Load `enso-browser` for
browser profiles. A workspace may name its preferred browser profile in `AGENTS.md`, but
the profile itself is private data under the home, not content to put in `skills/`.

## Retiring a workspace

Remove its bindings (`enso config unset bindings.slack:C…`) and any job that names it, confirm with `enso config check`, and only then delete or archive the directory. There is no restart step, so the order is what protects you. Confirm before deleting content.
