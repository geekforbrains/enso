---
name: enso-workspace
description: Inspect, create, bind, or retire Enso workspaces, write workspace instructions, or diagnose conversation and job ownership.
---

# Workspace

```bash
enso workspace list
enso workspace create NAME
enso workspace audit NAME          # --fix repairs layout; never deletes
enso config set bindings.slack:C… NAME
enso config unset bindings.slack:C…
enso config check
```

Use `$ENSO_HOME` (default `~/.enso`). Workspaces live at `workspaces/<name>/`; names are
lowercase kebab-case. Required `default` is the operator workspace and cannot be retired
or renamed. Restore it if missing, or create an empty replacement with the CLI.

## Files and instructions

Each workspace has `AGENTS.md`, `skills/`, `jobs/`, `projects/`, `work/`, and `uploads/`.
`workspace.json` supplies optional settings; `heartbeat/` holds optional gates. Load
`enso-config`, `enso-jobs`, or `enso-projects` for those formats.

Start workspace instructions with `## Workspace` and a short purpose. Add only useful
routing, terms, or rules needed every turn. Reference detailed knowledge by path. Use
`enso-knowledge` and the shared `Meta/Guide.md` when present; shared knowledge is the default,
while existing workspace knowledge and explicit filing rules remain supported.

Keep work in its established repository or destination; otherwise group retained outputs
under `work/`. Enso writes attachments to `uploads/`. Keep repositories outside workspaces:
a nested Git root can hide inherited instructions and skills.

Edit `AGENTS.md` and `skills/`, preserving generated `CLAUDE.md` and provider discovery links.
Load `enso-skills` for authoring or installation. Existing `knowledge/` and `drafts/` remain
supported; scaffolding and audits never migrate their content.

## Bindings and ownership

Bindings grant access, not just context. A bound channel trusts all human participants,
including guests and later additions; workspaces do not provide confidentiality. Only
trusted configuration or operator-initiated pairing may add access.

Keys in `config.json` are `slack:C…`, `slack:dm:U…` (or Enterprise Grid `W…`), and
`telegram:<user id>`; values name existing workspaces. Verify IDs through the transport,
never message-text names. Binding changes apply without restart; queued turns retain their
arrival workspace, and removing the binding drops them before startup.

Jobs belong to their containing workspace and use `WORKSPACE:JOB` references; projects live
at `projects/<KEY>/PROJECT.md`. Moving files does not transfer existing records.

## Retirement

For workspaces other than `default`, remove/repoint bindings, disable jobs, resolve open
tasks, and review active and paused beats with `enso heartbeat list --workspace NAME`,
paging as needed. End beats only when authorized; pausing retains the workspace reference,
and beats cannot transfer workspaces.

Let running work finish. There is no automated rename/transfer; keep the directory while
work or retained history needs it. Check configuration before and after an authorized
archive/deletion. No restart is needed.
