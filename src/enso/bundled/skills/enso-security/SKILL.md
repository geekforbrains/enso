---
name: enso-security
description: "Make an Enso workspace restricted: write the provider CLI's own policy file, add the provider args it needs, set the restricted flag, and audit it. Use when someone asks to restrict, lock down, or sandbox a workspace, or to set up its policy or permission rules."
---

# Security

## How Enso sets it up

A workspace with `"restricted": true` in `~/.enso/config.json` requires provider-policy prerequisites before chat execution, the job provider-turn sequence, and heartbeat assessments. Enso checks that the provider's expected policy path is a file, its effective arguments contain no recognized bypass flag, and Codex/Grok have the required trust for the resolved Enso home. Only Codex's exact `--dangerously-bypass-approvals-and-sandbox` and `--yolo` arguments are currently rejected. On failure, Enso reports the missing prerequisite and does not start that provider. It never parses policy contents or verifies enforcement.

Job prerun/postrun scripts, heartbeat gates, command/integration stages, workflow checks, and lifecycle scripts run outside provider policies with the service account's access. A prerun or gate may run before a provider is refused. Follow-ups and repairs do not make the initial policy check continuous enforcement. The [technical contract](https://github.com/geekforbrains/enso/blob/develop/docs/configuration.md#restricted-workspaces) owns these checks; the [public guide](https://ensobot.ai/docs/security/) provides readable examples and limitations.

| Provider | Policy file, inside the workspace | Provider args the workspace needs |
| --- | --- | --- |
| Claude Code | `.claude/settings.json` | The default `--dangerously-skip-permissions` keeps deny rules; `["--permission-mode", "dontAsk"]` for an allowlist |
| Codex | `.codex/config.toml` | Must drop `--dangerously-bypass-approvals-and-sandbox`; Enso refuses it (and `--yolo`) |
| Grok | `.grok/config.toml` | The default `--always-approve` keeps deny rules; add `--trust` |
| OpenCode | `opencode.json` | The default `--auto` keeps deny rules |
| Antigravity | none | Cannot run in a restricted workspace |

## Restricting a workspace

Do the steps in this order and report the audit and enforcement evidence at the end.

1. `enso workspace create NAME` if the workspace does not exist yet.
2. Find the chat provider from `enso config show`: the workspace's `agent`, else `defaults`. Also inspect jobs and beats that use the workspace; each saved provider needs its own file and settings. Use the templates below under `<ENSO_HOME>/workspaces/NAME/` (normally `~/.enso/workspaces/NAME/`).
3. When the template names provider args, put them under `workspaces.NAME.providers.<provider>.args` in `~/.enso/config.json`. An override replaces the global list, so it must be complete.
4. `enso config set workspaces.NAME.restricted true`
5. `enso workspace audit NAME` checks the chat provider's prerequisites only. Fix its `policy` findings, then test intended allowed and denied operations for every provider used. Report the audit separately from actual enforcement evidence and anything untested.

Use these as starting policies and add the person's intended rules. Merge with existing provider settings instead of overwriting unrelated configuration. The command-deny examples help prevent accidental `enso config` changes; they do not block every equivalent operation or direct file write. Complete changes from the operator's terminal when the workspace is already restricted; never clear environment markers to evade the config-write guard.

### Claude Code: `.claude/settings.json`

```json
{
  "permissions": {
    "deny": ["Bash(enso config *)"]
  }
}
```

Under `--dangerously-skip-permissions`, which setup writes, only `deny` counts: `allow` rules and `defaultMode` do nothing, and a project file cannot select `auto` or `bypassPermissions`. For an allowlist, add `permissions.allow` rules and override the workspace's args:

```json
{"workspaces": {"NAME": {"providers": {"claude": {"args": ["--permission-mode", "dontAsk"]}}}}}
```

### Codex: `.codex/config.toml` and `.codex/rules/`

```toml
sandbox_mode = "workspace-write"
approval_policy = "never"
```

Use `read-only` instead of `workspace-write` when the agent should not write at all. Codex takes command rules from `.codex/rules/*.rules`, so `.codex/rules/enso.rules` forbids the command:

