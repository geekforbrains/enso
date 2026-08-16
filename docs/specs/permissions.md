# Enso policies and native provider policies

An Enso policy defines what a workspace may offer. It names providers, a default provider, permitted Enso chat commands, and either unrestricted execution or a directory of native provider policies. Every workspace selects exactly one policy; Telegram, Slack routes, and scheduled jobs select only a workspace and derive its policy.

Enso does not define a cross-provider permission language and does not merge user, channel, workspace, or job permissions. It resolves the workspace's one complete policy, starts the installed CLI in that workspace, and supplies the CLI's native policy through its supported configuration mechanism.

## Configuration boundary

```jsonc
{
  "workspaces": {
    "acme": {
      "path": "~/.enso/workspaces/acme",
      "policy": "client-readonly"
    }
  },
  "policies": {
    "admin": {
      "unrestricted": true,
      "providers": ["claude", "codex", "grok", "agy"],
      "default_provider": "claude",
      "chat_commands": "*"
    },
    "client-readonly": {
      "policy_dir": "~/.enso/policies/client-readonly",
      "providers": ["claude", "codex", "grok"],
      "default_provider": "claude",
      "chat_commands": ["status", "clear", "stop", "help"]
    }
  }
}
```

`unrestricted: true` and `policy_dir` are mutually exclusive. Unrestricted execution retains Enso's existing bypass invocation and should be used only by trusted administrative workspaces. For a restricted policy, `policy_dir` defaults to `~/.enso/policies/<policy-name>`. The directory must supply the native file for every enabled provider:

```text
~/.enso/policies/client-readonly/
├── claude/settings.json
├── codex/
│   ├── config.toml
│   └── rules/*.rules
└── grok/config.toml
```

The policy directory belongs to the policy, not the workspace. This lets several client workspaces reuse one `client-readonly` policy while each CLI starts in the project directory selected by its route or job.

Policy directories must be absolute after expansion, outside every writable workspace, and protected from all restricted agents. Policy files must be regular owner-only files rather than symlinks. Enso rejects aliases, hard links, and overlapping directory layouts that let a writable workspace modify protected policy bytes.

## What Enso verifies

`enso config check` verifies Enso's own configuration and launch plumbing:

- Referenced workspaces and policies exist and have valid structure.
- The canonical shared instruction source exists, is an owner-owned regular non-symlink file with one hard link and no group/other write bits, passes encoding and size checks, and can be snapshotted for launch.
- Provider files exist at the expected path, are protected regular files, and parse as JSON or TOML.
- The selected provider is enabled by the workspace's policy.
- Enso can resolve the native file and policy revision it expects to use for every transport-, route-, or job-bound workspace and its policy.
- Required Enso launch safeguards, such as disabling Claude hooks, are present.

This is not semantic policy validation. A syntactically valid native rule can still grant too much, match the wrong path, rely on a feature unsupported by the installed CLI version, or expose authority through MCP and other tool surfaces. Enso must describe this command as a static plumbing check, never as proof that a workspace is sandboxed or safe.

Before using a restricted policy, test it with the installed provider CLI in a disposable workspace. At minimum, try an allowed read, allowed write if applicable, forbidden read, forbidden write, command execution, network access, environment-secret access, policy-file modification, and an approval/escalation attempt. A policy that grants MCP servers needs two more checks, run from inside a workspace bound to that policy: (a) write a workspace `.mcp.json` and a workspace `.claude/settings.json` that tries to enable it (for example `enableAllProjectMcpServers`), take a **new turn**, and confirm no new servers appear — the workspace is agent-writable and `--setting-sources project` loads it, so this is the cross-turn self-escalation path to rule out; (b) confirm `${VAR}` references in the MCP config resolve from the launch environment (a passthrough variable works) and that an unlisted variable does not — in the current CLI an unresolvable reference reaches the server as the literal `${VAR}` text rather than an empty string. The operator owns the native policy's meaning and must repeat those checks after material CLI upgrades.

## Process boundary

Enso starts provider binaries directly, never through a shell. A policy-controlled child receives a small allowlisted environment containing basic process variables, only the credential needed by the active provider, and any variables the policy's `env_passthrough` explicitly names. Enso's transport tokens, 1Password token, database authority, and unrelated provider credentials must not be inherited.

