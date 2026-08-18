# Enso Web UI — Product Requirements

> **Status: partially implemented.** Run history and the read/write dashboard ship
> today. Items explicitly marked **Planned** are the remaining v1 scope. The specs
> under [`docs/specs/`](specs/) own implementation details and route-level status;
> [`CHANGELOG.md`](../CHANGELOG.md) records releases.

## Summary

Enso's conversational surface remains chat (Telegram/Slack): you message an agent, it
works on your machine, and the reply comes back to the thread. The shipped **local web
UI** provides a place to *see* the system rather than converse with it, backed by
persisted **run history** for jobs and registered **data tables** for
structured records the operator asks Enso to track.

The web UI is a read/write dashboard for:

- **Jobs** and their **recent runs** — output, status, and timing. The UI can run a job,
  enable or disable it, and edit its prompt body or configured prerun script; full job
  CRUD is planned.
- **Skills** — browse the bundled/installed skills; edit Enso's own.
- **Reference docs** — discover, browse, create, edit, and delete user-owned operator
  knowledge, including three fresh-install starter references.
- **Data tables** — discover registered SQLite tables and inspect their schema
  and a bounded row preview. Agents manage their schemas and rows outside the web UI.
- **Execution configuration** — inspect workspaces, each workspace's one reusable policy,
  exact Slack routes, Telegram/job bindings, and safe native-policy validation status.
- **Instructions** — read and edit the canonical shared `~/.enso/AGENTS.md`, edit managed
  workspace-root instructions, and inspect nested workspace instructions read-only.

There is **no chat in the web UI** — chat lives in Telegram/Slack. The web UI is for
overview, organisation, and managing the scheduled work Enso already runs.

## Goals

- Give a single, glanceable view of what Enso is doing and has done — jobs and their
  runs — that chat can't provide.
- Make jobs **editable in place**. Prompt and configured prerun-script editing ship
  today; schedule, provider/model, prerun configuration, and create forms are planned.
- Keep Enso **file-first where content is authored prose or procedure**: jobs, skills,
  and reference docs stay inspectable, greppable Markdown. Structured records and run
  history use SQLite because their value comes from querying them.
- Surface run history in the web UI. A matching `enso runs` CLI is planned.
- Give agents one consistent way to discover and document user-owned data tables without
  inventing a custom SQL layer.

## Non-goals (v1)

- **No chat in the web UI.** Conversation stays in Telegram/Slack.
- **No dashboard accounts / login.** Enso remains a single-operator service even when
  Slack [routes](specs/teams.md) authorize coworkers or clients to use exact chat
  routes. Those participants are not Enso accounts and receive no web access. The
  web UI binds to localhost; remote access is via Tailscale (see
  [architecture.md](specs/architecture.md)). A configured shared token is the only
  built-in authentication; leaving it empty disables authentication.
- **No public hosting.** This is not a SaaS; it runs on your machine next to `enso serve`.
- **No built-in one-off task queue.** Enso's unit of background work stays the scheduled
  **job**; one-off work is handled from chat or an external tracker (Todoist), not a
  managed task object inside Enso.
- **No rich text editor / WYSIWYG.** Prompts are Markdown in a textarea.
- **No database editor.** The Tables UI is a read-only preview: no arbitrary SQL, schema
  builder, spreadsheet editing, or destructive actions.
- **No configuration or native-policy editor.** Workspace, policy, and Slack views explain
  the active process configuration without changing `config.json` or protected provider
  policy files.

## Vocabulary

- **Job** — a *scheduled* background unit of work defined by a `JOB.md` file, bound to one named workspace and run under that workspace's configured policy on a cron by the scheduler. Recurring. This work records its runs and makes it editable from the web UI.
- **Run** — one execution of a job: when it started/ended, its exit status,
  and its captured output. Recorded in SQLite. See [data-model.md](specs/data-model.md).
- **Table** — a registered, user-owned SQLite table containing structured facts an agent
  or operator needs to query. Registration supplies discovery metadata and UI visibility;
  it does not transfer schema/row ownership to Enso. See [tables.md](specs/tables.md).
- **Reference doc** — user-owned Markdown whose relative path and frontmatter description
  provide on-demand operator context. `enso doc list` derives its current discovery index
  directly from the doc tree. See [docs.md](specs/docs.md).
- **Transport / notify** — the existing chat delivery layer (Telegram/Slack). Host-side job failure and recovery alerts ride it; successful jobs are silent unless the prompt explicitly sends a message. Slack route auditing records inbound turns only and does not change notification behavior.

## Key decisions

