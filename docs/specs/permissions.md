# Access profiles and native policies

An access profile defines what a Slack route or scheduled job may offer. It names providers, a default provider, permitted Enso chat commands, and either unrestricted execution or a directory of native provider policies.

Enso does not define a cross-provider permission language and does not merge user, channel, workspace, or job permissions. It selects one complete access profile, starts the installed CLI in the selected workspace, and supplies that CLI's native policy through its supported configuration mechanism.

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

The policy directory belongs to the access profile, not the workspace. This lets several client workspaces reuse one `client-readonly` profile while each CLI starts in the project directory selected by its route or job.

Policy directories must be absolute after expansion, outside every writable workspace and Telegram's global `working_dir`, and protected from all restricted agents. Policy files must be regular owner-only files rather than symlinks. Enso rejects aliases, hard links, and overlapping directory layouts that let a writable workspace modify protected policy bytes.

## What Enso verifies

`enso config check` verifies Enso's own configuration and launch plumbing:

- Referenced workspaces and access profiles exist and have valid structure.
- Provider files exist at the expected path, are protected regular files, and parse as JSON or TOML.
- The selected provider is enabled by the profile.
- Enso can resolve the native file and policy revision it expects to use for each routed or job-bound workspace/profile pair.
- Required Enso launch safeguards, such as disabling Claude hooks, are present.

This is not semantic policy validation. A syntactically valid native rule can still grant too much, match the wrong path, rely on a feature unsupported by the installed CLI version, or expose authority through MCP and other tool surfaces. Enso must describe this command as a static plumbing check, never as proof that a workspace is sandboxed or safe.

Before using a restricted profile, test it with the installed provider CLI in a disposable workspace. At minimum, try an allowed read, allowed write if applicable, forbidden read, forbidden write, command execution, network access, environment-secret access, policy-file modification, and an approval/escalation attempt. A profile that grants MCP servers needs two more checks, run from inside the restricted profile: (a) write a workspace `.mcp.json` and a workspace `.claude/settings.json` that tries to enable it (for example `enableAllProjectMcpServers`), take a **new turn**, and confirm no new servers appear — the workspace is agent-writable and `--setting-sources project` loads it, so this is the cross-turn self-escalation path to rule out; (b) confirm `${VAR}` references in the MCP config resolve from the launch environment (a passthrough variable works) and that an unlisted variable does not — in the current CLI an unresolvable reference reaches the server as the literal `${VAR}` text rather than an empty string. The operator owns the native policy's meaning and must repeat those checks after material CLI upgrades.

## Process boundary

Enso starts provider binaries directly, never through a shell. A policy-controlled child receives a small allowlisted environment containing basic process variables, only the credential needed by the active provider, and any variables the profile's `env_passthrough` explicitly names. Enso's transport tokens, 1Password token, database authority, and unrelated provider credentials must not be inherited.

Filtering `PATH` is useful friction but is not an isolation boundary: an agent may invoke an executable by absolute path or through an interpreter. The native filesystem/process policy or an outer sandbox must protect:

- `~/.enso/config.json`
- `~/.enso/secrets/`
- `~/.enso/policies/`
- `~/.enso/enso.db`
- service-control commands and the `enso` executable
- unrelated native provider homes and credentials

The allowlist covers only the provider process's own environment. Commands the agent then spawns usually run through the user's shell, which re-sources shell startup files — `~/.zshenv` on every invocation, `~/.zprofile` for login shells — so any secret exported there re-enters the child environment despite the allowlist. Keep credentials out of shell startup files; export them only where the service that launches Enso can see them.

The policy must also account for every additional directory intentionally exposed to a staff or automation profile, such as `~/.enso/workspaces/clients/**`.

## Claude Code

For a restricted profile Enso invokes Claude with its native settings file and non-interactive permission mode:

```text
claude -p --settings <policy-dir>/claude/settings.json \
  --permission-mode dontAsk --setting-sources project \
  --strict-mcp-config [--mcp-config <policy-dir>/claude/mcp.json] \
  --model <model> -- <prompt>
```

with `--mcp-config` appended exactly when the conventional file exists. The exact output and session flags vary by interactive operation, but the policy selection does not. Enso removes its unrestricted `--dangerously-skip-permissions` flag.