Filtering `PATH` is useful friction but is not an isolation boundary: an agent may invoke an executable by absolute path or through an interpreter. The native filesystem/process policy or an outer sandbox must protect:

- `~/.enso/config.json`
- `~/.enso/secrets/`
- `~/.enso/policies/`
- `~/.enso/enso.db`
- service-control commands and the `enso` executable
- unrelated native provider homes and credentials

The allowlist covers only the provider process's own environment. Commands the agent then spawns usually run through the user's shell, which re-sources shell startup files — `~/.zshenv` on every invocation, `~/.zprofile` for login shells — so any secret exported there re-enters the child environment despite the allowlist. Keep credentials out of shell startup files; export them only where the service that launches Enso can see them.

The policy must also account for every additional directory intentionally exposed to a staff or automation workspace, such as `~/.enso/workspaces/clients/**`.

## Claude Code

For a restricted policy Enso invokes Claude with its native settings file and non-interactive permission mode:

```text
claude -p --settings <policy-dir>/claude/settings.json \
  --permission-mode dontAsk --setting-sources project \
  --strict-mcp-config [--mcp-config <policy-dir>/claude/mcp.json] \
  --append-system-prompt-file ~/.enso/runtime/instructions/<sha256>.md \
  --model <model> -- <prompt>
```

with `--mcp-config` appended exactly when the conventional file exists. The exact output and session flags vary by interactive operation, but the policy selection does not. Enso removes its unrestricted `--dangerously-skip-permissions` flag. The shared-instruction flag is also present on unrestricted Claude launches; it is independent of policy mode.

`dontAsk` is necessary because nobody can answer an interactive approval prompt in a headless transport turn or job. `--strict-mcp-config` is what makes the conventional file an exact allowlist: the launch loads only servers named by `--mcp-config` and ignores every other MCP configuration source, so ambient servers the operator configured for themselves stay out, and the policy's own `mcp.json` — when present — is passed explicitly beside it. With no `mcp.json`, that resolves to zero MCP servers. `--append-system-prompt-file` injects an immutable snapshot of Enso's canonical shared instructions independently of cwd, while `--setting-sources project` preserves the CLI's workspace-local instruction and skill discovery.

A restricted Claude policy must set:

```json
{
  "disableAllHooks": true
}
```

Workspace hooks can execute outside Claude's ordinary tool permission flow, so a restricted policy must not accept them. If the operator relies on Claude's command sandbox, enable and test it in the native file as well. Permission patterns and sandbox filesystem paths use provider-specific syntax; copy the [example](../examples/acme-claude-settings.json), then adapt and test it rather than translating a Codex policy by eye.

Claude Code's behavior and schemas evolve independently of Enso. Review the official [permissions](https://code.claude.com/docs/en/permissions), [settings](https://code.claude.com/docs/en/settings), [tools reference](https://code.claude.com/docs/en/tools-reference), and [skills](https://code.claude.com/docs/en/skills) documentation for the installed version. Those sources describe available modes, settings precedence, tool surfaces, and skill scopes; Enso deliberately does not reproduce or reinterpret them.

### Writing a restricted Claude policy

Under `--permission-mode dontAsk`, unmatched tool calls default to deny for Bash, Write, and Edit but to allow for Read. A `deny` list therefore blocks only what it names: the Read tool can still reach any path neither the list nor the sandbox covers, and a rule that is mistyped, uses the wrong path form (absolute paths need the `//` prefix), or is written with the wrong JSON shape (for example a string where a list is expected) is silently ignored by `claude -p` — a plumbing check cannot see that. Prefer a fail-closed shape: `"deny": ["*"]` for a chat-only route, where the agent keeps no tools and answers only from the conversation Enso injects; or, to allow workspace reads, confine them with both `permissions` deny rules and `sandbox.filesystem.denyRead`, which constrains the Read tool as well as spawned commands. Confirm the result by attempting a forbidden read, never by assuming the file loaded.

Because `--setting-sources project` also loads the workspace's own `.claude/settings.json`, treat that file as attacker-influenced. A `permissions` deny there cannot widen the launch (deny always wins), but a scalar such as `sandbox.enabled: false` can turn the sandbox off on the next launch. Any policy that grants writes must therefore deny writes to the control files that policy trusts — `.claude/**`, `.codex/**`, `AGENTS.md`, `CLAUDE.md`, and skill directories. A Claude `deny` on those paths blocks the Write and Edit tools and Bash redirection to them; a read-only policy that grants no write tool cannot plant them at all.

