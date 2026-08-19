# Reference docs

User-owned reference material the agent consults at turn time: how a machine is wired, a
deploy runbook, service topology, account conventions — the standing context that does
not belong in a prompt and is not a reusable procedure. Fresh setup provides three small
starter references; after installation they behave exactly like operator-authored docs.
All docs are stored as files, listed through the CLI, and viewed or edited in the
dashboard.

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

A plain Markdown tree under `~/.enso/docs/`, with each doc path capped at **eight
segments including the filename**:

```
~/.enso/docs/
├── enso/
│   ├── content_model.md  # fresh-install placement and source-of-truth contract
│   └── layout.md         # fresh-install filesystem and local-history reference
├── operator.md           # fresh-install editable operator-context template
└── stuff/
    └── sub_stuff.md      # example later user-created doc
```

- **Identity is the path relative to `~/.enso/docs/`** — `stuff/sub_stuff.md`. Not a slug.
  This is the single structural difference from jobs and skills, and it drives routing,
  validation, and deletion (see § Path rules).
- **Filenames are lowercase** and may use `_` or `-`. The UI never shows the raw filename
  as a title.
- **Every doc carries frontmatter.** The `name` field is what the UI displays.
- Non-`.md` files (an image a doc references) may sit in the tree. They are left alone and
  omitted from listings.
- Enso packages exactly three starter docs: `enso/content_model.md`, `enso/layout.md`, and
  `operator.md`. Only a genuinely fresh setup copies them. Enso does not seed empty
  account, browser, network, service, project, or business docs.
- Installed starters are ordinary user-owned files, with no pristine-hash tracking or
  deletion tombstones. They may be edited or deleted, and no startup, repair, completed
  setup rerun, or upgrade restores or advances them.

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

The walk is bounded against pathological trees — the eight-segment path cap and a
500-document listing cap. The depth cap only prunes directories whose files would fail
the same path-depth validation, so it never hides an addressable doc and reports nothing.
The document cap can cut a real listing short, and when it does the CLI and the UI **say
so** rather than silently presenting a partial tree as complete.

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
Deleting a starter therefore removes it from discovery just like deleting any other doc;
references to starter paths must always account for their possible absence.

`enso doc create` mirrors `create_job` in `jobs.py`, including its cleanup-on-failure
path: validate the relative path, create missing parents, refuse an existing file, and
write a scaffold whose `name` is derived from the filename unless `--name` is given. A
missing `.md` suffix is appended.

The scaffold is intentionally incomplete, so `enso doc create` does not snapshot it.
After filling the frontmatter and body into one coherent reference, record the exact doc
path with the local content command:

```bash
enso snapshot create --message "docs: add homelab reference" -- \
  ~/.enso/docs/stuff/homelab.md
```

Dashboard and direct filesystem edits likewise do not create hidden snapshots. One
reviewed content change gets one scoped snapshot after it is complete; never use broad
Git staging or include config, credentials, databases, uploads, drafts, or runtime state.
See [snapshots.md](snapshots.md) for the full boundary.

No `show` or `delete` subcommands. When the active workspace policy permits filesystem operations, ordinary reads and deletes already cover them — the same reason `enso job` has no `show`; the Enso CLI does not bypass a restrictive policy. `list` earns its place because it surfaces descriptions without opening every file.

## Discovery: the `docs` skill

Docs reach the agent through a **bundled `docs` skill** at
`src/enso/skills/docs/SKILL.md`, alongside the other bundled skills. It
covers:

- where docs live and that `enso doc list` enumerates them
- **check the docs before answering from memory** about the operator's setup
- search existing docs and authoritative sources before creating or copying material
- the placement contract across prompts, global docs, workspace knowledge, repository
  docs, the configured knowledge base, skills, jobs, tables, drafts, and the current reply
- how setup-specific runbooks differ from reusable procedures, which belong in skills
- how to write one: `enso doc create <path>`, fill the frontmatter, and make
  `description` specific enough to match against
- record credential locations rather than secret values, and live-verify volatile facts

Fresh setup copies the bundled `docs` skill into `~/.enso/skills/docs/` along with the
other global skills. The workflow is lazy: `enso doc list` reads frontmatter descriptions
for discovery, and the agent opens a body only when a task looks relevant.

