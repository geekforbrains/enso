---
name: workspace
description: Use this skill to inspect, create, repair, bind, or retire an Enso-managed workspace; write focused workspace guidance; place global or local skills; or diagnose workspace discovery, policy, and routing configuration.
---

# Workspace

Treat a workspace as a named content root and provider working directory, not as a security boundary. Its configured policy defines authority for every bound conversation and job.

## Start with the invariants

A workspace name uses lowercase kebab-case: lowercase letters and numbers separated by single hyphens. Its name derives its only valid location:

```text
~/.enso/workspaces/<name>
```

Workspace configuration has no `path` field. Do not accept an external directory, a differently named directory, or a symlink as a workspace root. The managed `~/.enso/workspaces/` directory and each workspace root must be real directories. A workspace root must not contain a `.git` entry; repositories deeper inside it are allowed.

These are current-layout requirements, not conventions to relax for an older install. Direct users with an old configurable path or hand-made discovery links to the versioned migration guide. Do not implement compatibility links or move their content implicitly.

## Inspect before changing

1. Run `enso workspace list`, then `enso workspace show <name>` for the relevant
   workspace.
2. Run `enso config check` and record existing errors.
3. Resolve the intended workspace name, policy, concurrency, and bindings. Reuse an
   existing policy unless the user explicitly requests different authority.
4. Inspect the physical workspace root, its direct `.git` entry, discovery links,
   instruction files, and global/local skill-name collisions.
5. Check `enso workspace --help` before invoking a mutation command. Use only commands
   actually provided by the installed version.

Do not edit `~/.enso/config.json` or make provider discovery links by hand for workspace
lifecycle operations. The workspace commands keep configuration, the canonical scaffold,
validation, and local content history on the same contract.

Never widen a policy merely to make workspace setup pass. Treat permission changes and new transport authorization as security-sensitive.

## Know the canonical layout

Enso-wide instructions and skills have one canonical copy:

```text
~/.enso/
├── AGENTS.md
├── CLAUDE.md -> AGENTS.md
├── skills/
├── .agents/skills -> ../skills
└── .claude/skills -> ../skills
```

Every workspace uses this structure:

```text
~/.enso/workspaces/<name>/
├── AGENTS.md
├── CLAUDE.md -> AGENTS.md
├── skills/
├── .agents/skills -> ../skills
├── .claude/skills -> ../skills
├── knowledge/
│   └── README.md
├── drafts/
└── uploads/
```

All discovery links are relative. `~/.enso/skills/` is the global canonical skill source, and `<workspace>/skills/` is the workspace-local canonical source. The local skills directory starts empty; it is only for reusable procedures unique to that workspace. Never copy global skills into it, and reject duplicate skill directory names across the global and local scopes.

The exact skill discovery links are `~/.enso/.agents/skills -> ../skills`, `~/.enso/.claude/skills -> ../skills`, `<workspace>/.agents/skills -> ../skills`, and `<workspace>/.claude/skills -> ../skills`.

`AGENTS.md` is canonical at each level. Keep it as a small routing prompt: identify the workspace purpose and scope, define the meanings of ambiguous terms, list only critical approvals or safety rules that must appear every turn, and point to each authoritative source with a path and specific “when to read” description. Keep detailed operating context in `knowledge/`; when `knowledge/README.md` is present, maintain it as the knowledge index rather than loading domain detail on every turn.

## Preserve ownership

Create a workspace with an explicit existing policy:

```bash
enso workspace create <name> --policy <policy> [--concurrency <n>]
```

Concurrency defaults to `1`. The command exposes no path option: `<name>` determines the
only valid root. It validates the candidate catalog before publishing, builds the complete
scaffold in a staged directory, moves it into place atomically, saves configuration, runs
the installation checks, and automatically snapshots the five versionable seed entries:
`AGENTS.md`, `CLAUDE.md`, `.agents/skills`, `.claude/skills`, and
`knowledge/README.md`. The ignored `config.json` is not part of that commit, so local
content history is not a configuration backup. An existing
destination is an error rather than something to merge into. If configuration persistence
fails after publication, preserve and report the unused directory; never delete it by
guessing.

