# CLI

The topic page linked from each section below defines that command's behaviour. CLI help
defines accepted command and option syntax: `enso --help` for the root and
`enso <command> --help` for a command. A disagreement between them is a bug.

Commands that operate on Enso's runtime data honour `ENSO_HOME` (default `~/.enso`). A
custom home does not relocate the fixed operating-system units managed by `service`
and `web install`.

The CLI has two audiences. You use it to operate Enso. The agent uses it, from inside a
turn or a job, to send messages, read Slack, work with tables, and move tasks. Commands
intended for agent automation expose `--json` where shown; not every operator command does.

For commands with `--json`, expected configuration, database initialization,
transport-loading, job validation, and lookup failures print exactly one
`{"ok": false, "error": "…"}` object on stdout and exit 1, with no text error on stderr.
Independent problems appear together in `error`, separated by semicolons. Invalid command
syntax, such as a missing required argument or an unknown option, still uses CLI usage
errors on stderr and exits 2. Commands that report health or validation findings, such as
`doctor`, `workspace audit`, and the onboarding commands below, keep their documented report objects.

Messages, notes, task bodies, and heartbeat definitions, messages, and checkpoints accept
at most 256 KiB (262,144 bytes) of UTF-8 per input, whether supplied as a text argument,
a file, or stdin (`-`). Slack `--rich` envelope files have the same limit, including JSON
syntax and whitespace. `config apply` accepts 1 MiB; `connect` accepts 16 KiB. These inputs
use one bounded reader and are refused with `input exceeds N bytes` in the command's usual
error output when they pass the limit. Limits count encoded bytes, not characters; input
that is not UTF-8 is refused too.

## Operating

```text
enso setup                          first-run wizard for a fresh home
enso init [--json]                   prepare a home offline; preserve existing content
enso serve [--debug]                run every configured transport and the scheduler
enso service install|uninstall|start|stop|restart|status
enso logs [-f] [-n N] [--turn ID] [--job NAME] [--grep TEXT]
enso config show
enso config check [--json]
enso config apply --file FILE|- [--expected-hash HASH] [--json]
enso config set PATH VALUE [--expected-hash HASH] [--json]
enso config unset PATH [--expected-hash HASH] [--json]
enso providers [--json]             bundled provider, model, and effort choices
enso slack manifest                 packaged app manifest, JSON on stdout
enso connect start|status|cancel|finish  private owner pairing; see Connections for arguments
enso models [--all] [--json]       look up OpenRouter models OpenCode can run
enso doctor [--json]                config, home, workspaces, providers, transports, service, viewer_service, jobs, heartbeat, knowledge, memory
enso update check|apply|status|recover  managed release checks and recovery; see Updates
```

`doctor` is one pass with one exit code: `config check`, the [workspace audit](workspaces.md)
for the home and every workspace, whether each provider path is still an executable,
whether each configured transport's extra is installed, the service state (installed,
loaded, running, and its pid, plus a warning when the unit runs a different `enso` than
the one on `PATH`), the optional viewer service, every `JOB.md`, and current heartbeat
health. `viewer_service` reports its unit, home, and process ownership; an uninstalled
viewer service is healthy, while one installed for this home but not serving it is an
error. A unit for another home is reported separately without a health error.
The heartbeat check reads saved state without running gates or migrating the database.
The knowledge and memory sections compose the existing note audits across their roots,
including metadata, identity, timestamps, placement, links, and memory capture sources.
They report note/finding counts and at most ten findings per section, with each finding's
message limited to 500 characters. Use `enso knowledge audit --workspace NAME` (or `--shared`)
and `enso memory audit --workspace NAME` for complete scoped findings. Reads reuse the
existing file parse caches; doctor never rewrites notes or creates a note database.
These checks establish structural validity, not factual truth or the most useful workspace
for a note. Unsupported shared memory and invalid roots are reported without following links.
An error-level finding is a health problem and makes
`doctor` exit 1; warnings alone exit 0. That strict health result does not mean every Enso
operation is blocked: for example, a workspace layout error fails `doctor`, while `serve`
logs it and continues when the workspace directory itself still exists. Sections that need
a valid `config.json` are `skipped` until it is.

`--json` prints `{"ok", "home", "sections": [...]}`, one section per line above in that
order, each `{"name", "status", "note", "problems", "warnings", "details"}`. `status` is
`ok`, `warning`, `error`, or `skipped`; `problems` and `warnings` are the messages the text
output shows; `details` holds the facts (provider paths and whether they resolve, transport
extras, the service pid, job names, and beat counts and attention references). The note
sections' `details` contain `notes` and `findings` counts and a `roots` object mapping scope
names to paths; they run even when configuration is invalid.

`serve` logs to `~/.enso/enso.log` (rotating) and, when stderr is a terminal, to the
terminal. `--debug` adds the full prompt and every raw provider event. Each chat turn is
tagged `[t:<id>]` and each job run `[j:<workspace>:<job>]`, which `enso logs --turn` and `--job`
filter on; `-f` follows across rotations.
Logs, debug prompts, run output, and errors may contain private installation data;
workspace filters organize context and do not restrict database or log access.