| Decision                       | Choice                                                                                                                                                                           |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Authored intent (jobs, skills, docs) | **Files** — Markdown + YAML frontmatter, source of truth, edited by human and agent alike                                                                                  |
| Structured storage             | **SQLite** (`~/.enso/enso.db`) for run metadata and explicitly registered user data tables; **run output blobs on disk** (`~/.enso/runs/<id>.log`)                               |
| Table discovery                | `_enso_tables` is an explicit catalog; only valid registered tables appear in the CLI/UI, while agents use standard SQLite for schema and row operations                         |
| Frontmatter                    | PyYAML `BaseLoader` for valid job metadata, with a legacy line-parser fallback for malformed older files; raw web edits preserve formatting                                      |
| Web server                     | **Starlette + Uvicorn + Jinja2**, run separately with `enso web` and sharing the file/SQLite model with `enso serve`                                                             |
| Web access                     | Bind **localhost** by default; Tailscale for remote; Host allowlist and optional shared token. No login                                                                          |
| Web capability                 | **Read/write, scoped to owned files** — edit job prompts, toggle/run jobs, edit Enso-owned skills, shared instructions, and managed workspace-root instructions. Configuration, policies, nested instructions, and external skills are read-only |
| Tables web capability          | **Read-only, bounded inspection** — list metadata, show schema, and page through capped previews; no SQL or row/schema mutations                                                 |
| Notifications                  | Reuse `transport.notify` / `enso message send`; exact Slack routing does not alter job delivery. No transport implicitly broadcasts                                              |

## Personas

One owner persona: the operator. Runs Enso as a personal service on their own machine,
talks to it from their phone via Telegram/Slack, and wants a laptop/desktop (or
phone-over-Tailscale) surface to organise scheduled work and audit what the agents have
been doing — without turning Enso into a hosted product. Slack teams participants are
authorized chat senders, not additional owners or dashboard personas.

## Features

### F1 — Run history

- Job provider executions, parsed jobs that fail execution configuration/binding validation, and failed job preruns record a **run** row in SQLite; job files that cannot be loaded, intentional prerun no-work, and triggers skipped by locking or scheduling do not. Rows include kind (`job`), name, trigger
  (`schedule`/`manual`), start/end times, exit code,
  status (`running`/`ok`/`error`/`timeout`, plus `prerun_error`/`prerun_timeout` for
  failed job gates), and a pointer to its output log on disk.
- Captured output is written to `~/.enso/runs/<run_id>.log`; the row stays lean.
- For a provider execution, a run is created at **spawn** (status `running`) and finalised at exit, so a crash mid-run leaves a visible `running` row rather than nothing. A classified pre-provider failure is written directly as a terminal run.
- Retention: a configurable cap prunes old runs (and their logs) so history doesn't
  grow without bound. See [data-model.md](specs/data-model.md).
- Surfaced in the web UI (per job and a global recent-runs feed).
- **Planned:** `enso runs list` / `enso runs show <id>` CLI access.

### F2 — Web UI: dashboard & runs

- `/` — overview: active workspace/policy/Slack-route status, recent runs, and enabled-job,
  visible-skill, doc, and registered-table counts at a glance.
- `/runs/<id>` — a run's output preview, on-disk log path, status, timing, and trigger.
- Read-only views; the data comes from SQLite (runs) and file scans (jobs and skills).

### F3 — Web UI: jobs (partially implemented)

- `/jobs` — list with schedule, provider/model, workspace, enabled state.
- `/jobs/<name>` — configuration, prompt, prerun state, recent runs, **Run now**,
  enable/disable, and confirmed directory deletion.
- Editing the prompt has a focused endpoint that rewrites only the `JOB.md` body,
  mirroring in-place skill editing.
- An existing configured prerun script can be edited in a second, mode-preserving editor
  below the prompt; unsafe or missing paths remain read-only.
- Deleting a job removes its whole directory, including companion and prerun files;
  recorded run history remains available.
- **Planned:** create and fully edit jobs from the UI: name, schedule, provider, model,
  workspace, enabled, timeout, notify, prompt body, and optional prerun script.

### F4 — Web UI: skills, execution configuration & AGENTS.md

- `/skills` lists two tiers: **Enso skills** — everything under `~/.enso/skills/`, whether
  user-created or seeded from Enso's starter set at install — **editable**; and
  **external / "parent"** skills auto-discovered from the underlying CLIs'
  own skill roots (e.g. `~/.claude/skills/`), shown **read-only with their source path** for
  awareness. Fresh setup copies the starter set once; every installed skill is user-owned
  thereafter. Startup and upgrades never seed missing bundle entries, advance older
  copies, create deletion tombstones, or clean up files copied from a skill.
- Enso-owned skill directories can be edited or deleted after confirmation. **Planned:**
  create skills and edit their tool scripts. The skills UI never writes outside
  `~/.enso/`.
