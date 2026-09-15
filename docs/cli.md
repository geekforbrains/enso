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
enso doctor [--json]                config, home, workspaces, providers, transports, service, viewer_service, jobs, heartbeat
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
An error-level finding is a health problem and makes
`doctor` exit 1; warnings alone exit 0. That strict health result does not mean every Enso
operation is blocked: for example, a workspace layout error fails `doctor`, while `serve`
logs it and continues when the workspace directory itself still exists. Sections that need
a valid `config.json` are `skipped` until it is.

`--json` prints `{"ok", "home", "sections": [...]}`, one section per line above in that
order, each `{"name", "status", "note", "problems", "warnings", "details"}`. `status` is
`ok`, `warning`, `error`, or `skipped`; `problems` and `warnings` are the messages the text
output shows; `details` holds the facts (provider paths and whether they resolve, transport
extras, the service pid, job names, and beat counts and attention references).

`serve` logs to `~/.enso/enso.log` (rotating) and, when stderr is a terminal, to the
terminal. `--debug` adds the full prompt and every raw provider event. Each chat turn is
tagged `[t:<id>]` and each job run `[j:<name>]`, which `enso logs --turn` and `--job`
filter on; `-f` follows across rotations.

`serve`, `setup`, and the job, run-history, task, project, messaging, Slack, Telegram, and
table commands initialize `enso.db`. They reject a newer database schema with an `error:` line and exit 1
before modifying that database or starting a transport or job. This also means some CLI
reads can create or migrate a database. Filesystem-only operations such as `workspace create`
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
enso update check [--manifest SOURCE] [--notify] [--quiet] [--json]
enso update apply [--manifest SOURCE] [--drain-timeout SECONDS] [--startup-timeout SECONDS] [--json]
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

`apply` queues an independent updater and returns before the upgrade finishes. An equal
release is a no-op; downgrades, reused release versions with different code, and a second
pending operation are refused. Managed releases use stable `major.minor.patch` versions.
`--drain-timeout` defaults to 300 seconds (1–3600); `--startup-timeout` defaults to 60
seconds (1–600). The updater defers when accepted work or another ordinary CLI command is
still using the home at the drain deadline. Commands retain that access while waiting for
stdin, so an update cannot migrate underneath a pending `--file -` operation.

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
`1`, independent of the package version and configuration schema version.

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
enso heartbeat create --file FILE [--json]
enso heartbeat update REF --file FILE [--if-revision N] [--json]
enso heartbeat list [--all] [--state STATE] [--workspace W] [--limit N] [--offset N] [--json]
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
stdin with `--file -`. Creation is paused; the agent writes and validates any gate before
`resume`. No creation or validation command performs the future action.

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

```text
enso job list [--json]
enso job create --name N --provider P --model M --effort E --workspace W [--schedule S] [--project KEY --stage NAME] [--json]
enso job show NAME [--json]
enso job run NAME [--json]
enso runs list [--job NAME] [-n N] [--json]
enso runs show ID [--json]
```

`job create` needs `--name --provider --model --effort --workspace` and either `--schedule`
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

`runs list --json` returns the retained run rows, including final `session_id` and
`postrun_error` (null when absent). `runs show --json` adds an ordered `attempts` array,
whose entries carry `number`, `status`, `exit_code`, `output`, `error`, `session_id`,
`duration_ms`, `postrun_exit_code`, `postrun_output`, and `postrun_error`. Provider turns
start at 1; a reaction hook when no provider ran uses attempt 0. Plain `runs show` displays
these attempts too. History from before attempt recording was added has an empty array.

## Tasks and projects

```text
enso task add TITLE --project KEY [--body TEXT | --body-file PATH|-] [--priority N] [--backlog] [--after REF] [--from REF] [--json]
enso task list [--project KEY] [--stage NAME] [--ready] [--claimed] [--attention] [--all] [--idle-for 30m] [--json]
enso task show REF [--json]
enso task advance REF --message TEXT|- [--ref KIND:VALUE ...] [--force] [--json]
enso task return REF --message TEXT|- [--force] [--json]
enso task block REF --message TEXT|- [--after REF] [--force] [--json]
enso task resume REF [--message TEXT|-] [--to STAGE] [--json]
enso task drop REF --message TEXT|- [--json]
enso task release REF --message TEXT|- [--force] [--json]
enso task edit REF [--title T] [--body-file PATH|-] [--priority N] [--after REF] [--force] [--json]
enso task note REF TEXT|- [--attention] [--json]
enso task ref REF KIND VALUE [--json]
enso task land REF [--json]
enso task sweep [--project KEY] [--json]
enso project list [--json]
enso project add KEY --name NAME --workspace WS [--repo PATH] (--stages a,b,c:human | --flow F) [--setup CMD] [--copy PATH]... [--json]
enso workflow init KEY --preset basic|dev [--lint COMMAND --test COMMAND] [--base BRANCH] [--worktree-root PATH] [--migrate] [--json]
enso workflow show REF [--json]
enso workflow verify REF --message TEXT [--json]
enso workflow retry REF --message TEXT [--json]
enso workflow approve-rules REF --message TEXT [--json]
```

