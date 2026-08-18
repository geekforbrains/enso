---
name: docs
description: Use this skill to find the authoritative home for information, consult or maintain operator reference docs under ~/.enso/docs, record durable setup facts, or decide whether material belongs in instructions, workspace knowledge, repository docs, a configured knowledge base, a skill, job, table, draft, or reply.
---

# Docs

Operator reference docs are standing notes about this Enso installation: how a machine or service is configured, account-routing conventions, topology, and setup-specific runbooks. They live as Markdown below `~/.enso/docs/`, and their relative path is their identity.

Nothing loads every doc automatically. `enso doc list` is the dynamic index: its descriptions say what each doc contains and when to read it. Read only what the current task needs.

## Search before creating

Before writing durable material:

1. Run `enso doc list` and search the relevant workspace knowledge, repository documentation, and configured knowledge base when the policy permits it.
2. If an authoritative source already covers the subject, update the existing authoritative source instead of creating a competing note.
3. If another source should remain authoritative, link to it instead of copying its content into an Enso doc.
4. Create a narrowly scoped doc only when a durable operator or installation fact has no better home.

Keep one responsibility per doc. A description must name the material it contains and the situation in which it is worth reading. Record explicit source or account routing when it matters, and do not infer missing operator facts.

## Choose the owning source

- Put always-loaded behavior and critical rules in the applicable `AGENTS.md`.
- Put installation and operator facts, account routing, topology, and setup-specific runbooks in global Enso docs.
- Put workspace-only durable material in the workspace's `knowledge/` tree and maintain `knowledge/README.md` as its path-and-reading index.
- Put product and project facts in that product or project's repository docs.
- Put human and business knowledge in the configured knowledge base.
- Put reusable general procedures in skills. A runbook specific to this installation belongs in a doc; a repeatable method that applies across installations belongs in a skill.
- Put work that runs on a schedule in jobs.
- Put structured, queryable facts in tables.
- Put ordinary editable output in `drafts/`.
- Keep facts needed only for the current turn in the reply.

A repository or configured knowledge base takes precedence over an Enso cache of the same material. Prefer the most direct, maintained source and preserve its ownership. Links and concise routing notes are better than duplicated bodies.

## Protect secrets and freshness

Record credential locations and the account or environment they apply to, but never secret values. Use the installation's approved secret store and avoid printing credentials while inspecting configuration.

Mark volatile facts with their source and verification date, and require live verification before relying on current service state, addresses, versions, prices, schedules, permissions, or other changeable facts. Do not silently present stale notes as current.

## Read and write operator docs

List descriptions before reading:

```bash
enso doc list
```

Then read only the matching path below `~/.enso/docs/`. To create a new doc after the placement checks:

```bash
enso doc create stuff/sub_stuff.md
enso doc create homelab.md --name "Home Lab"
```

Fill the scaffold with valid frontmatter:

```markdown
---
name: Home Lab
description: Hosts and services on the home network; read before changing local routing or deployment targets.
---

Body prose.
```

The `name` is display text; the relative path remains the doc's identity. Use lowercase filenames with `_` or `-`. Make the description discovery-oriented rather than generic.

Fresh setup may provide `enso/content_model.md`, `enso/layout.md`, and `operator.md`. Treat them like all seeded content: after creation they are user-owned, may be edited or deleted, and must not be recreated by ordinary startup or repair.
