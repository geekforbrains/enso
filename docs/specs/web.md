# Web UI

The local dashboard: pages, routes, and read/write behaviour. Server-rendered, no chat.
See [architecture.md](architecture.md) for how the server runs and is secured, and
[data-model.md](data-model.md) for what it reads/writes.

## Shape

A small **server-rendered** app (Starlette + Jinja2) using **HTMX** for partial page and
inline updates. There is no SPA, runtime build, or external CDN: compiled CSS and the
latest stable HTMX 2.0 runtime are vendored under `web/static/`. Every navigable URL still
returns a complete document when opened directly, and forms and links remain usable when
JavaScript is off.

The whole UI is a thin skin over the file model and the shared DB: pages read `JOB.md` /
`SKILL.md` / `AGENTS.md`, run history, and registered user tables. Writes go straight
back to owned files (atomic replace) and the run store; user-table pages are read-only.
There is no separate web database or cache (see [data-model.md](data-model.md)).

**Write boundary.** Every write the UI makes lands inside `~/.enso/` (jobs,
Enso-owned skills) or the working-dir `AGENTS.md`. It never writes outside that tree —
external "parent" skills discovered from the CLIs' own roots (e.g. `~/.claude/skills/`)
are strictly read-only. This is both the safety boundary and the ownership model: Enso
manages what lives in its own dir and only observes the rest.

**Request protection.** Host headers must match loopback, the concrete bind host, or a
name/IP in `web.allowed_hosts`; wildcard binds do not disable this check. All POST routes
also require a random, process-scoped CSRF token supplied by the rendered form or an
`X-CSRF-Token` header. Responses deny framing, disable MIME sniffing, use a no-referrer
policy, and prevent HTML caching. Host filtering is not authentication: an empty
`web.token` accepts every reachable client.

## Routes

| Route | Method | Status | Purpose |
| --- | --- | --- | --- |
| `/` | GET | Implemented | Dashboard — recent runs plus job and skill counts |
| `/health` | GET | Implemented | Unauthenticated process-health probe |
| `/jobs` | GET | Implemented | Job list — schedule, provider/model, enabled state |
| `/jobs/new` | GET, POST | **Planned** | Create-job form and `JOB.md` scaffold |
| `/jobs/{name}` | GET | Implemented | Job configuration, prompt, prerun state, and recent runs |
| `/jobs/{name}/edit` | POST | **Planned** | Edit job metadata and prerun configuration |
| `/jobs/{name}/prompt` | POST | Implemented | Edit only the prompt body while preserving raw frontmatter |
| `/jobs/{name}/prerun` | POST | Implemented | Edit the configured prerun script while preserving its mode |
| `/jobs/{name}/toggle` | POST | Implemented | Enable or disable a job |
| `/jobs/{name}/delete` | POST | Implemented | Delete a job directory after confirmation |
| `/jobs/{name}/run` | POST | Implemented | Run now and record a `manual` run |
| `/runs` | GET | Implemented | Paginated run feed; filter by `?name=`, `?status=` |
| `/runs/{id}` | GET | Implemented | Run metadata and captured log output |
| `/skills` | GET | Implemented | Enso-owned and external read-only skill tiers |
| `/skills/new` | GET, POST | **Planned** | Create an Enso-owned skill |
| `/skills/{name}` | GET | Implemented | View `SKILL.md`; edit controls appear for Enso-owned skills |
| `/skills/{name}/edit` | POST | Implemented | Replace an Enso-owned skill's `SKILL.md` |
| `/skills/{name}/delete` | POST | Implemented | Delete an Enso-owned skill directory after confirmation |
| `/docs` | GET | Implemented | Reference-doc list — name, description, relative path |
| `/docs/new` | GET, POST | Implemented | Create a doc and scaffold its frontmatter |
| `/docs/{path:path}` | GET | Implemented | View and edit one doc |
| `/docs/edit` | POST | Implemented | Replace a doc's contents atomically |
| `/docs/delete` | POST | Implemented | Delete a doc after confirmation |
| `/tables` | GET | Implemented | Registered data-table list with discovery metadata |
| `/tables/{name}` | GET | Implemented | Schema summary and bounded, read-only row preview |
| `/agents` | GET | Implemented | View the working-directory `AGENTS.md` |
| `/agents/edit` | POST | Implemented | Save `AGENTS.md` atomically |
| `/static/*` | GET | Implemented | Vendored HTMX and CSS assets |

The toggle endpoint returns its control fragment when targeted by HTMX. Page requests
return the stable main-content fragment for HTMX and a complete document otherwise;
table pagination and the Runs browser have smaller focused fragments. Other writes use
`HX-Location` to navigate to their resulting page without reloading the shell and retain
ordinary `303` redirects when JavaScript is unavailable.

