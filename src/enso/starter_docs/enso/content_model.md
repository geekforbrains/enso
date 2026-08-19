---
name: Enso Content Model
description: Where durable Enso context belongs and which source wins; read before creating, moving, or duplicating persistent knowledge.
---

# Enso content model

Put information in the narrowest authoritative source that will still be found when it
is needed. Keep one responsibility per document, and give every reference document a
description that says what it contains and when to read it.

## Placement contract

- Put always-loaded behavior and rules needed on every turn in `AGENTS.md`.
- Put installation and operator facts, plus setup-specific runbooks, in global reference
  docs under `~/.enso/docs/`.
- Put workspace-only durable material with no better owner in that workspace's
  `knowledge/` directory.
- Put product and project facts in their repository docs.
- Put human and business knowledge in the configured knowledge base.
- Put reusable general procedures in skills. A procedure specific to this installation
  is a global reference doc instead.
- Put schedules and recurring automation definitions in jobs.
- Put structured or queryable facts in tables.
- Put editable output and work in progress in `drafts/`.
- Keep turn-only facts in the reply; do not create durable content for them.

## Ownership and precedence

1. Search before creating. Use `enso doc list`, repository search, the configured
   knowledge base, skill discovery, and table discovery as appropriate.
2. Update an existing authoritative source when possible. Do not create a competing
   note just because another location is easier to edit.
3. Link to authoritative material instead of copying it. Prefer repository docs or the
   configured knowledge base over an Enso cache or summary.
4. Record enough routing context to find the original source, service, and account.
   Record where credentials are stored, never credential or secret values.
5. Mark volatile facts for live verification and verify them at use time. Include a
   source and last-checked date when that makes staleness visible.

Create a new narrowly scoped document only after durable facts exist and no current
authoritative source owns them. Do not create empty account, browser, network, service,
project, or business documents as placeholders.
