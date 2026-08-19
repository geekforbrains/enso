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
├── .snapshot.lock                # persistent snapshot serializer
├── .snapshot.transaction.json    # recoverable transaction marker when present
├── .snapshot-transaction-*.tmp   # protected atomic marker-write temporary
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

After one coherent change, create one snapshot with explicit paths only:

```bash
enso snapshot create --message "<summary>" -- <changed-path> [<changed-path>...]
```

The command requires a clean staging area, leaves unrelated unstaged work alone, and
treats a request with no diff as a successful no-op. Never use raw broad Git staging or
invent restore, reset, or delete operations; Enso intentionally exposes none.

Internally, Enso builds and audits a protected alternate index named
`.snapshot-index-<32-lowercase-hex>` inside the resolved Git directory as a complete
owner-only `0600` index. Enso uses filter-free descriptor reads to preserve the reviewed
bytes: `git hash-object -w --no-filters --stdin` writes each blob, and
`git update-index --add --cacheinfo` enters its exact object ID and mode without applying
worktree attributes and clean filters. The owner-only transaction marker at
`.snapshot.transaction.json` records its new-index SHA;
each atomic marker update first uses a protected owner-only
`.snapshot-transaction-<32-lowercase-hex>.tmp` at the worktree root. Before moving the
ref, Enso atomically hard-links that index to the native `index.lock`, rechecks the old
native-index checksum while raw Git is excluded, and atomically compare-and-swaps `HEAD`.
It then atomically replaces the native index with that exact lock and fsyncs the Git
directory without changing worktree files.

If an interruption leaves a marker, the next snapshot recovers only the exact `old/old`,
`new/old`, or `new/new` `HEAD`/native-index states recorded by that transaction; any
divergence fails closed. Recovery removes or finalizes a native lock only when it matches
the exact Enso-created lock inode and checksum named by the marker. An unrelated native
lock is preserved, never deleted on the assumption that it is stale.

Runtime-only or potentially sensitive content is ignored and must not be snapshotted.
This includes `config.json`, secrets, databases, messages, state, audits, runs, caches,
logs, native policy homes, snapshot and job locks, job output, `drafts/`, and `uploads/`.
Unknown paths are not assumed safe merely because they are below `~/.enso/`.

This reference is seeded only during a genuinely fresh setup and is user-owned afterward.
Upgrades do not replace it; an existing installation adopts newer guidance through the
documented migration steps.