A policy does not create instructions or copy skills. Enso maintains canonical shared instructions at `~/.enso/AGENTS.md` and injects them on every launch. When Enso bootstraps a missing workspace, it writes a small focused local `AGENTS.md` and sibling `CLAUDE.md` symlink; after that, customized workspace instructions and native skill directories remain the operator's responsibility.

### Granting credentials and MCP servers to a restricted policy

A restricted launch deliberately withholds the operator's environment and ambient MCP configuration. When a restricted workspace legitimately needs one credential or one internal MCP server, two narrow grants exist so that `unrestricted: true` — which discards the entire privilege boundary for the workspace — is never the answer:

- `env_passthrough` on the policy names environment variables (names, never values) to copy into the otherwise fixed child environment. It is provider-neutral and applies identically to every policy-controlled provider.
- The conventional file `<policy-dir>/claude/mcp.json`, sibling to `settings.json`, declares the policy's exact Claude MCP server set. There is no new config key: the file present means those servers and only those; the file absent means zero servers. Codex needs no Enso mechanism — it declares MCP servers natively in the policy's `codex/config.toml`, which is already staged, hashed, and integrity-checked; the environment half still comes from `env_passthrough`, and how Codex forwards environment to its MCP server processes is native behavior the operator owns and verifies.

Grant nothing by default. If the policy does not need a credential, do not pass one through; if it does not need an MCP server, leave the file absent. Every passed-through variable is readable by a process launched under the policy, and every declared server is reachable from it.

**Keep secrets out of the file.** The two halves are designed to pair: `mcp.json` carries `${VAR}` references only, so it stays a committable, reviewable artifact, while values arrive at runtime through the service environment — drop `METRICS_API_TOKEN=...` into a `~/.enso/secrets/*.env` file, which `enso serve` loads at startup, then name the variable in the policy. For stdio servers, `${VAR}` references in the server's `env` block resolve against the same minimal-plus-passthrough launch environment, and the server's `command` resolves against the filtered `PATH`, so it must be on that PATH or absolute.

A worked example. The policy:

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

And permission rules in the same policy's `settings.json`, because under `dontAsk` a declared server that no allow rule references has every tool denied (deny or ask references cannot admit a tool):

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

- `env_passthrough` names must match `[A-Z][A-Z0-9_]*`, contain no duplicates, and must not name launch-controlled or Enso-owned variables (`HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TERM`, `TMPDIR`, `USER`, `SHELL`, `PATH`, `CODEX_HOME`, `GROK_HOME`, `GROK_SANDBOX`, `GROK_FOLDER_TRUST`, or anything `ENSO_`-prefixed). Neither grant is meaningful on an unrestricted policy: `env_passthrough` there is a config error — the policy already inherits the full environment, and silently accepting the key would let the operator believe they scoped something — and an unrestricted policy has no policy directory to hold the conventional file.
- `mcp.json` fails closed. Present but unusable — an integrity failure, unparseable JSON, not a JSON object, a missing or non-object `mcpServers`, or an empty `mcpServers` (delete the file to disable MCP) — refuses the turn as a policy error rather than silently launching with fewer servers. A symlink at the conventional path is an integrity error, never "absent". The file is hashed into `policy_revision`, so adding, editing, or removing it rotates the revision and audit rows describe the server set each turn actually had.
- A configured passthrough name absent from the service environment warns at spawn (names only, never values) and the turn proceeds: environment presence varies by deployment, and the downstream failure — the MCP server rejects authentication — is contained and attributable. Note the shape of that failure: the current Claude CLI passes an unresolvable `${VAR}` reference to the server as the literal text `${VAR}`, not as an empty string, so a server-side "invalid credential" with that literal is the signature of a missing passthrough variable.

Verify what a policy actually has with `enso config check`. It prints each restricted policy's passthrough names with a resolvability mark (checked against the invoking shell plus `~/.enso/secrets/*.env`; the service environment may differ), lists the resolved server names on each Claude native-launch line (`✓ claude (a1b2c3d4e5f6) mcp: metrics`), and warns when a `mcp__` permission rule matches no declared server (the rule can never apply — with no `mcp.json`, every `mcp__` rule is inert) or when a declared server is referenced by no allow rule (every tool on it denied under `dontAsk`; deny or ask references cannot make it usable). It also warns when a header or `env` value in `mcp.json` has a credential-shaped key (`auth`, `token`, `secret`, `key`, `password`, `bearer`) and a literal value containing no `${` reference — precisely the anti-pattern the pairing exists to avoid. As everywhere else, this is a plumbing check, not proof that the grant is safe.

