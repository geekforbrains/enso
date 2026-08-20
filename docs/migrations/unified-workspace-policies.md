# Migrating to unified workspace policies

This is a manual, breaking migration for installations created before every execution was bound to a named workspace and that workspace's single reusable policy. Enso deliberately does not provide an `enso migrate` command: choosing which policy owns a workspace, separating formerly shared files, and splitting customized instructions require operator judgment.

> **Historical record:** the schema examples and setup behavior below describe the
> release that introduced unified policies. For current Enso, use this guide only for
> the older policy and route rewrite, then follow the
> [v2.0 managed-workspace guide](v2.0-managed-workspaces.md). It supersedes the path,
> layout, linking, instruction-delivery, and automatic-seeding instructions below:
> current Enso uses native ancestor discovery instead of launch injection, no longer
> reads `~/.enso/runtime/instructions/`, and seeds content only during a genuinely
> fresh setup, never at service start. Complete both guides before starting current
> Enso.

The new invariants are:

- The top-level `working_dir` key and `enso serve --working-dir` option no longer exist.
- Every Telegram conversation, exact Slack route, and scheduled job selects a named workspace.
- Every workspace selects exactly one policy; transports, routes, and jobs derive provider and command authority from it and cannot override it.
- Slack credentials, options, and exact route maps all live under `transports.slack`.
- Shared Enso instructions live at `~/.enso/AGENTS.md`, with `~/.enso/CLAUDE.md -> AGENTS.md`, and are injected into every provider launch. Each workspace keeps a separate, focused local `AGENTS.md` and `CLAUDE.md` symlink.
- The service has no process working directory. Each provider process receives the selected workspace as its cwd.

Do not start the upgraded service until `enso config check` succeeds.

## 1. Stop and back up

Stop Enso before moving files or changing policies so no queued or running provider outlives the old binding:

```bash
enso service stop
cp -a ~/.enso ~/.enso-backup-before-workspace-policies
```

If the old `working_dir` points outside `~/.enso`, back up that directory separately. Also retain the installed package version or Git commit and, if you manage service files outside Enso, a copy of `~/Library/LaunchAgents/com.enso.agent.plist` on macOS or `~/.config/systemd/user/enso.service` on Linux.

Record the legacy execution fields before editing:

```bash
jq '{working_dir, workspaces, access, policies, routes, transports}' ~/.enso/config.json
```

The output can contain transport metadata, so keep it private.

## 2. Choose and populate the default workspace

Fresh installations use workspace `default` at `~/.enso/workspaces/default`, bound to policy `admin`. An upgraded installation may keep an existing absolute workspace path, but using the managed layout makes the result match a fresh install. Every configured workspace path and explicit `policy_dir` must be absolute or start with `~/`; relative paths are rejected because services no longer have a working-directory contract.

For the old default `~/.enso/workspace`, move the directory only when the destination does not already exist:

```bash
mkdir -p ~/.enso/workspaces
mv ~/.enso/workspace ~/.enso/workspaces/default
```

For a custom legacy `working_dir`, either move its contents into `~/.enso/workspaces/default` or keep that path in `workspaces.default.path`. Preserve hidden project files, `knowledge/`, `drafts/`, and `uploads/`. Review `.claude/`, `.agents/`, `tools/`, and skill directories individually: keep genuinely project-local controls in the workspace, but move shared Enso behavior to `~/.enso/` rather than copying old global control files wholesale.

Configured workspace paths may not overlap or nest. A legacy workspace used with more than one access profile cannot stay a single workspace under the new model. Choose one policy for it, or create separate non-overlapping workspace directories for the distinct trust levels and copy only the content each needs. Then point each Slack route or job at the appropriate new workspace.

## 3. Split shared and local instructions

Save the legacy instruction files before changing them. Older installations commonly had one `AGENTS.md` in the global working directory containing both Enso-wide workflow and project-specific facts.

Create the shared file at `~/.enso/AGENTS.md` from the current bundled [shared template](../../src/enso/prompts/AGENTS.md), then merge only installation-wide customizations: operator safety rules, Enso CLI conventions, durable docs/tables guidance, and behavior that should apply in every workspace. `AGENTS.md` itself must be an owner-owned regular non-symlink file with no additional hard links or group/other write bits, valid UTF-8 no larger than 20 KiB, and free of NUL bytes; an unsafe source fails configuration validation and every launch closed. Keep `~/.enso/CLAUDE.md` as a relative symlink to it:

```bash
cd ~/.enso
chmod go-w AGENTS.md
ln -s AGENTS.md CLAUDE.md
```

If `CLAUDE.md` already exists, inspect and merge it before replacing it; do not overwrite a customized standalone file blindly.

