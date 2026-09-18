# Changelog

All notable changes to Enso are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Chat conversations can select a configured provider, model, and effort with Slack `!use`
  or Telegram's `/use` picker, then return to their workspace default without clearing
  provider sessions.

## [0.2.1] - 2026-09-18

### Added

- The workspace audit warns, with check id `script`, when a project's `setup`, stage
  `command`, check `command`, or `hooks` entry is a bare `./script` beside `PROJECT.md`
  that is missing or not executable. `--fix` never writes a script.

### Changed

- New homes no longer receive a copy of the Slack app manifest; `enso slack manifest`
  remains the source for setup. Upgrades remove untouched old copies and preserve edits.
- Knowledge opens as a folder hierarchy over shared and workspace roots, with configurable
  recently updated notes above the Workspaces list at its Index. The former Recent view is
  removed; All notes is now newest-first and its page size is configurable in `web.knowledge`.
- Knowledge rows keep folder counts and chevrons on one line and show calendar dates for
  notes older than seven days.
- Reduce the default `enso-memory` job schedule from every 15 minutes to hourly.
- Simplify memory viewer rows to a status dot, readable note name, source count, and relative
  occurrence time. Memory titles omit generated UUID suffixes in lists and detail views.

### Fixed

- Scheduled memory jobs now retry brief shared-writer collisions, so workspaces starting
  together do not fail their prerun while another workspace prepares a batch.
- Heartbeat retention now removes a closed beat's script directory, record, and lock file
  together, removes the record last so an interrupted pass is retried, reclaims lock files
  left by earlier pruning, and keeps a beat with an unreconciled action or undelivered
  notification, logging why.

## [0.2.0] - 2026-09-17

### Breaking

- The bundled workspace memory job is now `enso-memory`, referenced as
  `<workspace>:enso-memory`. Unreleased development homes with the old bundled `memory`
  job must retire it when installing the new job to avoid duplicate harvests; an operator's
  separate `memory` job is preserved.
- 0.2.0 does not read a 0.1.x home, configuration, or database. Automatic migrations
  start at 0.2.0; conversion from older versions is unsupported.
- Automatic discovery and adoption of old home-level worktrees are removed. Task worktrees
  use their configured root or recorded path; existing files elsewhere remain untouched.

### Changed

- Memory harvesting now records only notable events: a decision, a commitment, a change,
  or an outcome. The bundled `enso-memory` job, skill, and batch guidance state that most
  batches produce no note and that an explicit `no_memory` result is a successful run,
  so ordinary discussion no longer becomes a memory.
- The official installer keeps its own uv executable and defaults to GitHub releases,
  including local-bundle installs. Update checks also work for unmanaged installations
  and explain adoption; terminal `enso update apply` needs no workspace argument.
- Home migrations have ordered, cumulative revisions for database, configuration, and
  file changes; fresh homes record the latest revision directly.
- Updates snapshot the new release's declared paths, including absent destinations, and
  restore code and data together on migration or startup failure. Completed upgrades and
  rollbacks clean their snapshots and failed candidates; repeated upgrades retain only the
  latest operation and at most two runtimes. Retired untouched bundles are removed safely.

- The audit now covers installation hygiene from one core layout definition shared by
  setup, the scaffold, and `enso workspace audit`. It classifies each top-level entry as
  required, managed, user-owned, extension, or unexpected; reports unexpected entries in
  the home as well as in a workspace, irregular optional links and operating roots,
  `config.json`/`runtime/`/`secrets/` with group or other access, and SQLite sidecars left
  by a removed `enso.db`. `--fix` removes shared access while preserving owner permissions,
  repairs a missing `workspaces/` root, and still never deletes anything. `--json` gains
  `layout` and `attention` per root and per finding.

- `enso doctor --attention` exits 1 for anything worth reporting rather than health
  problems alone, and `--json` gains `attention` on the report and each section. Plain
  `enso doctor` keeps its exit codes. The bundled `enso-audit` job uses the new flag, so a
  working but untidy home is reported; an orphan workspace or an unedited `AGENTS.md`
  template still keeps it silent.

- The read-only web viewer now has a Memory page with workspace-scoped memories and
  capture evidence, receipt-based processing status, source links, and paginated filters.

