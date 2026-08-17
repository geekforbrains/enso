---
name: workspace
description: Use this skill to create, inspect, change, or retire an Enso workspace; bind conversations or jobs to one; write focused workspace AGENTS.md guidance; decide whether a skill belongs globally or in one workspace; or diagnose workspace, policy, and routing configuration.
---

# Workspace

Treat a workspace as a named content root and provider working directory, not as a security boundary. Its configured policy defines authority for every bound conversation and job.

## Inspect before changing

1. Run `enso config check` and record existing errors.
2. Inspect only the relevant parts of `~/.enso/config.json`. Do not print the whole file because it may contain credentials.
3. Resolve the intended workspace name, path, policy, concurrency, and bindings. Reuse an existing policy unless the user explicitly requests different authority.
4. Check that the path does not overlap another workspace or a protected policy directory.

Never widen a policy merely to make workspace setup pass. Treat permission changes and new transport authorization as security-sensitive.

## Workspace layout

Use a lowercase, stable name and configure an absolute path or a path beginning with `~/`:

```json
{
  "workspaces": {
    "project-name": {
      "path": "~/.enso/workspaces/project-name",
      "policy": "admin",
      "concurrency": 1
    }
  }
}
```

`admin` is only an example; select an existing policy appropriate for the requested work. Create this minimum layout:

```text
project-name/
├── AGENTS.md
├── CLAUDE.md -> AGENTS.md
├── knowledge/
├── drafts/
└── uploads/
```

Keep `AGENTS.md` focused on the workspace's purpose, sources of truth, important paths, output conventions, and domain-specific constraints. Do not copy shared Enso behavior, credentials, transport instructions, or a static inventory of globally available skills into it.

## Place skills deliberately

Put a skill useful across Enso-managed workspaces in `~/.enso/skills/<skill-name>/SKILL.md`. Enso exposes that root through provider discovery links under its configuration directory and does not install those links into unrelated project directories. Put the canonical copy of a genuinely workspace-specific skill at:

```text
<workspace>/.agents/skills/<skill-name>/SKILL.md
```

Some provider CLIs also require a provider-native discovery path such as `.claude/skills/`. Expose the same canonical skills there using the workspace's existing management convention; use a relative symlink only when local tooling and policy allow it. Do not maintain divergent copies.

Every skill must follow the Agent Skills specification: its directory name and frontmatter `name` must match, the name must use lowercase letters, digits, and single hyphens, and `description` must say what the skill does and when to use it. Keep conditional detail in shallow `references/`, `scripts/`, or `assets/` resources rather than bloating `SKILL.md`.

Do not create a workspace-scoped copy of a global skill. Do not use skill placement as a permission boundary; the workspace policy remains authoritative.

## Bind work to the workspace

Use the configuration shape for the installed transport:

- A direct workspace transport binding names the workspace in its transport configuration.
- A route-based transport maps each exact authorized conversation to the workspace.
- A scheduled job names the workspace in `JOB.md`.

When working with current built-in transports, consult the relevant transport skill or `enso --help` for exact identifiers and commands. Never guess a user, channel, or destination ID, and never add a wildcard route.

## Apply and verify

1. Write configuration through a JSON-aware, atomic update that preserves all unrelated keys and the file's existing permissions. Never use a partial shell redirection on `config.json`.
2. Create or update the workspace files without overwriting customized instructions.
3. Run `enso config check` until it exits successfully.
4. Use the relevant route explanation or a manual job run when one is available.
5. Restart with `enso service restart` when the service is installed; otherwise tell the user that the running `enso serve` process must be restarted. Configuration bindings are loaded at service startup.

Report the workspace path, selected policy, bindings changed, validation result, and whether a restart completed. Do not claim a route is live until validation and restart both succeed.

## Retire or repoint

Inspect every binding first. Confirm before removing a workspace, deleting its files, or moving existing conversations to a policy with different authority. Prefer unbinding while retaining workspace data unless the user explicitly asks to delete it.
