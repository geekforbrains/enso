# Install

## Requirements

- The release installer supplies **Python 3.14** and keeps its own copy of
  [uv](https://docs.astral.sh/uv/) in the home; `curl` is required to bootstrap uv.
- **At least one agent CLI**, installed and already authenticated: `claude`, `codex`,
  `grok`, `agy`, or `opencode`. Enso drives them; it does not manage their credentials.
- **A transport**: a Slack app in Socket Mode, or a Telegram bot token.
- macOS or Linux. The service integration uses launchd or systemd.

## Install

Install Enso without cloning the repository:

```bash
curl -fsSL https://github.com/geekforbrains/enso/releases/latest/download/install.sh | sh
```

To install from a downloaded release bundle, run this from its directory:

```bash
sh install.sh --manifest ./release.json
```

The published installer includes its version-pinned manifest URL and the update feed, so
the one-line command needs no arguments. `--manifest` and `--feed` override those defaults.
[Releases](releasing.md) owns the bundle, hosting, authentication, and build contracts.
The source checkout's root `install.sh` is a local wrapper that needs the checkout;
downloads use the generated bundle's self-contained installer.

The installer creates a managed runtime inside `ENSO_HOME` (default `~/.enso`) and a stable
`enso` command in `~/.local/bin`. It installs code only: it does not apply configuration,
contact a transport, or install the background service. Put the chosen bin directory on
`PATH`, then run `enso setup`. `--home DIR` and `--bin-dir DIR` choose explicit destinations;
pass installer options after `sh -s --` when piping the download to the shell.

The default extras are `slack,telegram,web`; use `--extras slack,web` to select features, or
`--extras ''` for the base CLI. Without `web`, the base CLI and `enso web status|stop|uninstall`
still work; `enso web start` and `install` explain the missing feature. A local installation
can save its future update source with `--feed URL`; otherwise it follows the official GitHub
feed, even when the manifest was a downloaded local file.
Private feeds use `--token-file FILE`, whose bearer token is copied privately into the
runtime. It is never placed in a URL or forwarded to a different origin.

## Setup

```bash
enso setup
```

The wizard is linear and requires `config.json` to be absent. A home prepared with `init`
can use it too; existing scaffold content is preserved.
Both commands refuse an obsolete configuration, database, or home-level `jobs/` before
seeding files. Automatic migrations support 0.2.0 onward; older layouts are unsupported.

1. Detects which provider CLIs are on `PATH` and records their model lists and unattended
   flags.
2. Asks for the default agent — provider, model, effort.
3. Prepares the home and `default` workspace, including instructions, skills, links,
   an editable config example, the packaged Slack manifest, and an empty Git root.
4. Connects one transport. Slack checks both tokens and asks you to send a fresh code in
   the bot's private chat. Telegram supplies a Start link with a fresh code. The wizard
   discovers your identity and notification target automatically; see [Connections](connections.md).
   Pairing creates an explicit user binding to `default`; it does not grant wildcard access.
   Setup explains shared memory and channel audience trust before pairing.
5. Writes `config.json`, seeds the `enso-audit` and `enso-update` jobs in `default` and a
   `enso-memory` job in each workspace with its effective agent, sends a test message, and
   offers to install the background service.

The connection acknowledgment and test message verify chat delivery, not provider login.
If you declined service installation, run `enso serve` yourself. With Enso running, say
`!help` in Slack or `/help` in Telegram, then send a normal message and check that the
chosen agent answers.

OpenCode setup starts with `openrouter/deepseek/deepseek-v4-flash`; its models always use
the full `provider/model` id. Run `enso models` to find other tool-capable OpenRouter
models, then add the ids you choose to `providers.opencode.models`. See
[Configuration](configuration.md#opencode) for model variants and permissions.

Add the second transport, more workspaces, and bindings by editing `config.json` directly;
see [Configuration](configuration.md). A running service reads the file again for its next
turn and tick, so bindings and workspaces apply at once; a second transport needs a restart.
Do not rerun `setup` over an existing active config;
use that configuration workflow, or resume an unfinished hosted connection with
[`enso connect`](connections.md#hosted-command-contract).

## Non-interactive setup

```bash
enso init --json
enso providers --json
enso slack manifest > slack-manifest.json
# Fill in ~/.enso/config.example.json, then apply the complete document:
enso config apply --file ~/.enso/config.example.json --expected-hash missing --json
enso config check --json
```

`init` prepares the home and `default` workspace without prompts, network access, provider
authentication, transport extras, a database, or service installation. It writes an editable
`config.example.json`; its empty credentials deliberately make it incomplete. It leaves
`config.json` absent in a new home. Existing configuration, instructions, skills, workspaces,
and jobs are preserved. A conflicting file or link is reported so you can resolve it; there
is no force/reset option. Rerun after an interrupted preparation to finish missing files.
Git is initialized when its executable is available.

Apply validates a full document before saving it privately and seeds missing bundled jobs
with the chosen agent, including each workspace's [memory job](jobs.md#workspace-memory-job-in-020).
Existing jobs retain their own agent. It does not contact a bot or
start the service; an initialized home and a valid config are intermediate steps, not proof
that Enso can reply. Authenticate the provider CLI manually and start Enso when ready.
See [Configuration](configuration.md#applying-configuration) for conflict handling and
[CLI](cli.md#onboarding-contracts) for the machine-readable result fields.
For a guided host that collects tokens and pairs the owner before applying configuration,
use the [connection commands](connections.md#hosted-command-contract).

## The service

```bash
enso service install     # write the unit for this `enso` and start it
enso service status
enso service restart
enso service uninstall
```

`install` writes `~/Library/LaunchAgents/com.enso.agent.plist` on macOS or
`~/.config/systemd/user/enso.service` on Linux, pointing at the `enso` on your `PATH`,
with a `PATH` covering Enso, the configured providers, and the installing shell's tools.
The unit captures these paths at installation; reinstall it after relocating a CLI or
adding a provider whose directory the unit's `PATH` does not cover yet. `restart` is for a
new release or a change to the `transports` or `logging` sections of `config.json`; a
`web` change needs `enso web stop`, then `enso web start`, instead. `restart` does not
rewrite the unit, and the rest of the configuration needs no restart at all.
For a managed install, the unit must use the stable launcher, so selecting a new release
does not require rewriting it. A running self-update requires Enso to belong to its
user-level launchd or systemd service; a foreground development process must be stopped first.

The service's own stdout and stderr go to `~/.enso/launchd.log`, which is only interesting
after a crash. Everything else is in `~/.enso/enso.log`:

```bash
enso logs -f
enso logs --job meteor:meteor-forum-watch
enso logs --turn a1b2c3
```

Editing workspace `WORKSPACE.md` or project `PROJECT.md` needs no restart.
Editing `config.json` needs no restart for `bindings`, `defaults`, `providers`, `agent`,
`runs`, and `heartbeat`: the service reads the file again
for its next chat turn and scheduler tick, the same way `JOB.md` files are reloaded every
minute. `transports` and `logging` are read when `enso serve` starts, so a change there
needs `!restart` in chat or `enso service restart`; `web` is read when the viewer starts,
so a change there needs `enso web stop`, then `enso web start`. A file that is invalid
when it is read is logged once and the last valid configuration stays in force; see
[Applying configuration](configuration.md#applying-configuration).

## Verify

```bash
enso config check         # config validity and every problem at once
enso workspace audit      # workspace layout and skill wiring
enso doctor               # both, plus runtime health and knowledge/memory audits
```

The `enso-audit` job that setup installed runs `enso doctor` nightly and reports problems
to your notify target; see [Jobs § Bundled jobs](jobs.md#bundled-jobs).

## The viewer

```bash
enso web start            # http://127.0.0.1:8787, in the background, logging to ~/.enso/web.log
enso web install          # optional: supervise it and start it at future logins
enso web status
enso web stop             # stop the process and its supervisor until the next start/login
enso web uninstall        # stop it and remove automatic startup; keep the home and logs
```

The read-only [web viewer](web.md) runs independently of the agent service and shows the
same doctor report on its Health page even when `config.json` is broken. A standalone
`start` lasts until shutdown. `install` writes `~/Library/LaunchAgents/com.enso.web.plist`
on macOS or `~/.config/systemd/user/enso-web.service` on Linux, starts it immediately, and
enables it for future user sessions, including login after a reboot. macOS requires a
GUI login; on Linux, starting before login requires an already configured lingering user
manager. Installation does not change that machine setting.

The unit runs `enso web start --foreground`, reads `web.host` and `web.port` from
`config.json`, and restarts the viewer after a crash. Its stdout and stderr go to
`ENSO_HOME/launchd-web.log` on both platforms. It captures the installing environment as
the agent unit does and pins an absolute `ENSO_HOME`. Managed releases use their stable
launcher; source/tool installs use the `enso` on `PATH`. Reinstall after moving that
launcher or changing the captured environment.

There is one fixed viewer unit per OS user, even with multiple Enso homes. `install`
can adopt a standalone viewer or replace a compatible handwritten unit for the same
home, including the existing `com.enso.web` workaround. It stops that viewer first;
there is no need to create a second unit. A unit for another home, an unrecognized
command, a symlink, or an indirect/overridden command or home is left unchanged with
an error. Resolve custom unit overrides manually before using these commands.
`uninstall` stops supervision and removes only the viewer unit; the agent, config,
database, workspaces, pidfile, and existing logs are preserved. `doctor` reports the
viewer service separately; leaving this optional service uninstalled is healthy.

## Development

For source checkout setup, local checks, and safe scratch-home testing, see
[Development](development.md). Start with [Contributing](../CONTRIBUTING.md) when preparing
a change. This page owns installing and operating Enso, not contributor workflow.

## Upgrading

This upgrade flow supports homes created by 0.2.0 onward. Managed updates follow published
`major.minor.patch` releases, not changes to the repository's default branch. The package
version comes from installed package metadata and the runtime receipt; `config.json`'s
`version` and the database schema version remain independent format versions.

```bash
enso update check
enso update apply
enso update status
```

Checking does not authorize installation. The bundled nightly check announces each new
release once to the configured notification target and stays quiet when unchanged or offline.
An unmanaged installation also gets one notice explaining adoption. The check spends no
provider tokens and never installs an update. You can ask in chat
whether an upgrade is available, then ask Enso to upgrade itself when ready.

Update requests and checks with `--notify` use `--workspace NAME`, then `ENSO_WORKSPACE`,
then `default` for terminal use. The update saves this owner for its completion
notification after restart. See [CLI § Updates](cli.md#updates) for the command contract.

`apply` queues an independent updater under launchd or user systemd and returns immediately.
An agent requesting the update finishes its current turn instead of waiting inside it.
The helper stages and verifies a separate release environment, stops accepting new turns and
jobs, and waits for already accepted work. Other CLI commands hold access to the home through
their execution, including waiting for stdin. The updater waits for them too; a busy deadline
defers the update without interrupting work or changing the selected release.

Once work has drained, the helper stops Enso and any running viewer, asks the candidate which
paths it will change, and snapshots them. It selects the candidate, runs all pending database,
configuration and file migrations, validates the result, and refreshes eligible bundled content,
then restarts the previous services. The new daemon must become ready while new work is still
paused. Only then does the helper commit the installed version, admit work, and report the
outcome in the originating conversation or default notification target. A stopped daemon or
viewer stays stopped. The standard viewer unit is detected automatically by its owning
process. Cloud can identify a differently named viewer service with `--viewer-service NAME`
during installation; the updater checks ownership before stopping either kind.

Managed self-updates support the user services generated by `enso service install`,
`enso web install`, and Enso Cloud. Keep their service definitions and any overrides
unchanged until the update or recovery finishes; concurrent service reconfiguration is
unsupported. Viewer lifecycle commands refuse changes while a managed update is pending.

The snapshot includes configuration, the database and sidecars, migration and bundle receipts,
bundled content, and every path declared by pending migrations. Folder moves include both
their source and destination; new destinations are restored to their original absence on failure.
Other home content is preserved. Migrations cannot change files outside the home or the
updater's own operating state. [Home migrations](migration.md) owns the authoring rules.
Customized bundles survive according to [Customizing](customizing.md#the-bundled-skills).

If preparation or startup fails before work is admitted, the helper restores the previous code
and snapshot together and verifies the old service. If the helper or host is interrupted:

```bash
enso update status --json
enso update recover --json
```

Recovery uses the original operation and snapshot. If recovery cannot verify the restored
service, Enso remains paused and reports `recovery_failed`; fix the reported service problem
and retry recovery. Never delete the maintenance gate or edit the runtime receipts by hand.
A terminal operation whose gate cleanup was interrupted only completes that cleanup; it does
not restore old data after successful operation has resumed.

### Automatic cleanup

After a successful upgrade or verified rollback, Enso removes the temporary snapshot and
failed-state copies. It keeps only the latest small operation record and log. A successful
upgrade keeps the current runtime and at most one previous runtime while the old updater exits;
the next update removes that previous runtime before staging another. Failed candidates,
unused managed Python versions, and the download cache are cleaned automatically.
Packages are copied into each runtime, so clearing the cache cannot break the installation.
This keeps normal repeated upgrades from accumulating installations or backups.

If recovery is incomplete, its original snapshot and runtimes remain intact and work stays
paused. Cleanup errors appear in `enso update status --json` and are retried on the next
update; they never undo a successful upgrade. Keep independent backups for long-term recovery.

This is recovery for an incomplete update, not a general downgrade command. Once a release
has admitted work, there is no automatic rollback to an older snapshot. The database still
refuses code that understands an older schema, before changing the database or its journal
mode; there is no backward migration. Follow release-specific instructions for intentional
data recovery and retain independent backups of your home.

### Adopt an existing installation

This procedure requires a 0.2.0-or-newer home compatible with the selected release.

An editable checkout or `uv tool` install is unmanaged and never changes itself.
[The local development loop](development.md#local-development-loop) connects a maintainer's
services to a checkout and refreshes it without replacing home content. For the
one-time move, stop its daemon and viewer, then run the release installer with `--adopt` and
the same home and bin directory. The previous launcher is preserved beside the new one;
existing configuration and user content stay in place. The installer refuses an active
daemon or viewer and checks that the candidate can read the existing home before switching.
If structural migrations are needed, first adopt a compatible release, then use `update apply`.

```bash
enso service stop
enso web stop
enso update install --adopt
enso init --json
enso config apply --file ~/.enso/config.json --json
enso service install
```

The default source is the official GitHub feed. Use the release shell installer with
`--adopt` instead if uv needs bootstrapping, and reinstall `enso web install` if the viewer
was supervised. `--manifest` selects a specific release; `--feed` chooses its future update
source. A development version ahead of the feed reports that state without recommending a downgrade.

Use the actual home path if it is customized. `init` adds missing bundled skills and
instructions; applying the existing valid configuration seeds missing bundled jobs, including
the nightly release check. Neither command replaces existing authored content. Review and merge
new guidance into historical bundles without recorded baselines when needed. Reinstalling the
service makes it point at the stable launcher. Cloud performs this adoption while its guest
services are stopped; a fresh guest starts from the same managed release format.

See [CLI](cli.md#updates) for command fields and [Releases](releasing.md) for producing the
bundle. Development and [Docker smoke checks](upgrade-testing.md) use scratch homes and never
the active `~/.enso`.
