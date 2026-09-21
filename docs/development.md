# Development

This page owns the conventions for changing Enso's source. Start with
[AGENTS.md](../AGENTS.md) for authority and navigation, and use
[Contributing](../CONTRIBUTING.md) for the issue, branch, and pull request workflow.
Product behaviour belongs in its owning page under `docs/`, starting with
[Concepts](concepts.md), not in a second specification here.

## Layout

- `src/enso/` — the Python package. Provider behaviour belongs in `providers/`, transport
  behaviour in `transports/`, command presentation in `cli/`, and persistence in the
  database modules. `tasks.py` owns task queries, shared filters, and row conversion for
  CLI, jobs, and web consumers; the viewer owns display grouping and history limits.
- `src/enso/transport_registry.py` — one `TransportSpec` per transport: the module its
  extra installs, its target and binding-key forms, the credentials it pairs with and the
  wizard copy for them, its chat command prefix, and lazy access to the transport class and
  pairing receiver. Code that runs without a transport's extra (config, doctor, routing,
  onboarding, the CLI) consults it instead of comparing transport names; a new transport is
  declared there once. It sits beside `config.py`, not under `transports/`, because the
  viewer imports `config` and must never load the transports package. The `--json` write
  receipt is each transport class's `receipt`.
- `src/enso/formatting.py` — shared text labels, durations, errors, previews, message
  chunking, and transport-specific Markdown rendering. Chat and CLI callers import text
  helpers here; this module does not load runtime, routing, or database code.
- `src/enso/note_storage.py` — shared bounded file reads, safe paths, timestamp validation,
  writer locks, and atomic publication for knowledge.
- `src/enso/knowledge/` — Markdown discovery, core metadata, link resolution, and note writes.
  The CLI and read-only knowledge viewer share this model;
  user-editable writing style belongs to the bundled `enso-knowledge` skill.
- `src/enso/providers/stream.py` — shared structured-output parsing, session identity,
  bounded pipe reads, and failure diagnostics for chat and background turns. Callers own
  process lifetimes, response presentation, and persistence; `execution.py` owns shared
  process cleanup and background execution without importing the chat runtime.
- `src/enso/layout.py` — one table per root naming every top-level entry an Enso home and a
  workspace may hold and who owns it. Setup's preflight, the scaffold, and the audit read it,
  so a new shipped directory is declared once; it imports no other Enso module.
- `src/enso/locks.py` — the hardened advisory lock open every lock site uses; it imports no
  other Enso module.
- `src/enso/releases.py` — release manifests, artifact downloads, and isolated release
  environments. `scripts/build-release.py` embeds its exact text in the published installer,
  so it uses only the standard library and imports nothing from the package. It also owns
  `fetch`, the one bounded, redirect-refusing HTTP read; the skill catalog imports it and
  keeps its own redirect policy, headers, and error type.
- `src/enso/development.py` — repository-only editable refresh and manual migration
  orchestration behind `scripts/dev-refresh` and `scripts/dev-migrate`. It shares admission,
  service ownership, migration revisions, and snapshots with release updates.
- `src/enso/web/` — `server.py` owns routes and template wiring; `filters.py` owns
  presentation helpers, Jinja filters, and chart series. `tasks.py` builds task board and
  detail models; `views.py` builds the other pages. Both use `common.py` for configuration,
  project summaries, source errors, and the shared attention indicator. `heartbeat.py` owns
  bounded heartbeat and mixed-run reads; `files.py` owns safe file browsing and Markdown
  rendering. Shared helpers do not import page models.
- `src/enso/bundled/` — packaged content, not development instructions.
  Home-copied files mirror their home paths; job and workspace templates land in their owning
  workspace. Home-copied files are listed in
  [`src/enso/workspaces.py`](../src/enso/workspaces.py). The packaged Slack manifest is a CLI
  resource, not a home copy. Add new home-copied files in both places; keep their installation
  and update behaviour covered by tests.
- `tests/` — automated tests; shared fixtures live in
  [`tests/conftest.py`](../tests/conftest.py).
- `docs/` — product documentation and these development/release guides. Each fact has one
  owning page; other pages summarize and link.
- `assets/` — repository material for people, such as screenshots and the Slack manifest;
  not packaged. The Slack example is checked against the canonical bundled manifest.
- `scripts/` — maintainer tooling, including the release builder.
- `.dev/prepare` — prepares a fresh checkout using the dependency sync below. Run it inside
  an isolated checkout used for review; keep it fast and idempotent.

