---
name: enso-security
description: "Make an Enso workspace restricted: write the provider CLI's own policy file, add the provider args it needs, set the restricted flag, and audit it. Use when someone asks to restrict, lock down, or sandbox a workspace, or to set up its policy or permission rules."
---

# Security

## How Enso sets it up

A workspace with `"restricted": true` in `~/.enso/config.json` refuses every launch there — chat turns, jobs, and heartbeat assessments — until the provider CLI's own project-level policy file is in place. Enso has no permission language of its own and never reads the file. At each launch it checks three things: the file exists, as a regular file, where that CLI reads it from the working directory; the workspace's arguments for that provider carry no flag that makes the CLI discard the file; and, for Codex and Grok, which read a project file only below a trusted root, that the CLI's own user config trusts the home (`$CODEX_HOME/config.toml` `[projects."<home>"]` `trust_level = "trusted"`; `$GROK_HOME/trusted_folders.toml` `[folders."<home>"]` `trusted = true`, or `--trust` in the workspace's Grok args). When any is missing, the request gets a one-line error naming what to add, the run is recorded as failed, and no CLI starts. The rules are the CLI's, in the CLI's own format, and the CLI enforces them.

| Provider | Policy file, inside the workspace | Provider args the workspace needs |
| --- | --- | --- |
| Claude Code | `.claude/settings.json` | The default `--dangerously-skip-permissions` keeps deny rules; `["--permission-mode", "dontAsk"]` for an allowlist |
| Codex | `.codex/config.toml` | Must drop `--dangerously-bypass-approvals-and-sandbox`; Enso refuses it (and `--yolo`) |
| Grok | `.grok/config.toml` | The default `--always-approve` keeps deny rules; add `--trust` |
| OpenCode | `opencode.json` | The default `--auto` keeps deny rules |
| Antigravity | none | Cannot run in a restricted workspace |

## Restricting a workspace

Do the steps in this order and report the audit at the end.

1. `enso workspace create NAME` if the workspace does not exist yet.
2. Find the provider the workspace runs with: `enso config show`, the workspace's `agent` block, else `defaults`. Write that provider's policy file from the template below into `~/.enso/workspaces/NAME/`.
3. When the template names provider args, put them under `workspaces.NAME.providers.<provider>.args` in `~/.enso/config.json`. An override replaces the global list, so it must be complete.
4. `enso config set workspaces.NAME.restricted true`
5. `enso workspace audit NAME`, and report what it says. A `policy` error names what is still missing; fix that and audit again.

Copy the template exactly, then add the person's own rules to it. Every template denies `enso config`, because a restricted agent that can run `enso config set workspaces.NAME.restricted false` could lift its own restriction in one command.

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
- Codex: `codex exec "Print ok"` opens with a header; its `sandbox:` line must show the mode the file sets — `workspace-write` for the template. `read-only` while the file says `workspace-write` means the file was not loaded (the home is not trusted); `danger-full-access` means a flag outranks it. A file that sets `read-only` cannot be told from the default by the header alone; the audit below is the check.
- Grok: `grok inspect --json` must show `permissions.loaded` above `0` and `projectTrusted` true; `0` means the folder is not trusted yet.
- OpenCode: `opencode run --auto "Run enso config show"` must report the command as denied.
- Every provider: `enso workspace audit NAME` passes without a `policy` error.

## What restricted guarantees

Say this plainly when asked, and do not oversell it.

- Enso proves the file is there, no bypass flag is set, and, for Codex and Grok, that the home is trusted — nothing more. What the rules permit is the CLI's decision, and a rule the CLI does not honour is not made stronger by the flag.
- Deny rules are text matches on the command: they stop the plain form of a command, not every way of reaching the same effect. Only Codex's sandbox is an operating-system boundary; its `workspace-write` also keeps `~/.enso/config.json` out of reach, because the file is outside the workspace.
- The agent works inside the workspace, so it can edit the policy file during a turn. The `enso config` deny is what keeps the restriction from being lifted from within. Never loosen a policy file or set `restricted` back to false unless the person asks for exactly that.
- The check runs at every launch and looks at the provider that launch uses, so a job or heartbeat in the workspace that runs a different provider needs that provider's file too.