Two security properties to hold in mind when granting:

- **MCP servers bypass the sandbox's network rules.** Servers are dialled by the provider process itself, not by sandboxed Bash, so an allowlisted server is reachable regardless of what the OS sandbox permits on the network. Grant only servers whose *entire* tool surface is acceptable for the policy, then narrow within it using `mcp__<server>__<tool>` permission rules.
- **Passthrough is a delivery mechanism, not a confidentiality one.** If the policy permits Bash at all, the agent can read its own environment; `env_passthrough` hands the process the real value. Prefer narrowly-scoped, read-only tokens, and where the deployment supports it, a credential mask or egress proxy so the child sees a sentinel.

## Codex

For a restricted policy Enso stages the policy's Codex tree into a dedicated runtime `CODEX_HOME` and invokes:

```text
CODEX_HOME=<staged-home> codex exec --strict-config \
  --skip-git-repo-check [--ignore-rules] \
  -c developer_instructions=<TOML-encoded-shared-instructions> \
  -m <model> -- <prompt>
```

The staged policy bytes and protected `.rules` files must match their protected source. Authentication material required by Codex is copied into the staged home; ambient user config is not selected. Enso safely TOML-encodes the validated shared text in the command-line override. It removes its unrestricted `--dangerously-bypass-approvals-and-sandbox` flag. The `developer_instructions` injection applies to restricted and unrestricted Codex launches alike.

`--skip-git-repo-check` bypasses a CLI usability check; it grants no filesystem authority. The native config must select a permission profile and set a non-interactive approval policy. Do not mix permission profiles with legacy `sandbox_mode` configuration: the installed Codex CLI decides which system is active, and mixing them can select a different boundary than intended.

Codex resolves some referenced configuration relative to the declaring config file. Use protected absolute paths for references that Enso does not stage, or keep the complete required source tree inside the policy's Codex directory. The top-level `developer_instructions` key is reserved for Enso's shared injection and is rejected in a restricted policy's `config.toml`; put policy-specific project guidance in the workspace-local `AGENTS.md`. If the protected policy contains no `.rules`, Enso uses `--ignore-rules`; if it contains rules, project-discovered rules may still compose according to the installed CLI's native behavior. Verify the staged launch with the installed version; parsing TOML alone cannot establish that every referenced file or nested setting was accepted.

Enso supplies the validated in-memory contents of `~/.enso/AGENTS.md` as Codex `developer_instructions` on every launch. Codex separately discovers the focused local `AGENTS.md` from its starting workspace. Enso does not run `git init` in `~/.enso` to extend native project search and does not turn the whole Enso state directory into a repository. A company workspace that needs client material should explicitly tell the agent where it lives and require the relevant protected instructions to be read.

Use the [Codex example](../examples/acme-codex-config.toml) only as a starting point. Provider schemas change; the installed CLI's help, diagnostics, and documentation are authoritative.

## Grok

For a restricted policy Enso stages the policy's Grok tree into a dedicated runtime `GROK_HOME` and invokes:

```text
GROK_HOME=<staged-home> grok --output-format ... \
  --permission-mode dontAsk --rules=<shared-instructions> \
  --model <model> --single=<prompt>
```

The exact output and session flags vary by interactive operation, but the policy selection does not. `--single=<prompt>` and `--rules=<shared-instructions>` each ride as one attached argument, so content beginning with `-` — a hyphen-leading prompt, a markdown bullet or frontmatter opening the instructions — cannot be parsed as a flag. Enso removes its unrestricted `--always-approve` flag. The `--rules` flag is reserved for Enso's validated shared instructions — Grok receives the validated content itself, not a snapshot path — and the injection applies to restricted and unrestricted Grok launches alike.