```starlark
prefix_rule(pattern=["enso", "config"], decision="forbidden")
```

Setup writes `--dangerously-bypass-approvals-and-sandbox`, which discards the file, so the workspace must override the args; an empty list lets the file decide:

```json
{"workspaces": {"NAME": {"providers": {"codex": {"args": []}}}}}
```

Codex loads a project's `.codex/` files, rules included, only in a trusted project, and Enso refuses a restricted launch until that trust is recorded. Trust the home once in `~/.codex/config.toml` (`$CODEX_HOME/config.toml` when that variable is set), keyed by its absolute path (`$ENSO_HOME`, normally `~/.enso` spelled out):

```toml
[projects."/Users/you/.enso"]
trust_level = "trusted"
```

### Grok: `.grok/config.toml`

```toml
[permission]
deny = ["Bash(enso config *)"]
```

Grok reads project permission rules only under a trusted folder and silently skips them otherwise. `--trust` in the workspace's Grok args grants that trust at launch and records it in `~/.grok/trusted_folders.toml` (`$GROK_HOME/trusted_folders.toml` when that variable is set); a person can also trust the home there once instead, and Enso refuses a restricted launch until one of the two is in place. Keep `--always-approve`, which setup writes:

```json
{"workspaces": {"NAME": {"providers": {"grok": {"args": ["--always-approve", "--trust"]}}}}}
```

### OpenCode: `opencode.json`

```json
{
  "permission": {
    "bash": {"enso config *": "deny"}
  }
}
```

Keep `--auto`: it approves only what the file does not deny, so state every boundary as `"deny"`. The last matching rule wins, so a broad pattern such as `"*"` goes before the specific ones.

### Antigravity

Antigravity has no workspace policy: its permissions live in `~/.gemini/antigravity-cli/settings.json` and its own project catalog, so a restricted workspace refuses to run `agy`. Say so, and offer to give the workspace an `agent` block (all three keys: `provider`, `model`, `effort`) naming Claude Code, Codex, Grok, or OpenCode before restricting it.

## Verifying it loaded

Run each from inside the workspace directory.

- Claude Code: `claude --dangerously-skip-permissions -p "Run enso config show"` must report the command as denied rather than print the config.
- Codex: `codex exec "Print ok"` opens with a header; its `sandbox:` line must show the mode the file sets — `workspace-write` for the template. If the reported mode differs, check root trust and higher-priority provider configuration or arguments; the header alone does not identify the cause. A `read-only` header cannot prove the project file loaded because that can be a default. Test an intended read and a harmless denied write against disposable files; the audit alone cannot prove enforcement.
- Grok: `grok inspect --json` must show `permissions.loaded` above `0` and `projectTrusted` true; `0` means no permission rules loaded; check both trust and policy contents.
- OpenCode: `opencode run --auto "Run enso config show"` must report the command as denied.
- Audit: `enso workspace audit NAME` should have no `policy` error for the chat provider. It does not test rules or providers used only by jobs or beats. A model's assurance or exit code alone is not evidence of a denied action; inspect tool results and file effects. Recheck after provider upgrades or settings changes.

## What restricted guarantees

Say this plainly when asked, and do not oversell it.

- Enso verifies file presence, recognized argument exclusions, and required trust settings. An empty or malformed policy can pass. Other arguments, provider/user configuration, plugins, and tools can change the effective access.
- Command deny rules match commands, not every way to achieve the same effect. They do not create an OS sandbox. Configure and verify the provider's sandbox or separately enforced OS isolation when the task needs that boundary.
- Enso does not protect policy files or `config.json` from direct writes by a process with access. The `enso config` guard depends on caller-controlled `ENSO_WORKSPACE`; it helps prevent accidents, not hostile changes. Never loosen policy or disable `restricted` without the person's explicit request.
- Workspaces share the service account and inherited secrets. A filesystem sandbox does not automatically limit remote tools or connected-account actions. Instructions and skills do not grant new authorization or guarantee resistance to prompt injection.
- Policy prerequisites use the provider of the actual execution. A chat-provider audit is insufficient for a job or heartbeat that uses another provider; scripts remain outside the policy gate.
