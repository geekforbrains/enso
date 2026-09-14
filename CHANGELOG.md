# Changelog

All notable changes to Enso are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- The managed updater stops and restarts the agent and viewer through the same
  service-manager code as `enso service` and `enso web`, so an unsupported platform or
  a failing launchd/systemd command reports the same way in every command.

### Added

- `enso web install` and `uninstall` manage an optional viewer user service on macOS
  and Linux. It starts at login, restarts after crashes, reports its health in `doctor`,
  and cooperates with viewer start/stop and managed release updates.

### Fixed

- Reject conflicting provider session IDs consistently in chat and background turns,
  preventing resumed chats from silently switching conversations. Unrecognized provider
  output now reports an error instead of establishing a chat session.

### Security

- Every lock file under the home, including the per-job locks under `jobs/` and the
  viewer pidfile, is opened without following symbolic links and refused unless it is a
  regular file.

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
