---
name: policy
description: Use this skill to inspect or create an Enso policy, deliberately choose unrestricted or restricted authority, author provider-native policy files, configure chat commands or environment passthrough, validate a policy, or prepare one for workspace binding.
---

# Policy

Treat a policy as an authority boundary. A workspace supplies content and a working
directory; its one bound policy decides which providers and Enso chat commands are
available and whether each provider launches unrestricted or with protected
provider-native settings.

Enso does not define a cross-provider permission language. Author each restricted
provider policy in that provider's own format and verify it with the installed provider
version. Do not translate permissions from another provider by analogy.

## Inspect before changing authority

Start with the read-only catalog commands:

```bash
enso policy list
enso policy show <name>
enso config check
```

`list` summarizes capabilities, consumers, and validation. `show` adds safe paths,
revision digests, warnings, and names such as environment variables or MCP servers. They
never print secret values or native policy-file contents. `enso config check` is the sole
complete configuration validator; it checks the whole workspace, route, job, discovery,
and native-policy plumbing without certifying that the provider rules grant the intended
authority.

Before creating a policy, identify:

- every provider it should authorize and which one is the default;
- the exact Enso chat commands it should expose, if any;
- whether the requested work genuinely needs unrestricted execution;
- for restricted authority, the provider-native files and narrowly scoped environment
  variables the provider needs; and
- every workspace, route, and job that may eventually consume the policy.

Fresh `enso setup` creates unrestricted policy `admin` and binds workspace `default` to
it. That is the only automatic policy creation and the only setup-only automatic grant of
full authority. Every later policy and every later workspace binding is explicit.

## Choose exactly one authority source

Create an unrestricted policy only for a deliberately trusted administrative use case:

```bash
enso policy create <name> --unrestricted \
  --provider <provider> [--provider <provider>...] \
  --default-provider <provider> \
  [--chat-command <command>...] [--all-chat-commands]
```

`--provider` is repeatable, and the default provider must be one of those values. Repeat
`--chat-command` to allow only named Enso transport commands, pass
`--all-chat-commands` for the explicit `"*"` capability, or pass neither to allow no
chat commands. The two chat-command forms are mutually exclusive. Unrestricted mode
inherits the provider's ordinary environment, so it rejects `--env-passthrough` rather
than pretending that the environment was narrowed.

Restricted creation instead registers one existing native-policy directory:

```bash
enso policy create <name> --policy-dir <path> \
  --provider <provider> [--provider <provider>...] \
  --default-provider <provider> \
  [--chat-command <command>...] [--all-chat-commands] \
  [--env-passthrough <name>...]
```

`--unrestricted` and `--policy-dir` are mutually exclusive and one is required. A
restricted policy has no implicit directory. `--policy-dir <path>` must name the exact
absolute or `~/` path you prepared, and every selected provider must already have its
complete required source there. Repeat `--env-passthrough` for environment-variable
names only, never values; absence grants none. Launch-controlled names and `ENSO_` names
are reserved.

## Author restricted native content first

Before running the restricted create command:

1. Read the installed provider's current official policy, settings, sandbox, tool, and
   extension documentation.
2. Choose a stable directory outside every writable workspace. Create the physical
   directory and its provider subdirectories yourself; do not use symlinks or aliases.
3. Author or deliberately copy every required provider-native source:
   - Claude: `claude/settings.json`; optional exact MCP declaration at
     `claude/mcp.json`.
   - Codex: `codex/config.toml` and every local file or `rules/*.rules` it depends on.
   - Grok: `grok/config.toml` and no unreviewed trust or marketplace files.
   - Antigravity: use only an explicitly unrestricted policy until Enso documents a
     tested restricted native launch contract.
4. Make the directories and files physical and owned by the Enso service user. Directories
   must not be writable by group or other users; canonical source files must be regular,
   owner-only, and free of extra hard links. Apply `chmod 600` to source files yourself
   where appropriate.
5. Test the native files with the installed provider in a disposable workspace. Exercise
   allowed and forbidden reads, writes, processes, network, environment access,
   extension/tool servers, approval attempts, and modification of the policy itself.
6. Run the create command only after the directory is existing, complete, and safe.

Files under `docs/examples/` in the Enso source tree are explanatory starting points, not trusted or certified presets.
Review and adapt one before copying it into your chosen directory. After the copy, it is
user-owned native content, not an Enso preset.

Enso validates and registers restricted native sources, then revalidates them for checks
and launches. Enso never generates canonical restricted content, never copies it into the
canonical source, never changes permissions on it, never rewrites it, never upgrades it,
and never repairs it. A validation failure is a user-owned source problem to inspect and
correct deliberately; Enso does not mutate the file to make a check pass.

## Keep capabilities narrow

Provider selection and native permissions are separate. Listing a provider authorizes it
for the policy but does not prove its native file is usable or safe. The default provider
must be authorized, and a stored route preference can never widen that set.

`--chat-command` controls Enso's Telegram `/...` and Slack `!...` commands only. It does
not control provider-native tools, slash commands, skills, plugins, hooks, or MCP
servers. Grant administrative Enso commands only to an administrative policy.

Environment passthrough delivers a named variable's real value to every selected
restricted provider. It does not scope or conceal that credential from a provider that
can inspect its own environment or execute a process. Prefer narrowly scoped credentials,
keep values in the service environment or `~/.enso/secrets/*.env`, and register names
only.

Provider-native extension surfaces are authority too. In particular, MCP servers may be
reached by the provider process outside a command sandbox's network rules. Declare only
reviewed servers and tools, keep credentials out of native files, and retest after
provider upgrades.

## Register and bind through supported commands

Do not edit `~/.enso/config.json` to create a policy. The create command strictly reloads
the current config under Enso's mutation lock, validates the complete candidate catalog
and existing native sources, and saves only after validation succeeds. It does not create
an inactive scaffold or partially register a failed policy.

After registration, bind a new workspace explicitly:

```bash
enso workspace create <name> --policy <policy> [--concurrency <n>]
```

Use the `workspace` skill for workspace creation, structure, knowledge, instructions,
skills, and bindings. Policy deletion and rebinding existing consumers are deliberately
outside this lifecycle because they require impact reporting across workspaces, routes,
and jobs. Never invent a command or hand-edit a binding merely to finish a policy task.

Policy config and canonical native policy sources are protected runtime/authority state,
not versionable Enso content. Never commit them to Enso's local Git history.

## Validate and report

After creation, run:

```bash
enso policy show <name>
enso config check
```

Fix every error in the user-owned native source or configuration and repeat the complete
validator. A syntactically valid result is not a security certification, so report the
native behavioral tests separately. Policy changes require a service restart before a
running bot or dashboard uses the new catalog:

```bash
enso service restart
```

If Enso is running in the foreground, restart that `enso serve` process instead. Report
the policy name, unrestricted or restricted authority source, selected/default providers,
chat-command scope, environment-variable names (never values), native test result,
`enso config check` result, intended consumers, and whether the service restart finished.
