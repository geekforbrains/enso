# Enso workspace

This directory is a named Enso workspace: the shared content root and provider working directory for every Telegram conversation, Slack route, or scheduled job bound to it. The directory itself is not a security boundary. Its configured policy defines provider and command authority, and the shared Enso instructions are injected separately at every launch.

## Local conventions

- Keep project-specific context and operating conventions in this file; shared Enso workflow belongs in `~/.enso/AGENTS.md`.
- Put durable workspace knowledge in `knowledge/`.
- Put ordinary generated or editable output in `drafts/`.
- Enso stores downloaded chat attachments in persistent `uploads/<random-id>/` directories. These are retained workspace files; the operator decides when to remove them.
- Treat `AGENTS.md`, `CLAUDE.md`, `.agents/`, `.claude/`, and skill definitions as control files. Do not modify them unless the request and active policy explicitly allow it.
- Do not assume another workspace is available unless this file names its path and the active policy allows access.
