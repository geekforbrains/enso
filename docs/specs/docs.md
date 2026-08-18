# Reference docs

Operator-authored reference material the agent consults at turn time: how a machine is
wired, a deploy runbook, service topology, account conventions — the standing context that
does not belong in a prompt and is not a procedure. Stored as files, listed through the
CLI, viewed and edited in the dashboard.

**Status: implemented.** See [data-model.md](data-model.md) for where it sits in storage
and [web.md](web.md) for the dashboard surface.

## What docs are not

Enso already has two file-backed kinds, and docs deliberately sit beside them rather than
extending either:

| Kind | Answers | Loaded |
| --- | --- | --- |
| **Jobs** (`~/.enso/jobs/`) | "run this on a schedule" | By the scheduler |
| **Skills** (`~/.enso/skills/`) | "here is *how* to do X" | By the CLI, on relevance |
| **Docs** (`~/.enso/docs/`) | "here is what is *true* about this setup" | On demand, by the agent |

Docs are reference knowledge, not procedure. Nothing loads them automatically; the agent
finds them when a task calls for them.

## Storage layout

A plain Markdown tree under `~/.enso/docs/`, nested to **any depth**:

```
~/.enso/docs/
├── homelab.md
├── deploy_runbook.md
└── stuff/
    ├── sub_stuff.md
    └── deeper/
        └── notes.md
```

- **Identity is the path relative to `~/.enso/docs/`** — `stuff/sub_stuff.md`. Not a slug.
  This is the single structural difference from jobs and skills, and it drives routing,
  validation, and deletion (see § Path rules).
- **Filenames are lowercase** and may use `_` or `-`. The UI never shows the raw filename
  as a title.
- **Every doc carries frontmatter.** The `name` field is what the UI displays.
- Non-`.md` files (an image a doc references) may sit in the tree. They are left alone and
  omitted from listings.
- Enso ships **no bundled docs** in this phase. The tree starts empty, with no
  pristine-hash tracking or deletion tombstones. Bundled skills are also copied only by
  fresh setup; neither kind is installed or upgraded during startup.

## Frontmatter

Two required fields, mirroring `SKILL.md`:

```markdown
---
name: Sub Stuff
description: One line covering what this doc holds and when it is worth reading.
---

Body prose.
```

`description` is the discovery surface — it is what `enso doc list` prints and what the
agent matches against before opening anything. It carries the weight that a maintained
index would otherwise carry, which is why there is no `INDEX.md`.

**`name` is display only.** It never moves the file. Editing `name` in the dashboard
retitles the doc; the path is unchanged and the two are free to diverge. Moving a doc is a
filesystem operation (§ Non-goals).

**Malformed or missing frontmatter must never hide a doc.** A file that fails to parse is
still listed, titled from its filename (`sub_stuff.md` → "Sub Stuff"), and flagged in the
UI as needing frontmatter. A store an agent writes to unattended cannot have files that
silently vanish from the operator's view because a header broke.

## Path rules

`_safe_name` (`web/app.py`) validates a bare segment and rejects `/` outright, and
`_remove_owned_tree` is scoped to one direct child. Neither applies to a nested path, so
docs need their own validation:

1. Split the request path on `/`. Every segment must match `[A-Za-z0-9._-]+` and must not
   start with `.` — which rules out empty segments, `.`, `..`, and dotfiles — so no
   backslash, no NUL, and no space or non-ASCII character reaches the filesystem. Holding
   segments to that charset is what lets a validated path be interpolated straight into
   `/docs/{rel}` and into HTML with no percent-encoding.
2. Require a `.md` suffix.
3. Join onto the docs root, then `realpath` and confirm `is_within(docs_root, target)`
   (`fsutil.py`, shared with the job and skill write guards). It resolves symlinks, so a
   link inside the tree pointing at `~/.ssh/id_rsa` fails this check on both read and
   write.

The **listing walk** closes the remaining gap: it must not follow symlinks
(`os.walk(followlinks=False)`, the default) and must skip symlinked files, or a link
appears in the list and then fails validation when opened. It applies the same segment
rule as validation, so the two agree in both directions — every listed file opens, and a
file validation would reject is never advertised. A `.md` file whose name falls outside
the charset is therefore not a doc: it is left on disk, untouched and unlisted, exactly
like a `.png`.

The walk is bounded against pathological trees — a depth cap and a total-entry cap. The
depth cap only prunes directories whose contents could not satisfy rule 1 anyway, so it
never hides an addressable doc and reports nothing. The entry cap can cut a real listing
short, and when it does the CLI and the UI **say so** rather than silently presenting a
partial tree as complete.

**Deletion** removes the file, then prunes now-empty parent directories up to (but not
including) the docs root. Without pruning the tree accumulates hollow folders as docs are
reorganised.

## CLI

```bash
enso doc list                       # recursive: path, name, description
enso doc create stuff/sub_stuff.md  # create parents, scaffold frontmatter
```

`enso doc list` **is the index** — computed from frontmatter on every call, so it cannot
drift from what is on disk. This is the reason the design carries no `INDEX.md` and keeps
no doc inventory inside `AGENTS.md`: both would be a second source of truth that every
create, rename, and delete has to remember to update, with nothing enforcing it.

