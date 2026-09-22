# Knowledge

Knowledge is ordinary Markdown maintained through conversation and the CLI. Files are the
source of truth and remain usable outside Enso. The [viewer](web.md#knowledge) browses,
searches, and reads them without editing.

## Starter collection

A fresh installation starts with `!Inbox/`, `Meta/`, `Memory/`, `People/`, `Projects/`,
`Areas/`, `Topics/`, `Ideas/`, and `Archive/` under shared knowledge. `Meta/Guide.md` owns
editable filing and writing conventions; `Meta/Index.md` provides navigation.
`Meta/Templates/` contains optional Person and Project note shapes. The
[bundled guide](../src/enso/bundled/shared/knowledge/Meta/Guide.md) supplies the starting
conventions. These are user-owned notes with fresh IDs and dates. Change them through
conversation; copy only template bodies, not their metadata.

Initialization installs the starter once. Updates, audits, startup, and repeated
initialization never restore changed or deleted notes. Existing homes, even with an empty
knowledge root, are not seeded. Interrupted initialization retries only while no collection
exists and publication has not begun; a later interruption can leave the optional starter
absent rather than risk recreating user-deleted notes.

Homes without a guide keep their filing conventions and use the skill's formatting fallback.
Adopting the starter in an existing collection requires an explicit editing task. Folders
such as `Memory/` are ordinary knowledge; automation is configured separately.

## Finding and maintaining knowledge

Use the knowledge CLI and `enso-knowledge` skill, starting in the relevant shared folders
and broadening as needed. Maintain each fact in one owning note with its source context;
other notes link to it. Folders and [workspace selection](workspaces.md#context-selection-in-020)
organize context without restricting access or automatically loading notes.

## Locations and context

- `$ENSO_HOME/shared/knowledge/` is the default home for notes, addressed as `shared`.
- `$ENSO_HOME/workspaces/<name>/knowledge/` belongs to that workspace, addressed as
  `workspace:<name>`. Existing roots remain supported; new workspaces do not create them.

Until home revision 2, shared knowledge lived at `$ENSO_HOME/knowledge/` and was addressed
as `general`. [Upgrading](install.md#upgrading) moves it and rewrites `general:` link
targets to `shared:` in notes. It changes nothing in the home
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

Folders can contain notes and subfolders. Each visit discovers current files and caches
unchanged parsed notes using file identity, size, modification time, and change time.
Removed files and superseded versions leave the cache. Direct edits, imports, renames, and
deletions appear on refresh; there is no database index.

## Where new notes go

New notes go in shared knowledge unless the user gives a different filing rule, including
from chat, jobs, Heartbeat, and agents outside Enso. Update existing notes where they live;
a retained workspace note can be linked with
`[[workspace:research:Projects/Topic]]`.

The bundled `enso-knowledge` skill and `AGENTS.md` templates carry this default. Existing
workspace notes, instructions, and customized bundled files are not rewritten. Scripts
that previously relied on `ENSO_WORKSPACE` for knowledge must now pass `--workspace NAME`.
[Workspaces](workspaces.md#consolidating-knowledge-and-work-files) explains deliberate
consolidation; [Customizing](customizing.md#the-bundled-skills) explains what upgrades refresh.

## The note format

Knowledge notes use `enso.note/v1`.

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

Timestamps use ISO 8601 with a timezone; Enso writes UTC ending in `Z`. Unknown import dates
remain absent: neither import time nor file modification time becomes note metadata.
`updated` cannot precede `created`. A later substantive edit sets `updated` while leaving
unknown `created` absent. Moves without body changes and format migrations preserve dates.

These are the only frontmatter fields. Titles come from filenames without `.md`; paths
provide context. Sources belong beside the relevant text in the body. Updates, adoption,
and moves refuse duplicate IDs, including in linked notes needing repair, even with an exact
path. Repair duplicate identity deliberately first.

Notes with absent or invalid metadata remain readable by path, with findings and the original
source visible. Read-only audits report metadata, identity, link, and heading problems.
`enso doctor` summarizes every root; scoped knowledge audits provide complete findings.
Neither verifies factual truth or appropriate filing.

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

Resolved notes open inside the viewer, with backlinks and stable URLs for unique valid IDs;
other notes use scope/path URLs. HTTP, HTTPS, and mail links remain external. Unsafe schemes
and paths escaping the root are refused.

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

`enso-knowledge` owns note operations; shared `Meta/Guide.md` owns organization and style
across roots. The skill's `references/formatting.md` is the fallback when no guide exists.
[Customizing](customizing.md#knowledge-formatting) explains changing conventions and checks.

User preferences govern filing, note shapes, and style; they are not core validation rules.
Metadata, link identity, and filesystem safety remain enforced by Enso. The viewer never
runs the style checker or treats fetched note content as agent instructions.

After writing, run the core audit and the skill's `scripts/lint.py`, fix supported issues,
and report unresolved source/link problems. The read-only style checker excludes metadata
and code; it checks whitespace, blank lines, heading spacing, and final newlines without
requiring a title heading or maximum line length.

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
