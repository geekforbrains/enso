# Enso documentation

The README explains what Enso is. These guides explain how to install, configure, and
operate it; the specifications record the detailed contracts behind those workflows.

## Choose a guide

| I want to… | Read |
| --- | --- |
| Install Enso and send a first message | [Getting started](getting-started.md) |
| Configure the Slack app and route conversations | [Slack](slack.md) |
| Create workspaces and policies or supply credentials | [Configuration](configuration.md) |
| Run the service, dashboard, updater, docs, and tables | [Operations](operations.md) |
| Schedule recurring agent work | [Background jobs](jobs.md) |
| Develop, test, or release Enso | [Contributing](contributing.md) |
| See what changed in a release | [Changelog](../CHANGELOG.md) |

## Upgrading to 2.0

Version 2.0 removes the compatibility path for legacy workspace, access, and route
configuration. There is no `enso migrate` command. Back up `~/.enso`, follow the
[unified workspace-policy migration](migrations/unified-workspace-policies.md), then the
[managed-workspace migration](migrations/v2.0-managed-workspaces.md), run
`enso config check`, and restart Enso.

## Product and technical reference

Guides optimize for completing an operator task. Specifications are authoritative for
validation rules, security boundaries, storage, and implementation behavior:

| Reference | Owns |
| --- | --- |
| [Product requirements](PRD.md) | Web dashboard scope and planned extensions |
| [Architecture](specs/architecture.md) | Bot/dashboard processes, concurrency, and shared storage |
| [Data model](specs/data-model.md) | `config.json`, SQLite schemas, and the `~/.enso/` layout |
| [Reference docs](specs/docs.md) | Operator-authored notes and their CLI/dashboard workflow |
| [Permissions](specs/permissions.md) | Enso policies and provider-native policy launches |
| [Slack output](specs/slack-output.md) | Rich replies, structured blocks, App Home, and Canvas |
| [Slack triggers](specs/slack-triggers.md) | Mentions, thread following, and Slack command admission |
| [Tables](specs/tables.md) | Registered user tables and bounded dashboard previews |
| [Teams and routes](specs/teams.md) | Exact transport routes, workspaces, policies, and audit data |
| [Web UI](specs/web.md) | Dashboard routes and read/write behavior |

Examples under [`examples/`](examples/) illustrate native provider policies and a full
route configuration. They are starting points, not certified security presets: review,
adapt, and test copies with the exact provider CLI version you run.

When implementation and documentation differ, treat that as a bug. Update the guide and
the specification that owns the behavior in the same change.
