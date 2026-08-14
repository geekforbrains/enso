# Feature: restricted-policy environment passthrough and MCP server allowlist

Two additions to **restricted policies**:

1. **`env_passthrough`** — a new policy key naming environment variables (names, never values) to admit into the otherwise fixed child environment.
2. **A per-policy MCP server allowlist**, resolved by convention from the policy directory — `<policy_dir>/claude/mcp.json` for Claude — and passed to the provider as an exact server set. No new config key.

Together they let a restricted workspace bound to a route reach a declared credential and a declared set of MCP servers without flipping its policy to `unrestricted: true`, which today is the only way to grant either and which discards the entire privilege boundary for that workspace. Total new configuration surface: one key and one conventional file.

______________________________________________________________________

## 1. Motivation

A restricted launch builds a deliberately fixed child environment. In `src/enso/policy.py`, `_minimal_env()` admits the locale/identity allowlist `_KEEP_ENV` (`HOME LANG LC_ALL LC_CTYPE TERM TMPDIR USER SHELL`), a filtered `PATH`, and the active provider's own auth keys (`provider_cls.env_keys`). That allowlist is correct and stays: a newly added secret can never leak into a restricted child by omission. But it also means a credential the policy legitimately needs has no way in at all.

Separately, `ClaudeProvider._permission_args()` hardcodes the restricted launch as `--settings <policy> --permission-mode dontAsk --setting-sources project --strict-mcp-config`. `--strict-mcp-config` means *use only servers named by `--mcp-config`, ignore every other MCP configuration source* — and with no `--mcp-config`, that resolves to **zero MCP servers**.

So a workspace serving a shared-channel route that needs a single API token, or a single internal read-only MCP server, has no supported path to it. The operator's only escape is `unrestricted: true` — the worst possible answer in exactly the shared-channel setting policies exist for.

### The symptom: the inert allowlist

The gap does not announce itself — it produces policy that looks configured and does nothing. A policy author writes `"allow": ["mcp__metrics__query_series"]` into their native settings; the file parses, `enso config check` passes, and the rule can never match anything because no MCP server is ever loaded for a restricted policy. A policy can carry dozens of `mcp__*` rules, every one a silent no-op, indefinitely. §7 closes this loop directly.

### Why no configuration-only route works

- **Secrets in the policy or workspace tree.** Claude's `settings.json` takes literal values only, and policy directories are version-controlled review artifacts — literal credentials become permanent git history. Workspace trees are worse: agent-writable by design.
- **Inheriting the operator's ambient MCP servers** (dropping `--strict-mcp-config`) is a worse boundary — the policy would silently gain any server the operator adds for themselves, with nothing in config recording what the policy can reach. A restricted workspace's capability set must be declared through its policy, not inherited.
- **And inheritance would not even work.** MCP server definitions overwhelmingly authenticate through environment references — `"Authorization": "${METRICS_API_TOKEN}"` for HTTP servers, `"env": {"API_KEY": "${TICKETS_API_TOKEN}"}` for stdio servers — resolved against the child environment, which is exactly the fixed allowlist above. **The environment half is required no matter where the server list lives.** This is the single most important constraint in this document, and it is why the two halves ship together.

______________________________________________________________________

## 2. Design

### 2.1 `env_passthrough`

```jsonc
"policies": {
  "reporting": {
    "policy_dir": "~/.enso/policies/reporting",
    "providers": ["claude"],
    "default_provider": "claude",

    // names, never values
    "env_passthrough": ["METRICS_API_TOKEN", "TICKETS_API_TOKEN"]
  }
}
```

Adds named variables to the otherwise fixed child environment, per policy rather than by widening `_KEEP_ENV` globally. Nothing is implicit: a name absent from the service's environment is simply not set (and logged — §5).

Values arrive through the service environment. The standard delivery path already exists: `~/.enso/secrets/*.env` is loaded into the service process at `serve` startup (`_load_secret_env()` in `cli.py`), so the operator story is "drop `METRICS_API_TOKEN=...` into a secrets env file, name it in the policy."

`env_passthrough` is provider-neutral — it is just the child environment — and applies identically to every policy-controlled provider. It is a **config error on an unrestricted policy**: such a policy already inherits the full environment, and silently accepting the key would let the operator walk away believing they scoped something.

### 2.2 MCP allowlist by convention

For Claude, the server allowlist is the conventional file `<policy_dir>/claude/mcp.json`, sibling to the `settings.json` that already resolves by convention (`POLICY_FILES` in `teams.py`):

