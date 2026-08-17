---
name: docs
description: Use this skill to consult or write operator reference docs under ~/.enso/docs when a task depends on how the user's machines, network, services, or accounts are configured, or when a durable setup fact should be recorded instead of answered only for the current turn.
---

# Docs

Reference docs are the operator's standing notes about *this* setup — how a
machine is wired, a deploy runbook, service topology, account conventions.
They live as Markdown under `~/.enso/docs/`, nested to any depth. A doc's
identity is its path relative to that root, e.g. `stuff/sub_stuff.md`.

Nothing loads docs automatically. Finding and reading the right one is your
job.

## Check the docs before answering from memory

Any time a task depends on how the operator's machines, network, services, or
accounts are set up, **list the docs first**:

```bash
enso doc list                    # path, name, description for every doc
```

The descriptions are the index — they are written to be matched against.
Read only the docs that match, with `cat ~/.enso/docs/<path>`. Do not answer
from a guess or from a stale memory of an earlier turn when a doc covers the
question.

## Writing a doc

```bash
enso doc create stuff/sub_stuff.md              # parents created, frontmatter scaffolded
enso doc create homelab.md --name "Home Lab"    # explicit display name
```

`create` writes the file with frontmatter in place; fill it in:

```markdown
---
name: Home Lab
description: Hosts, addresses, and services on the home network, and how they are reached.
---

Body prose.
```

- `name` is display only. It never moves the file — the path is identity, and
  renaming a doc is a plain `mv`.
- `description` is what `enso doc list` prints and what you match against
  later. Make it specific enough to pick out of a list of twenty: what the doc
  holds and when it is worth opening. "Notes" is useless.
- Keep filenames lowercase with `_` or `-`.

## What belongs in a doc

Standing truths about the operator's setup, recorded once and consulted many
times:

- infrastructure and topology — hosts, addresses, ports, what runs where
- account and naming conventions, where credentials live (never the secrets)
- runbooks and gotchas that outlive the task that surfaced them

What does not:

- **How to do something generally** — that is a skill.
- **Work that runs on a schedule** — that is a job (`enso job create`).
- Anything true only for the current turn — just say it in the reply.

When you learn a durable fact about the setup during a task, write it into a
doc rather than only reporting it. Enso ships no docs; the tree starts empty
and grows from what you and the operator put in it.
