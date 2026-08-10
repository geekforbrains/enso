# Access profiles and native policies

An access profile defines what a Slack route may offer. It names providers, a default provider, permitted chat commands, and either unrestricted execution or a directory of native provider policies.

Enso does not define a cross-provider permission language and does not merge user and channel permissions. It selects one complete access profile, starts the installed CLI in the route's workspace, and supplies that CLI's native policy through its supported configuration mechanism.

## Configuration boundary

```jsonc
{
  "access": {
    "admin": {
      "unrestricted": true,
      "providers": ["claude", "codex", "agy"],
      "default_provider": "claude",
      "chat_commands": "*"
    },
    "client-readonly": {
      "policy_dir": "~/.enso/policies/client-readonly",
      "providers": ["claude", "codex"],
      "default_provider": "claude",
      "chat_commands": ["status", "clear", "stop", "help"]
    }
  }
}
```

`unrestricted: true` and `policy_dir` are mutually exclusive. Unrestricted execution retains Enso's existing bypass invocation and should be used only on trusted administrative routes. For a restricted profile, `policy_dir` defaults to `~/.enso/policies/<access-name>`. The directory must supply the native file for every enabled provider:

```text
~/.enso/policies/client-readonly/
├── claude/settings.json
└── codex/
    ├── config.toml
    └── rules/*.rules
```

The policy directory belongs to the access profile, not the workspace. This lets several client workspaces reuse one `client-readonly` profile while each CLI starts in the project directory selected by its route.

Policy directories must be absolute after expansion, outside every writable workspace and the legacy `working_dir`, and protected from all restricted agents. Policy files must be regular owner-only files rather than symlinks. Enso should also reject aliases, hard links, and overlapping directory layouts that let a writable workspace modify protected policy bytes.

## What Enso verifies

`enso policy check` verifies Enso's own launch plumbing:

- Referenced workspaces and access profiles exist and have valid structure.
- Provider files exist at the expected path, are protected regular files, and parse as JSON or TOML.
- The selected provider is enabled by the profile.
- Enso can resolve the native file and policy revision it expects to use for each routed workspace/profile pair.
- Required Enso launch safeguards, such as disabling Claude hooks, are present.

This is not semantic policy validation. A syntactically valid native rule can still grant too much, match the wrong path, rely on a feature unsupported by the installed CLI version, or expose authority through MCP and other tool surfaces. Enso must describe this command as a static plumbing check, never as proof that a workspace is sandboxed or safe.

Before using a restricted profile, test it with the installed provider CLI in a disposable workspace. At minimum, try an allowed read, allowed write if applicable, forbidden read, forbidden write, command execution, network access, environment-secret access, policy-file modification, and an approval/escalation attempt.

## Process boundary

Enso starts provider binaries directly, never through a shell. A policy-controlled child receives a small allowlisted environment containing basic process variables and only the credential needed by the active provider. Enso's transport tokens, 1Password token, database authority, and unrelated provider credentials must not be inherited.

Filtering `PATH` is useful friction but is not an isolation boundary: an agent may invoke an executable by absolute path or through an interpreter. The native filesystem/process policy or an outer sandbox must protect:

- `~/.enso/config.json`
- `~/.enso/secrets/`
- `~/.enso/policies/`
- `~/.enso/enso.db`
- service-control commands and the `enso` executable
- unrelated native provider homes and credentials

The policy must also account for every additional directory intentionally exposed to a staff profile, such as `~/.enso/workspaces/clients/**`.

## Claude Code

For a restricted profile Enso invokes Claude with its native settings file and non-interactive permission mode:

```text
claude -p --settings <policy-dir>/claude/settings.json \
  --permission-mode dontAsk --setting-sources project \
  --strict-mcp-config --model <model> -- <prompt>
```

The exact output and session flags vary by interactive operation, but the policy selection does not. Enso removes its unrestricted `--dangerously-skip-permissions` flag.