## Pages

### Dashboard (`/`)

The dashboard shows:

- **Recent runs** — the last six rows from `runs`, newest first: kind, name,
  status pill (running/ok/error/timeout/prerun error/prerun timeout), trigger, duration,
  relative time; each links to `/runs/{id}`.
- **Jobs enabled** — the enabled and total job counts, linking to the job list.
- **Skills** — deduplicated Enso-owned and visible system counts, linking to the skill
  list.
- **Tables** — available registered user-table count, linking to the read-only table list.

### Jobs (`/jobs`, `/jobs/{name}`)

- Read: schedule, provider/model, timeout, notify destination, prompt body, and whether
  the configured prerun script exists.
- A dedicated **enable/disable** toggle flips `enabled:` for one-click pause, and
  **Run now** executes the job immediately.
- **Edit the prompt** (`/jobs/{name}/prompt`): save just the `JOB.md` body from the job
  detail page — the same edit-in-place affordance skills have (`/skills/{name}/edit`),
  preserving the original frontmatter text byte-for-byte.
- **Edit the pre-run script** (`/jobs/{name}/prerun`): when a configured script exists
  safely inside the job directory, its UTF-8 content appears in a second editor below
  Prompt. Saving is atomic and preserves the file's permission mode. Missing, non-file,
  symlinked, and out-of-directory paths are not editable.
- **Delete** (`/jobs/{name}/delete`): a native disclosure confirms the destructive
  action before removing the entire job directory, including prerun and companion files.
  Existing run history remains available.
- Recent runs for this job, linking to `/runs/{id}`.
- **Planned:** browser forms for create and full metadata editing, including choosing the
  prerun path. Until then use `enso job create` or edit the job files directly.

### Run detail (`/runs/{id}`)

- Metadata: kind, name/title, trigger, provider/model, status, exit code, start/end,
  duration.
- Output: up to the first 200,000 bytes of `runs/{id}.log`, monospace and wrapped. If
  truncated, the page shows the full byte count and on-disk path.
- A run with no captured output displays an empty-state message.

The `/runs` feed fetches at most 51 records to render a 50-record page and determine
whether a next page exists without `COUNT(*)`. Filters and page state stay in the URL;
HTMX swaps only the filter/results browser while direct requests return the full page.
Each record has one responsive DOM representation rather than separate hidden mobile and
desktop copies.

### Skills (`/skills`, `/skills/{name}`)

Two tiers, split by the `~/.enso/` write boundary:

- **Enso skills** — everything under `~/.enso/skills/`, whether created here or seeded from
  Enso's starter set at install. Listed with name + description (from SKILL.md
  frontmatter). `/skills/{name}` offers whole-file `SKILL.md` editing and confirmed
  directory deletion. Missing bundled files are seeded unless they have an explicit
  deletion marker; known pristine prior versions may be upgraded, and customized files
  or symlinks are preserved. Deletion also removes any unmodified, unshared tool copy
  installed from that skill; modified or shared tool files are preserved.
- **External / "parent" skills** — auto-discovered from the underlying CLIs' own skill
  roots *outside* `~/.enso/` (e.g. `~/.claude/skills/`; the set of roots is configurable,
  see [data-model.md](data-model.md) § Config). Listed **read-only** with their absolute
  **source path**, so the operator sees what is available without the UI reaching outside
  `~/.enso/`. Detail renders read-only.
- The list has a client-side search across names and descriptions. Names are deduplicated
  using the same precedence as detail routing: Enso-owned skills first, then the first
  configured external root.
- **Planned:** create controls and tool-script editing for Enso-owned skills.

### Reference docs (`/docs`, `/docs/{path}`)

Operator-authored reference material under `~/.enso/docs/`, nested to any depth. Unlike
skills there is a single tier: docs are Enso-owned, always editable, never discovered from
outside `~/.enso/`.

- Rows show the frontmatter `name` and `description`, with the relative path as a small
  mono secondary line, grouped under their parent directory. Directory headings are
  derived from the path segment (`some_thing` → "Some Thing") because directories carry no
  frontmatter.
- Detail reuses the same whole-file textarea as `/skills/{name}`, plus confirmed deletion
  that prunes emptied parent directories.
- Docs are identified by **relative path**, so they need path-segment validation and a
  symlink-skipping walk rather than the single-segment `_safe_name` check. Mutations carry
  the path in the POST body.
- Full behaviour, including the CLI and the bundled `docs` skill that teaches the agent to
  consult them, is specified in [docs.md](docs.md).

### Data tables (`/tables`, `/tables/{name}`)

