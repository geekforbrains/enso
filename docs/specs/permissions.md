# Permissions

How Enso selects and launches an agent CLI after a workspace has been resolved. Enso
remains a thin proxy: it does not invent a permission language, translate policy between
providers, or certify that two providers enforce equivalent access.

**Verified against:** Claude Code 2.1.226 and Codex CLI 0.147.0 on 2026-08-09, exercised
by an adversarial harness (reads of `~/.ssh`, `~/.enso/enso.db` via a `sqlite3` subprocess,
out-of-workspace writes, the `enso` CLI, secret env vars) that confirmed each escape is
blocked and legitimate in-workspace work is allowed. Both permission surfaces change
frequently; re-verify the launch contract and native policy syntax on a supported-version
change.

Sibling specs: [teams.md](teams.md) owns Slack groups, routes, and workspace selection.
This document starts after one workspace and provider have been selected.

## Principles

1. The operator authors and tests each CLI's native policy. Enso never compiles a shared
   policy into provider-specific files.
1. Enso owns only the launch seam: cwd, provider selection, policy source selection,
   ambient configuration, and process environment.
1. Missing, invalid, rejected, ambiguous, or unapplied policy fails closed. Enso refuses
   the turn rather than falling back to its current unrestricted invocation.
1. A native file that deliberately grants broad access is still the operator's policy.
   Enso does not reinterpret or grade it.
1. The authoritative policy lives outside the workspace. The operator is responsible for
   making it non-writable through the native policy or an outer boundary.

The cost of this design is deliberate. A workspace that permits Claude and Codex needs
two policies, and Enso cannot say they mean the same thing. The benefit is that every
provider capability remains available without a second policy engine that can drift from
the CLI.

## The seam

| Enso enforces at launch                           | The operator's native policy enforces           |
| ------------------------------------------------- | ----------------------------------------------- |
| Which workspace is the cwd                        | Filesystem read and write access                |
| Which provider may run                            | Commands and built-in tools                     |
| Which exact policy source is loaded               | Network access and destinations                 |
| Suppression of ambient user/project customization | MCP, connector, browser, and plugin exposure    |
| A minimal launch environment                      | What environment reaches agent-spawned commands |
| Route authorization and audit capture             | Provider-specific credential controls           |

Enso validates that the requested native policy was selected and accepted. It does not
grade the policy's meaning.

## Policy states

The terms are intentionally narrow:

- **Unrestricted** — the workspace explicitly sets `unrestricted: true`. Enso uses the
  existing bypass invocation. This is the only implicit yolo path.
- **Configured** — the provider's native policy exists outside the workspace, parses, and
  the pinned CLI accepts and applies it under the launch contract below. This says nothing
  about how much access it grants or whether the operator's intended boundary is effective.
- **Unavailable** — every other state. Dispatch is refused with a specific diagnostic.

Use **policy-controlled**, not **restricted**, when only `configured` is known. A policy
can parse successfully and intentionally allow everything.

## Authoritative files

