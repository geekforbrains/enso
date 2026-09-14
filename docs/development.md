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
- `src/enso/formatting.py` — shared text labels, durations, errors, previews, message
  chunking, and transport-specific Markdown rendering. Chat and CLI callers import text
  helpers here; this module does not load runtime, routing, or database code.
- `src/enso/providers/stream.py` — shared structured-output parsing, session identity,
  bounded pipe reads, and failure diagnostics for chat and background turns. Callers own
  process lifetimes, response presentation, and persistence; `execution.py` owns shared
  process cleanup and background execution without importing the chat runtime.
- `src/enso/web/` — `server.py` owns routes and template wiring; `filters.py` owns
  presentation helpers, Jinja filters, and chart series. `tasks.py` builds task board and
  detail models; `views.py` builds the other pages. Both use `common.py` for configuration,
  source errors, and the shared attention indicator. `heartbeat.py` owns bounded heartbeat
  and mixed-run reads; `files.py` owns safe file browsing and Markdown rendering. Shared
  helpers do not import page models.
- `src/enso/bundled/` — content shipped to an Enso home, not development instructions.
  Files are laid out as they land and listed in
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

### Installing an unreleased snapshot locally

When the operator explicitly requests unreleased code on their active home, prefer a
pinned development installation over editing a managed release environment in place.
Build a wheel from a clean, recorded commit, install it with the locked dependencies in
a separate persistent environment, and record the source commit and wheel checksum.
A development version may be assigned in the build's private source copy; keep the
repository's release version unchanged. Test the installed wheel in a scratch home first.

Keep the prior runtime intact and back up the launcher, service units, configuration,
and database before switching. Drain active work and serialize the switch against the
updater. As part of this explicit conversion, archive the original managed install
receipt intact; do not rewrite it to describe different code. Point the launcher at the
pinned development environment, then verify services and data before admitting work.
Record the backup and recovery instructions outside the repository. A failed switch
restores the previous launcher and receipt; do not restore an old database over newer
accepted work.

This installation is deliberately unmanaged: release checks report development mode,
and later code updates are manual. Returning to managed releases uses
[the adoption procedure](install.md#adopt-an-existing-installation) with stopped services
and the intended release manifest. Do not reuse a published version for different code,
alter an existing immutable environment, or assign a future public version just to bypass
the managed updater's version checks.

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
`develop` because Enso reads its current branch when creating and landing task worktrees;
it does not pin a task's base permanently at creation. Do not switch that checkout for a
patch or release while stage jobs can run. A user-authorized direct feature branch there
is appropriate only while the project's stage jobs are disabled; return it to `develop`
before resuming them.

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
