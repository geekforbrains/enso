# Knowledge

Knowledge is a collection of ordinary Markdown files maintained through conversation with
Enso. The [web viewer](web.md) provides read-only folder browsing, search, and linked note
reading. Files remain usable outside Enso; the viewer is not an editor or the source of truth.

## Knowledge and memory in 0.2.0

Knowledge holds current maintained facts and useful reference material.
[Memory](memory.md) holds dated conversations and experiences in the workspace
where they happened. Both remain ordinary Markdown and are authoritative for their maintained
content; operational records, including source message captures, live in the home database.

When a user asks for a current fact or reference, agents use the knowledge CLI and
`enso-knowledge` skill, starting with relevant folders in shared knowledge. Questions about
earlier discussions, decisions, promises, and follow-ups use the memory CLI and bundled
`enso-memory` skill. Broaden a lookup deliberately when needed; selecting a workspace
organizes context rather than enforcing confidentiality.
[Workspaces](workspaces.md#context-selection-in-020) owns selection.

For example, the currently agreed support hours belong in a maintained knowledge note.
The conversation on September 16 that proposed changing those hours belongs in the team's
dated memory. Promote a confirmed lasting fact into its owning knowledge note deliberately,
with source context; do not copy every memory into knowledge. Removing that memory later
does not automatically remove a fact already promoted into knowledge.

## Locations and context

- `$ENSO_HOME/shared/knowledge/` is the default home for notes, addressed as `shared`.
- `$ENSO_HOME/workspaces/<name>/knowledge/` belongs to that workspace, addressed as
  `workspace:<name>`. Existing roots remain supported; new workspaces do not create them.

Until home revision 2, shared knowledge lived at `$ENSO_HOME/knowledge/` and was addressed
as `general`. [Upgrading](install.md#upgrading) moves it and rewrites `general:` link
targets to `shared:` in every knowledge and memory note. It changes nothing in the home
while the old root sits beside a non-empty `shared/knowledge/` or a `.knowledge-move-*`
[recovery](#writing-adoption-and-recovery) directory remains: a managed update fails after
stopping services and restores the previous release, and `scripts/dev-migrate` reports it
in its preview. Resolve that and retry. The old name has no fallback: a remaining
`general:` link is an ordinary broken link and a `scope=general` viewer URL no longer
resolves; stable ID URLs keep working. Existing workspace `AGENTS.md` files, customized
bundled files, and any instructions, jobs, skills, or scripts naming `$ENSO_HOME/knowledge`
are not rewritten; point them at `$ENSO_HOME/shared/knowledge`. The
[layout audit](workspaces.md#what-belongs-where) reports a leftover top-level `knowledge/`
as unexpected.

Workspace knowledge roots are discovered from the filesystem without requiring workspace
configuration or a viewer restart. Hidden directories and files, symbolic links, `node_modules`,
and `__pycache__` are excluded. A root itself must be a real directory. Files outside these
roots are not knowledge attachments or link destinations.

Knowledge commands default to shared knowledge, regardless of `ENSO_WORKSPACE`.
`--workspace NAME` selects an existing workspace root, and `--shared` explicitly selects
the default. The two flags cannot be combined. No workspace context is needed for shared
notes; an invalid explicit workspace still errors. UUIDs identify notes globally but CLI
operations require their root to be selected, just like paths. The viewer's stable ID URLs
and cross-root links continue to resolve globally. [CLI](cli.md#knowledge) owns the command
signatures.

Nested folders may contain both notes and subfolders. Start agents in the relevant branch,
read selectively, and broaden when needed; folders organize context but do not create
access permissions or automatically load notes.

The [viewer](web.md#knowledge) owns folder browsing, All notes, search, and pagination.
Enso caches parsed notes in memory using file identity, size, modification time, and change
time; the Markdown files remain authoritative and no database migration is needed.

## Where new notes go

New notes go in shared knowledge unless the user gives a different filing rule. This applies
to interactive turns, jobs, Heartbeat beats, and agents running outside Enso. Find and update
an existing note where it lives. Each fact keeps one owning note that others link to rather
than copy; a retained workspace note can still be linked with
`[[workspace:research:Projects/Topic]]`.

The bundled `enso-knowledge` skill and `AGENTS.md` templates carry this default. Existing
workspace notes, instructions, and customized bundled files are not rewritten. Scripts
that previously relied on `ENSO_WORKSPACE` for knowledge must now pass `--workspace NAME`.
[Workspaces](workspaces.md#consolidating-knowledge-and-work-files) explains deliberate
consolidation; [Customizing](customizing.md#the-bundled-skills) explains what upgrades refresh.

## The note format

Knowledge notes use `enso.note/v1`; memory has its own [schema](memory.md#the-note-format).

Every managed note begins with YAML frontmatter:

```markdown
---
schema: enso.note/v1
id: 3d6d560a-21ef-49fa-a4db-7640b8dcab89
created: "2026-09-15T18:20:00Z"
updated: "2026-09-15T18:20:00Z"
---

## Overview

The note's contents.
```

| Field | Rule | Purpose |
| --- | --- | --- |
| `schema` | Required; exactly `enso.note/v1` | Identify the supported format. |
| `id` | Required; a unique UUID | Stable identity through moves and renames. |
| `created` | Set on new notes; optional on imports | Original creation time. |
| `updated` | Set on new notes and substantive edits; optional on imports | Latest content update, including repaired links. |

Timestamps use ISO 8601 with a timezone; Enso writes UTC ending in `Z`. An imported file's
filesystem timestamp does not prove when its contents were created or updated, so unknown
dates stay absent. `updated` cannot precede `created`. A move that does not change the body
does not change its update time. Neither does a format migration, such as home revision 2
rewriting `general:` links to `shared:` in knowledge and memory notes: it is not a content
update.

For example, an imported reference with no known dates has only
`schema: enso.note/v1` and its preserved or newly assigned `id`. Neither the import date
nor the source file's modification time becomes `created` or `updated`. A later substantive
CLI edit sets `updated` to that edit's time while leaving an unknown `created` absent.

These are the only frontmatter fields. There are no types, tags, aliases, summaries, or custom
property namespaces in v1. Titles come from filenames, with `.md` removed; paths supply folder
and workspace context. Sources and useful context belong in the body near the relevant text.
Duplicate IDs are reported and never arbitrarily resolved. Managed updates, adoption,
and moves (including linked notes they repair) refuse duplicate identity even when given
an exact path; inspect and repair the duplicate deliberately first.

Legacy notes with absent or invalid metadata remain readable by path. The viewer exposes core
properties and metadata problems; View source retains the original document. Core auditing is
read-only and reports independent metadata, identity, link, and heading problems together.
`enso doctor` summarizes this audit across all knowledge roots; the scoped knowledge audit
retains the complete findings. Neither check proves factual truth or appropriate filing.

## Links and attachments

Supported note links include:

```markdown
[[Page]]
[[Folder/Page|Display text]]
[[Page#Heading]]
[Display text](../Folder/Page.md#heading)
[[shared:Projects/Enso/Plan]]
[[workspace:development:Design/Plan#Tradeoffs]]
[Shared plan](shared:Projects/Enso/Plan.md)
```

A bare wiki name must be unique within the source scope. A qualified wiki path is checked
relative to the source folder and the scope root; if both identify different notes it is
ambiguous. If neither matches, a wiki path can match a unique trailing path within the same
scope: `[[Compliance/HIPAA]]` can open `Knowledge/Compliance/HIPAA.md`. Multiple trailing
matches are ambiguous. Explicit `shared:` and `workspace:<name>:` links start at that root
and require an exact path. Ordinary Markdown paths are relative to their source note and
do not use trailing-path matching. Targets may omit `.md`; matching is case
insensitive for note names and paths. Ambiguous targets are reported so the author can use a
fully qualified path. Enso never searches other scopes to guess a bare name.

Heading links use the visible heading text in lowercase, spaces replaced with hyphens, and
punctuation removed. Repeated headings receive `-1`, `-2`, and so on. The audit reports
headings that do not exist. File and link paths may use URL escaping, including spaces and
literal `#` characters in filenames. Links inside fenced, indented, and inline code are data,
not navigation. CommonMark parsing distinguishes nested list links from actual indented code;
code indentation and trailing spaces are preserved. Markdown reference-link definitions are
supported as well.

Clicking a resolved note opens it inside the viewer. Notes with valid unique IDs use stable
URLs; legacy notes use scope/path URLs. Browser back/forward and open-in-new-tab work normally.
The note view includes backlinks. External HTTP, HTTPS, and mail links remain external; unsafe
schemes and paths escaping the knowledge root are refused.

Keep attachments within a knowledge root and link with ordinary Markdown or Obsidian-style
`![[image.png]]`. A bare wiki attachment name must also be unique in its scope. Local images
can display in the viewer; other attachments can be opened or downloaded. The reader limits
notes to 2 MiB; the viewer serves attachments up to 20 MiB. Hidden files, symlinks, and
special files are never served. Embedded notes are links rather than recursive note
transclusion.

Root reads and note publication traverse all directory ancestors without following symbolic
links, so replacing a workspace parent with a link cannot redirect a previously captured root.

Use `enso knowledge move` to rename or relocate a note. It keeps its ID and rewrites only
the resolved links the move would otherwise change or break: incoming links to the note,
its own outgoing note and attachment links when its folder or scope changes, and another
note's bare name link that the new filename would make ambiguous. Links that still reach
the same destination afterwards, such as a bare wiki name within one scope or a
fragment-only link, stay exactly as written. Rewritten links include the explicit scope
and path, preserving labels and heading targets. An ambiguous incoming link involving the
note must be qualified before moving it. Notes whose
bodies need link repair must have valid managed metadata; adopt those notes first. Direct
filesystem moves preserve the ID URL but do not repair Markdown paths.

## Consistency and user preferences

The bundled `enso-knowledge` skill explains where notes go and how agents maintain them. Its
`references/formatting.md` contains the default writing/formatting rules; `scripts/lint.py`
checks mechanical style. Both land under `$ENSO_HOME/skills/enso-knowledge/` and are preserved
on upgrades. [Customizing](customizing.md) owns the rules for changing installed skills.

Use one style across every knowledge root. Users can request a change in
conversation; the agent updates the style document and its checker together. Do not relax a
rule merely to make a note pass. Core fields, link identity, and filesystem safety belong to
Enso code and cannot be weakened by a custom formatting checker. The viewer never runs that
checker or treats fetched note content as agent instructions.

After writing, run the core audit and the editable style checker, fix supported issues, and
report unresolved source/link problems without inventing their answers. The default style
checker is read-only, excludes metadata and code, and checks whitespace, blank lines, heading
spacing, and final newlines. It does not impose a title heading or a maximum line length.

## Writing, adoption, and recovery

The [knowledge CLI](cli.md#knowledge) works without transport configuration or database setup.
`create` and `update` take a body file, not a second frontmatter block. Enso writes the core
fields. `show --json` supplies `sha256`; `update --expected-hash` requires that revision to
prevent overwriting intervening changes. `adopt` and `move` accept the same optional check and
also detect changes during their operation. Writes take an advisory home lock, use atomic
file publication, and refuse occupied destinations and hidden/escaping paths.

`adopt` adds or normalizes a single existing note's core fields. Valid IDs and known timestamps
are preserved. Unsupported fields, invalid values, or malformed original frontmatter are
retained verbatim in a fenced Imported metadata section. An unterminated frontmatter block
causes the original document to be retained in a fenced Imported document section for manual
review through conversation. Adoption does not guess dates or silently discard provenance.
Use adoption on copied imports first; reorganizing a vault is a separate intentional change.

For a bulk copy from a checkout, run:

```bash
uv run python scripts/import-knowledge.py SOURCE DEST --receipt OUTSIDE_ROOTS.json
```

The destination must be absent or empty (an initialized empty knowledge directory is fine).
The utility stages and atomically publishes visible regular files, preserves nested paths,
attachments and source modification times, skips hidden entries and symlinks, and normalizes
only note metadata. It leaves the source untouched and does not rewrite prose, filenames, or
links. A receipt outside both roots records per-file hashes and import results. Review that
receipt and run the core audit and style checker before further restructuring.

Moves publish the destination first, repair linked notes atomically, and remove the source
last. On an ordinary failure Enso rolls back only its own unchanged writes. A process crash or
concurrent edit may leave a `.knowledge-move-*` recovery directory in the home, containing a
manifest and original files; preserve that directory, inspect current files and hashes, and
finish or reverse the recorded move without overwriting newer content. A multi-file move is
not a filesystem-wide atomic transaction. Cooperating Enso writers serialize through the
lock; direct editors must still avoid editing participating notes during a move.
