# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] (0.11.0)

### Added

- Telegram reply support — reply to any message in the chat and the quoted context is included in the prompt, with the bot's response visually threaded back
- Context window usage percentage in Telegram response prefix — `(Claude / 11% / 23s)`
- Launchd plist now snapshots API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.) so provider CLIs work under launchd's minimal environment
- 15-minute hard timeout for background jobs (previously could hang indefinitely)

### Fixed

- Token usage percentage now uses last assistant turn's per-turn counts instead of cumulative modelUsage totals (was over-reporting by 3-4x)
- Collapse excessive blank lines (3+) in formatted output

## [0.10.0] - 2026-04-02

### Added

- Inline keyboard buttons for `/use`, `/model`, and `/clear` — tap to select instead of typing
- `/model` now shows available models as tappable buttons (merged `/models` into `/model`)
- `/clear` shows "Clear current" / "Clear all" buttons instead of requiring `/clear all`
- Active provider/model marked with ● in button lists

### Removed

- `/models` command (folded into `/model`)

### Fixed

- Progressive backoff on status message edits to avoid Telegram flood control errors

## [0.9.1] - 2026-03-31

### Changed

- Prerun exit code convention: `exit 1` = no work (silent skip at DEBUG), `exit 2+` = real error (WARNING with stderr)
- "Running job" log now fires after the gate passes — idle gated jobs produce zero INFO output
- Runner captures stderr from prerun scripts for error diagnostics

### Fixed

- harbour-poll: API failures (curl errors, empty claims) now exit 2 instead of being silently swallowed as "no work"
- youtube-playlist-summaries: playlist fetch failure now exit 2 instead of exit 1

## [0.9.0] - 2026-03-31

### Added

- `enso message attach <file> [caption]` — send files (images, video, audio, documents) to Telegram
  - Auto-selects Telegram API method based on file extension (sendPhoto, sendVideo, sendAudio, sendDocument)
  - Captions rendered as HTML with markdown conversion
- Both `send` and `attach` now queue a background message so the agent retains context of what was sent

### Changed

- Merged `enso message send` and `enso message notify` into a single `send` command — sends to Telegram immediately and queues as background context
- Removed `enso message notify` (redundant)
- Updated system prompt and bundled skills to document `attach` and simplified `send`

## [0.8.0] - 2026-03-31

### Added

- Native Telegram slash commands with autocomplete menu (replaces `!` prefix commands)
- Markdown → HTML rendering for agent responses, notifications, and CLI messages
  - Bold, italic, underline, strikethrough, inline code, code blocks, links, headers, blockquotes
  - Word-boundary guards prevent false positives on snake_case and math expressions
  - Fallback to plain text if HTML parsing fails
- Typing indicator while agents work (refreshed every 4s)
- Thinking/narration surfaced in status updates (Claude thinking blocks, Codex agent messages)
- `/logs` command to view recent log entries from Telegram

### Changed

- Status prefix format simplified to `(Provider / Xs)` — model name removed, parens instead of brackets
- Response prefix on its own line so markdown headings render correctly
- Session ID `new:` prefix stripped on spawn instead of on result, preventing "already in use" errors

### Fixed

- `clear_session` now only deletes the specific session file Enso owns, not all sessions in the project directory
- Gemini CLI flag updated (`-p` → `--prompt`) for compatibility with recent Gemini CLI versions

## [0.7.0] - 2026-03-30

### Changed

- Renamed project from Overlord to Enso
- Package name: `enso`
- CLI command: `enso`
- Config directory: `~/.enso/`
- Service identifiers: `com.enso.agent` (launchd), `enso.service` (systemd)

## [0.6.0] - 2026-03-25

### Added

- `enso message notify` command — sends directly to Telegram (real-time, not queued)
- Documented installation directory structure, symlink strategy, and agent compatibility in README

### Changed

- Telegram is now a required dependency — install with `pip install -e .` (no extras needed)
- Setup goes straight to Telegram configuration (no transport picker)
- Jobs only notify on failure — successful jobs handle their own messaging via `enso message notify`
- Rewrote system prompt and `jobs` skill to document `message notify` and silent-by-default behavior
- Response text splitting applies provider prefix before splitting (consistent across chunks)

### Removed

- `[telegram]` optional extra — Telegram is always included
- Transport discovery machinery (`available_transports`, `get_transport_class`)
- Slack/Discord placeholders

## [0.5.0] - 2026-03-19

### Added

- Pluggable transport system — Telegram is now an optional dependency
- Built-in job scheduler with croniter (60s tick, no launchd/systemd per job)
- Background message queue (`enso message send/list/clear`) with auto-injection into next conversation
- CLI subcommands: `enso job`, `enso message`, `enso service`
- Service management: `enso service status/install/uninstall/start/stop/restart/logs`
- Bundled `jobs` skill (agentskills.io spec) with format reference and examples
- Skills auto-discovered via `.claude/skills` and `.agents/skills` symlinks
- Session isolation — Enso-managed UUIDs prevent cross-session bleed with local CLI usage
- 50 pytest tests

### Changed

- Config shape: `transport`/`transports` dict replaces hardcoded `telegram` key
- Job runner is now Python (replaces bash scripts: runner.sh, notify.sh, install.sh)
- `enso job create` scaffolds with `enabled: false`, agent edits JOB.md directly
- Providers add `--` before prompts to prevent content parsed as CLI flags
- Stderr surfaced as error events (no more silent "(No response)")
- System prompt simplified — jobs detail moved to skill

### Removed

- Platform-specific per-job scheduling (launchd plists, systemd units for individual jobs)
- `BACKGROUND_MESSAGES.md` file approach (replaced by `messages.json`)
- Hardcoded Telegram dependency in core

## [0.4.0] - 2026-03-05

### Changed

- Renamed project from Operator to Overlord
- Package name: `overlord-ai`
- CLI command: `overlord`
- Config directory: `~/.overlord/`
- Service identifiers: `com.overlord.agent` (launchd), `overlord.service` (systemd)

## [0.3.0] - 2026-02-12

### Added

- Auto-install system prompt files (CLAUDE.md, AGENTS.md, GEMINI.md) to working directory on serve
- Bundled system prompt as package data so it ships with the package
- Existing user-customized prompt files are never overwritten

### Removed

- Symlinked AGENTS.md and GEMINI.md from repo root (canonical source is now bundled in package)

## [0.2.0] - 2026-02-12

### Added

- Telegram file upload support (documents, photos, audio, voice, video)
- Files downloaded to `{working_dir}/uploads/` and passed to the active agent
- Caption text included as context alongside the file path

## [0.1.1] - 2026-02-12

### Fixed

- Lowered Python requirement from 3.12 to 3.10

## [0.1.0] - 2026-02-12

Initial public release.

### Added

- Interactive setup wizard (`enso setup`) with provider detection, Telegram bot onboarding, and working directory configuration
- Telegram transport with live status updates as agents work
- Support for Claude, Codex, and Gemini CLI agents
- Chat commands for switching providers, models, stopping tasks, and managing sessions
- Background service installation for macOS (launchd) and Linux (systemd)
- Platform-aware setup summary with service management commands