`dontAsk` is necessary because nobody can answer an interactive approval prompt inside a Slack turn. `--strict-mcp-config` is what makes the conventional file an exact allowlist: the launch loads only servers named by `--mcp-config` and ignores every other MCP configuration source, so ambient servers the operator configured for themselves stay out, and the profile's own `mcp.json` — when present — is passed explicitly beside it. With no `mcp.json`, that resolves to zero MCP servers. `--setting-sources project` preserves the CLI's project-native instruction and skill discovery; Enso does not recreate that discovery.

A restricted Claude policy must set:

```json
{
  "disableAllHooks": true
}
```

Workspace hooks can execute outside Claude's ordinary tool permission flow, so a restricted access profile must not accept them. If the operator relies on Claude's command sandbox, enable and test it in the native file as well. Permission patterns and sandbox filesystem paths use provider-specific syntax; copy the [example](../examples/acme-claude-settings.json), then adapt and test it rather than translating a Codex policy by eye.

Claude Code's behavior and schemas evolve independently of Enso. Review the official [permissions](https://code.claude.com/docs/en/permissions), [settings](https://code.claude.com/docs/en/settings), [tools reference](https://code.claude.com/docs/en/tools-reference), and [skills](https://code.claude.com/docs/en/skills) documentation for the installed version. Those sources describe available modes, settings precedence, tool surfaces, and skill scopes; Enso deliberately does not reproduce or reinterpret them.

### Writing a restricted Claude policy

Under `--permission-mode dontAsk`, unmatched tool calls default to deny for Bash, Write, and Edit but to allow for Read. A `deny` list therefore blocks only what it names: the Read tool can still reach any path neither the list nor the sandbox covers, and a rule that is mistyped, uses the wrong path form (absolute paths need the `//` prefix), or is written with the wrong JSON shape (for example a string where a list is expected) is silently ignored by `claude -p` — a plumbing check cannot see that. Prefer a fail-closed shape: `"deny": ["*"]` for a chat-only route, where the agent keeps no tools and answers only from the conversation Enso injects; or, to allow workspace reads, confine them with both `permissions` deny rules and `sandbox.filesystem.denyRead`, which constrains the Read tool as well as spawned commands. Confirm the result by attempting a forbidden read, never by assuming the file loaded.

Because `--setting-sources project` also loads the workspace's own `.claude/settings.json`, treat that file as attacker-influenced. A `permissions` deny there cannot widen the launch (deny always wins), but a scalar such as `sandbox.enabled: false` can turn the sandbox off on the next launch. Any profile that grants writes must therefore deny writes to the control files a stricter profile trusts — `.claude/**`, `.codex/**`, `AGENTS.md`, `CLAUDE.md`, and skill directories. A Claude `deny` on those paths blocks the Write and Edit tools and Bash redirection to them; a read-only profile that grants no write tool cannot plant them at all.

An access profile does not create instructions or copy skills. When Enso bootstraps a missing workspace, it writes a small `AGENTS.md` and a sibling `CLAUDE.md` symlink; after that, workspace instructions and native skill directories remain the operator's responsibility.

### Granting credentials and MCP servers to a restricted profile

A restricted launch deliberately withholds the operator's environment and ambient MCP configuration. When a restricted route legitimately needs one credential or one internal MCP server, two narrow grants exist so that `unrestricted: true` — which discards the entire privilege boundary for the route — is never the answer:

- `env_passthrough` on the access profile names environment variables (names, never values) to copy into the otherwise fixed child environment. It is provider-neutral and applies identically to every policy-controlled provider.
- The conventional file `<policy-dir>/claude/mcp.json`, sibling to `settings.json`, declares the profile's exact Claude MCP server set. There is no new config key: the file present means those servers and only those; the file absent means zero servers. Codex needs no Enso mechanism — it declares MCP servers natively in the profile's `codex/config.toml`, which is already staged, hashed, and integrity-checked; the environment half still comes from `env_passthrough`, and how Codex forwards environment to its MCP server processes is native behavior the operator owns and verifies.

Grant nothing by default. If the profile does not need a credential, do not pass one through; if it does not need an MCP server, leave the file absent. Every passed-through variable is readable by the profile, and every declared server is reachable from it.

