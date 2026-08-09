# Permissions

**Planned.** Nothing in this document ships today. It defines how Enso selects and
launches an agent CLI after a workspace has been resolved. Enso remains a thin proxy: it
does not invent a permission language, translate policy between providers, or certify that
two providers enforce equivalent access.

**Verified against:** Claude Code 2.1.226 and Codex CLI 0.147.0 on 2026-08-09. Both
permission surfaces change frequently. Re-verify the launch contract and native policy
syntax before implementation or a supported-version change.

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
`~/.enso/policies/<workspace>` and may name another protected directory:

| Provider | Canonical source                                                    |
| -------- | ------------------------------------------------------------------- |
| `claude` | `<policy_dir>/claude/settings.json`                                 |
| `codex`  | `<policy_dir>/codex/config.toml` and optional `codex/rules/*.rules` |
| `agy`    | none; see [Antigravity](#antigravity-agy)                           |

These are operator-owned source files, not generated output. They must be regular files,
must not resolve through a symlink into the workspace, and must sit outside every path the
agent can write. A read-only bit alone is not a boundary when the agent runs as the same OS
user; the selected native sandbox or an outer container/VM must enforce non-writability.

Some CLIs require policy at a provider-specific runtime location. Enso may stage a
byte-for-byte copy in a protected runtime config directory and select it explicitly. That
is configuration plumbing, not compilation. The exact source digest and staged digest
must match. A native config may itself reference other files. The operator must use
absolute protected references, or Enso must stage the complete referenced subtree with
its relative topology unchanged; Enso never rewrites references. Every dependency joins
the policy manifest and `policy_revision`. An escaping, missing, writable, or
semantics-changing dependency makes the provider unavailable. Staging and session-resume
behaviour must be proven against the pinned CLI before implementation.

## Dispatch gate

For the selected workspace and provider:

1. Reject a workspace that combines `unrestricted: true` with an explicit or discovered
   native policy source. Enso never chooses one mode by precedence.
1. If `unrestricted: true`, run the existing bypass invocation.
1. Otherwise locate the provider's canonical native policy.
1. Require a regular file outside the workspace and parse its JSON or TOML syntax.
1. Run the provider-native validation/preflight and confirm the intended source was
   loaded. A CLI that silently ignores the file is a failure.
1. Construct the provider-specific command below without a bypass flag or ambient policy
   source.

Any failure refuses only that turn and reports the exact reason to an authorized caller.
The service and other workspaces continue running. There is never a fallback from step 2
through 5 to unrestricted mode.

## Claude Code launch contract

The policy-controlled invocation is based on:

```text
claude --setting-sources "" --settings <protected-settings> \
  --permission-mode dontAsk --strict-mcp-config --no-chrome \
  --tools <workspace-tools> -p ...
```

The adapter must verify the exact argument ordering, empty setting-source syntax, isolated
config/home behaviour, and session resume behaviour against the pinned version.

- Drop `--dangerously-skip-permissions`; it is reserved for `unrestricted: true`.
- Pass `--settings` explicitly. In Claude 2.1.226 this source overrides matching scalar
  keys but omitted keys retain lower-layer values, and array settings such as permission
  and sandbox paths merge across sources. `--settings` alone is therefore not isolation.
- Suppress user, project, and local settings sources and run with an isolated provider
  config/home containing only the authentication and runtime state the invocation needs.
  Ambient user CLAUDE.md, skills, plugins, hooks, MCP servers, commands, agents, and
  auto-memory must not leak into the workspace. Managed organizational policy may still
  apply.
- Use `--permission-mode dontAsk`. A headless process has nobody to approve a prompt, so
  every unapproved or `ask` action must be denied rather than stalled.
- Use `--strict-mcp-config`; pass only a protected workspace-specific MCP file when the
  operator explicitly configures one. Disable Chrome and other ambient integrations by
  default.
- Pass an explicit `--tools` list. Permission rules approve calls; `--tools` controls
  which built-ins exist at all.

The protected project instructions and allowlisted workspace skills are intentional
inputs and remain available from the cwd. Enso provisions only those inputs, read-only,
instead of loading the operator's ambient customizations. `--safe-mode` is useful for
diagnostics but is not the normal launch contract because it also disables these intended
instructions and skills. If the pinned CLI cannot combine isolated ambient state with the
protected project inputs, Claude is unavailable for policy-controlled workspaces.

Claude's sandbox is separate from permission mode and applies only to Bash and its child
processes. It is disabled by default. If an operator relies on it as the boundary, their
native file should set `sandbox.enabled: true`, `sandbox.failIfUnavailable: true`, and
`sandbox.allowUnsandboxedCommands: false`; filesystem and network rules then apply at the
OS layer to Bash children. Other Claude tools remain governed by their own permission
rules. Enso reports these settings but does not declare them universally required because
an operator may provide a container or VM boundary instead.

Claude `-p` silently ignores invalid settings rather than presenting an interactive error
dialog. JSON parsing alone is insufficient: preflight must use Claude's own validation and
prove that the explicit settings source was loaded. If that cannot be established for the
pinned version, dispatch is blocked.

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

The profile and its referenced subtree are staged without changing bytes or relative
topology; this is not an Enso translation. Codex resolves native references such as
`agents.<name>.config_file` relative to the declaring config, so copying only the root
file is invalid unless every reference is absolute and protected. The dedicated,
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

| Check                                                                          | Result         |
| ------------------------------------------------------------------------------ | -------------- |
| Workspace path exists and is a directory                                       | error          |
| A policy-controlled provider lacks its canonical native file                   | provider error |
| `unrestricted: true` and any native policy source are both present             | error          |
| Canonical file is regular, outside cwd, and owner-only                         | provider error |
| JSON/TOML parses and provider-native preflight accepts it                      | provider error |
| Pinned provider version supports the launch contract                           | provider error |
| Intended source/profile is selected with no ambiguous higher-precedence source | provider error |
| Bypass flag appears in a policy-controlled launch                              | provider error |
| Codex mixes permission profiles and legacy sandbox settings                    | provider error |
| `agy` is enabled without `unrestricted: true`                                  | provider error |

Startup reports all workspaces and continues. A workspace-level structural error blocks
that workspace. A provider-specific error refuses only that provider, so another
correctly configured provider in the same structurally valid workspace remains usable.
Diagnostics do not rewrite or grade an otherwise valid native policy.

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