The staged home is revision-keyed under the policy's `.runtime/grok-home` and holds owner-read-only (`0400`) copies of the policy's `config.toml` and of the operator's Grok `auth.json`, which is refreshed from the real Grok home on every launch. Enso pre-seeds the CLI's marketplace stanza — `[marketplace]` plus the official `[[marketplace.sources]]` entry — into the staged `config.toml` before hashing it, because the CLI rewrites its `config.toml` after a run to append exactly that stanza and `0400` does not stop it: the CLI replaces the file by rename, so the mode survives but the bytes would not. Seeding the stanza at staging time keeps the staged bytes identical across runs, so the manifest's byte verification passes on every subsequent launch. A policy `config.toml` that declares its own `[marketplace]` is staged as written and flagged by `enso config check`, because the CLI's write-back would then change the staged bytes and the next launch would fail closed on verification.

`dontAsk` is necessary because nobody can answer an interactive approval prompt in a headless transport turn or job. All permission rules come from the staged `config.toml`; the launch passes no rule flags.

**Grok fails open on a malformed permission config.** A wrong-shaped `[permission]` table — a `[permissions]` typo, an `allowed` key, a rule entry in an unrecognized form (lowercase `toolname(glob)` entries are dropped; the loadable families are bare tool names and the documented capitalized `ToolPrefix(glob)` forms), or empty rule arrays — loads zero rules with no error, no non-zero exit, and an empty `skipped` list, and the launch then runs on the permission mode's defaults alone. Enso rejects a policy that declares no rules statically, and `grok inspect --json`, which reports `permissions.loaded` and the contributing `sources`, is the visibility mechanism for the rest: `enso config check` gates every grok policy binding dynamically by staging the home exactly as a launch would, running `grok inspect --json` from the workspace with the staged `GROK_HOME` and a scratch `HOME` — the operator's own always-trusted `~/.claude` rules would otherwise count into the total, false-failing the equality or masking a dropped rule — and requiring the reported `permissions.loaded` count to equal the number of rules the policy file declares. A mismatch, a grok policy that declares no rules, or a missing grok binary fails the check for that binding.

Folder trust in Grok is a kill-switch that only loosens. A fresh staged home leaves the workspace project untrusted, so a workspace-planted `.grok/config.toml` or vendor-compat settings file contributes no rules, hooks, or MCP servers. Disabling folder trust — `GROK_FOLDER_TRUST=0` in the environment or `[folder_trust] enabled = false` in config — inverts that gate and admits project-level hooks, MCP, and config. That matters more here than for the other providers: a workspace is agent-writable, so an ungated project config is a policy that can widen itself.

All three routes to undoing that trust are closed. Enso never sets `GROK_FOLDER_TRUST` and reserves it from `env_passthrough` together with `GROK_HOME` and `GROK_SANDBOX`, the launch-owned variables that select the staged home and a kernel sandbox profile. A policy `config.toml` may not carry a `[folder_trust]` table at all — only an explicit `enabled = true`, which restates the default and grants nothing, is accepted. And the policy's `grok/` tree may not contain `trusted_folders.toml`, the file the CLI reads trust from inside `GROK_HOME`, so a staged home never carries pre-granted trust. `enso config check` reports each of these as a problem for that binding. The dynamic `permissions.loaded` gate backs them up from the other direction: loading *fewer* rules than the policy declares means rules were silently dropped, and loading *more* means rules reached the launch from outside the policy — the check fails either way, and names the contributing sources in the second case.

**Known limitation — a Grok policy launch is not hermetic against the operator's own home configuration.** Grok discovers home-scope vendor-compat sources (`~/.claude/*`, `~/.cursor/*`) relative to `$HOME`, not `GROK_HOME`; it treats home-scope sources as always trusted, with no folder-trust gate; and `HOME` passes through Enso's minimal launch environment for Grok exactly as it does for Claude and Codex policy launches. A restricted Grok agent can therefore see instructions, skills, permission rules, and MCP servers from the operator's own `~/.claude` or `~/.cursor` configuration — home-scope MCP servers are dialled by the provider process itself — and `[compat.claude]` overrides in the policy config did not close that path in the tested CLI. A policy that must not reach ambient MCP tools should carry a bare `MCPTool` deny rule, as the example does; keep the operator's home vendor configuration as clean as the policy assumes, and retest after CLI upgrades.

Grok also has kernel-enforced sandbox profiles (`--sandbox workspace|devbox|read-only|strict`, Seatbelt on macOS and Landlock on Linux) whose filesystem rules hold even under permission bypass. They are not part of Enso's launch contract today: Enso passes no `--sandbox` flag, and a policy that wants one configures it natively in its `config.toml` and tests it with the installed CLI. Note that `/tmp`, `/var/tmp`, and the grok home remain writable in every profile, so a workspace under `/tmp` gains no read-only protection from the sandbox.