**Keep secrets out of the file.** The two halves are designed to pair: `mcp.json` carries `${VAR}` references only, so it stays a committable, reviewable artifact, while values arrive at runtime through the service environment — drop `METRICS_API_TOKEN=...` into a `~/.enso/secrets/*.env` file, which `enso serve` loads at startup, then name the variable in the profile. For stdio servers, `${VAR}` references in the server's `env` block resolve against the same minimal-plus-passthrough launch environment, and the server's `command` resolves against the filtered `PATH`, so it must be on that PATH or absolute.

A worked example. The profile:

```jsonc
"reporting": {
  "policy_dir": "~/.enso/policies/reporting",
  "providers": ["claude"],
  "default_provider": "claude",
  "chat_commands": ["status", "clear", "stop", "help"],

  // names, never values
  "env_passthrough": ["METRICS_API_TOKEN"]
}
```

The server file, `~/.enso/policies/reporting/claude/mcp.json` — a protected owner-only regular file like every other policy file:

```jsonc
{
  "mcpServers": {
    "metrics": {
      "type": "http",
      "url": "https://metrics.internal.example/mcp",
      "headers": { "Authorization": "${METRICS_API_TOKEN}" }
    }
  }
}
```

And permission rules in the same profile's `settings.json`, because under `dontAsk` a declared server that no allow rule references has every tool denied (deny or ask references cannot admit a tool):

```json
"permissions": {
  "allow": [
    "mcp__metrics__query_series",
    "mcp__metrics__list_dashboards"
  ]
}
```

`mcp__<server>` covers a server's entire tool surface; `mcp__<server>__<tool>` covers one tool. The [example MCP file](../examples/acme-claude-mcp.json) is a copyable starting point.

Validation and failure semantics:

- `env_passthrough` names must match `[A-Z][A-Z0-9_]*`, contain no duplicates, and must not name launch-controlled or Enso-owned variables (`HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`, `USER`, `SHELL`, `PATH`, `CODEX_HOME`, or anything `ENSO_`-prefixed). Neither grant is meaningful on an unrestricted profile: `env_passthrough` there is a config error — the profile already inherits the full environment, and silently accepting the key would let the operator believe they scoped something — and an unrestricted profile has no policy directory to hold the conventional file.
- `mcp.json` fails closed. Present but unusable — an integrity failure, unparseable JSON, not a JSON object, a missing or non-object `mcpServers`, or an empty `mcpServers` (delete the file to disable MCP) — refuses the turn as a policy error rather than silently launching with fewer servers. A symlink at the conventional path is an integrity error, never "absent". The file is hashed into `policy_revision`, so adding, editing, or removing it rotates the revision and audit rows describe the server set each turn actually had.
- A configured passthrough name absent from the service environment warns at spawn (names only, never values) and the turn proceeds: environment presence varies by deployment, and the downstream failure — the MCP server rejects authentication — is contained and attributable. Note the shape of that failure: the current Claude CLI passes an unresolvable `${VAR}` reference to the server as the literal text `${VAR}`, not as an empty string, so a server-side "invalid credential" with that literal is the signature of a missing passthrough variable.

Verify what a profile actually has with `enso config check`. It prints each policy-controlled profile's passthrough names with a resolvability mark (checked against the invoking shell plus `~/.enso/secrets/*.env`; the service environment may differ), lists the resolved server names on each Claude native-launch line (`✓ claude (a1b2c3d4e5f6) mcp: metrics`), and warns when a `mcp__` permission rule matches no declared server (the rule can never apply — with no `mcp.json`, every `mcp__` rule is inert) or when a declared server is referenced by no allow rule (every tool on it denied under `dontAsk`; deny or ask references cannot make it usable). It also warns when a header or `env` value in `mcp.json` has a credential-shaped key (`auth`, `token`, `secret`, `key`, `password`, `bearer`) and a literal value containing no `${` reference — precisely the anti-pattern the pairing exists to avoid. As everywhere else, this is a plumbing check, not proof that the grant is safe.

Two security properties to hold in mind when granting:

- **MCP servers bypass the sandbox's network rules.** Servers are dialled by the provider process itself, not by sandboxed Bash, so an allowlisted server is reachable regardless of what the OS sandbox permits on the network. Grant only servers whose *entire* tool surface is acceptable for the profile, then narrow within it using `mcp__<server>__<tool>` permission rules.
- **Passthrough is a delivery mechanism, not a confidentiality one.** If the profile can run Bash at all, the agent can read its own environment; `env_passthrough` hands the profile the real value. Prefer narrowly-scoped, read-only tokens, and where the deployment supports it, a credential mask or egress proxy so the child sees a sentinel.