`serve`, `setup`, and the job, run-history, task, project, messaging, Slack, Telegram, and
table commands initialize `enso.db`. They reject a pre-0.2.0 database or a newer database
schema with an `error:` line and exit 1 before modifying that database or starting a
transport or job. This also means some CLI
reads can initialize a missing database. Filesystem-only operations such as `workspace create`
and service-unit management do not use this guard, and setup seeds files before it checks
the database; see [Upgrading](install.md#upgrading).

`service install` writes `~/Library/LaunchAgents/com.enso.agent.plist` (macOS) or
`~/.config/systemd/user/enso.service` (Linux) for the `enso` on `PATH`, with a `PATH`
capturing Enso's directory, the configured provider directories, Node's directory when
found, the installing shell's nonempty `PATH` entries, and standard system directories,
then starts it. See [The service](install.md#the-service) for when to reinstall the unit.

## Updates

The [installation and update lifecycle](install.md#upgrading) owns the behavior; the
[release format](releasing.md) owns artifact validation and authenticated downloads.

```text
enso update install --manifest SOURCE [--bin-dir DIR] [--extras slack,telegram,web]
                     [--token-file FILE] [--feed URL] [--viewer-service NAME] [--adopt] [--json]
enso update check [--manifest SOURCE] [--notify] [--workspace W] [--quiet] [--json]
enso update apply [--manifest SOURCE] [--workspace W] [--drain-timeout SECONDS] [--startup-timeout SECONDS] [--json]
enso update status [--json]
enso update recover [--json]
```

`SOURCE` is a trusted local `release.json` or HTTPS manifest URL. `install` selects the first
managed runtime under `ENSO_HOME`; the shell installer bootstraps uv and Python before
calling it. It writes a stable launcher into `--bin-dir` (default `~/.local/bin`) and saves
the feed and chosen extras for future upgrades. `--token-file` supplies a private bearer
token, `--feed` saves a different future manifest source, and `--viewer-service` names the
user service owning a hosted viewer. `--adopt` preserves and replaces a previous launcher;
the old daemon and viewer must be stopped. It does not configure chat or install services.
An already managed home uses `apply` instead.

`check` is read-only unless `--notify` is given. That flag announces an available release
once to the default notify target and records delivery only after a successful send. It
does not inherit a calling job or chat's destination. `--quiet` suppresses ordinary text
and expected check/delivery failures; a later check retries. `--json` always emits a report,
including an error when quiet checking fails. A check without a feed on an unmanaged
checkout succeeds with `managed: false`, `available: null`, and no available update.
`--notify` requires `--workspace` or `ENSO_WORKSPACE` for the outbox owner, even with
`--quiet`; missing or invalid context fails before checking the feed. A read-only check
needs no workspace.

`apply` queues an independent updater and returns before the upgrade finishes. An equal
release is a no-op; downgrades, reused release versions with different code, and a second
pending operation are refused. Managed releases use stable `major.minor.patch` versions.
`--drain-timeout` defaults to 300 seconds (1–3600); `--startup-timeout` defaults to 60
seconds (1–600). The updater defers when accepted work or another ordinary CLI command is
still using the home at the drain deadline. Commands retain that access while waiting for
stdin, so an update cannot migrate underneath a pending `--file -` operation.
`apply` requires `--workspace` or `ENSO_WORKSPACE` before queuing work. It records the
resolved workspace for the eventual completion notification, preserving that owner
through service restarts or chat binding changes. An explicit flag overrides the
environment; no workspace defaults to `default`. Recovery uses the saved owner.

`status` reports the installed receipt, recent running-daemon version when available, and
the latest operation. `recover` retries an interrupted operation's recovery using its
original snapshot; it never starts a competing worker or downgrades a completed update.
Keep using `status` after queueing either command. All five commands support the standard
single-object JSON error contract.

| Command | Successful JSON fields |
| --- | --- |
| `install` | `ok`, `managed`, `installed_version`, `binary` |
| `check` | `ok`, `managed`, `current`, `available`, `update_available`, optional `release_notes_url`; `notified` when requested |
| `apply` | `ok`, `operation`; a no-op also includes `current`, `update_available: false` and has `operation: null` |
| `status` | `ok`, `managed`, `installed_version`, `installed_commit`, `running_version`, `operation` |
| `recover` | `ok`, `operation` |

An operation includes `id`, `status`, `from_version`, `to_version`, `started_at`,
`updated_at`, and an `error` or `notification_error` when applicable. Terminal statuses are
`succeeded`, `failed`, `rolled_back`, `recovery_failed`, and `deferred`. A `recovery_failed`
operation keeps new work paused until recovery succeeds. Internal worker commands are
implementation details; operators and agents use the commands above.

## Onboarding contracts

The offline commands in this section work without an active config or transport extras;
`connect` is the networked pairing workflow and requires the selected transport's extra.
`init`, `config apply`, `config set`, `config unset`, and `config check` print exactly one JSON report with `--json`;
`ok: false` exits 1 and `ok: true` exits 0. Errors never print submitted credentials. Their contract `version` is
`1`, independent of the package version and the `config.json` schema version, now `2`.
Version-1 configuration files receive the [migration notice](configuration.md#configuration-ownership-in-020).
`ENSO_WORKSPACE` does not restrict config edits; validation and write-conflict checks still apply.
These commands edit only `config.json`. Workspace overrides are edited directly in
[`WORKSPACE.md`](configuration.md#workspacemd-in-020) and checked by `enso config check`.

| Command | Report fields |
| --- | --- |
| `init --json` | `version`, `ok`, `home` (path), `changes` (strings), `problems` (strings) |
| `config check --json` | `version`, `ok`, `config_hash`, `problems`, `warnings` |
| `config apply --file FILE\|- --json` | `version`, `ok`, `applied`, `config_hash`, `jobs_complete`, `restart_required`, `changes`, `problems`, `warnings` |
| `config set PATH VALUE --json`, `config unset PATH --json` | the `config apply` fields |

`config_hash` is the SHA256 of exact bytes, `"missing"` for an absent file, or `null` if
the file could not be read. Check's hash describes the same byte snapshot it validated.
Apply's hash describes the current document when it can read it, including after a
successful write with incomplete job installation. Errors reading submitted input do not
inspect the current file and use `null`. Set and unset describe the current document once
they hold the lock, and report a missing or unreadable one as a problem rather than
creating it; a refusal for a busy lock or an active pairing reports `null`, as apply does.
`restart_required` is true when the `transports`, `web`, or `logging` sections of the
replaced document differ from the applied one, which `enso serve` (for `transports` and
`logging`) and the viewer (for `web`) read only at start; it is false on a fresh home and
whenever nothing was applied. See
[Applying configuration](configuration.md#applying-configuration) for replacement,
locking, revision checks, retry behavior, and what a running service picks up on its own.

`enso providers --json` prints an offline catalog:

```json
{
  "version": 1,
  "enso_version": "0.2.0",
  "providers": [{
    "id": "claude", "label": "Claude Code", "installed": true,
    "default_model": "opus", "default_effort": "high",
    "models": [{"id": "opus", "label": "opus", "efforts": ["low", "medium", "high", "xhigh", "max"], "known": true}]
  }]
}
```

The example is abbreviated: every supported adapter and its bundled models are included.
`installed` means its executable resolves on `PATH`, not that its account is authenticated.
Efforts come from each adapter and exclude combinations it would clamp. `known` means
Enso has bundled effort information, not verified account access. OpenCode has a dynamic
model/variant catalog, so its offline `efforts` is empty, `known` is false and
`default_effort` is null; use [Models](#models) for that catalog. These choices do not read
or override operator-specific provider settings in `config.json`.

`enso slack manifest` always prints the packaged manifest JSON, without a wrapper, network
request, or `--json` flag. It works from an installed wheel without a source checkout.
Setup also seeds a local copy at `~/.enso/slack/manifest.json` without replacing an existing
copy; the command always exports the package's current canonical version.
The [connection commands](connections.md#hosted-command-contract) add token verification,
expiring owner pairing, configuration apply, and evidence of a delivered first reply.

## Models

```text
enso models [--all] [--json]
```

`models` is a read-only lookup for the models OpenCode can route through OpenRouter. Its
source of truth is the `openrouter` catalog in <https://models.dev/api.json>; it does not
combine other models.dev providers or OpenRouter's separate catalog. Each `MODEL` is the
full, copy-ready OpenCode id, including the `openrouter/` prefix, for use in
`providers.opencode.models`. By default the command keeps only models with tool calling,
because a model without it cannot act as an Enso agent; `--all` includes both kinds.

Text output has exactly these columns:

```text
MODEL  TOOLS  CONTEXT  INPUT $/M  OUTPUT $/M  EFFORTS
```

Rows are sorted by `MODEL`. `TOOLS` is `yes` or `no`; `CONTEXT` is the integer token limit
with thousands separators; input and output costs are compact decimal numbers in USD per
million tokens. An unavailable context or cost is `-`. `EFFORTS` is the model's exact,
possibly sparse list of the variants OpenCode accepts for it, comma-separated, or `-` when
it has none. It is not a range: copy one of the listed values exactly.
[Configuration](configuration.md#opencode) says where that list comes from.

`--json` prints a top-level array with one object per displayed model. For example (catalog
values can change):

```json
[
  {
    "id": "openrouter/deepseek/deepseek-v4-flash",
    "tool_call": true,
    "context": 1048576,
    "cost": { "input": 0.08092, "output": 0.16184 },
    "efforts": ["high", "xhigh"]
  }
]
```

Those are the complete fields, and objects are sorted by `id`. `context`, `cost.input`, and
`cost.output` are JSON numbers or `null` when the source omits them; `efforts` holds the
same values in the same order as the text column, empty when the model has no variant.
Costs retain the same USD-per-million unit as text output.

Enso reuses the newest valid catalog no more than one hour old from either
`$ENSO_HOME/cache/models.json` or OpenCode's `$XDG_CACHE_HOME/opencode/models.json`
(`~/.cache/opencode/models.json` when that variable is unset or empty). It only reads the
OpenCode cache. With no fresh cache it fetches models.dev and updates Enso's cache. That
refresh gets 15 seconds in total, from name resolution through connecting, redirects,
response headers and the body, so a slow name server or a slow site can delay the command
by that much and no more, however slowly it answers.
If the fetch fails or runs out of time, the newest valid stale cache is used with a warning
on stderr; without a valid fallback the command fails. A successful fetch whose cache write
fails is still displayed, also with a warning. It never reads or writes `config.json`, so
selecting a model remains an explicit operator edit. Warnings stay on stderr, including with
`--json`; a JSON load failure prints `{"ok": false, "error": "…"}` and exits 1 instead of
printing an array.

## Workspaces

```text
enso workspace list                 names, bindings, jobs, and audit state
enso workspace create NAME          scaffold the full layout
enso workspace audit [NAME] [--fix] [--json]
```

`audit` checks the home and the workspaces: the layout, the `CLAUDE.md` link, the skill
wiring, and whether each workspace is bound or used by a job. `--fix` creates and repairs;
it never deletes. The command exits 1 while any error remains; warnings alone exit 0. See
[Workspaces](workspaces.md).

### Workspace context in 0.2.0

Job and Heartbeat creation, message sends, operational lists, and task, project, and workflow
commands, knowledge, and memory use the shared resolver.
Workspace-scoped commands use `ENSO_WORKSPACE`,
inherited from the Enso chat agent, job, or Heartbeat run calling them. Optional `--workspace` overrides it
for that operation. The [context contract](workspaces.md#context-selection-in-020) owns
validation and recorded ownership; there is no directory inference or implicit `default`.
Installation-wide commands retain their installation scope.

For an agent running with `ENSO_WORKSPACE=team`, commands select context as shown below:

```bash
enso task list                         # team
enso task list --workspace personal    # deliberate lookup in personal
enso heartbeat create --file beat.json # save team as the new follow-up's workspace
enso heartbeat create --file beat.json --workspace personal
enso job list --all-workspaces         # intentional installation-wide lookup
enso message send "Report ready" --workspace personal --to slack:C0123456789
```

An explicit invalid workspace errors instead of falling back to the environment; absent
context errors when an operation needs a workspace. `--workspace` does not change the
caller's environment or transfer an existing record. It introduces no special admin role.
Command syntax is listed below and in runtime help.

Job, run, message, Heartbeat, task, and project lists accept `--all-workspaces` to ignore
`ENSO_WORKSPACE` and include the installation. It cannot be combined with `--workspace`.
Filtering happens before result limits. The viewer, registered-table catalog, workspace
inventory, and installation health checks retain their installation-wide scope.

## Knowledge

Knowledge starts in the selected workspace. `--workspace NAME` overrides `ENSO_WORKSPACE`;
`--shared` explicitly selects the home knowledge root and ignores the environment.
`--workspace` and `--shared` are mutually exclusive. Missing workspace context without
`--shared`, or an invalid explicit workspace, is an error; shared knowledge is never a
fallback. These selectors apply to reads and writes, including UUID lookup.

```text
enso knowledge roots [--json]
enso knowledge list [--workspace NAME | --shared] [--folder PATH] [--limit N] [--offset N] [--json]
enso knowledge search QUERY [--workspace NAME | --shared] [--folder PATH] [--limit N] [--offset N] [--json]
enso knowledge show REF [--workspace NAME | --shared] [--json]
enso knowledge audit [--workspace NAME | --shared] [--json]
enso knowledge create PATH --file FILE|- [--workspace NAME | --shared] [--json]
enso knowledge adopt PATH [--workspace NAME | --shared] [--expected-hash SHA] [--json]
enso knowledge update REF --file FILE|- --expected-hash SHA [--workspace NAME | --shared] [--json]
enso knowledge move REF DEST [--workspace NAME | --shared] [--to-workspace NAME | --to-shared] [--expected-hash SHA] [--json]
```

`REF` is a UUID or an exact path inside the selected root. IDs remain globally unique;
a UUID belonging to another root errors and requires an explicit selection. New/destination
paths must end in `.md`. Moves stay in the source root unless `--to-workspace` or
`--to-shared` selects another destination; those destination flags are mutually exclusive.

`--folder` includes descendants. Listing/search returns up to 50 notes by default, with
`--limit` from 1 to 500 and a nonnegative `--offset`. Search matches every whitespace-separated
term against paths and bodies, case insensitively. There is no `--scope` or all-roots search
flag: broaden deliberately with another search using `--shared` or `--workspace NAME`.
`roots` inventories all roots without requiring context. Internal root identifiers remain
`general` and `workspace:<name>` in JSON results and cross-root links.
No command loads transport configuration or initializes the database.
[Knowledge](knowledge.md) owns the filesystem, metadata, link-resolution, adoption, and
write-safety contracts.

With `--json`, `roots` returns `[{scope, label, path}]`. Listing/search returns
`{total, offset, limit, notes, problems}`. Each note contains
`{scope, path, title, id, metadata, sha256, problems}`; `show` adds `body` and `backlinks`.
`metadata` exposes only core properties; raw unknown import fields remain in the original
file. `audit` returns `{ok, notes, problems}`, where `notes` is the count and each problem has
`{scope, path, problem}`; findings exit 1, a clean audit exits 0. Read failures are findings,
while command failures use the standard error object at the top of this page.

`create` and `update` read Markdown body text from `--file` (or stdin `-`) and manage
frontmatter themselves. `update` requires the `sha256` from the last read. `adopt` preserves
unfamiliar original metadata in the note body and leaves unknown dates absent. Successful
create/adopt/update return `{ok: true, ...note}`. `move` returns
`{ok: true, id, scope, path, links_updated}`, where `links_updated` counts other notes rewritten.
Moves preserve note identity and rewrite only the resolved links the move would change or
break, in the moved note and in others; they refuse ambiguous inbound targets, stale
contents, and existing destinations.

## Memory

The memory CLI finds and maintains dated workspace history. Start in the
[selected workspace](#workspace-context-in-020); `--workspace` overrides `ENSO_WORKSPACE`.
There is no shared memory root or all-workspace search. Missing context and invalid
selections error. The bundled `enso-memory` skill guides recall and automated harvesting;
[Memory](memory.md) owns the note schema, corrections, capture, and retention.

```text
enso memory list [--workspace NAME] [--limit N] [--offset N] [--json]
enso memory search QUERY [--workspace NAME] [--limit N] [--offset N] [--json]
enso memory show REF [--workspace NAME] [--json]
enso memory create NAME.md --occurred VALUE --file FILE|- [--workspace NAME] [--json]
enso memory update REF --file FILE|- --expected-hash SHA [--occurred VALUE] [--workspace NAME] [--json]
enso memory audit [--workspace NAME] [--json]
```

`REF` is a UUID or an exact `.md` path relative to the selected memory root. UUIDs remain
stable across moves but do not override workspace selection. `create` accepts one filename,
chooses its folder from the required occurrence, assigns a UUID, and sets document dates.
`VALUE` is a quoted date or timezone-aware timestamp, or `unknown`; manual notes use
`sources: []` and describe other provenance in their body. Commands require neither
transport configuration nor an initialized database.
Nonempty source lists are validated against the capture database: missing or differently
owned captures prevent managed updates, preserving the original sources.

```bash
enso memory create launch-proposal.md --workspace team --occurred 2026-09-16 --file proposal.md
enso memory show 2026/09/16/launch-proposal.md --workspace team --json
enso memory update 2026/09/16/launch-proposal.md --workspace team --file corrected.md --expected-hash HASH
enso memory search "launch proposal" --workspace team --json
enso memory audit --workspace team --json
```

Updates preserve identity, sources, occurrence, and known creation time, require the last
read's exact-byte SHA256, and change `updated` only for substantive edits. `--occurred`
explicitly corrects the event time; a changed day requires deliberate file relocation and
link maintenance first, following the [editing contract](memory.md#manual-maintenance).
Human edits and imports are discovered directly without database registration. Resetting a
provider session does not remove memory or captures.

Listing/search defaults to 50 results, with `--limit` from 1 to 500 and nonnegative
`--offset`. Every whitespace-separated query term must match the filename/path or body,
case insensitively. Sort order is newest known occurrence first, unknown/invalid last,
then path for ties; filtering precedes pagination.

JSON list/search results contain `{workspace, total, offset, limit, notes, problems}`.
Each note contains `{workspace, path, title, id, metadata, sha256, problems}`; `show` adds
`body`, and successful writes add `ok: true`. `metadata` exposes supported properties;
unsupported import metadata remains in the original file with findings. `audit` returns
`{ok, workspace, notes, problems}`, with a note count and `{scope, path, problem}` findings
using the internal root identifier `workspace:<name>`. Findings exit 1; a clean audit exits 0.

Harvesting and source inspection use JSON on stdout:

```text
enso memory source CAPTURE_ID [--workspace NAME]
enso memory batch [--workspace NAME] [--ready]
enso memory publish --file FILE|- [--workspace NAME]
enso memory job-hook prerun|postrun
```

`source` returns the selected workspace's capture with its sender, time, text, attachments,
kind, parent, generation/handling outcome, delivery parts, and truncation notice. An ID from
another workspace is not found. `batch` recovers pending publication before emitting
`{workspace, batch, sources, guidance, segments}`. Segments preserve conversation/thread
boundaries and identify incomplete context. No-work batches have empty `sources` and
`segments`; with `--ready`, no work emits nothing and exits 1, while an error exits 2.
This lets job preruns distinguish quiet workspaces from failed recovery.

`publish` accepts at most 256 KiB of UTF-8 JSON with exactly these fields:

```json
{
  "batch": "identity returned by memory batch",
  "sources": [1201, 1202],
  "notes": [{
    "name": "launch-proposal.md",
    "body": "The team proposed September 25; testing must finish before confirmation.",
    "sources": [1201]
  }],
  "no_memory": [1202]
}
```

Copy the batch identity and ordered IDs from the batch read. Each input must be cited or
explicitly assigned to `no_memory`. A note name is one `.md` filename, at most 218 UTF-8
bytes; Enso appends a stable UUID and supplies its date folder and validated metadata.
Successful publication returns `{ok: true, receipt, sources, notes}`, with each note's ID
and path. Invalid or stale results exit 1 with `{ok: false, error}` and never overwrite
existing notes. [Memory](memory.md#validating-and-publishing-a-pass) owns reconciliation
and the distinction between source validation and factual accuracy.

`job-hook` is the bundled job's adapter, requiring its active `ENSO_RUN_ID` and
`ENSO_WORKSPACE`. The prerun pins a batch and exits 1 without output when quiet; errors
exit 2. Postrun checks stdin only after `ENSO_RUN_STATUS=ok`; a correctable result requests
a follow-up with exit 10, while storage/recovery errors exit 2. The run's input budget stays
fixed through follow-ups. Use `enso job run WORKSPACE:memory --json` for a manual sweep.

Remove one selected note, with a preview by default:

```text
enso memory remove REF [--workspace NAME] [--yes]
```

`REF` is one UUID or an exact `.md` path relative to the selected workspace's memory root.
`ENSO_WORKSPACE` supplies context unless `--workspace` overrides it; absent context or
ambiguous identity errors. With no `--yes`, the command previews the selected note and
deletes nothing. With `--yes`, it reports the exact note before deleting it. There is no
bulk removal, wildcard selection, or capture deletion.

The report names the workspace, path, ID, source references, and any metadata problems.
Deletion rechecks that exact file revision; a change after the report is an error. If an
unfinished publication receipt names the note, removal stops until `enso memory batch`
has reconciled it. Neither preview nor removal advances processing or deletes capture data.

```bash
enso memory remove 2026/09/16/launch-date-proposal.md --workspace team
enso memory remove 2026/09/16/launch-date-proposal.md --workspace team --yes
```

Inspect the first command's preview before issuing the second. Source captures and their
processing state remain intact, so a subsequent ordinary sweep does not recreate the note
from already-processed inputs. [Retention and removal](memory.md#retention-and-removal)
owns the effects on later memories, knowledge, backups, and history.

## Skills

```text
enso skill list [--available] [--json]
enso skill show NAME [--json]
enso skill install NAME [--json]
```

`list` reads only installed home skills and works offline. `--available` reads the
official `geekforbrains/enso-skills` catalog; `show` fetches one catalog entry. `install`
adds one named skill to `$ENSO_HOME/skills/`, refusing any existing destination. There
are no repository, URL, branch, force, update, or remove options. Commands do not load
transport configuration or initialize the database. See
[Customizing § Official optional skills](customizing.md#official-optional-skills) for
source restrictions, requirements, receipts, and preservation rules.

With `--json`, local `list` returns an array of
`{name, scope, path, description, problems, collision, collides_with, origin, source, commit}`.
`scope` is `enso`; `origin` is `bundled`, `official`, or `manual`. Only an official receipt
supplies `source` and `commit`; otherwise they are `null`. Invalid skill directories are
listed with `problems`, not silently omitted; use the audit for a failing health check.

Remote `list` returns `{source, commit, skills: [...]}` with each entry containing
`{name, description, files, requires, installed}`. `installed` means the home destination
is occupied, including an invalid copy or link; it does not certify that copy. `show` returns
`{source, commit, name, description, files, requires}`. `files` are paths relative to the
skill directory, and `requires` names bundled skills that must already be valid in the
home. `install` returns `{ok: true, name, path, source, commit, files, requires}`. Expected
network, catalog, validation, or filesystem failures follow the single JSON error/exit 1
contract at the top of this page; they never run code from the catalog.

## Heartbeat

```bash
enso heartbeat status [--json]
enso heartbeat create --file FILE [--workspace W] [--json]
enso heartbeat update REF --file FILE [--if-revision N] [--json]
enso heartbeat list [--all] [--state STATE] [--workspace W] [--all-workspaces] [--limit N] [--offset N] [--json]
enso heartbeat show REF [--json]
enso heartbeat history REF [--limit N] [--before ID] [--after ID] [--unhandled] [--kind KIND] [--json]
enso heartbeat pause|resume REF [--message TEXT] [--json]
enso heartbeat cancel|expire REF --message TEXT [--json]
enso heartbeat complete REF --message TEXT [--checkpoint JSON] [--json]
enso heartbeat wait REF --message TEXT [--followup-at TIME] [--checkpoint JSON] [--json]
enso heartbeat note REF TEXT [--occurred-at TIME] [--json]
enso heartbeat action REF KEY --message TEXT [--json]
enso heartbeat action-result REF KEY --status STATUS --message TEXT [--receipt TEXT] [--json]
```

Heartbeat handles finite deferred work. [Heartbeat](heartbeat.md) owns its definition fields,
lifecycle, gate convention, history, and retention. JSON definition files can be read from
stdin with `--file -`. Creation resolves `--workspace` or `ENSO_WORKSPACE` and saves that
owner; `workspace` is rejected in JSON definitions and updates. Creation is paused; the
agent writes and validates any gate in the returned workspace directory before `resume`.
No creation or validation command performs the future action.

Current instructions live in the beat record. `history` is paginated; a normal page is newest
first, while `--after` and `--unhandled` read forward. `show` includes a history pointer and
counts so an agent can fetch context on demand. `wait` records progress without fulfilling the
beat. A running agent can acknowledge only the events supplied to its run, preserving newer
events for later.

`action` records an attempt under a stable key. `action-result` records `succeeded`, `failed`,
or `uncertain`, with a receipt when available. Reusing a succeeded or unresolved key is
refused; reconcile uncertain outcomes before retrying. Actor and run identity come from
Enso's environment, never from the JSON definition or CLI flags.

## Jobs and runs

Jobs use `<workspace>:<job>` references, such as `team:digest`, throughout commands, runs,
and `ENSO_JOB`; [Workspaces](workspaces.md#ownership-in-020) owns their locations and identity.
Show, run, and `runs list --job` require a qualified reference; bare names are errors.
Job and run lists use the [selected workspace](#workspace-context-in-020), with
`--all-workspaces` for installation-wide history. `--job` further narrows that selection.

```text
enso job list [--workspace W] [--all-workspaces] [--json]
enso job create --name N --provider P --model M --effort E [--workspace W] [--schedule S] [--project KEY --stage NAME] [--json]
enso job show WORKSPACE:JOB [--json]
enso job run WORKSPACE:JOB [--json]
enso runs list [--job WORKSPACE:JOB] [--workspace W] [--all-workspaces] [-n N] [--json]
enso runs show ID [--json]
```

`job create` needs `--name --provider --model --effort`, a workspace selected by
`--workspace` or `ENSO_WORKSPACE`, and either `--schedule`
(five-field cron) or `--project KEY --stage NAME`, which make a [stage job](jobs.md#stage-jobs)
and leave `--schedule` optional. It creates no job files when one of them is invalid. Like
the other job commands, it initializes the database before validating the job.
`job run --json` prints `{"ok", "status", "run_id", "output", "error", "exit_code",
"postrun_error", "session_id", "task"}` for an executed or skipped trigger (`task` is the
reference a [stage job](jobs.md#stage-jobs) claimed, else `null`) and exits 1 unless the final status is
`ok` or `no_work`. A failed postrun fills `postrun_error` and changes an otherwise `ok`,
`no_work`, or `skipped` result to `error`; primary provider/prerun failures keep their status.
The provider/prerun `exit_code` stays distinct from the hook's exit code. Plain output prints
the hook diagnostic on stderr. A per-job lock collision has `status: "skipped"` and
`run_id: null`; a group collision has a run ID and runs its reaction hook. Configuration,
database initialization, job validation, and lookup failures use the error object described
above. `JOB.md` can override the default two postrun follow-ups with `max_followups`;
see [Jobs](jobs.md#postrun-scripts).

`job create`, `job show`, and parsed entries in `job list --json` include `ref`
(the qualified reference), `workspace`, and `dir_name` (the local name). Unparsed entries
carry `ref` and `problems`.

`runs list --json` returns the retained run rows, with qualified `job`, including final `session_id` and
`postrun_error` (null when absent). `runs show --json` adds an ordered `attempts` array,
whose entries carry `number`, `status`, `exit_code`, `output`, `error`, `session_id`,
`duration_ms`, `postrun_exit_code`, `postrun_output`, and `postrun_error`. Provider turns
start at 1; a reaction hook when no provider ran uses attempt 0. Plain `runs show` displays
these attempts too. History from before attempt recording was added has an empty array.

## Tasks and projects

```text
enso task add TITLE --project KEY [--body TEXT | --body-file PATH|-] [--priority N] [--backlog] [--after REF] [--from REF] [--json] [--workspace W]
enso task list [--project KEY] [--stage NAME] [--ready] [--claimed] [--attention] [--all] [--idle-for 30m] [--json] [--workspace W] [--all-workspaces]
enso task show REF [--json] [--workspace W]
enso task advance REF --message TEXT|- [--ref KIND:VALUE ...] [--force] [--json] [--workspace W]
enso task return REF --message TEXT|- [--force] [--json] [--workspace W]
enso task block REF --message TEXT|- [--after REF] [--force] [--json] [--workspace W]
enso task resume REF [--message TEXT|-] [--to STAGE] [--json] [--workspace W]
enso task drop REF --message TEXT|- [--json] [--workspace W]
enso task release REF --message TEXT|- [--force] [--json] [--workspace W]
enso task edit REF [--title T] [--body-file PATH|-] [--priority N] [--after REF] [--force] [--json] [--workspace W]
enso task note REF TEXT|- [--attention] [--json] [--workspace W]
enso task ref REF KIND VALUE [--json] [--workspace W]
enso task land REF [--json] [--workspace W]
enso task sweep [--project KEY] [--json] [--workspace W] [--all-workspaces]
enso project list [--json] [--workspace W] [--all-workspaces]
enso project add KEY --name NAME [--workspace WS] [--repo PATH] (--stages a,b,c:human | --flow F) [--setup CMD] [--copy PATH]... [--json]
enso workflow init KEY --preset basic|dev [--lint COMMAND --test COMMAND] [--base BRANCH] [--worktree-root PATH] [--migrate] [--json] [--workspace W]
enso workflow show REF [--json] [--workspace W]
enso workflow verify REF --message TEXT [--json] [--workspace W]
enso workflow retry REF --message TEXT [--json] [--workspace W]
enso workflow approve-rules REF --message TEXT [--json] [--workspace W]
```

Every task, project, and workflow command accepts `--workspace W`, defaulting to
`ENSO_WORKSPACE`. A missing selection is an error, including commands with explicit task
references or project keys. Task/project lists and `task sweep` accept `--all-workspaces`;
`--all` only changes whether finished tasks are shown. Explicit dependency references can
cross workspaces without changing either task's owner.

The board, its moves, the claim rules, and what each command prints are in
[Tasks](tasks.md#the-cli). A refused move exits 1 with the reason, or with `--json` the error
object described above. The actor is derived from the environment, never passed: a job run
acts as `job:<workspace>:<job>`, a chat turn as its sender, a terminal as the local user. `--force`,
is refused inside a run and cannot override a live execution claim or required checks; `drop` is only offered
outside one; and a run may move, release, edit, or land only the task it holds. `--message -` and `--body-file -` read stdin. `project add` takes exactly one of
`--stages` and `--flow` (`basic`, `support`, `marketing`) and atomically creates
`PROJECT.md` in the selected workspace; see [Configuration](configuration.md#projects).

`workflow init` updates the existing project's `PROJECT.md` and preserves its Markdown body.
Commands run beside that file; repository checks enter `ENSO_TASK_DIR` explicitly. The basic preset has one unchecked stage;
dev requires a Git repository plus real lint/test commands and scaffolds plan, implement,
review, and engine integration. `--migrate` preserves old job definitions as disabled files
while retaining task history and worktree metadata. Existing task stages and blocked
return destinations must fit the replacement; no stages are renamed automatically. Inspect
and drain live work before replacement; review custom instructions before enabling new jobs.

`workflow show` exposes durable transactions, check results, repair budgets and lifecycle
history. `verify` runs the selected stage's acceptance without a provider; it does not bypass
checks. `retry` explicitly resets retry budgets and failed lifecycle delivery for another
attempt, with the required reason recorded. `approve-rules` records an operator's review
of changed protected validation inputs; later changes invalidate that approval. Recovery
commands are refused from an agent run. No recovery command fabricates a passing result.
See [Tasks](tasks.md#stage-transactions-and-checks) for evidence and the OS trust boundary.

## Web

```text
enso web start [--port N] [--host H] [--foreground]
enso web stop|status
enso web install|uninstall
```

Read-only, and a separate process from `serve`. Without its user service, `start` runs
the viewer in the background
(its output goes to `~/.enso/web.log`, its lock to `~/.enso/web.pid`), prints the URL
once it answers, and exits 0 saying `already running` when the same viewer is live;
`--foreground` runs it directly in the terminal. `stop` signals the live viewer and waits
for it; it says `not running` when there is nothing to stop. `status` prints one line and
exits 0 only while the viewer runs. For standalone and foreground starts, flags win over
`web.host` and `web.port`; a missing or invalid `config.json` falls back to
`127.0.0.1:8787` so the Health page can report it.
`install` writes and starts the optional viewer user service; `uninstall` stops it and
removes its unit while preserving the home and logs. With the service installed for
this home, `start` and `stop` control supervision, and bind flags require `--foreground`;
edit config and stop/start to change a supervised bind. Start/install need the `web`
extra; status/stop/uninstall work without it. Viewer lifecycle changes are refused while
a managed update is pending; retry afterward. See
[Web viewer](web.md) and [service installation](install.md#the-viewer).

## Messages

```text
enso message send TEXT|--file F|- [--to T] [--workspace W] [--action-key KEY] [--json]
enso message attach FILE [CAPTION] [--to T] [--workspace W] [--action-key KEY] [--json]
enso message list [--workspace W] [--all-workspaces] [-n N] [--json]
```

`message send` and `message attach` use explicit `--to` first, then the current beat's saved
notification destination/thread, then the conversation that started the current turn (from
`ENSO_ORIGIN_*`), then the first transport with a `notify`
target (Slack before Telegram). `--to` is `slack:C…`, `telegram:123`, or a bare id when one
transport is configured. An explicit target does not inherit the current Slack thread;
use `enso slack send -c C… -t TS` to target a particular thread.

Every CLI send or upload requires an existing workspace: `--workspace` overrides
`ENSO_WORKSPACE`, and neither being set is an error before connecting or sending. This
selects the new outbox record's owner; it does not select a channel, change a binding,
or move the calling job, beat, or chat. To send context for another workspace to a different
chat, supply both `--workspace` and `--to` (or Slack's `--channel`). Runner-generated job
and Heartbeat alerts use their recorded owner's workspace.

Native sends and uploads require a stable `--action-key` inside a heartbeat run, and reject
that flag outside one. They reserve the action before connecting and record the result and
receipt automatically; do not also reserve it with `heartbeat action`. Repeated succeeded,
pending, or uncertain keys are refused with a history lookup. A heartbeat gate cannot send
messages. An unusable saved beat destination fails clearly instead of falling back elsewhere.
See [Heartbeat](heartbeat.md) for handling uncertainty and other external actions.

Text comes from exactly one of the argument, `--file`, or stdin (`-`); files and stdin
avoid having to shell-quote the message body.

Every out-of-band send is recorded with its workspace, including failed sends. At the start
of a turn, successful unread rows for that conversation and the turn's workspace are shown
as `[Background messages]` and marked consumed. Within that workspace, a DM or Telegram
chat hears everything sent to it; a channel thread hears sends to the channel itself and
to that thread. Rows the turn's own agent sent in its workspace are retired when the turn
ends; explicit sends owned by another workspace remain unread for that workspace.

Changing a binding never reassigns outbox records. For example, after a chat changes from
`work` to `personal`, unread `work` messages wait until that chat uses `work` again. A queued
turn still uses its workspace from arrival. Session reset does not delete or consume the
outbox. `message list --json` includes `workspace` alongside the source, destination, send
status, and consumption timestamp.

## Slack

```text
enso slack send   -c C [-t TS] (TEXT | --file F | - | --rich F) [--workspace W] [--action-key KEY] [--json]
enso slack upload -c C [-t TS] FILE [--caption TEXT] [--workspace W] [--action-key KEY] [--json]
enso slack edit   -c C --ts TS (TEXT | --file F | -) [--json]
enso slack delete -c C --ts TS [--json]
enso slack react  -c C --ts TS EMOJI [--json]
enso slack unreact -c C --ts TS EMOJI [--json]
enso slack thread C TS [-n N] [--all] [--json]
enso slack history C [--since 24h] [-n N] [--all] [--json]
enso slack lookup-user Q | lookup-channel Q | whois U | open-dm U|Q | refresh [--users|--channels] [--json]
enso telegram send TEXT|--file F|- [--to CHAT] [--workspace W] [--action-key KEY] [--json]
enso telegram attach FILE [CAPTION] [--to CHAT] [--workspace W] [--action-key KEY] [--json]
```

`--rich F` posts a `enso-message` envelope file (fenced or bare JSON) as native blocks.
Plain Slack replies, sends, edits, and rich-message fallbacks translate ordinary Markdown: web,
email, and Slack deep links stay clickable, while other link targets become inline code. A reply
or send whose fenced code block names a language Slack highlights is posted as a native Markdown
block instead, so the code is highlighted; edits and unlabelled fences keep the plain
translation.
Reads print `time  name (id)  ts=…` headers with names from the directory cache; `--all`
keeps joins, pins, and other lifecycle noise. It does not remove the result limit:
`thread` defaults to the root plus the latest 99 replies, and `-n 0` includes the whole
thread; `history` requests at most 20 recent top-level messages by default, with no time
cutoff unless `--since` is given. Both display oldest first. There is no search: Slack's
`search.messages` accepts only user tokens, and Enso holds a bot token.

### The message write contract

On success, a `message`, `slack`, or `telegram` write run with `--json` prints one object
and exits 0:

```json
{"ok": true, "transport": "slack", "channel": "C…", "ts": "1725…", "thread_ts": null,
 "permalink": "https://…"}
```

Telegram writes carry `chat_id` and `message_id` instead. An upload has no message `ts`: it
returns `"ts": null` and `"file": "F…"`. `edit`, `delete`, and `react` return `ok`,
`transport`, `channel`, `ts` (and `reaction`); `open-dm` returns `user` and `channel`;
`refresh` returns the `users` and `channels` counts. A failed write prints
`{"ok": false, "error": "…"}` and exits 1; Slack API failures use Slack's own error code
when available. This also covers configuration, database initialization, and
transport-loading failures before a write starts. Without `--json`, successful writes print
non-empty result fields other than `ok` as `key=value` pairs on one line.

```bash
TS=$(enso slack send -c C… "Starting" --json | jq -r .ts)
enso slack send -c C… -t "$TS" "Done"
```

## Tables

```text
enso table list [--json]
enso table register NAME --description D [--name N] [--json]
enso table schema NAME [--json]
```

Registers an existing ordinary table in `enso.db` so agents can find it with `table list`.
Re-registering it updates its description and display name; it does not change the schema.
`schema` prints columns, constraints, indexes, and the CREATE statement. Names beginning
`_enso_` or `sqlite_`, and Enso's own tables, are reserved.

## Environment for agents

| Variable | Set for | Value |
| --- | --- | --- |
| `ENSO_HOME` | everything | The home directory |
| `ENSO_WORKSPACE` | Enso-launched chat agents, jobs, and beats | The workspace name; the provider's cwd is its directory |
| `ENSO_ORIGIN_TRANSPORT` | chat turns | `slack` or `telegram` |
| `ENSO_ORIGIN_USER_ID` / `_USER_NAME` | chat turns | Who sent the message |
| `ENSO_ORIGIN_CHANNEL` / `_CHANNEL_NAME` | chat turns | Where it came from (`dm` for direct messages) |
| `ENSO_ORIGIN_THREAD_TS` | chat turns | The Slack thread, when there is one |
| `ENSO_JOB` / `ENSO_RUN_ID` | jobs | The qualified job reference (e.g. `team:digest`) and this run's id |
| `ENSO_TASK` | stage jobs | The reference of the task claimed for this run, such as `EN-041` |
| `ENSO_TASK_DIR` | stages using a worktree | The recorded task worktree; the provider's cwd is still the workspace |
| `ENSO_PROJECT_REPO` | repo stage jobs and lifecycle scripts | The regular repository path for context |
| `ENSO_TRANSACTION_ID` / `ENSO_CANDIDATE` | workflow checks | Transaction and stable candidate being evaluated |
| `ENSO_EVENT_ID` | lifecycle scripts | Stable event identity; use for idempotency/deduplication |
| `ENSO_PROJECT` / `ENSO_FROM_STAGE` / `ENSO_TO_STAGE` | lifecycle scripts | Project and accepted transition |
| `ENSO_BRANCH` / `ENSO_BASE` | lifecycle scripts | Recorded task branch and target |
| `ENSO_ATTEMPT` | workflow checks and lifecycle scripts | Current verification or delivery attempt |
| `ENSO_LIFECYCLE` | lifecycle scripts | Marks lifecycle execution; recursive task moves are refused |
| `ENSO_BEAT` | beat gates and runs | The beat reference, such as `HB-001` |
| `ENSO_BEAT_RUN_ID` | beat agent runs | This assessment's run ID |
| `ENSO_BEAT_CHECKPOINT` | beat gates and runs | The saved source checkpoint as a JSON object |
| `ENSO_RUN_STATUS` / `_EXIT_CODE` / `_DURATION_MS` | postrun only | Current outcome and cumulative elapsed time before this hook; the row is still running |
| `ENSO_RUN_ATTEMPT` / `_FOLLOWUPS_REMAINING` | postrun only | Current provider turn (0 when none ran) and remaining extra turns |

Chat-origin variables are empty when unknown. The job runner does not populate or clear
origin variables: normal scheduled runs have none, while a manual `job run` launched
inside a chat turn inherits the caller's origin and can use it for untargeted sends.
A chat turn also opens its prompt with the origin block stating the same platform, sender,
location, and thread in words; see [Concepts § Chat origin](concepts.md#chat-origin).
The block is what the model reads, and these variables are what a command it runs reads.
A stage job does the same with the [Task block](tasks.md#the-task-block) and `ENSO_TASK`;
`ENSO_JOB` and `ENSO_RUN_ID` are also how `enso task` knows it is acting for a run.

Heartbeat runs and the updater's own notifications clear inherited job, task, run, beat,
and chat-origin variables before starting a child, so the child reports as its own source
rather than the caller's. Heartbeat's background context comes from the saved beat; a
prior conversation is not presented as a new incoming message.