Grok sessions are stored under the launch's `GROK_HOME` and also sync to a remote xAI session registry. Clearing a session removes the local session state; a remote copy may survive in xAI's registry, and resuming an unknown session ID triggers a remote restore attempt. Treat conversation content in a Grok turn as data that leaves the machine.

Use the [Grok example](../examples/acme-grok-config.toml) only as a starting point. Provider schemas change; the installed CLI's help, diagnostics, and documentation are authoritative.

## Antigravity

Antigravity (`agy`) is currently available only in an explicitly unrestricted policy. Enso does not yet have a tested native restricted-launch contract for it. For an allowed launch, Enso places the canonical shared instructions in a clearly delimited prompt envelope before the request; workspace-local context remains available through Agy's project binding. This is a limitation of Enso's adapter, not a claim that the CLI has no permission features.

## Workspace instructions and skills

Instructions have two layers. Canonical shared Enso workflow lives at `~/.enso/AGENTS.md`, with a sibling `CLAUDE.md` symlink, and is injected explicitly on every provider launch. Focused project instructions and skills belong to the workspace: Codex uses local `AGENTS.md` and `.agents/skills/`; Claude Code uses local `CLAUDE.md` and `.claude/skills/`. Each CLI discovers those local files from the selected workspace according to its own rules, while potentially also loading native user, managed, plugin, system, or bundled skill scopes. Enso does not suppress those scopes, expose a `skills` allowlist, or treat file discovery as a permission boundary. A restricted policy must remain safe even when the CLI knows that broader skills exist.

The shared instructions declare the active policy authoritative and classify quoted, forwarded, fetched, attached, and other untrusted transport content as data rather than higher-priority instructions. Neither shared nor local instructions can widen the native policy. The canonical source must be a stable, owner-owned regular non-symlink file with no additional hard links or group/other write bits, valid UTF-8 no larger than 20 KiB, and free of NUL bytes. At launch Enso validates it, hashes it, and publishes or verifies the exact owner-only snapshot Claude consumes; Codex, Grok, and Agy receive the same validated content in memory. Protect `~/.enso/AGENTS.md` as operator-owned control material, and keep local instructions focused so changing a workspace does not silently change Enso-wide behavior.

A restricted policy should not let one workspace edit instructions or skills in another workspace that is later trusted under a more permissive policy. Protect those control files with the native policy or an outer filesystem boundary. If both workspaces need to create content, put writable user data in a separate directory from control material.

For Telegram and Slack turns, Enso stores downloaded chat attachments in persistent `uploads/<random-id>/` directories under the selected workspace. These are ordinary retained workspace files, not temporary policy state. The workspace's native policy governs whether the provider can read or modify them, and the operator owns retention.

## Enso commands versus provider capabilities

A policy's `chat_commands` field controls only Enso transport commands such as Slack `!status` or Telegram `/status`, `/clear`, `/stop`, and `/compact`. Telegram registers and authorizes only allowed commands; Slack resolves the same list from the route's workspace. It does not control provider-native tools, slash commands, skills, plugins, hooks, MCP servers, or settings scopes. Most local Enso commands do not launch a provider; compaction does and therefore runs under the workspace's policy.

When a company workspace has native access to sibling client directories, its own `AGENTS.md` should explicitly require reading the selected client's protected instructions before acting. Merely changing directories during a run does not make every provider rebuild its startup instruction chain.

## Failure behavior

A missing, unreadable, malformed, or structurally unsafe native policy disables that provider for the affected policy. A missing or unsafe canonical shared instruction source likewise fails `enso config check` and every provider launch closed. If either is selected by a Telegram or Slack turn, the turn receives a configuration error and no provider process starts. If selected by a job, the job fails before prerun or provider execution and records/notifies that failure through the normal job path. Enso never falls back to an unrestricted launch, another policy, another workspace, an implicit cwd, or an unvalidated prompt.

Route, workspace, and policy changes live in `config.json` and require restarting Enso. `JOB.md` files are reloaded by the scheduler and by manual runs. Native policy files are checked again at the provider launch boundary, so a queued request uses the policy bytes available when it actually starts. Stop the service before coordinated or urgent permission changes so a running CLI process cannot outlive the change.
