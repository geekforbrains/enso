# Changelog

All notable changes to Enso are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

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

- Every paginated viewer page derives its shown range from the listed rows and reports
  count failures without listing rows; the Heartbeats list no longer claims a row range it
  did not render when its listing fails.
- Reject conflicting provider session IDs consistently in chat and background turns,
  preventing resumed chats from silently switching conversations. Unrecognized provider
  output now reports an error instead of establishing a chat session.

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
