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
  writer locks, and atomic publication for knowledge and memory.
- `src/enso/captures.py` — normalized conversation history, reply delivery checkpoints, and
  processing receipts in the home database; Markdown remains the maintained memory.
  `capture_runtime.py` owns best-effort live writes. Transports supply `captures.Message`
  after admission/command exclusion, start a `CaptureWriter`, and pass it through `defer`
  before awaiting preparation. Ambient input only awaits its capture. The runtime awaits
  admission before preparation and skips duplicates; every pending reservation starts its
  own durable write, so slow preparation does not postpone capture of queued messages.
  `Reply.deliver` checkpoints every final send and fallback through the writer; transports
  identify definite rejections separately from unknown network outcomes.
- `src/enso/memory.py` — workspace-only dated Markdown, occurrence/placement validation,
  source metadata, relative links, and manual corrections. `harvesting.py` owns bounded
  capture selection, generated-result validation, publication, and receipt reconciliation.
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
- `src/enso/web/` — `server.py` owns routes and template wiring; `filters.py` owns
  presentation helpers, Jinja filters, and chart series. `tasks.py` builds task board and
  detail models; `views.py` builds the other pages. Both use `common.py` for configuration,
  source errors, and the shared attention indicator. `heartbeat.py` owns bounded heartbeat
  and mixed-run reads; `files.py` owns safe file browsing and Markdown rendering. Shared
  helpers do not import page models.
- `src/enso/bundled/` — content shipped to an Enso home, not development instructions.
  Shared files mirror their home paths; job and workspace templates land in their owning
  workspace. Shipped files are listed in
  [`src/enso/workspaces.py`](../src/enso/workspaces.py). Add new shipped files in both places;
  keep their installation and update behaviour covered by tests.
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
record the exact installed code and service results. Ordinary development or a release
request alone does not authorize using the active home for tests.

```bash
ENSO_DEV_HOME=$(mktemp -d /tmp/enso-dev.XXXXXX)
ENSO_HOME="$ENSO_DEV_HOME" uv run enso setup
ENSO_HOME="$ENSO_DEV_HOME" uv run enso serve
```

Use fake systems for automated tests. For managed-install upgrades and failure recovery,
see the [isolated upgrade checks](upgrade-testing.md). Building and validating a publishable
bundle belongs to [Releases](releasing.md).

### Workspace acceptance coverage

The default suite checks the 0.2.0 workflow in disposable homes using synthetic messages,
fake transport clients, and local provider executables:

| Boundary | Evidence |
| --- | --- |
| Live addressed/ambient input → capture → checked memory job → fresh-session recall → deliberate knowledge promotion | `test_bundled_memory.py` runs the shipped hooks, rejects an escaping output path, repairs in the same session, reads the original ambient source through the CLI, and verifies a later quiet pass makes no provider call. |
| Admission and trusted pairing | `test_slack.py`, `test_telegram.py`, `test_capture_runtime.py`, and `test_connection_transports.py` cover the binding matrix, a channel participant's rejected DM, excluded commands/bots/edits, attachment rejection before download, and fresh operator-initiated pairing challenges. |
| Simultaneous workspace ownership | `test_capture_runtime.py` keeps queued captures with their owner while another workspace finishes; `test_runner.py` schedules same-named jobs with separate follow-up sessions, history, locks and restart recovery; `test_workflow_jobs.py` accepts simultaneous task handoffs in their owning projects. `test_heartbeat_runner.py` retains follow-up ownership and action receipts after restart. |
| Invalid or interrupted writes | `test_memory.py`, `test_harvesting.py`, `test_knowledge_core.py`, and `test_capture_runtime.py` exercise malformed notes, unsafe paths, byte limits, stale edits, concurrent writers, partial delivery and interrupted publication without replaying completed inputs. |

These checks establish routing, persistence and CLI contracts. Scripted summaries and answers
do not demonstrate model judgment, factual accuracy, or resistance to every prompt injection;
the shipped guidance treats captured instructions as evidence. Workspace ownership is not
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
    tests/test_capture_runtime.py tests/test_runner.py tests/test_workflow_jobs.py \
    tests/test_heartbeat_runner.py
)
```

The import must resolve inside the scratch `venv`, and the copied fixtures isolate both the
Enso and user homes. The omitted Git/document check still runs in the full source suite.
No command above installs a service or contacts a transport/provider account. This lane
checks installed package behavior; it does not publish a release or convert an existing home.

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