Put only project-specific context, paths, and local working conventions in each workspace's `AGENTS.md`, using the bundled [workspace template](../../src/enso/prompts/WORKSPACE_AGENTS.md) as the baseline. Each workspace also needs a relative `CLAUDE.md -> AGENTS.md` symlink. Enso seeds missing shared and local templates on setup or service start, but never overwrites customized files.

The shared file is injected at launch independently of cwd. Enso validates the owner-owned UTF-8 source and creates or verifies an immutable content-addressed snapshot under `~/.enso/runtime/instructions/`; Claude receives that snapshot through `--append-system-prompt-file`, Codex receives the validated content through `developer_instructions`, and unrestricted Agy launches receive it in Enso's prompt envelope. Agy remains invalid in a restricted policy. Workspace-local instructions continue through each provider's native project discovery.

## 4. Rewrite `config.json`

Apply all of these schema changes together:

1. Remove top-level `working_dir`.
2. Rename top-level `access` to `policies` if the old catalog uses that name.
3. Add one `policy` name to every workspace. Preserve `path` and `concurrency`.
4. For Slack, move `routes.slack.account_id`, `channel_defaults`, `dms`, and `channels` into the existing `transports.slack` object, then remove top-level `routes`.
5. Remove `access` or `policy` from every Slack route; a route now contains only `workspace`, optional `audit`, and the channel-only mention settings.
6. Add required `transports.telegram.workspace` when Telegram is configured. Keep exact numeric strings in `allowed_users`; `allowed_user_ids` and `"*"` remain invalid.
7. Remove `access` or `policy` from every `JOB.md`; retain its required `workspace`.

A minimal administrative Telegram binding is:

```jsonc
{
  "transport": "telegram",
  "transports": {
    "telegram": {
      "bot_token": "...",
      "allowed_users": ["123456789"],
      "notify_channel": "123456789",
      "workspace": "default"
    }
  },
  "workspaces": {
    "default": {
      "path": "~/.enso/workspaces/default",
      "policy": "admin",
      "concurrency": 1
    }
  },
  "policies": {
    "admin": {
      "unrestricted": true,
      "providers": ["claude", "codex", "agy"],
      "default_provider": "claude",
      "chat_commands": "*"
    }
  }
}
```

For Slack, preserve credentials and move the exact routes beside them:

```jsonc
{
  "transport": "slack",
  "transports": {
    "slack": {
      "bot_token": "xoxb-...",
      "app_token": "xapp-...",
      "account_id": "T0YOURTEAM",
      "dms": {
        "U01OWNER": {"workspace": "default", "audit": false}
      },
      "channels": {
        "C0CLIENT": {"workspace": "client-readonly", "audit": true}
      }
    }
  },
  "workspaces": {
    "default": {
      "path": "~/.enso/workspaces/default",
      "policy": "admin",
      "concurrency": 1
    },
    "client-readonly": {
      "path": "~/.enso/workspaces/clients/client-readonly",
      "policy": "client-readonly",
      "concurrency": 1
    }
  },
  "policies": {
    "admin": {
      "unrestricted": true,
      "providers": ["claude", "codex", "agy"],
      "default_provider": "claude",
      "chat_commands": "*"
    },
    "client-readonly": {
      "policy_dir": "~/.enso/policies/client-readonly",
      "providers": ["claude"],
      "default_provider": "claude",
      "chat_commands": ["status", "clear", "stop", "help"]
    }
  }
}
```

When both transport blocks are retained, both must be structurally valid even though top-level `transport` selects the active daemon.

## 5. Validate before starting

Install the upgraded Enso package or checkout, keep the service stopped, and run:

```bash
enso config check
enso route explain slack U01OWNER
enso route explain slack U04CLIENT C0CLIENT
```

Omit route explanations that do not apply. Fix every config error, including any shared-instruction integrity error, confirm each workspace resolves to the intended policy and provider set, and inspect every restricted native policy before proceeding. There is no fallback to another workspace, unrestricted execution, or an unvalidated shared prompt.

## 6. Reinstall the service definition and restart

Existing launchd and systemd definitions may still contain the removed process-level working directory. Reinstall the service definition with the upgraded binary; a plain restart is not enough to rewrite it:

```bash
enso service install
enso service status
enso service logs -f
```

If Enso is not installed as a service, start it with `enso serve`. The removed `--working-dir` option must not appear in scripts or service overrides. On first start Enso creates missing shared and workspace instruction templates; review both layers, then exercise one Telegram conversation, each important Slack trust level, and a manual run of a representative job.

## Rollback

Stop the upgraded service first. Preserve any new workspace output created during validation, reinstall the previous Enso version or Git commit, restore `~/.enso` and any external legacy working directory from the backups, and reinstall the old service definition before starting it. The previous version expects its old `working_dir`, `access`, and `routes.slack` schema; do not point the upgraded binary at that restored config or the old binary at the rewritten config.

Keep the backup until transport delivery, provider selection, command filtering, uploads, shared/local instructions, and scheduled jobs have all been verified under the new bindings.