`dontAsk` is necessary because nobody can answer an interactive approval prompt inside a Slack turn. `--strict-mcp-config` keeps ambient MCP configuration out of the launch. `--setting-sources project` preserves the CLI's project-native instruction and skill discovery; Enso does not recreate that discovery.

A restricted Claude policy must set:

```json
{
  "disableAllHooks": true
}
```

Workspace hooks can execute outside Claude's ordinary tool permission flow, so a restricted route must not accept them. If the operator relies on Claude's command sandbox, enable and test it in the native file as well. Permission patterns and sandbox filesystem paths use provider-specific syntax; copy the [example](../examples/acme-claude-settings.json), then adapt and test it rather than translating a Codex policy by eye.

An access profile does not create instructions or copy skills. When Enso bootstraps a missing workspace, it writes a small `AGENTS.md` and a sibling `CLAUDE.md` symlink; after that, workspace instructions and native skill directories remain the operator's responsibility.

## Codex

For a restricted profile Enso stages the profile's Codex tree into a dedicated runtime `CODEX_HOME` and invokes:

```text
CODEX_HOME=<staged-home> codex exec --strict-config \
  --skip-git-repo-check [--ignore-rules] -m <model> -- <prompt>
```

The staged policy bytes and `.rules` files must match their protected source. Authentication material required by Codex is copied into the staged home; ambient user config and ambient rules are not selected. Enso removes its unrestricted `--dangerously-bypass-approvals-and-sandbox` flag.

`--skip-git-repo-check` bypasses a CLI usability check; it grants no filesystem authority. The native config must select a permission profile and set a non-interactive approval policy. Do not mix permission profiles with legacy `sandbox_mode` configuration: the installed Codex CLI decides which system is active, and mixing them can select a different boundary than intended.

Codex resolves some referenced configuration relative to the declaring config file. Use protected absolute paths for references that Enso does not stage, or keep the complete required source tree inside the profile's Codex directory. Verify the staged launch with the installed version; parsing TOML alone cannot establish that every referenced file or nested setting was accepted.

Codex discovers `AGENTS.md` from its starting project. Enso does not run `git init` in `~/.enso` to extend that search and does not turn the whole Enso state directory into a repository. A company workspace that needs client material should explicitly tell the agent where it lives and require the relevant protected instructions to be read.

Use the [Codex example](../examples/acme-codex-config.toml) only as a starting point. Provider schemas change; the installed CLI's help, diagnostics, and documentation are authoritative.

## Antigravity

Antigravity (`agy`) is currently available only in an explicitly unrestricted access profile. Enso does not yet have a tested native restricted-launch contract for it. This is a limitation of Enso's adapter, not a claim that the CLI has no permission features.

## Workspace instructions and skills

Project instructions and skills belong to the workspace. Codex uses `AGENTS.md` and `.agents/skills/`; Claude Code uses `CLAUDE.md` and `.claude/skills/`. Each CLI discovers those project files from the route's starting directory according to its own rules, while potentially also loading native user, managed, plugin, system, or bundled skill scopes. Enso does not suppress those scopes, expose a `skills` allowlist, or treat file discovery as a permission boundary. A restricted profile must remain safe even when the CLI knows that broader skills exist.

A restricted client route should not be able to edit instructions or skills later trusted by a broader staff route using the same workspace. Protect those control files with the native policy or an outer filesystem boundary. If both routes need to create content, put writable user data in a separate directory from control material.

When a company workspace has native access to sibling client directories, its own `AGENTS.md` should explicitly require reading the selected client's protected instructions before acting. Merely changing directories during a run does not make every provider rebuild its startup instruction chain.

## Failure behavior

A missing, unreadable, malformed, or structurally unsafe native policy disables that provider for the affected access profile. If it is the selected provider, the Slack turn receives a configuration error and no provider process starts. Enso never falls back to an unrestricted launch, another access profile, or another workspace.

Route, workspace, and access-profile configuration changes take effect after restarting Enso. Native policy files are checked again at the provider launch boundary, so a queued request uses the policy bytes available when it actually starts. Stop the service before coordinated or urgent permission changes so a running CLI process cannot outlive the change.