- **File present** → it is integrity-checked and validated (§4, §5), then passed as `--mcp-config <path>`. `--strict-mcp-config` stays on **unconditionally** — that flag is what converts `--mcp-config` from *additive* to *exact*, so the policy sees those servers and never the operator's ambient ones.
- **File absent** → the launch is exactly today's: strict MCP config, zero servers. Absence is unambiguous because nothing requested the feature.

```jsonc
// policies/reporting/claude/mcp.json — committable, no secrets
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

**Why convention resolution, not an `mcp_config` path key:**

- It matches the existing mental model exactly — the policy directory is the control surface, and `settings.json` already behaves this way. Zero new config keys.
- The file lands inside the tree that integrity checks and revision hashing already govern, so §4 holds by construction rather than by remembering to wire a second path.
- Absence semantics are cleaner: a missing conventional file means *off*, whereas a missing explicitly-configured file would have to mean *error*.
- It is forward-compatible: an explicit escape-hatch key can be added later without migration; retiring a shipped key in favor of convention cannot.

The cost of convention is implicitness — dropping a file into the policy directory changes the launch with nothing in `config.json` recording it. `enso config check` compensates by printing each policy's resolved server names (§7), and the file is hashed into `policy_revision` (§4), so the change is visible and audited even though it is not in `config.json`.

**Codex needs no Enso mechanism.** Codex configures MCP servers natively in `config.toml` (`mcp_servers` tables) inside the policy's `codex/` tree, which is already staged into the revision-keyed `CODEX_HOME`, already whole-tree hashed, and already per-file integrity-checked. The environment half comes from `env_passthrough`; how Codex forwards environment to its MCP server processes is native behavior the operator owns and verifies, like the rest of the native policy.

**agy** remains unrestricted-only (no verified launch contract), so neither addition applies to it; the unrestricted config-error rule covers the pairing.

### 2.3 The pairing

The two halves are designed to be used together: the MCP file stays free of secrets (`${...}` references only), so it remains safe to commit and review, while values arrive at runtime through the environment and are never written to any file. For stdio servers, the server process is spawned by the provider from the launch environment, so `${...}` references in a server's `env` block resolve against the same minimal-plus-passthrough environment; a stdio server's `command` resolves against the filtered `PATH`, so it must be on that PATH or absolute.

______________________________________________________________________

## 3. Launch contract

The restricted Claude invocation becomes:

```text
claude -p --settings <policy_dir>/claude/settings.json \
  --permission-mode dontAsk --setting-sources project \
  --strict-mcp-config [--mcp-config <policy_dir>/claude/mcp.json] \
  --model <model> -- <prompt>
