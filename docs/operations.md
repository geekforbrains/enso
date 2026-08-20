# Operations

This guide covers the long-running service, dashboard, direct notifications, local
content history, operator reference docs, and structured data tables. Configuration and
policy authoring are in [Configuration](configuration.md); scheduled work is in
[Background jobs](jobs.md).

## Service management

```bash
enso serve                   # foreground bot and scheduler
enso service status
enso service install         # launchd on macOS, systemd user service on Linux
enso service uninstall
enso service logs -f
```

The service definition has no process working directory. Each provider subprocess gets
its resolved workspace as cwd. After upgrading from a version whose launchd or systemd
definition contained `WorkingDirectory`, run `enso service install` again; a restart
alone does not rewrite the definition.

Configuration and route changes require a restart. `enso config check` should pass
before one. For launch failures, inspect `enso service status`, follow logs, validate the
affected route with `enso route explain`, and manually run a representative job if the
scheduler is involved.

## Validated updates

`/update` in Telegram or `!update` in Slack is deterministic: the model does not modify
the installation. Enso checks stable `geekforbrains/enso` `main`, pins its exact Git
commit, builds a wheel, installs it in an isolated environment, runs that revision's test
suite, then installs the same wheel and restarts the service. If the installed revision
already matches, it reports that there is nothing to do.

The updater confirms success after the bot restarts. If a separately managed dashboard
service is named `com.enso.web` on launchd or `enso-web.service` on systemd, it is also
restarted and health-checked. A foreground `enso web` process must be restarted manually.
An editable checkout that already contains stable `main` is recognized as ahead and is
not downgraded.

Update state lives in `~/.enso/update.json`, outside user configuration. It records the
Git commit as well as the package version because development revisions can share a
version number.

## Dashboard

Install and run the optional web process separately from the bot:

```bash
pip install -e ".[web]"
enso web                         # http://127.0.0.1:1337
```

The dashboard shows run history and makes execution configuration traceable: workspaces
and their policies, exact Slack routes, Telegram/job bindings, shared instructions, and
workspace-root `AGENTS.md` files. Policy and Slack views never render secret values or
native policy contents. Shared and valid canonical workspace-root instructions have
bounded revision-checked editors; nested workspace instructions are read-only. Invalid,
external, nested, or symlinked workspace roots are not inspected or rendered.

### Remote access

A concrete `web.host` is accepted automatically. If listening on `0.0.0.0` or `::`, add
every hostname or IP that clients will send in `web.allowed_hosts`; a wildcard bind does
not authorize arbitrary `Host` headers:

```json
{
  "web": {
    "host": "0.0.0.0",
    "allowed_hosts": ["enso.example.ts.net", "100.64.0.10"],
    "token_1password": {
      "item": "Enso - Web - Dashboard",
      "field": "WEB_TOKEN"
    }
  }
}
```

The allowlist blocks DNS-rebinding requests; it is not authentication. With neither
`web.token` nor `web.token_1password`, authentication is disabled. Any remotely reachable
dashboard needs a strong token or trusted tailnet/reverse-proxy access controls. The
[web specification](specs/web.md) owns exact routes, write boundaries, and validation.

## Send a message or file

```bash
enso message send "Deploy finished"
enso message attach report.pdf "Weekly summary"
```

Pass `--to` for one explicit destination. Without it, a command running inside an
interactive Enso turn returns to `ENSO_ORIGIN_CHANNEL`; outside a turn it uses that
transport's `notify_channel`. The command errors if none exists.

| Transport | `--to` value | Without `--to` |
| --- | --- | --- |
| Telegram | Numeric chat ID | Interactive origin, then `notify_channel` |
| Slack | Channel, DM, or user ID | Interactive origin, then `notify_channel` |

Neither transport broadcasts implicitly. Slack outgoing file uploads accept any type up
to 1 GiB. CLI message text, file captions, scheduled-job notifications, and other direct
notifications are text-only; rich structured rendering belongs to interactive Slack
final answers. See [Slack](slack.md#replies-and-persistent-surfaces).

When Enso starts a provider for an interactive turn it exports origin metadata as
`ENSO_ORIGIN_TRANSPORT`, `ENSO_ORIGIN_USER_ID`, `ENSO_ORIGIN_USER_NAME`,
`ENSO_ORIGIN_CHANNEL`, `ENSO_ORIGIN_CHANNEL_NAME`, and (for Slack threads)
`ENSO_ORIGIN_THREAD_TS`. Agents use these when a task needs the requester or destination;
normal `enso message` routing already handles the reply path.

## Local content history

`~/.enso` is a local-only Git repository for user-owned content. After one coherent
change, stage exactly the reviewed files and make an ordinary scoped commit:

```bash
git -C ~/.enso add workspaces/company/AGENTS.md \
  workspaces/company/knowledge/onboarding.md
git -C ~/.enso commit -m "docs: update onboarding"
```

The managed `.gitignore` excludes config, credentials, databases, messages, audits,
runs, caches, logs, uploads, drafts, native policies, and other runtime state. Never use
broad staging such as `git add -A` or force-add an ignored file. `enso config check`
reports tracked paths the protective rules would otherwise exclude, because a tracked
file no longer receives `.gitignore` protection.

Enso never creates or contacts a remote for this repository and provides no history,
restore, reset, or delete command. It is a local content journal, not a complete backup.
`enso doc create` and `enso job create` intentionally make incomplete placeholders;
finish the note or disabled job first, then record the coherent result once.

## Operator reference docs

Durable facts about an installation—machine layout, account conventions, deploy
runbooks—live as Markdown below `~/.enso/docs/`. They are not automatically injected;
the provider consults them when a task calls for that context.

```bash
enso doc list                       # path, name, and description
enso doc create stuff/homelab.md    # scaffold frontmatter and parent dirs
```

Each note has `name` and `description` frontmatter and is identified by its relative
path, up to eight path segments including the filename. `enso doc list` is computed from
frontmatter on every call, so there is no separate index to drift.

Fresh setup creates three ordinary user-owned references: `enso/content_model.md`,
`enso/layout.md`, and an editable `operator.md` template. Edit or delete them as needed;
completed setup, startup, repair, and upgrades never recreate them. The dashboard can
browse and manage notes. See the [reference-doc specification](specs/docs.md).

## Structured data tables

Agents can keep queryable user records in ordinary SQLite tables inside
`~/.enso/enso.db`:

```bash
enso table list
enso table schema weight_entries
enso table register weight_entries \
  --name "Weight" \
  --description "One body-weight measurement per row, recorded in kilograms."
```

Registration is the dashboard visibility boundary. Only catalogued user tables appear;
internal `runs`, `_enso_*`, and `sqlite_*` names stay hidden and reserved. The dashboard
shows schema and a bounded paginated preview. Agents use normal SQLite transactions for
schema and row changes rather than an Enso query language.

Enso uses short-lived connections and bounded lock waits. The dashboard distinguishes a
retryable **Database busy** response from a broader **Database unavailable** failure
without taking down its health endpoint. See the [tables specification](specs/tables.md)
for registration, safety, and preview limits.

## Run and audit history

Scheduled and manually triggered jobs record bounded local run metadata for the
dashboard. Interactive turns are not job runs; routed Slack work may instead enable
turn-based auditing per route. Use:

```bash
enso audit tail
enso audit export
```

Audit policy, retention, delivery-ledger behavior, and the fields intentionally excluded
from operational logs are specified in [Teams and routes](specs/teams.md) and
[Architecture](specs/architecture.md).