`enso doc create` mirrors `create_job` in `jobs.py`, including its cleanup-on-failure
path: validate the relative path, create missing parents, refuse an existing file, and
write a scaffold whose `name` is derived from the filename unless `--name` is given. A
missing `.md` suffix is appended.

No `show` or `delete` subcommands. When the active workspace policy permits filesystem operations, ordinary reads and deletes already cover them — the same reason `enso job` has no `show`; the Enso CLI does not bypass a restrictive policy. `list` earns its place because it surfaces descriptions without opening every file.

## Discovery: the `docs` skill

Docs reach the agent through a **bundled `docs` skill** at
`src/enso/skills/docs/SKILL.md`, alongside the other bundled skills. It
covers:

- where docs live and that `enso doc list` enumerates them
- **check the docs before answering from memory** about the operator's setup
- how to write one: `enso doc create <path>`, fill the frontmatter, keep `description`
  specific enough to match against
- when a fact belongs in a doc rather than a reply

Fresh setup copies the bundled `docs` skill into `~/.enso/skills/docs/` along with the
other global skills. It is lazy: the CLI reads frontmatter descriptions for discovery and
loads the body only when a task looks relevant, so an operator who never writes a doc pays
nothing.

The installed copy is user-owned immediately. Deleting it in the dashboard removes its
directory without a tombstone, and neither service startup nor an upgrade recreates it.
Nested bundled resources are included in package data so a deliberate fresh seed can copy
the complete skill tree.

### AGENTS.md and existing installs

The canonical shared `~/.enso/AGENTS.md` carries a short `enso doc` block in its CLI
section, matching how `enso job` appears, plus one line pointing at the skill. Fresh setup
copies that prompt and the bundled skill once.

Existing installations do not receive either change automatically. Enso maintains no
known-pristine hashes, startup content installer, or retired-content cleanup. Operators
review the bundled source and merge only the guidance they want; setup repair preserves a
missing or customized prompt instead of recreating it.

## Web UI

Docs follow the skills pages closely. No new rendering machinery — the same textarea
editor, because this surface exists so an operator can see and correct what Enso is
storing, not to browse a wiki.

| Route | Method | Status | Purpose |
| --- | --- | --- | --- |
| `/docs` | GET | Implemented | Doc list — display name, description, relative path |
| `/docs/{path:path}` | GET | Implemented | View and edit one doc |
| `/docs/edit` | POST | Implemented | Replace a doc's full contents atomically |
| `/docs/delete` | POST | Implemented | Delete a doc after confirmation, pruning empty parents |
| `/docs/new` | GET, POST | Implemented | Create a doc from the browser |

Mutations take the doc path in the **POST body**, not the URL. `/docs/{path:path}/edit`
would work — the `:path` convertor backtracks, and the required `.md` suffix prevents a
real collision — but body-carried paths remove the need to reason about greedy matching at
all, and keep CSRF wrapping identical to the existing routes.

### List (`/docs`)

- Rows show the frontmatter `name`, the `description`, and the relative path as a small
  mono secondary line — the treatment external skills already use for their source paths.
- Rows are grouped under their parent directory, sorted by path. **Directories have no
  frontmatter**, so their headings are derived from the segment: `stuff` → "Stuff",
  `some_thing` → "Some Thing". This is the one place the display-name rule cannot read
  from a file.
- Client-side search across name, description, and path, matching the skills list.
- Docs with unparseable frontmatter appear with a filename-derived title and a marker.

### Detail (`/docs/{path}`)

- The `editor_form` macro from `macros.html`: whole-file textarea, atomic save, confirmed
  delete. Identical to `/skills/{name}`.
- A breadcrumb built from the same title-cased directory segments as the list.

### Dashboard (`/`)

The doc count joins the existing job and skill counts, linking to `/docs`.

## Non-goals

- **No rendered Markdown.** Every editing surface in the dashboard is a plain textarea and
  this one matches. Rendering would mean a new dependency in a tree whose runtime deps are
  four packages.
- **No wiki.** No cross-links, backlinks, tags, search index, or tree navigator.
- **No rename or move in the UI.** Path is identity; changing it is a filesystem
  operation the agent performs. A browser affordance can come later.
- **No external doc roots.** Unlike skills, docs are Enso-owned only. The write boundary
  stays exactly where it is.
- **No bundled doc content in this phase.** Enso ships the skill that explains docs, not
  a pre-populated reference set.

## Implementation map

| Area | Change |
| --- | --- |
| `config.py` | Adds `DOCS_DIR` beside `JOBS_DIR` |
| `docs.py` *(new)* | Path validation, bounded recursive listing, scaffold and delete |
| `cli.py` | Adds a `doc` Typer group with `list` and `create` |
| `scaffolding.py` | Creates the structural `~/.enso/docs/` directory; fresh setup separately seeds the global prompt and skills |
| `skills/docs/SKILL.md` *(new)* | Bundled skill teaching discovery and authoring |
| `prompts/AGENTS.md` | Adds the `enso doc` CLI block and a pointer to the skill in the shared launch instructions |
| `web/app.py` | Adds doc routes, path-safe resolution, and the dashboard count |
| `web/templates/` | Adds `docs.html`, `doc_detail.html`, and `doc_new.html` |

`frontmatter.py` needed no change — `read`, `parse`, `dumps`, and its `fsutil`-backed
atomic writers already covered what docs require.