Native policies live outside the agent's working directory. `policy_dir` defaults to
`~/.enso/policies/<workspace>` and may name another protected directory. **Planned:** this
directory is renamed `~/.enso/permissions/<workspace>/`, matched to the workspace by name,
with the config key becoming `permissions_dir` and `enso policy check` becoming
`enso permissions check` (see
[data-model.md](data-model.md#planned-scaffolding-and-the-permissions-rename)).

| Provider | Canonical source                                                    |
| -------- | ------------------------------------------------------------------- |
| `claude` | `<policy_dir>/claude/settings.json`                                 |
| `codex`  | `<policy_dir>/codex/config.toml` and optional `codex/rules/*.rules` |
| `agy`    | none; see [Antigravity](#antigravity-agy)                           |

These are operator-owned source files, not generated output. They must be regular files,
must not resolve through a symlink into the workspace, and must sit outside every path the
agent can write. A read-only bit alone is not a boundary when the agent runs as the same OS
user; the selected native sandbox or an outer container/VM must enforce non-writability.

Codex requires its policy at a provider-specific runtime location, so Enso stages a
byte-for-byte copy into a protected runtime home and selects it explicitly (see the Codex
launch contract below). That is configuration plumbing, not compilation: the source digest
and the staged digest must match, and both join the policy manifest and `policy_revision`.
Enso stages the config file and any configured `.rules` files; it never rewrites
references, so a native config that points at other files must use absolute protected
references for them. An escaping, missing, or writable dependency makes the provider
unavailable.

## Dispatch gate

For the selected workspace and provider:

1. Reject a workspace that combines `unrestricted: true` with an explicit or discovered
   native policy source. Enso never chooses one mode by precedence.
1. If `unrestricted: true`, run the existing bypass invocation.
1. Otherwise locate the provider's canonical native policy.
1. Require a regular, owner-only file resolving outside the workspace, and parse its JSON
   or TOML syntax.
1. Compute the `policy_revision` digest and, for Codex, stage the protected runtime home.
1. Construct the provider-specific command below without a bypass flag or ambient policy
   source.

Any failure refuses only that turn and reports the exact reason to an authorized caller.
The service and other workspaces continue running; there is never a fallback to
unrestricted mode. Enso does not run the provider's own validator, so an operator confirms
the policy has the intended effect through the testing described below.

## Claude Code launch contract

The policy-controlled invocation, verified against Claude 2.1.226:

```text
claude -p --output-format stream-json --verbose \
  --settings <protected-settings> --permission-mode dontAsk \
  --setting-sources "" --strict-mcp-config --model <model> \
  [--session-id <id> | --resume <id>] -- <prompt>
```

- `--dangerously-skip-permissions` is dropped; it is reserved for `unrestricted: true`.
- `--settings` names the operator's file explicitly. In Claude 2.1.226 this source
  overrides matching scalar keys but omitted keys retain lower-layer values, and array
  settings such as permission and sandbox paths merge across sources — so `--settings`
  alone is not isolation.
- `--setting-sources ""` suppresses the user, project, and local settings sources, so
  ambient user `CLAUDE.md`, skills, plugins, hooks, MCP servers, commands, agents, and
  auto-memory do not leak into the workspace. Managed organizational policy may still
  apply.
- `--permission-mode dontAsk` denies every unapproved or `ask` action rather than stalling;
  a headless process has nobody to approve a prompt.
- `--strict-mcp-config` keeps ambient MCP servers out; a protected workspace-specific MCP
  file is passed only when the operator configures one.

**Known defect.** `--setting-sources ""` suppresses more than settings: measured against
2.1.226, it also stops Claude from loading `CLAUDE.md` up the directory chain *and* from
discovering workspace skills. A policy-controlled workspace therefore runs today with no
workspace or shared instructions and only built-in skills, so the bootstrapped `AGENTS.md`
and the workspace `skills` allowlist are inert. Isolation is stronger than documented, not
weaker — nothing ambient leaks — but the intended inputs are missing.

Dropping the flag is not the fix: without it, the operator's entire `~/.claude` comes
along (in testing, more than twenty ambient user skills). The planned repair is a
per-workspace `CLAUDE_CONFIG_DIR` holding exactly the shared instructions, workspace
instructions, and allowlisted skills Enso stages, so the "user" scope Claude loads is one
Enso controls rather than the operator's own. That approach is unverified against the
pinned CLI; the fallbacks are prepending the instructions to the prompt (provider-neutral,
costs tokens per turn) or `--add-dir` with
`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`. Whichever lands must keep ambient user
configuration out while restoring layered instructions and allowlisted skills.

Claude's sandbox is separate from permission mode and applies only to Bash and its child
processes; it is disabled by default. An operator relying on it as the boundary should set
`sandbox.enabled: true`, `sandbox.failIfUnavailable: true`, and
`sandbox.allowUnsandboxedCommands: false` in the native file; filesystem and network rules
then apply at the OS layer to Bash children. Other Claude tools remain governed by their
own permission rules. Enso warns when the sandbox is off but does not require it, since an
operator may provide a container or VM boundary instead.

Claude `-p` silently ignores invalid settings rather than raising an interactive error, so
the operator verifies enforcement with the native diagnostics and disposable test
workspaces described under [Operator testing](#operator-testing). Enso's own check is
plumbing, not semantics: it confirms the file parses and is a protected regular file, not
that a given rule has the intended effect.

References: [settings](https://code.claude.com/docs/en/settings), [CLI](https://code.claude.com/docs/en/cli-usage), [permission modes](https://code.claude.com/docs/en/permission-modes), and [sandboxing](https://code.claude.com/docs/en/sandboxing).

## Codex launch contract

Codex 0.147.0 has beta permission profiles that combine filesystem and network policy. A
protected native config names the active profile with `default_permissions` and defines it
under `[permissions.<name>]`. The schema below was verified against the pinned CLI by the
security harness; earlier drafts of this doc guessed the shape and were wrong.

Filesystem access is a table mapping a path (or a scope alias like `":minimal"` and
`":workspace_roots"`) to `read`, `write`, or `deny`; more specific paths win, and `deny`
beats `write`/`read` at equal specificity. Network is a `[permissions.<name>.network]`
table with `enabled` plus a `domains` sub-table of `"host" = "allow"|"deny"`; local
binding and Unix sockets have their own keys and stay closed unless explicitly opened. A
minimal, workspace-only, network-off profile is:

```toml
default_permissions = "enso"
approval_policy = "never"

[permissions.enso.filesystem]
":minimal" = "read"

[permissions.enso.filesystem.":workspace_roots"]
"." = "write"

[permissions.enso.network]
enabled = false
```

Permission profiles require Codex 0.138.0 or later and do **not** compose with legacy
`sandbox_mode` or `[sandbox_workspace_write]` settings. If `sandbox_mode` appears in any
loaded config, the selected config overlay sets it, or Enso passes `--sandbox`, Codex uses
the legacy sandbox instead of `default_permissions`. Mixing the two systems is an error
for a policy-controlled workspace.

The verified non-interactive shape (note: `exec` is the subcommand, and the profile is
selected by the staged config's `default_permissions`, not a `--profile` flag — `exec`
does not accept `--ask-for-approval`):

```text
CODEX_HOME=<protected-runtime-home> codex exec --strict-config \
  --skip-git-repo-check [--ignore-rules] -m <model> -- <prompt>
```

`--skip-git-repo-check` bypasses Codex's "are you in a trusted git repo" UX guard; a
workspace need not be a git repo, and this is not a security boundary. `approval_policy = "never"` in the staged config makes an attempted escalation fail rather than wait for a
user who cannot answer. **Model availability is account-scoped:** a ChatGPT-account login
rejects many model slugs (`gpt-5.1-codex`, `gpt-5-codex`, …) with a 400 before any policy
applies, so a workspace's Codex model must be one the operator's account actually serves
(the security harness used the `sol` → `gpt-5.6-sol` alias).

The `config.toml` and any `.rules` files are staged byte-for-byte (digests must match);
this is not an Enso translation. Codex resolves native references such as
`agents.<name>.config_file` relative to the declaring config, and Enso does not stage those
extra files, so any such reference must be an absolute protected path. The dedicated,
service-owned `CODEX_HOME` contains only this staged tree, required authentication
(`auth.json`, copied from the user Codex home), and workspace-scoped runtime state; it is
never the operator's normal home or inside the writable workspace. The adapter:

- drops `--dangerously-bypass-approvals-and-sandbox` except for `unrestricted: true`;
- uses `--strict-config` so unknown keys are rejected;
- selects the protected Enso profile via the staged `default_permissions`;
- relies on `approval_policy = "never"` so an escalation fails rather than waiting; and
- passes `--ignore-rules` when no rules are configured, or stages only the configured
  `.rules` files in the isolated runtime home.

Codex permission profiles govern local sandboxed command execution. They do not govern
MCP, connectors, browser/computer-use tools, Codex cloud, or an approved escalation; those
surfaces need their own native settings or must be absent. `requirements.toml` is a
system/managed policy surface, not a per-workspace file Enso should generate.

References: [permission profiles](https://learn.chatgpt.com/docs/permissions), [agent approvals and security](https://learn.chatgpt.com/docs/agent-approvals-security), and [configuration](https://learn.chatgpt.com/docs/config-file/config-reference).

## Antigravity (`agy`)

`agy` has no documented permission or sandbox model. It is available only when the
workspace explicitly sets `unrestricted: true`. In a policy-controlled workspace it is
hidden from `!use`, and direct selection is refused immediately.

## Process environment and local authority

`enso serve` loads `~/.enso/secrets/*.env`; provider processes must not inherit that whole
environment. For a policy-controlled workspace Enso constructs a minimum launch
environment containing locale, a controlled `PATH`, provider runtime variables, and only
the credential required to authenticate the active CLI.

The active provider credential may be required by the parent CLI, so the operator's native
shell-environment controls must remove it from commands the agent spawns. Claude provides
credential environment controls; Codex provides `shell_environment_policy`. The operator
must test that child commands cannot observe the active provider credential. Unrelated
Enso, 1Password, transport, database, and provider credentials are never passed to the
CLI at all.

The agent must also be unable to reach Enso's ambient authority:

- do not expose `~/.enso/config.json`, the shared database, jobs, messages, or policy
  directories through workspace links;
- expose only explicitly selected skills, and make shared sources read-only by an actual
  sandbox or mount rather than by symlink convention;
- remove the Enso executable directory from the child `PATH`; and
- require the native policy or outer sandbox to deny both Enso state and absolute-path
  invocation of the `enso` executable.

`PATH` filtering and command-name rules are only friction when an absolute path or an
interpreter can reach the same capability. The filesystem or outer process boundary is
the real control.

## Static validation

`enso policy check` runs at startup and on demand. It verifies plumbing, not semantics:

| Check                                                                | Result         |
| -------------------------------------------------------------------- | -------------- |
| Workspace path exists and is a directory                             | error          |
| Teams schema is valid (known groups/workspaces, `default_provider`)  | error          |
| `unrestricted: true` and any native policy source are both present   | workspace error |
| A policy-controlled provider lacks its canonical native file         | provider error |
| Canonical file is a regular, owner-only file resolving outside cwd   | provider error |
| JSON/TOML parses                                                     | provider error |
| Codex mixes permission profiles and legacy sandbox settings          | provider error |
| `agy` is enabled without `unrestricted: true`                        | provider error |
| Claude sandbox is not enabled in a policy-controlled workspace        | warning        |

Startup reports all workspaces and continues. A workspace-level structural error blocks
that workspace. A provider-specific error refuses only that provider, so another
correctly configured provider in the same structurally valid workspace remains usable.
The check confirms the file parses and is protected; it does not run the provider's own
validator or grade an otherwise valid native policy. Enforcement is the operator's to
verify — see the next section.

## Operator testing

Policy semantics are the operator's responsibility. Enso does not provide an acceptance
state, grade test results, or treat a successful launch as proof of isolation. The
operator should use each provider's native diagnostics and disposable test workspaces to
cover at least:

- reading and writing an allowed file inside the workspace;
- reading and writing a synthetic sentinel outside the workspace;
- reading and modifying the staged policy itself;
- executing a harmless stand-in through Bash/Python and by absolute path;
- reaching a disposable allowed and denied network target when the policy uses network;
- observing a fake unrelated secret and the active provider credential from a spawned
  shell; and
- attempting an approval or sandbox escape in a non-interactive run.

`enso policy check` may print the resolved cwd, policy path and digest, CLI version, and a
redacted command shape to help the operator reproduce the exact launch. It does not run
semantic probes. The operator repeats their native tests whenever the policy, CLI, OS, or
Enso launch contract changes.

## Failure modes

| Symptom                                    | Likely cause                                                               |
| ------------------------------------------ | -------------------------------------------------------------------------- |
| Policy file exists but has no effect       | Wrong source selected, ambient config won, or a bypass flag remained       |
| Claude allow/deny paths unexpectedly widen | Array settings merged from another source                                  |
| Claude subprocess reads a denied path      | Tool rule used without the OS sandbox or outer isolation                   |
| Claude silently uses defaults              | Invalid settings were ignored in `-p` mode                                 |
| Codex ignores `default_permissions`        | A legacy sandbox setting or `--sandbox` selected the old system            |
| Codex reaches an unexpected domain         | Broad `*` rule, another tool surface, or network outside the command proxy |
| Secret appears in a spawned shell          | Launch environment or native child-environment filter is too broad         |
| Agent changes its own policy               | Canonical or staged policy was inside a writable root                      |

Every one of these failures can look like a valid configuration. That is why Enso fails
closed when it cannot prove the native file was applied, while leaving the policy's actual
meaning and testing to the operator.
