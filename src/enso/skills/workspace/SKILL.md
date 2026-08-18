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

1. Run `enso config check` and record existing errors.
2. Inspect only the relevant parts of `~/.enso/config.json`; never print the whole file because it may contain credentials.
3. Resolve the intended workspace name, policy, concurrency, and bindings. Reuse an existing policy unless the user explicitly requests different authority.
4. Inspect the physical workspace root, its direct `.git` entry, discovery links, instruction files, and global/local skill-name collisions.
5. Check `enso --help` before invoking a mutation command. Use only commands actually provided by the installed version; do not substitute direct config edits or hand-built links for a missing command.

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

New workspace creation is all-or-nothing: Enso builds the complete scaffold in a staged directory, then moves it into place atomically. An existing destination is an error rather than something to merge into.

After creation, prompts, knowledge, and skills are user-owned content. Enso never overwrites, upgrades, deletes, or resurrects that seeded content during repair, startup, or a software upgrade. Intentional deletion remains deleted.

Structural repair is conservative. It may create missing structural directories or discovery links and recognize correct relative links. It never overwrites unknown paths, unexpected directories, missing user-owned content, or links with unknown targets; preserve and report them for the user to resolve. Do not make provider discovery links by hand.

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

After a supported change, run `enso config check` until it exits successfully. Use route explanation or a manual job run when available. Restart with `enso service restart` when the service is installed; otherwise say that the running `enso serve` process must be restarted because bindings load at startup.

Report the derived workspace path, selected policy, bindings changed, structural warnings, validation result, and whether restart completed. Do not claim a route is live until validation and restart both succeed.

Before retiring or repointing a workspace, inspect every binding. Confirm before deleting workspace data or moving conversations to a policy with different authority. Prefer unbinding while retaining user-owned files unless deletion is explicit.