The board, its moves, the claim rules, and what each command prints are in
[Tasks](tasks.md#the-cli). A refused move exits 1 with the reason, or with `--json` the error
object described above. The actor is derived from the environment, never passed: a job run
acts as `job:<name>`, a chat turn as its sender, a terminal as the local user. `--force`,
is refused inside a run and cannot override a live execution claim or required checks; `drop` is only offered
outside one; and a run may move, release, edit, or land only the task it holds. `--message -` and `--body-file -` read stdin. `project add` takes exactly one of
`--stages` and `--flow` (`basic`, `support`, `marketing`), validates the whole
`config.json`, and writes it atomically; see [Configuration](configuration.md#projects).

`workflow init` configures an existing project. The basic preset has one unchecked stage;
dev requires a Git repository plus real lint/test commands and scaffolds plan, implement,
review, and engine integration. `--migrate` preserves old job definitions as disabled files
and remaps legacy stages while retaining task history and worktree metadata. Inspect and
drain live work before migration; review custom instructions before enabling new jobs.

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
enso message send TEXT|--file F|- [--to T] [--action-key KEY] [--json]
enso message attach FILE [CAPTION] [--to T] [--action-key KEY] [--json]
enso message list [-n N] [--json]
```

`message send` and `message attach` use explicit `--to` first, then the current beat's saved
notification destination/thread, then the conversation that started the current turn (from
`ENSO_ORIGIN_*`), then the first transport with a `notify`
target (Slack before Telegram). `--to` is `slack:C…`, `telegram:123`, or a bare id when one
transport is configured. An explicit target does not inherit the current Slack thread;
use `enso slack send -c C… -t TS` to target a particular thread.

Native sends and uploads require a stable `--action-key` inside a heartbeat run, and reject
that flag outside one. They reserve the action before connecting and record the result and
receipt automatically; do not also reserve it with `heartbeat action`. Repeated succeeded,
pending, or uncertain keys are refused with a history lookup. A heartbeat gate cannot send
messages. An unusable saved beat destination fails clearly instead of falling back elsewhere.
See [Heartbeat](heartbeat.md) for handling uncertainty and other external actions.

Text comes from exactly one of the argument, `--file`, or stdin (`-`); files and stdin
avoid having to shell-quote the message body.

Every out-of-band send is recorded. At the start of a turn, rows sent into that
conversation since its last turn are shown to the agent as `[Background messages]` and
marked consumed. A DM or Telegram chat hears everything sent to it; a channel thread hears
sends to the channel itself and to that thread. Rows the turn's own agent sent are retired
when the turn ends.

## Slack

```text
enso slack send   -c C [-t TS] (TEXT | --file F | - | --rich F) [--action-key KEY] [--json]
enso slack upload -c C [-t TS] FILE [--caption TEXT] [--action-key KEY] [--json]
enso slack edit   -c C --ts TS (TEXT | --file F | -) [--json]
enso slack delete -c C --ts TS [--json]
enso slack react  -c C --ts TS EMOJI [--json]
enso slack unreact -c C --ts TS EMOJI [--json]
enso slack thread C TS [-n N] [--all] [--json]
enso slack history C [--since 24h] [-n N] [--all] [--json]
enso slack lookup-user Q | lookup-channel Q | whois U | open-dm U|Q | refresh [--users|--channels] [--json]
enso telegram send TEXT|--file F|- [--to CHAT] [--action-key KEY] [--json]
enso telegram attach FILE [CAPTION] [--to CHAT] [--action-key KEY] [--json]
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
| `ENSO_WORKSPACE` | everything | The workspace name; the provider's cwd is its directory |
| `ENSO_ORIGIN_TRANSPORT` | chat turns | `slack` or `telegram` |
| `ENSO_ORIGIN_USER_ID` / `_USER_NAME` | chat turns | Who sent the message |
| `ENSO_ORIGIN_CHANNEL` / `_CHANNEL_NAME` | chat turns | Where it came from (`dm` for direct messages) |
| `ENSO_ORIGIN_THREAD_TS` | chat turns | The Slack thread, when there is one |
| `ENSO_JOB` / `ENSO_RUN_ID` | jobs | The job's directory name and this run's id |
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

Heartbeat clears inherited job, task, run, and chat-origin fields. Its background context
comes from the saved beat; a prior conversation is not presented as a new incoming message.
