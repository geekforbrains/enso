---
name: Enso Layout
description: The current managed Enso filesystem and local-history boundaries; read when locating, validating, or repairing installation content.
---

# Enso layout

Enso owns one managed root at `~/.enso/`. Every workspace is internal and name-derived:
the lowercase kebab-case name `<name>` maps exactly to
`~/.enso/workspaces/<name>/`. Workspace roots and their container are physical
directories, not symlinks, and a workspace root has no `.git` entry.

```text
~/.enso/
├── .git/                         # local history, not a backup
├── .gitignore                    # Enso's protected-path boundary
├── AGENTS.md                     # global instructions
├── CLAUDE.md -> AGENTS.md
├── skills/                       # canonical global skills
├── .agents/skills -> ../skills
├── .claude/skills -> ../skills
├── docs/
│   ├── enso/content_model.md
│   ├── enso/layout.md
│   └── operator.md
├── jobs/<job>/                   # durable definitions plus runtime output
└── workspaces/<name>/
    ├── AGENTS.md                 # workspace routing instructions
    ├── CLAUDE.md -> AGENTS.md
    ├── skills/                   # canonical workspace skills
    ├── .agents/skills -> ../skills
    ├── .claude/skills -> ../skills
    ├── knowledge/                # durable workspace-only context
    ├── drafts/                   # editable, unversioned work
    └── uploads/                  # retained inbound files, unversioned
```

The fresh-install references therefore live at `docs/enso/content_model.md`,
`docs/enso/layout.md`, and `docs/operator.md` relative to the managed root.

The relative links expose one canonical instruction file and skill directory to each
supported provider without copied provider-specific content. Repositories deeper inside
a workspace are ordinary content repositories; they do not change the Enso workspace
root.

## Local history boundary

Versioned content is the allowlisted, human-authored layer: instructions and discovery
links, canonical skills, global docs, workspace `knowledge/`, and approved durable job
definition or support files. Enso's root Git repository keeps local history only. It
does not create a remote, push, pull, or provide backup.

Runtime-only or potentially sensitive content is ignored and must not be snapshotted.
This includes `config.json`, secrets, databases, messages, state, audits, runs, caches,
logs, policy runtime files, job output, `drafts/`, and `uploads/`. Unknown paths are not
assumed safe merely because they are below `~/.enso/`.