After creation, prompts, knowledge, and skills are user-owned content. Enso never overwrites, upgrades, deletes, or resurrects that seeded content during repair, startup, or a software upgrade. Intentional deletion remains deleted.

Repair a configured workspace with:

```bash
enso workspace repair <name>
```

Structural repair is conservative. It may create missing structural directories or
discovery links and recognize correct relative links. It never overwrites unknown paths,
unexpected directories, missing user-owned content, or links with unknown targets;
preserve and report them for the user to resolve.

Do not make provider discovery links by hand.

Bundled root prompts and global skills are seeded only by fresh setup. Workspace prompts and `knowledge/README.md` are seeded only when that workspace is first created. Ordinary startup validates structure but does not repair or install content.

## Place skills deliberately

Put a skill useful across Enso workspaces at:

```text
~/.enso/skills/<skill-name>/SKILL.md
```

Put a genuinely workspace-specific skill at:

```text
<workspace>/skills/<skill-name>/SKILL.md
```

Never write the canonical copy through `.agents/skills` or `.claude/skills`; those paths only expose the canonical directory to provider discovery.

Every skill follows the Agent Skills specification: its directory name and frontmatter `name` match, its name uses lowercase letters, digits, and single hyphens, and its `description` says what it does and when to use it. Keep conditional detail in shallow `references/`, `scripts/`, or `assets/` resources rather than bloating `SKILL.md`.

Do not use skill placement as a permission boundary; the workspace policy remains authoritative.

## Record content changes safely

Create one scoped local snapshot after each coherent change to versionable Enso content:

```bash
enso snapshot create --message "<summary>" -- <changed-path> [<changed-path>...]
```

Pass explicit paths for only the workspace or global instruction, skill, knowledge, or discovery content changed together. Never use raw broad Git staging such as `git add -A`; `config.json`, native policy homes, uploads, drafts, credentials, databases, snapshot locks and transaction state (`.snapshot.lock`, `.snapshot.transaction.json`, `.snapshot-transaction-*.tmp` at the worktree root, and `.snapshot-index-*` in the resolved Git directory), and other protected runtime state are not eligible. Never remove a native Git index lock; Enso handles one only when its marker proves the exact lock is Enso-created. If the active policy denies a requested read or an internal repository or transaction write, report that boundary; never widen or rewrite the policy or substitute raw Git. The command creates local history only; do not use or invent restore, reset, or delete operations.

## Bind work to the workspace

A workspace entry names policy and concurrency, but never a filesystem path:

```json
{
  "workspaces": {
    "project-name": {
      "policy": "admin",
      "concurrency": 1
    }
  }
}
```

`admin` is only an example; select an existing policy appropriate for the requested work.

- A direct workspace transport binding names the workspace in its transport configuration.
- A route-based transport maps each exact authorized conversation to the workspace.
- A scheduled job names the workspace in `JOB.md`.

Consult the relevant transport skill or `enso --help` for exact identifiers and supported commands. Never guess a user, channel, or destination ID, and never add a wildcard route.

## Verify and report

After a supported change, run `enso config check` until it exits successfully. Use route
explanation or a manual job run when available. Workspace creation reports that a service
restart is required. Restart with `enso service restart` when the service is installed;
otherwise say that the running `enso serve` process must be restarted because bindings
load at startup.

Report the derived workspace path, selected policy, bindings changed, structural warnings, validation result, and whether restart completed. Do not claim a route is live until validation and restart both succeed.

Before retiring or repointing a workspace, inspect every binding. Confirm before deleting workspace data or moving conversations to a policy with different authority. Prefer unbinding while retaining user-owned files unless deletion is explicit.