- `/workspaces` and `/workspaces/<name>` show each active execution root, its one policy,
  concurrency, Slack/Telegram/job consumers, problems, and a bounded nested `AGENTS.md`
  inventory. Every lowercase kebab-case workspace name derives the exact physical root
  `~/.enso/workspaces/<name>`; external, nested, symlinked, and workspace-root Git layouts
  are invalid. Managed root instructions are revision-checked and editable; child files
  are read-only.
- `/policies` and `/policies/<name>` show reusable policy configuration, consuming
  workspaces, and safe provider-validation results. Native policy contents and secret
  values are never rendered or edited.
- `/slack` shows exact DM/channel IDs, cache-only friendly labels, workspace-to-policy
  bindings, audit/trigger state, and route problems without making Slack API requests.
- `/agents` renders the canonical shared `~/.enso/AGENTS.md`; its revision-checked editor
  writes only the regular target while leaving `~/.enso/CLAUDE.md -> AGENTS.md` intact.

### F5 — Reference docs and starter context

- `/docs` lists the current `~/.enso/docs/` tree from each file's relative path, `name`,
  and discovery-oriented `description`; there is no static index to drift when a user
  creates, edits, or deletes a doc.
- A genuinely fresh setup copies exactly three references:
  `enso/content_model.md` for content placement and source precedence,
  `enso/layout.md` for the managed filesystem and local-history boundary, and
  `operator.md` for confirmed identity, timezone/locale, preferences, and standing
  personal context. It does not seed empty topic placeholders.
- Setup persists `setup.completed_at: null` before fresh seeding, records the complete
  initial content tree in one local Git snapshot, and then writes a timezone-bearing
  completion timestamp. Failed seeds or snapshots remain retryable; if only the timestamp
  write fails, retry completes that transition without creating a second initial commit.
- Installed starters are user-owned and may be edited or deleted. Timestamped setup,
  pre-feature configurations with no setup field, structural repair, startup, dashboard
  startup, and upgrades never seed or restore them.
- The shared prompt treats starter paths as optional routing hints and relies on the
  dynamic doc list for everything the operator adds later.

### F6 — Registered data tables

- User tables live in the existing WAL-mode `~/.enso/enso.db`; `runs`, `_enso_*`, and
  `sqlite_*` remain reserved internal namespaces.
- A small `_enso_tables` catalog records the physical name, display name, and discovery
  description. Unregistered SQLite tables are not surfaced.
- `enso table list`, `enso table schema <name>`, and `enso table register <name>` provide
  the Enso-specific discovery layer. Agents use ordinary SQLite for creation, queries,
  writes, and migrations.
- A bundled `tables` skill teaches agents to inspect first, store well-typed raw facts,
  use explicit timestamps/units and safe transactions, verify writes, and confirm before
  destructive changes.
- `/tables` lists registrations; `/tables/{name}` shows schema and one bounded,
  horizontally scrollable page of escaped values. The web UI never writes table data.
- Enso applies no automatic retention or deletion to user tables.

## Success criteria (v1 target)

- Editing a job's schedule, prompt body, or prerun from the web UI writes back to
  `~/.enso/jobs/<name>/` and the next scheduled run uses the new definition.
- Every job provider execution or classified configuration/prerun failure leaves a run row with retrievable output, visible in the web UI; intentional no-work and skipped triggers remain absent by design.
- A registered data table can be discovered consistently by an agent and inspected in a
  bounded web view without exposing internal or unrelated SQLite tables.
- Every configured workspace, reusable policy, and exact Slack route can be traced in the
  web UI without exposing a transport secret or native policy source file; an invalid
  binding has a visible, actionable status.
- Read-only startup and configuration checks reject an incomplete scaffold or duplicate
  global/workspace skill name without repairing it or applying provider-specific
  precedence.
- A fresh setup exposes the three starter descriptions through the dynamic doc list,
  captures them in its single initial local snapshot, and marks setup complete only
  afterward; deleting a starter leaves it absent through restart, repair, and upgrade.
- A stale shared or managed-workspace instruction form cannot overwrite a newer agent or
  operator edit; unsafe links, path traversal, and files outside `~/.enso/` remain outside
  the browser write boundary.
- The web UI runs via `enso web`, reachable at `http://localhost:<port>` and, when
  deliberately bound there, over the tailnet.
- Slack authorization uses exact routes configured alongside credentials and transport options in `transports.slack`; Telegram remains private with exact numeric allowed-user IDs; every Telegram configuration, Slack route, and job requires a named workspace and inherits that workspace's single policy.

## Future ideas (explicitly out of v1)

Editing external "parent" skills in place (v1 keeps them read-only); run output streaming
(live tail) rather than post-hoc; a proper auth layer if Enso ever goes multi-operator;
full-text search over runs; table row/schema editing, import/export, charts, saved queries,
and first-class SQLite views.