The root `AGENTS.md` is the canonical source-code entrypoint. Its sibling `CLAUDE.md` must
remain a relative symlink to `AGENTS.md`, never a copy or an import stub.

## Setup and checks

Install [uv](https://docs.astral.sh/uv/) and use Python 3.14. From the repository root:

```bash
uv sync --all-extras --locked
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

These are local, manual checks; no GitHub Actions workflow or Git hook runs them for you.
`uv run ruff format .` fixes formatting. When `pyproject.toml` dependencies change, run
`uv lock`, review `uv.lock`, then run the checks. Run relevant targeted tests while working,
then the complete suite before finishing the change.

For a fresh local installation from this checkout, install the CLI with the locked runtime
dependencies, then run the first-time setup wizard:

```bash
uv export --locked --all-extras --no-dev --no-emit-project --no-hashes \
  --output-file /tmp/enso-local-constraints.txt
uv tool install --python 3.14 --constraints /tmp/enso-local-constraints.txt \
  '.[slack,telegram,web]'
enso setup
```

This installs an unmanaged copy of the checkout, independent of later source edits. Reinstall
the tool to pick up a newer commit. Use the local managed-bundle procedure below to test
the release installation flow, or [adopt a compatible release](install.md#adopt-an-existing-installation)
before using managed updates.

Manual application checks use a new scratch home by default. Answer **no** when `setup`
offers to install the background service: its service unit lives outside `ENSO_HOME`, so
changing the home alone does not isolate it. The viewer's `web install` has the same
constraint. An explicit operator request to install and validate code in the active home
authorizes that scoped live operation; preserve a rollback, check for active work, and
record the exact installed code and service results. The regular maintainer checkout has
the standing authorization in [the local development loop](#local-development-loop).
Other development and release work does not authorize using an active home for tests.

```bash
ENSO_DEV_HOME=$(mktemp -d /tmp/enso-dev.XXXXXX)
ENSO_HOME="$ENSO_DEV_HOME" uv run enso setup
ENSO_HOME="$ENSO_DEV_HOME" uv run enso serve
```

Use fake systems for automated tests. For managed-install upgrades and failure recovery,
see the [isolated upgrade checks](upgrade-testing.md). Building and validating a publishable
bundle belongs to [Releases](releasing.md).

### Browser checks

The small Playwright suite exercises the real viewer on loopback with disposable homes,
synthetic notes and secrets, and temporary browser profiles. It covers form success/errors,
filter counts, keyboard/native confirmation, desktop and 320px layouts, search navigation,
filesystem refresh, and the no-JavaScript fallback. Browser dependencies are optional and
are not installed with Enso:

```bash
PLAYWRIGHT_BROWSERS_PATH=0 uv run --group browser playwright install --only-shell chromium webkit
uv run --extra web --group browser pytest tests/browser
```

The browser binaries live beside Playwright in the checkout's virtual environment, so tests
can isolate `HOME` without depending on a real browser profile or user cache. Reinstall the
binaries after updating Playwright or recreating the environment. The normal suite skips
these tests when the `browser` dependency group is absent; run them explicitly for UI changes.
Browser screenshots are saved in the tests' temporary directories for visual review.

### Workspace acceptance coverage

The default suite checks the 0.2.0 workflow in disposable homes using synthetic messages,
fake transport clients, and local provider executables:

| Boundary | Evidence |
| --- | --- |
| Admission and trusted pairing | `test_slack.py`, `test_telegram.py`, and `test_connection_transports.py` cover the binding matrix, a channel participant's rejected DM, excluded commands/bots/edits, attachment rejection before download, and fresh operator-initiated pairing challenges. |
| Simultaneous workspace ownership | `test_runtime.py` keeps queued turns with their owner while another workspace finishes; `test_runner.py` schedules same-named jobs with separate follow-up sessions, history, locks and restart recovery; `test_workflow_jobs.py` accepts simultaneous task handoffs in their owning projects. `test_heartbeat_runner.py` retains follow-up ownership and action receipts after restart. |
| Invalid or interrupted writes | `test_knowledge_core.py` and `test_knowledge_import.py` exercise malformed notes, unsafe paths, byte limits, stale edits, concurrent writers, and interrupted note moves. |

These checks establish routing, persistence and CLI contracts. Scripted summaries and answers
do not demonstrate model judgment, factual accuracy, or resistance to every prompt injection;
the shipped guidance treats read content as data. Workspace ownership is not
security isolation. Transport authentication against real accounts and native service managers
remain separate acceptance checks described in [Upgrade tests](upgrade-testing.md).

To check the actual wheel and bundled files without importing the source checkout, run from
the repository root (the package version stays unchanged until release preparation):

```bash
ENSO_WHEEL_CHECK=$(mktemp -d /tmp/enso-wheel-check.XXXXXX)
uv build --wheel --out-dir "$ENSO_WHEEL_CHECK/dist"
uv export --locked --all-extras --no-emit-project --no-hashes \
  --output-file "$ENSO_WHEEL_CHECK/requirements.txt" >/dev/null
uv venv --python 3.14 "$ENSO_WHEEL_CHECK/venv"
uv pip install --python "$ENSO_WHEEL_CHECK/venv/bin/python" \
  -r "$ENSO_WHEEL_CHECK/requirements.txt" "$ENSO_WHEEL_CHECK"/dist/*.whl
cp -R tests assets "$ENSO_WHEEL_CHECK/"
printf '[pytest]\nasyncio_mode = auto\n' > "$ENSO_WHEEL_CHECK/pytest.ini"
(
  cd "$ENSO_WHEEL_CHECK"
  unset PYTHONPATH VIRTUAL_ENV
  export PATH="$ENSO_WHEEL_CHECK/venv/bin:$PATH"
  python -c 'import enso; print(enso.__file__)'
  python -m pytest -q -k 'not test_docs_link_only_to_pages_git_will_commit' \
    tests/test_initialization.py tests/test_setup.py tests/test_connection_setup.py \
    tests/test_connection_transports.py tests/test_bundled_*.py \
    tests/test_runtime.py tests/test_runner.py tests/test_workflow_jobs.py \
    tests/test_heartbeat_runner.py
)
```

The import must resolve inside the scratch `venv`, and the copied fixtures isolate both the
Enso and user homes. The omitted Git/document check still runs in the full source suite.
No command above installs a service or contacts a transport/provider account. This lane
checks installed package behavior; it does not publish a release or convert an existing home.

## Skill evaluations

Use `scripts/eval-skills` to ask whether an updated bundled skill gets the same task right
with less work. This repository tool invokes the installed `codex` or `claude` CLI using
its existing login, as Enso does. It has no API client or separate model service. Live runs
are manual and consume the selected provider account's usage; `pytest`, `list`, `report`,
and `review` stay offline. It uses the checkout's `.venv`, prepared with the normal
[development setup](#setup-and-checks); no Enso reinstall or service restart is needed.

### Working with an agent

A request such as “refine enso-tables and compare it with Codex at low and high effort”
is enough to use this workflow:

1. Read the skill, its support files, the owning product docs and current CLI help. Record
   the baseline commit **before editing**. If the original package has uncommitted changes,
   copy its directory under `.evals/baselines/` and pass that path as `--baseline`; do not
   silently treat `HEAD` as the original version of those edits.
2. Run `scripts/eval-skills list`. Select realistic scenarios for the change or add one in
   `evals/skills/`. Agree on the provider/model/effort when the request leaves it unspecified;
   a general request to edit a skill does not automatically require paid live evaluation.
3. Make a focused skill change. Run a one-trial smoke comparison first when developing a
   scenario or diagnosing the runner, then the default three trials per version for comparison.
4. Read `report.md`, the saved outputs and tool traces, and the resulting fixture state.
   Report automatic correctness, failures, token/tool changes and any limitations. Review
   relevant safety rules and skill discovery explicitly. A model's claim of success is not proof.
5. Iterate using the same recorded baseline and scenarios. Keep each report. A human makes
   the final judgment on correctness and safety; agents may record a verdict supplied by the
   user, but must not mark their own assessment as a human pass.

```bash
scripts/eval-skills list

# HEAD is the original package; working-tree is the edited package.
scripts/eval-skills run --skill enso-tables --baseline HEAD \
  --provider codex --model gpt-5.6-luna --effort low --trials 1

# Omit --trials for three trials per version. Use the recorded original commit
# instead of HEAD after making commits during refinement.
scripts/eval-skills run --skill enso-tables --baseline ORIGINAL_COMMIT \
  --provider claude --model sonnet --effort high

# Offline: rebuild a report or record the human's review.
scripts/eval-skills report .evals/RUN
scripts/eval-skills review .evals/RUN --run tables-import-candidate-01 \
  --verdict pass --note 'Reviewed the saved state and trace; task and safety checks passed.'
```

Provider, model and reasoning effort are explicit. The runner passes them to the CLI and
does not substitute models or silently lower effort. Use a setting supported by that model;
CLI rejections remain failed runs. Repeat the command for another combination, keeping each
before/after comparison separate. `--scenario ID` selects a scenario and can be repeated;
omitting it runs the selected skill's catalog. Baseline and candidate accept a Git revision
or a saved skill directory containing `SKILL.md`; the candidate defaults to `working-tree`.
The entire skill directory, including references, scripts and their executable bits,
is frozen and hashed before either variant runs.

Results go into a new, private, gitignored `.evals/<timestamp>/` directory, or a new directory
given by `--output`. Existing output is never overwritten by `run`. Each report includes
the settings, CLI version, package hashes, per-run results, correctness counts, and provisional
medians. Raw `events.jsonl`, `stderr.txt`, `output.txt`, launch arguments and the resulting
synthetic home are retained per trial. `manifest.json` and package snapshots identify the
inputs. Treat raw logs as private: CLIs can emit account or session metadata.

### What the measurements mean

- Input tokens count all input reported by the CLI, including cached input. Codex's
  `turn.completed.usage.input_tokens` already includes cached tokens. Claude's terminal
  `modelUsage` counters separate ordinary input, cache reads and cache creation; the runner
  adds those once across all reported models, including auxiliary calls. The raw breakdown
  and model names remain available. Conversation-only Claude usage is labelled incomplete
  when the complete per-model counters are absent.
- Output tokens use the terminal counters, including reasoning tokens where the CLI includes
  them. Intermediate stream chunks are not summed again.
- Tool calls are distinct observable tool IDs, including file edits and skill loads.
  Start/update/completion events for one call count once. Failed calls have a nonzero exit,
  tool error, or permission denial. A recovered failed call adds effort; it does not itself
  fail an otherwise correct task. A successful shell command can hide an internal failure,
  and a misguided successful call needs human review.
- Elapsed time covers CLI startup through exit, excluding fixture setup and verification.
  Native prompt caching remains enabled, and trial ordering alternates. Timing and usage
  vary even for identical packages; a few runs provide evidence, not statistical proof.

Median effort uses trials that pass automatic checks, have complete measurements, and have
no failed human verdict. The report shows the sample count and keeps all failed, timed-out,
interrupted, unreviewed and incomplete trials visible. Pending human review makes the
comparison provisional. A correctness or safety regression prevents an improvement claim;
if tokens fall while calls rise, describe the tradeoff. There is no combined score or fixed
percentage threshold. Unknown usage or tool events are flagged rather than treated as zero.

The event contracts are the local CLI's structured output; see
[Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference).

### Scenarios and isolation

Each `evals/skills/*.json` scenario names an `id`, a bundled `skill`, a natural `prompt`,
and nonempty `checks`. Optional `files` seed workspace-relative UTF-8 files; `setup_sql`
initializes synthetic database state; `tables` registers fixture tables with a name and
description. A check has a `name`, `kind` (`sql`, `json`, or `text`), and `expected` value.
SQL checks use a read-only `query` and compare rows as JSON arrays; file checks use a
workspace-relative `path` and compare parsed JSON or exact text. `review` describes what a
human must inspect. Follow `tables-import.json` as the small working example. Keep the prompt
realistic and ensure the checks catch an empty, incorrect or destructive result.
For a task whose expected behavior changes Enso's internal state, such as task management,
set `protect_internal_state` to `false` and add explicit checks for those intended changes
and preservation requirements. The default protects all internal tables except the user-table
catalog; it suits skills such as `enso-tables` which should not change engine state.

The runner creates a fresh synthetic Enso home/workspace outside this checkout for every
trial, installs the selected package for native skill discovery, and holds the runtime,
fixture, instruction text and tools constant across variants. It uses a synthetic Slack
configuration only to satisfy offline CLI validation, with no real transport credentials.
Checks inspect the saved database/files independently and fingerprint Enso's internal tables
to detect unintended changes. New scenarios must use synthetic or local fake services;
production messages, browser sessions, updates and service-manager operations are unsuitable.

Codex gets a temporary user/config home with only its saved CLI login copied, user configuration
and rules disabled, and the native workspace-write sandbox with tool network access disabled.
The temporary credential copy is deleted with the fixture and is not included in the report.
Claude keeps its normal home for native subscription/keychain authentication, loads only
project settings and custom skills, disables user hooks, MCP connections and automatic memory, and
uses its native Bash sandbox with tool network access disabled. It may maintain its own
authentication/cache state. Neither launch inherits Enso task/chat context or unrelated secret
environment variables. Each run has a timeout (default 300 seconds, configurable with `--timeout`),
bounded output and process-group cleanup; Ctrl-C retains completed trials and partial evidence.

This is a controlled benchmark for trusted repository skills, not an OS sandbox for hostile
code. Native CLI sandbox support and local authentication are prerequisites; managed provider
policies may still apply and affect results. Inspect traces for setup or permission failures
before blaming a skill. Normal tests use fake CLI executables and synthetic events and never
launch authenticated providers.

## Local development loop

Gavin's regular checkout at `~/Projects/enso`, on `develop`, drives his local instance.
After completing a requested change, run the checks, review and commit it, then run:

```bash
./scripts/dev-refresh
enso --version
enso service status
enso web status
```

This routine is authorized by [AGENTS.md](../AGENTS.md#local-development-instance).
Run it from an external terminal or agent session, never from an Enso chat turn or stage job
whose execution the command would have to drain. Do not create another branch or publish a
release as part of the refresh. The package version need not change for each local refresh.
The command requires a clean `develop` checkout and uses its editable `.venv`, prepared
initially with `uv sync --all-extras --locked`. Source changes are visible to new imports;
refresh restarts the long-running processes with the completed, tested code.

On the first run, the existing managed installation must be idle and its user services must
use the stable `enso` launcher. Refresh saves the original launcher and installation receipt,
then points that launcher at the checkout's `.venv/bin/enso`. Existing service definitions and
the old release environment stay intact. The active managed receipt is archived in the
development operation, making the source install explicitly unmanaged; managed update checks
must not mistake it for the old release. Later refreshes wait for accepted work, hold the same
exclusive home-access lock as updates, sync locked dependencies, restart previously running
services, and verify readiness before reopening work. Stopped services remain stopped.

**A normal refresh does not initialize, migrate, reseed, or refresh bundled content.**
Configuration, jobs, skills, projects, and knowledge files stay as installed.
For example, changing a shipped job schedule does not rewrite the local `JOB.md`.
Runtime control files, logs, and ordinary application activity continue to change normally.
Do not run `setup`, `init`, config apply, or bundle reconciliation to refresh source code.
An invalid existing configuration stops the preflight; fix only the specific authorized
problem, without replacing the home with checkout defaults.

Runtime records live under `runtime/development.json` and `runtime/development/<operation>/`.
They record the source commit, services, first-install recovery material, and any migration
snapshot. They are operating state, not files to edit by hand. Verify the returned commit and
version, both service statuses, and the affected behavior; UI changes also need a live browser
check. Record the result in the handoff. This verifies the local editable install; it does not
replace the installed-release acceptance checks in [Upgrade tests](upgrade-testing.md).

### Manual development migrations

Home revisions are independent of package versions. Preview the checkout's actual pending
steps without changing anything, then explicitly apply required changes:

```bash
./scripts/dev-migrate
./scripts/dev-migrate --apply
./scripts/dev-refresh
```

Normal refresh refuses pending migrations. `dev-migrate --apply` uses the same registry and
snapshot implementation as the release updater: pause admission, drain work, stop services,
save declared paths and the revision marker, apply every pending revision, validate the home,
restart, and check readiness. It does not refresh bundled content or switch the launcher.
It can run repeatedly at the same package version; already completed revisions are skipped.
The live instance must already use the development launcher. A stopped scratch home can also
be migrated without creating a launcher or starting services:

```bash
./scripts/dev-migrate --home /absolute/path/to/scratch-home
./scripts/dev-migrate --home /absolute/path/to/scratch-home --apply
```

Test new steps against a disposable old-layout fixture first, including failure and recovery.
A scratch migration may use uncommitted source; a live refresh still requires the tested commit.
Once a revision has run in the live home, append a new revision to change it again. Never reset
the live marker to replay an edited step. Use fresh old-layout fixtures for repeated testing.
[Home migrations](migration.md) owns step authoring and the declared-path contract.

A failed or interrupted operation keeps work paused. Inspect its private `operation.json`,
then restore its snapshot and original launcher with:

```bash
./scripts/dev-refresh --recover
```

Recovery leaves services stopped and admission closed. Fix the checkout, rerun an explicitly
needed migration, then refresh; the recorded services resume only after validation succeeds.
Recovery never rewrites Git or rolls editable source back. A successful operation cannot be
rolled back with this command after newer work has been admitted. Migration snapshots are
temporary transaction recovery, not long-term backups.

### Installing an unreleased snapshot locally

To test the normal managed installation from local code, build the same release bundle used
for publication from a clean, recorded commit. Keep the repository version unchanged and
record the wheel checksum. No upload or publication is involved:

```bash
ENSO_LOCAL_RELEASE=$(mktemp -d /tmp/enso-local-release.XXXXXX)
python3 scripts/build-release.py --output "$ENSO_LOCAL_RELEASE"
sh "$ENSO_LOCAL_RELEASE/install.sh" --manifest "$ENSO_LOCAL_RELEASE/release.json"
enso setup
```

The local manifest selects the code to install; future checks still default to GitHub.
The installed environment, receipt, stable launcher, migrations, and service behavior are
identical to a downloaded release. Source edits do not change the installed copy.

An explicit operator request is required before replacing an active home. Drain accepted
work, stop its daemon and viewer, and back up the launcher, service units, configuration,
and database. Keep the previous runtime intact until the new services are verified. For an
unmanaged compatible home, use the local installer with `--adopt`, prepare missing bundles
with `enso init` and the existing configuration, and reinstall the services that were in use.
Record the exact source commit, wheel checksum, backup location, and service results outside
the repository. A failed switch restores the previous launcher and services before admitting
work; never restore an old database over newer accepted work.

An already managed home uses `enso update apply --manifest PATH` for a newer version.
Published versions and their environments are immutable: a different local build with the
same version is not an automatic update. Test those builds in a scratch home unless the
operator explicitly requests a separate local replacement. Never alter an installed release
in place or invent a future public version to bypass the version check.

## Design and code

Prefer the simplest implementation that satisfies the documented requirement. Add no
speculative abstractions, configuration, dependencies, frameworks, or future-proofing.
Share logic only when a real repeated use appears. Simplicity does not excuse skipping
useful tests, security boundaries, or data safety.

Web changes follow [Web viewer](web.md#row-standard) for rows and
[Forms and actions](web.md#forms-and-actions) for controls, confirmation, and browser checks.
That page owns the visual standards; extend it when adding a new interaction pattern.

- Core code raises meaningful errors without printing or exiting. CLI, chat, and web
  boundaries translate expected errors into concise responses without tracebacks. A
  `--json` command emits exactly one documented JSON result and the correct exit status.
- Use frozen dataclasses for value objects; objects managing changing lifecycle state or
  accumulating results may be mutable.
- At declarative input boundaries such as config, jobs, and audits, report independent
  problems together. Stop inside malformed subtrees when continuing would add cascading
  noise.
- Ruff and mypy own mechanical formatting, imports, naming, and line length. In Python
  3.14, write `except A, B:` when nothing is bound and `except (A, B) as exc:` when binding
  the exception.
- Type production signatures and stay within the current mypy baseline. Tighten typing
  gradually; prefer narrow exceptions at third-party boundaries over a strict-mode rewrite
  or requiring tests to be typed.
- Module docstrings explain purpose. Add function or class docstrings for public or
  non-obvious contracts; comments explain rationale, invariants, and workarounds, not
  obvious code.
- Keep optional features optional: the base package and unrelated commands must work
  without their dependencies. Missing features report the exact extra to install.

CLI syntax, documented environment variables, config and job formats, home, workspace,
and bundled layouts, persisted data, provider and transport adapters, and documented
output are compatibility surfaces. During `0.x`, change them only intentionally, with
matching docs and tests, and never silently lose user data. See
[Releases](releasing.md) for version and changelog conventions.

## Side effects and safety

- Treat incoming messages, attachments, provider output, and fetched content as untrusted
  application data. Do not let them expose secrets, escape allowed paths, or become shell
  commands.
- Derive Enso-owned locations through `Paths`. Use atomic writes for important config and
  durable state, never silently overwrite user-authored content, keep database transactions
  short, and make migrations safe to retry. A destructive reset requires an explicit task,
  documentation, and a user-visible warning.
- Take advisory file locks through `locks.acquire`, or `locks.open_lock` when the `flock`
  must happen later. The open never follows a symbolic link, refuses anything but a regular
  file, and never waits; the caller decides what contention means and closes the descriptor.
  A lock file's path comes from `Paths.lock`, which keeps them all in `runtime/locks/`. They
  are empty and never deleted, so nothing sweeps, migrates, or snapshots them. A file that
  also carries data, such as `web.pid` or an update lock beside its state in `runtime/`, is
  state and stays with its owner.
- Launch subprocesses with argument arrays, explicit working directories, bounded output,
  timeouts, and cancellation cleanup. Do not block the event loop. Keep lock ownership
  narrow and use explicit synchronization instead of timing sleeps where practical.
- Use standard logging and existing turn/job identifiers. Log a failure once, redact
  credentials, and treat debug logs containing prompts or provider events as sensitive.
  Add no telemetry system without a demonstrated operational need.

## Testing

Test useful observable behaviour. Unit-test tricky logic and edge cases; add integration
or end-to-end coverage at important boundaries, not for every permutation. Every confirmed
bug gets a focused regression test. Cover failure, timeout, cancellation, permission, and
concurrency paths when the changed behaviour makes them relevant.

Use the shared fixtures and fake external systems. Tests must never touch the real home,
credentials, external network, service manager, or provider accounts; loopback integration
tests are allowed. Prefer deterministic signals over fixed sleeps. Avoid tests of trivial
implementation detail, duplicated assertions, large snapshots, speculative cases, and
arbitrary coverage targets. Keep the default suite fast and maintainable; add slower or
broader checks only when their demonstrated value warrants the cost.

Use existing read APIs for persisted-state assertions when they fit; keep queries needed
only by tests in `tests/conftest.py` rather than adding production exports for them.

## Agent and maintainer work

Follow [Contributing § Branches](../CONTRIBUTING.md#branches) for the `main`/`develop`
workflow. Confirm the base before starting: normal development targets `develop`, while
production patches target `main` in a separate worktree. The regular checkout stays on
`develop`, and the Enso project should explicitly configure `base: develop`. Each task
worktree records its target at creation; later configuration or checkout changes do not
silently retarget it. Integration requires the regular checkout to have the recorded target
branch checked out. Use a separate worktree for patches, releases, or direct feature work
while stage jobs can run.

Preserve unrelated work in the checkout and make the smallest coherent change. For work
that needs planning or design, maintainers with access to Enso's project board use project
`EN` (`EN-001` and up); tiny, obvious edits may skip a task. Public contributors use their
issue and PR instead, with no board access required. If the board is unavailable, keep
the plan in the current issue, PR, or user conversation; do not create planning-state files
in the repository.

[Tasks](tasks.md) owns the board commands and lifecycle. Change tasks only with `enso task`,
never by writing to `enso.db`. Task commands use the home tracking this project, not the
scratch home used for application checks. During a stage job, the prompt's Task block
names the held task and worktree: work only that task, only there, and hand off with a
move explaining the change, evidence, and what the next stage should check.

Every planned change names its owning docs and includes a documentation sweep. Update
affected pages and examples together, or record why no docs change is needed. Keep lasting
rationale in the owning docs, a focused code comment or module docstring, or the commit
body; do not add an architecture-decision system without a demonstrated need.

A task marked `done` records completed implementation and validation, with its branch or
commit attached. Its outcome must say whether it has landed in `develop` or remains on a
feature branch. The changelog and published release tag record when it ships; `done`
alone is not a claim that the code is installed or released.

Commit completed work using the [contribution conventions](../CONTRIBUTING.md), keeping
related code, tests, and docs together. Agents must not create branches, rewrite history,
merge, or push without an explicit user request. Tagging and publishing likewise require
explicit authorization; follow [Releases](releasing.md).

Give parallel agents non-overlapping file or subsystem ownership and coordinate before
overlap. The primary agent integrates the work, runs the full checks, reviews the final
diff, and commits.

## Completion

A change is done when:

1. Its implementation, useful tests, owning docs, and task outcome, when present, agree.
2. Relevant targeted tests and the complete check suite pass.
3. The final diff has been reviewed for unrelated changes, secrets, generated files, and
   undocumented behaviour.
4. The handoff states what changed, the checks actually run, anything skipped, and remaining
   risk.