```

with `--mcp-config` appended exactly when the conventional file exists. The Codex invocation is unchanged.

**Environment construction order** in `_minimal_env()`: the `_KEEP_ENV` allowlist, then `env_passthrough`, then the launch-controlled assignments — filtered `PATH`, provider auth keys, and (for Codex) `CODEX_HOME` — so a launch-controlled variable always wins. Validation independently rejects those names (§6); the ordering is defence in depth, so a bypassed or future-refactored validator still cannot produce a launch where config displaces a launch-controlled value. Note the honest scope of this rule today: because passthrough copies values from the same `os.environ` the allowlist reads, `PATH` is the only name whose launch value actually differs from its parent value (filtered vs. raw). The ordering rule earns its keep the moment any value-indirection shape lands (§10), at which point every reserved name becomes redirectable without it.

**`LAUNCH_CONTRACT_VERSION` bumps to `"3"`.** Flags and environment construction both change. The bump feeds `_manifest_revision()`, rotating every `policy_revision` (including the unrestricted sentinel) — the intended effect of changing what a launch *is*. This is the only visible change for installs that adopt neither addition.

______________________________________________________________________

## 4. Integrity and revision

The MCP file is part of the launch contract and is treated like every other policy source file:

- **Integrity.** When present, `<policy_dir>/claude/mcp.json` must pass `_file_problems()`: a regular file, not a symlink, `st_nlink == 1`, no group/world permission bits, and not resolving inside the workspace. Presence is tested with `lexists`, so a symlink at the conventional path is an integrity error, never "absent".
- **Revision.** `_policy_revision()` for Claude digests a manifest of the files that participate in the launch: `{"settings.json": <sha256>}` plus `{"mcp.json": <sha256>}` when the file exists — the same manifest shape Codex already uses for its staged tree. Adding, editing, or removing `mcp.json` therefore rotates `policy_revision`, so audit rows (`src/enso/audit.py`) and anything keyed on the revision correctly describe the server set the turn actually had.

`Launch` gains `mcp_config: str | None`, and `PolicyCheck` gains the resolved server names so `enso config check` can display them (§7).

______________________________________________________________________

## 5. Failure semantics

**The MCP file fails closed.** Absent → the feature is off. Present but unusable — integrity failure, unparseable JSON, not a JSON object, missing or non-object `mcpServers`, or an empty `mcpServers` — → `PolicyError`, refusing the turn (an empty object gets the guidance "delete the file to disable MCP"). The failure direction here is deceptively benign: with `--strict-mcp-config` on, a broken file would degrade to *fewer* servers, which is safe. Fail anyway, because "a policy file failed to load" must never be silently absorbed — the operator would see a broken integration with no cause, and the identical code path in a file whose job is to *restrict* would be a genuine hole. Policy faults are loud in both directions.

**A missing passthrough variable warns, but does not refuse.** The fail-closed rule above is about policy files; environment presence varies by deployment, and a policy may legitimately list variables only some turns use. At spawn, configured-but-absent names are logged (`log.warning`, names only), and `enso config check` surfaces them statically (§7). The downstream failure when a needed variable is absent — the MCP server rejects authentication — is contained and attributable.

Spawn logging records the effective capability set per turn — resolved MCP server names and the passthrough names actually present — at `INFO`, names only, never values. The audit table schema is unchanged; `policy_revision` plus the version-controlled policy directory already recover the full server set for any audited turn.

______________________________________________________________________

## 6. Validation rules

Both additions are validated where policies load (`src/enso/teams.py`), reporting through the normal `problems` list so `enso config check` catches them statically and workspaces binding a broken policy become unusable.

| Subject | Rule | Rationale |
| --- | --- | --- |
| `env_passthrough` | must be a list of strings | shape |
| `env_passthrough` | rejected on `unrestricted: true` | §2.1 — the operator must not believe they scoped something |
| `env_passthrough` | no duplicates | a duplicated name signals a merge/edit mistake |
| `env_passthrough` | each name matches `^[A-Z][A-Z0-9_]*$` | rejects lowercase, `FOO=BAR`, and injection-shaped input |
| `env_passthrough` | must not name `HOME LANG LC_ALL LC_CTYPE TERM TMPDIR USER SHELL PATH CODEX_HOME`, nor any `ENSO_`-prefixed name | launch-controlled and Enso-owned namespaces (§3) |
| `claude/mcp.json` | when present: `_file_problems()`, parses as JSON, top-level object, non-empty `mcpServers` object | §4, §5 — checked identically by `check_provider` and at launch |

The reserved-name set is defined locally in `teams.py` rather than imported from `policy.py` — `policy.py` imports `teams.py`, and the reverse import would be circular; a brief comment says so. Because comments do not stop drift, a test asserts the reserved set is a superset of `policy._KEEP_ENV ∪ {"PATH", "CODEX_HOME"}` (tests can import both modules).

Two static warnings (never errors — neither can widen access) round out validation, both emitted by the Claude policy check where `settings.json` and `mcp.json` are both already parsed:

1. **Inert-rule cross-check.** Any `mcp__<server>__…` or `mcp__<server>` rule in the policy's `permissions` whose `<server>` is not defined by the policy's resolved MCP config → warn, naming the rule. This kills the §1 inert-allowlist class outright, converting a silent permanent no-op into a startup-time message. It is a warning because a rule for a nonexistent server grants nothing, and an operator mid-migration may legitimately stage rules ahead of the server. The mirror image also warns: a defined server that no **allow** rule references will have every tool denied under `dontAsk` — deny or ask references cannot admit a tool, so a server named only there is defined-but-unusable, the same class of silent mistake.
2. **Secret-shaped literals.** A header or `env` value in `mcp.json` whose key looks credential-bearing (case-insensitive `auth|token|secret|key|password|bearer`) and whose value contains no `${` reference → warn. This is precisely the anti-pattern the pairing exists to avoid. Keys outside that pattern (e.g. `Content-Type`) are not flagged; deeper schema validation is out of scope (§10).

______________________________________________________________________

## 7. `enso config check` surface

For each restricted policy, `config check` prints the declared passthrough names, marking any name not currently resolvable (checked against the invoking shell environment plus `~/.enso/secrets/*.env`, with the printed caveat that this approximates but may not equal the service environment). For each `workspace → policy` native-launch line, the Claude entry lists the resolved MCP server names beside the revision, e.g. `✓ claude (a1b2c3d4e5f6) mcp: metrics, tickets`, so the conventional file's effect is always visible where operators already look. The §6 warnings print here through the existing warning channel.

______________________________________________________________________

## 8. Security properties the docs must state

- **MCP servers bypass the sandbox's network rules.** Servers are dialled by the provider process itself, not by sandboxed Bash, so an allowlisted server is reachable regardless of what the OS sandbox permits on the network. Grant only servers whose *entire* tool surface is acceptable for the policy, then narrow within it using `mcp__<server>__<tool>` permission rules.
- **Passthrough is a delivery mechanism, not a confidentiality one.** If the policy can run Bash at all, the agent can read its own environment. `env_passthrough` hands the policy the real value; the docs say so plainly rather than implying scoping. Prefer narrowly-scoped, read-only tokens, and where the deployment supports it, a credential mask or egress proxy so the child sees a sentinel.
- **The launch's exactness rests on `--strict-mcp-config` behavior in the installed CLI.** The permissions.md verification checklist gains two items to run from inside the restricted policy: (a) write a workspace `.mcp.json` and a workspace `.claude/settings.json` that tries to enable it (e.g. `enableAllProjectMcpServers`), take a **new turn**, and confirm no new servers appear — the workspace is agent-writable and `--setting-sources project` loads it, so this is the cross-turn self-escalation path to rule out; (b) confirm `${VAR}` references in the MCP config resolve from the launch environment (passthrough works) and that an unlisted variable does not.

______________________________________________________________________

## 9. Files involved

| File | Change |
| --- | --- |
| `src/enso/teams.py` | `Policy` gains `env_passthrough: tuple[str, ...]` (default `()`); loader + §6 validation; local reserved-name set with circular-import comment |
| `src/enso/policy.py` | `Launch` gains `mcp_config`; `PolicyCheck` gains resolved server names; `_minimal_env()` applies passthrough per §3 ordering; conventional `mcp.json` resolution, integrity, parse checks, and manifest inclusion; spawn logging; `LAUNCH_CONTRACT_VERSION = "3"` |
| `src/enso/providers/claude.py` | `_permission_args()` appends `--mcp-config <path>` when `launch.mcp_config` is set, keeping `--strict-mcp-config` unconditionally |
| `src/enso/providers/codex.py` | no change |
| `src/enso/cli.py` | `config check` display per §7 |
| `tests/test_teams.py` | §6 validation matrix; reserved-set drift test against `policy` |
| `tests/test_policy.py` | env construction and ordering (including a validator-bypass test: a directly constructed policy naming `PATH` still gets the filtered value); mcp.json absent/symlink/unparseable/empty/valid; flag construction; revision rotation on mcp.json add/edit/remove; contract bump |
| `docs/specs/permissions.md` | launch-contract command block; `--strict-mcp-config` paragraph; §8 verification-checklist items |
| `docs/specs/data-model.md` | `policies.<name>` schema gains `env_passthrough`; conventional `claude/mcp.json` documented beside `claude/settings.json` |
| `docs/specs/teams.md` | policy prose: both halves, §8 properties |
| `docs/examples/teams-config.jsonc` | worked `env_passthrough` example |
| `docs/examples/acme-claude-mcp.json` | secret-free example MCP file |
| `CHANGELOG.md` | contract bump; both additions default to off |

Both additions defaulting to off keeps every existing policy unaffected; the only visible change for an untouched install is the rotated `policy_revision` from the contract bump.

______________________________________________________________________

## 10. Deferred (explicitly out of scope)

- **An explicit `mcp_config` path key** as an escape hatch for files outside the policy tree. The secret-free-by-design premise removes the main reason to want one, and adding a key later is backward-compatible.
- **Value indirection for `env_passthrough`** (per-launch resolution from a file or secret manager, so the service process need not hold every policy's secrets). The codebase already has the idiom — `secret_refs.py` resolves `<key>_1password` references through the 1Password helper for transport secrets — and a list of names leaves room for a reference-shaped variant later. Not now: per-turn subprocess resolution has real latency and failure-mode cost.
- **Audit schema columns** for per-turn capability sets. Spawn-time logging plus the revision fix (§4, §5) cover the need; revisit only if log-based answers prove insufficient in practice.
- **Deeper `mcp.json` schema validation** beyond §6's shape checks and the secret-shaped-literal warning. Provider schemas evolve; Enso verifies plumbing, not native semantics.