- Setup explains binding trust and shared memory, and home/workspace guidance routes current
  reference and dated recall through their respective skills. Setup and offline initialization
  reject obsolete homes before seeding content.

- Viewer workspace routes now reject linked workspace directories and avoid scanning roots
  outside the workspace. Health retains diagnostic context and detailed note-audit commands
  when findings exist.

- `enso doctor` now summarizes knowledge and memory audits with bounded findings, note
  counts, and paths, including invalid roots and capture sources; detailed scoped audits
  remain available without modifying notes.

- Knowledge commands default to the selected workspace, with `--workspace` or explicit
  `--shared` replacing `--scope` and implicit shared/all-root defaults. UUID operations
  respect the selected root; moves use `--to-workspace` or `--to-shared` for transfers.

- Background messages retain their sending workspace through chat binding changes. CLI
  sends and operational lists require `--workspace` or `ENSO_WORKSPACE`; sends can explicitly
  select another workspace, and lists offer `--all-workspaces`. Registered tables and the
  read-only viewer remain installation-wide.
  Update requests and release notifications use an explicit workspace, then
  `ENSO_WORKSPACE`, then `default`; completion notifications retain that owner through restarts.

- Heartbeat gates and helpers now live under their saved workspace's `heartbeat/<REF>/`.
  Creation requires `--workspace` or `ENSO_WORKSPACE`; JSON `workspace` fields and transfers
  of existing beats are rejected. Scheduling, run history, action receipts, and script
  cleanup retain the recorded owner through restarts and context changes.

- Projects now live in workspace `projects/<KEY>/PROJECT.md` files; duplicate keys and the
  old `projects` config block are rejected. Setup, checks, command stages, and lifecycle
  scripts run beside the definition, with task code in `ENSO_TASK_DIR`. Task commands select
  a workspace, persisted task ownership prevents silent reassignment, and stage jobs must
  share their project's workspace. Workflow replacement preserves tasks without the old
  automatic stage-name conversion.

- Jobs now live under `workspaces/<workspace>/jobs/` and use `workspace:job` references
  across commands, runs, alerts, task claims, and the viewer. Setup and managed bundle
  refresh use the same paths, preserving customization. Concurrency groups remain shared
  across workspaces. The database starts a new schema line; old databases and home-level
  jobs are refused without conversion.

- Conversation bindings are the sole chat access rule. Telegram's `allowed_users` setting
  is rejected; pairing writes only the user binding and notification target. Unbound
  conversations receive one fixed notice, and missing workspaces cannot admit messages,
  attachments, or chat commands.
- Workspace agent and provider-argument overrides now live in optional `WORKSPACE.md`
  files, reloaded alongside installation settings; the old `workspaces` config block is
  rejected. Workspace scaffolding adds memory, job, and project directories while retaining
  provider discovery, and queued turns keep their selected workspace through rebinding.

- Configuration now requires schema version 2; version-1 files are refused with a link to
  the migration guide. Enso's workspace restriction mode, provider prerequisite gates,
  policy audit findings, and mode-dependent config-write guards are removed. Provider
  arguments, native permissions, transport authentication, and pairing remain unchanged.
- Agent task handoffs are submitted for Enso acceptance after execution finishes. Required
  stage checks, when configured, run outside the model and retain candidate-bound evidence,
  bounded repair/return history, and diagnostics. Simple unchecked workflows remain valid.
- Task worktrees default to `<repo>/.worktrees`, support custom roots, and retain their
  recorded target and ownership across configuration changes. Project concurrency allows
  different tasks in different stages while execution and landing remain serialized where needed.
- CLI text inputs use one bounded reader: messages, task notes, task bodies, heartbeat input
  (including checkpoints), and Slack rich-message envelopes accept 256 KiB, `config apply`
  1 MiB, and `connect` 16 KiB. Limits apply equally to text arguments, files, and stdin,
  counting UTF-8 bytes. Previously unbounded inputs are refused with consistent errors,
  including invalid UTF-8 in rich-message files.
- The managed updater stops and restarts the agent and viewer through the same
  service-manager code as `enso service` and `enso web`, so an unsupported platform or
  a failing launchd/systemd command reports the same way in every command.

### Added