Registered user-owned SQLite tables are an inspection surface, not a database editor.
The list shows the display name, discovery description, physical table name, availability,
and column count for every valid catalog entry in `_enso_tables`. A stale entry remains
visible as unavailable, while internal and unregistered tables never appear.
An absent catalog renders as an empty list; the web surface never creates it or mutates a
registered user table's schema or rows.

Detail shows a compact schema summary followed by a horizontally scrollable grid:

- one bounded page is fetched with `LIMIT`/`OFFSET`; page size is capped server-side
- the number of displayed columns and rendered length of each cell are capped
- schema columns, CREATE SQL, index count, and index SQL are independently capped, with
  explicit truncation notices
- `NULL`, BLOBs, and truncated values are visually explicit
- identifiers come from validated registrations and are still quoted
- cell content is escaped as untrusted data
- no unconditional `COUNT(*)` is needed to render a preview
- reaching the maximum allowed offset suppresses further forward navigation
- Previous/Next requests replace only the data section and push a shareable page URL when
  HTMX is available; the same links perform ordinary full navigation without JavaScript

The page uses a short-lived SQLite connection with a busy timeout and fails clearly if the
table was removed after registration or the database cannot be read. The route exposes no
write operation, SQL input, filter/sort expression, schema control, row editing, or delete
action. The catalog, CLI, agent workflow, and failure semantics are specified in
[tables.md](tables.md).

### AGENTS.md (`/agents`)

- Renders the system prompt (`AGENTS.md` from the working dir).
- **Editable**: a textarea + save, POST to `/agents/edit`, atomic write back to
  `AGENTS.md`. The `CLAUDE.md` symlink to it is left intact (we write the target, not the
  link). This is the one system-prompt surface the operator can tweak without opening an
  editor.

## Run-now

"Run now" executes through the dashboard process's `Runtime`:

- It uses the same prerun/provider pipeline as `enso job run` and records
  `trigger='manual'` in run history.
- The POST waits for the run to finish, then redirects to its run detail page. Live
  progress polling and output streaming are future work.

## Rendering & assets

- **Responsive layout**: the sidebar appears only when the viewport has room for it
  (1024px+). The Runs feed uses one record representation that changes from compact cards
  to a wide grid instead of duplicating hidden markup; the dashboard's short "Latest runs"
  list still keeps separate card and table markup. The capped main column stays
  left-aligned beside the sidebar on wide screens, and long IDs, paths, upload controls,
  and metadata must never widen the document.
- **Text editing**: Enso-owned `SKILL.md`, job prompts, `AGENTS.md`, and reference docs use
  plain textareas; read-only external skills use escaped preformatted text. Rich Markdown
  rendering is not implemented.
- **Table grids**: schema and row values remain readable on narrow screens via
  bounded, horizontal overflow; long values cannot widen the whole document.
- **Form controls**: native single-select dropdowns share consistent spacing, focus
  states, and chevrons across browsers and themes.
- **Styling**: compiled Tailwind utilities are vendored as `web/static/tailwind.css`, with
  the small hand-written layer in `web/static/app.css`. Rebuild the generated file with
  `cd src/enso/web && npx tailwindcss@3.4.17 -c tailwind.config.js -i tailwind.input.css
  -o static/tailwind.css --minify`. Light, dark, and system themes are user-selectable.
  Templates use semantic neutral tokens (`canvas`, `surface`, `border`, `ink`, `muted`,
  `action`, and related states) backed by CSS variables in `app.css`; green, amber, and red
  are reserved for success, warning, and destructive states rather than ordinary actions.
- **No external requests**: compiled CSS and HTMX 2.0.10 (the latest stable release) are
  vendored under `web/static/`, so the UI works offline and over a locked-down tailnet
  with no CDN trust or flash of unstyled content.
- **Navigation**: links are progressively boosted toward a stable `#main-content` target,
  leaving the sidebar, mobile drawer, theme controls, and responsive shell mounted.
  Focused table/run controls target smaller stable sections. Request indicators are
  persistent, overlapping requests replace stale ones, and mutation forms temporarily
  disable their submit button.
- **History and variants**: HTMX history snapshots use the stable main element. History
  cache misses explicitly request a full document (`historyRestoreAsHxRequest: false`),
  and HTML responses vary on `HX-Request` and `HX-Target`. HTML remains `no-store` because
  pages contain a process CSRF token and local operational data.

## Non-goals (recap)

No chat, no login/accounts, no writes outside Enso-owned paths (`~/.enso/` plus the
working-directory `AGENTS.md`), no table-row/schema editor, no arbitrary SQL, no live
output streaming, and no public exposure. See [PRD.md](../PRD.md) § Non-goals.