The installed copy is user-owned immediately. Deleting it in the dashboard removes its
directory without a tombstone, and neither service startup nor an upgrade recreates it.
Nested bundled resources are included in package data so a deliberate fresh seed can copy
the complete skill tree.

### Starter references and the shared prompt

The three package resources deliberately have narrow responsibilities and precise
discovery descriptions:

| Installed path | Responsibility |
| --- | --- |
| `enso/content_model.md` | Placement, ownership, precedence, secret-handling, and freshness rules; read before creating, moving, or duplicating durable knowledge |
| `enso/layout.md` | The generic managed tree, discovery links, local Git boundary, and versioned-versus-runtime content; read when locating, validating, or repairing installation content |
| `operator.md` | Confirmed identity, locale/timezone, communication preferences, and standing personal context; read only when a task depends on those facts |

The shared `~/.enso/AGENTS.md` stays a small routing layer. It names those paths only as
fresh-install starters that may be consulted **when present**, tells the agent to use the
dynamic list for everything else, and does not enumerate later user-created docs. The
workspace prompt similarly routes deferred detail through `knowledge/README.md` when
present instead of becoming a domain manual.

### Fresh-setup lifecycle

Starter docs are part of the one-time initial content transaction, not managed upgrade
assets:

1. Setup creates and persists a genuinely fresh configuration with
   `setup.completed_at: null` **before** any starter content is seeded.
2. While that marker remains `null`, explicit setup exclusively creates missing bundled
   prompt, skill, starter-doc, and default-workspace content. It never follows a symlink
   or replaces a collision. Once the complete tree exists, setup records it in one initial
   local Git snapshot.
3. Only after that snapshot succeeds does setup replace `null` with an ISO 8601 completion
   timestamp that includes a timezone.

A seed or snapshot failure leaves the marker `null` and reports setup as incomplete. The
next explicit setup attempt reuses matching files already created, fills only missing
pieces, and retries the snapshot without overwriting user content. If the initial snapshot
succeeded but saving the completion timestamp failed, the retry recognizes that snapshot
and writes the timestamp without creating a second initial commit.

A timestamped setup is complete. A configuration with no `setup` field is a pre-feature
installation, not an interrupted setup. Neither state seeds starter docs, and operators
must never fabricate `setup.completed_at: null` to obtain them. Ordinary `enso serve` and
`enso web` startup, `enso config check`, structural repair, and software upgrades are also
non-seeding. Existing installations adopt desired starter references or guidance only
through an explicit, operator-reviewed copy or merge; adopted files are user-owned.

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
- **No external doc roots.** Unlike skills, every visible doc is user-owned content inside
  Enso's managed `~/.enso/docs/` write boundary.
- **No static catalog.** The three fresh-install starters are not a permanent inventory;
  users may remove them and create different docs, and `enso doc list` remains the only
  document index.

## Implementation map

| Area | Change |
| --- | --- |
| `config.py` | Adds `DOCS_DIR` beside `JOBS_DIR` |
| `docs.py` *(new)* | Path validation, bounded recursive listing, scaffold and delete |
| `cli.py` | Adds the `doc` Typer group, the explicit snapshot command, and coordinates the fresh seed → initial snapshot → completion-timestamp transition |
| `scaffolding.py` | Creates the structural docs directory and exclusively copies starter resources only for an incomplete fresh setup |
| `starter_docs/` | Packages the content-model, layout, and operator starter references |
| `repository.py` | Records the initial tree and explicit later content changes through the same scoped snapshot service |
| `skills/docs/SKILL.md` *(new)* | Bundled skill teaching discovery and authoring |
| `prompts/AGENTS.md` | Keeps a compact dynamic-doc routing rule and optional pointers to the three starter paths |
| `web/app.py` | Adds doc routes, path-safe resolution, and the dashboard count |
| `web/templates/` | Adds `docs.html`, `doc_detail.html`, and `doc_new.html` |

`frontmatter.py` needed no change — `read`, `parse`, `dumps`, and its `fsutil`-backed
atomic writers already covered what docs require.