- SQLite capture storage with permanent transport message identities, separate reply and
  delivery outcomes, bounded workspace queries, recoverable processing receipts, and
  workspace-checked memory source references. Slack and Telegram capture admitted human
  messages before preparation, including unaddressed Slack discussion, and retain final
  replies with rich-text fallback and per-part delivery outcomes. Retries keep their first
  workspace, stopped/dropped turns retain their outcome, and capture failures leave normal
  handling available without resending work.

- Workspace Markdown memory with explicit event dates, UTC date folders, undated imports,
  stable IDs, relative links, and conflict-checked corrections. `enso memory` lists, searches,
  reads, creates, updates, and audits manual notes without a database. Bounded harvesting
  validates workspace sources, records explicit no-memory results, and reconciles durable
  publication receipts before advancing progress; source captures remain inspectable.
  The bundled `enso-memory` skill guides recall and corrections; each workspace gets an
  enabled 15-minute `enso-memory` job with a fixed input budget and no provider call when quiet.
  `enso memory remove` previews one note and requires `--yes` to delete it, preserving
  source captures and processing receipts so ordinary sweeps do not recreate it.

- Shared `knowledge/` and automatically discovered workspace knowledge, with a read-only
  folder and search viewer, stable note URLs, Markdown and wiki links, and backlinks.
  `enso knowledge` maintains minimal metadata and repairs links on moves; the bundled
  `enso-knowledge` skill supplies customizable formatting rules and a style checker.
  A scripted importer copies existing Markdown collections without changing the originals.
- `enso workflow` commands and the bundled `enso-workflow` skill for a simple unchecked flow,
  a checked development preset, existing-job migration, audit history, and explicit recovery.
- Command and integration stages without a model, optional lifecycle scripts with durable
  retry history, and task/run web views showing handoff acceptance, checks, repairs, and cleanup.
- `enso web install` and `uninstall` manage an optional viewer user service on macOS
  and Linux. It starts at login, restarts after crashes, reports its health in `doctor`,
  and cooperates with viewer start/stop and managed release updates.

### Fixed

- Knowledge writes refuse duplicate note identities even when selected by path. Adoption
  writes known timestamps in UTC without inventing dates, and edits cannot publish an
  update time earlier than document creation.
- Every paginated viewer page derives its shown range from the listed rows and reports
  count failures without listing rows; the Heartbeats list no longer claims a row range it
  did not render when its listing fails.
- Reject conflicting provider session IDs consistently in chat and background turns,
  preventing resumed chats from silently switching conversations. Unrecognized provider
  output now reports an error instead of establishing a chat session.
- `enso workspace audit` reads jobs with the configuration it was given, so jobs serving a
  stage Enso runs itself — a `command` stage or `integrate: true` — appear in a workspace's
  job list instead of vanishing. A workspace whose only jobs are of that kind is no longer
  reported as an orphan, and `enso serve` no longer skips its startup warning.

### Security

- Every lock file under the home, including the per-job locks under `jobs/` and the
  viewer pidfile, is opened without following symbolic links and refused unless it is a
  regular file. This also applies when checking viewer status or stopping the viewer.

## [0.1.2] - 2026-09-10

### Fixed

- Start browser profiles only when a browser tool needs Chrome or the user opens one,
  so MCP startup and tool discovery no longer open unused browsers; fresh installs and
  new profiles use the same behaviour while existing registrations and sessions remain usable.

## [0.1.1] - 2026-09-10

### Changed

- Refresh Enso's default voice and add conversational onboarding for learning about the
  people and space around an install, with brief cues for when to load each core skill.
  Existing customized instructions are preserved.

### Fixed

- Correct bundled skill guidance for live changes and restart exceptions, custom home
  database paths, workspace retirement, safe job testing, and task handoffs.

## [0.1.0] - 2026-09-10

Initial public beta release of Enso: a personal AI assistant that runs on your
machine, connects to your chat, and keeps work moving between conversations.

### Added

- Chat with Enso in Slack or Telegram, powered by Claude Code, Codex, Grok,
  Antigravity, or OpenCode.
- Workspaces for your preferences, project instructions, files, and skills, with
  persistent browser profiles and an optional skill catalog.
- Scheduled jobs for recurring work and Heartbeat for reminders and follow-ups.
- Project task boards with stages and handoffs, plus a read-only web viewer for
  schedules, activity, and work that needs your attention.
- One-line installation on macOS and Linux, verified release bundles, and managed
  updates with recovery when an upgrade fails.
