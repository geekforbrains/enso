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

## Workspace lifecycle

Inspect the configured catalog without changing it:

```bash
enso workspace list
enso workspace show <name>
```

Create a workspace only through the lifecycle command and bind it to an existing policy:

```bash
enso workspace create <name> --policy <policy> [--concurrency <n>]
```

Concurrency defaults to `1`. There is no path option because the name determines the
root. Enso validates the complete candidate catalog, publishes a staged scaffold
atomically, saves configuration, and runs the installation checks. Record the new
scaffold in local history afterwards with one scoped commit
(`git -C ~/.enso add workspaces/<name>` and `git -C ~/.enso commit`). `config.json`
stays ignored, so content history is not a configuration backup. A successful creation
requires a service restart before routing processes see the new catalog entry.

Repair one configured workspace with:

```bash
enso workspace repair <name>
```

Repair owns structural directories and the known relative discovery links only. It does
not recreate or rewrite user-owned `AGENTS.md`, skill definitions, or
`knowledge/README.md`; missing
launch-critical content remains missing and is reported for deliberate repair.

## Policy lifecycle

Inspect policy capability metadata and safe validation state without exposing native
policy contents or secret values:

```bash
enso policy list
enso policy show <name>
```

Every post-setup policy chooses one explicit authority source and names its authorized
and default providers:

```bash
enso policy create <name> --unrestricted \
  --provider <provider> --default-provider <provider> \
  [--chat-command <command>...] [--all-chat-commands]

enso policy create <name> --policy-dir <path> \
  --provider <provider> --default-provider <provider> \
  [--chat-command <command>...] [--all-chat-commands] \
  [--env-passthrough <name>...]
```

The provider, named chat-command, and environment-name options may be repeated. Passing
neither chat-command form grants no Enso chat commands; `--all-chat-commands` is the
explicit all-commands choice, and it is mutually exclusive with `--chat-command`.
Environment passthrough is restricted-only and records names, never values.

Fresh setup's unrestricted `admin` policy has full authority and is the only automatic
policy creation. For a restricted policy, the user or agent first authors or deliberately
copies an existing, complete provider-native policy directory outside every writable
workspace, makes its physical regular files owner-safe, and tests them with the installed
providers. The create command validates and registers that exact directory; it never
supplies a default path or partial scaffold. Canonical restricted native content stays
user-owned: Enso does not generate, copy, change permissions, rewrite, upgrade, or repair
it. Source-tree examples are explanatory starting points rather than trusted or certified
presets, and copies become user-owned immediately.

`enso config check` remains the one complete read-only validator for workspaces, routes,
jobs, discovery, and native-policy plumbing; native behavioral testing remains the
operator's responsibility. Policy creation and later binding changes require a service
restart before a running process sees them. Deletion, consumer rebinding, presets, and
policy repair are not part of the supported lifecycle.

## Local history boundary

Versioned content is the allowlisted, human-authored layer: instructions and discovery
links, canonical skills, global reference docs, workspace knowledge under `knowledge/`,
and approved durable job definition or support files. Enso's root Git repository keeps
local history only. It does not create a remote, push, pull, or provide backup.

After one coherent change, record one scoped commit with explicit paths only:

```bash
git -C ~/.enso add <changed-path> [<changed-path>...]
git -C ~/.enso commit -m "<summary>"
```

Stage only what changed together and leave unrelated work alone. Never use broad staging
such as `git add -A`, never `--force`-add an ignored path, and never add a remote, push,
pull, fetch, or run destructive history or worktree commands (`reset --hard`,
`checkout`/`restore` over uncommitted files, `clean`, rebase). If history looks broken,
report it to the operator instead of repairing it.

Runtime-only or potentially sensitive content is excluded by the managed `.gitignore`
block and must never enter history. This includes `config.json`, secrets, databases,
messages, state, audits, runs, caches, logs, native policy homes, job locks, job output,
`drafts/`, and `uploads/`. Do not commit a path merely because it is below `~/.enso/`.

This reference is seeded only during a genuinely fresh setup and is user-owned afterward.
Upgrades do not replace it. An existing installation adopts newer guidance through the
[v1.3 managed-workspace migration guide](https://github.com/geekforbrains/enso/blob/main/docs/migrations/v1.3-managed-workspaces.md),
never through automatic content replacement.