## Codex

For a restricted profile Enso stages the profile's Codex tree into a dedicated runtime `CODEX_HOME` and invokes:

```text
CODEX_HOME=<staged-home> codex exec --strict-config \
  --skip-git-repo-check [--ignore-rules] -m <model> -- <prompt>
```

The staged policy bytes and protected `.rules` files must match their protected source. Authentication material required by Codex is copied into the staged home; ambient user config is not selected. Enso removes its unrestricted `--dangerously-bypass-approvals-and-sandbox` flag.

`--skip-git-repo-check` bypasses a CLI usability check; it grants no filesystem authority. The native config must select a permission profile and set a non-interactive approval policy. Do not mix permission profiles with legacy `sandbox_mode` configuration: the installed Codex CLI decides which system is active, and mixing them can select a different boundary than intended.

Codex resolves some referenced configuration relative to the declaring config file. Use protected absolute paths for references that Enso does not stage, or keep the complete required source tree inside the profile's Codex directory. If the protected profile contains no `.rules`, Enso uses `--ignore-rules`; if it contains rules, project-discovered rules may still compose according to the installed CLI's native behavior. Verify the staged launch with the installed version; parsing TOML alone cannot establish that every referenced file or nested setting was accepted.

Codex discovers `AGENTS.md` from its starting project. Enso does not run `git init` in `~/.enso` to extend that search and does not turn the whole Enso state directory into a repository. A company workspace that needs client material should explicitly tell the agent where it lives and require the relevant protected instructions to be read.

Use the [Codex example](../examples/acme-codex-config.toml) only as a starting point. Provider schemas change; the installed CLI's help, diagnostics, and documentation are authoritative.

## Antigravity

Antigravity (`agy`) is currently available only in an explicitly unrestricted access profile. Enso does not yet have a tested native restricted-launch contract for it. This is a limitation of Enso's adapter, not a claim that the CLI has no permission features.

## Workspace instructions and skills

Project instructions and skills belong to the workspace. Codex uses `AGENTS.md` and `.agents/skills/`; Claude Code uses `CLAUDE.md` and `.claude/skills/`. Each CLI discovers those project files from the route or job's starting directory according to its own rules, while potentially also loading native user, managed, plugin, system, or bundled skill scopes. Enso does not suppress those scopes, expose a `skills` allowlist, or treat file discovery as a permission boundary. A restricted profile must remain safe even when the CLI knows that broader skills exist.

A restricted client route or job should not be able to edit instructions or skills later trusted by a broader route using the same workspace. Protect those control files with the native policy or an outer filesystem boundary. If both profiles need to create content, put writable user data in a separate directory from control material.

For Slack routes, Enso stores downloaded chat attachments in persistent `uploads/<random-id>/` directories under the selected workspace. These are ordinary retained workspace files, not temporary policy state. Native policy governs whether the provider can read or modify them, and the operator owns retention. Telegram does not use access profiles and stores downloads directly in `uploads/` under its global `working_dir`.

## Enso commands versus provider capabilities

An access profile's `chat_commands` field controls only Enso transport commands such as `!status`, `!clear`, `!stop`, and `!compact`. It does not control provider-native tools, slash commands, skills, plugins, hooks, MCP servers, or settings scopes. Most local Enso commands do not launch a provider; `!compact` does and therefore runs under the selected access profile's native policy.

When a company workspace has native access to sibling client directories, its own `AGENTS.md` should explicitly require reading the selected client's protected instructions before acting. Merely changing directories during a run does not make every provider rebuild its startup instruction chain.

## Failure behavior

A missing, unreadable, malformed, or structurally unsafe native policy disables that provider for the affected access profile. If it is selected by a Slack turn, the turn receives a configuration error and no provider process starts. If selected by a job, the job fails before prerun or provider execution and records/notifies that failure through the normal job path. Enso never falls back to an unrestricted launch, another access profile, another workspace, or the global `working_dir`.

Route, workspace, and access-profile changes live in `config.json` and require restarting Enso. `JOB.md` files are reloaded by the scheduler and by manual runs. Native policy files are checked again at the provider launch boundary, so a queued request uses the policy bytes available when it actually starts. Stop the service before coordinated or urgent permission changes so a running CLI process cannot outlive the change.
