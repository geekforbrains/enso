# Enso workspace

This directory is a named Enso workspace: the shared content root and provider working directory for every conversation or scheduled job bound to it. The directory itself is not a security boundary. Its configured policy defines authority, and shared Enso instructions are injected separately at every launch.

## Local conventions

- Keep project-specific context, sources of truth, and operating conventions in this file; shared Enso workflow belongs in `~/.enso/AGENTS.md`.
- Put durable workspace knowledge in `knowledge/`.
- Put ordinary generated or editable output in `drafts/`.
- Enso stores downloaded attachments in persistent `uploads/<random-id>/` directories. The operator decides when to remove them.
- Add a workspace-scoped skill only when its workflow is genuinely irrelevant outside this workspace; use the `workspace` skill for the expected layout.
- Treat `AGENTS.md`, `CLAUDE.md`, `.agents/`, `.claude/`, and skill definitions as control files. Do not modify them unless the request and active policy allow it.
- Do not assume another workspace is available unless this file names it and the active policy permits access.
